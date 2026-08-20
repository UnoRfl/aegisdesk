"""Unit tests for the agent: framing, damage detection, input mapping,
file service safety, config handling."""
import os
import sys
import tempfile
import time
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))

from aegis_agent import capture, crypto, protocol as P          # noqa: E402
from aegis_agent.config import Config                           # noqa: E402
from aegis_agent.files import FileService                        # noqa: E402
from aegis_agent.inputctl import InputRouter, RecordingInput     # noqa: E402
from aegis_agent.keymap import SCANCODES                         # noqa: E402


class TestProtocol(unittest.TestCase):
    def test_outer_frame_roundtrip(self):
        frame = P.pack_outer(0xDEADBEEF, b"sealed-bytes")
        sid, body = P.unpack_outer(frame)
        self.assertEqual(sid, 0xDEADBEEF)
        self.assertEqual(body, b"sealed-bytes")

    def test_outer_frame_rejects_foreign_bytes(self):
        with self.assertRaises(P.ProtocolError):
            P.unpack_outer(b"\x00\x00\x00\x00\x00")
        with self.assertRaises(P.ProtocolError):
            P.unpack_outer(b"\xd1")

    def test_json_channel_roundtrip(self):
        blob = P.pack_json(P.CH_INPUT, {"k": "kd", "c": "KeyA"})
        ch, payload = P.split_inner(blob)
        self.assertEqual(ch, P.CH_INPUT)
        self.assertEqual(P.parse_json(payload), {"k": "kd", "c": "KeyA"})

    def test_tile_frame_exact_framing(self):
        tiles = [(0, 0, 64, 64, b"a" * 100), (128, 64, 32, 16, b"b" * 7), (0, 512, 64, 8, b"")]
        blob = P.pack_tile_frame(2, P.CODEC_JPEG, P.FLAG_KEYFRAME, 65535, 1920, 1080, tiles)
        out = P.unpack_tile_frame(blob)
        self.assertEqual(out["monitor"], 2)
        self.assertEqual(out["seq"], 65535)
        self.assertEqual((out["w"], out["h"]), (1920, 1080))
        self.assertTrue(out["flags"] & P.FLAG_KEYFRAME)
        self.assertEqual([(x, y, w, h, d) for x, y, w, h, d in out["tiles"]], tiles)

    def test_tile_frame_detects_truncation(self):
        blob = P.pack_tile_frame(1, 1, 0, 1, 100, 100, [(0, 0, 10, 10, b"xyz")])
        with self.assertRaises(Exception):
            P.unpack_tile_frame(blob[:-1])

    def test_file_data_roundtrip(self):
        payload = P.pack_file_data(0xFFFFFFFF, 12345, b"\x00\xff" * 50)
        xid, seq, chunk = P.unpack_file_data(payload)
        self.assertEqual((xid, seq), (0xFFFFFFFF, 12345))
        self.assertEqual(chunk, b"\x00\xff" * 50)

    def test_timestamp_roundtrip(self):
        ts = 1787228672123
        ch, payload = P.split_inner(P.pack_ts(P.CH_PING, ts))
        self.assertEqual(ch, P.CH_PING)
        self.assertEqual(P.unpack_ts(payload), ts)


class TestCrypto(unittest.TestCase):
    def setUp(self):
        self.vpriv, self.vpub = crypto.generate_keypair()
        self.apriv, self.apub = crypto.generate_keypair()
        self.sid = 4242
        self.key = crypto.derive_key(self.apriv, self.vpub, self.sid, self.vpub, self.apub)

    def test_both_sides_agree(self):
        other = crypto.derive_key(self.vpriv, self.apub, self.sid, self.vpub, self.apub)
        self.assertEqual(self.key, other)

    def test_key_is_bound_to_sid(self):
        other = crypto.derive_key(self.vpriv, self.apub, self.sid + 1, self.vpub, self.apub)
        self.assertNotEqual(self.key, other)

    def test_rejects_malformed_public_key(self):
        with self.assertRaises(crypto.CryptoError):
            crypto.derive_key(self.apriv, b"\x04" + b"\x00" * 10, self.sid, self.vpub, self.apub)

    def test_replay_rejected(self):
        a = crypto.SecureChannel(self.key, crypto.DIR_AGENT_TO_VIEWER)
        v = crypto.SecureChannel(self.key, crypto.DIR_VIEWER_TO_AGENT)
        f = a.seal(b"frame")
        self.assertEqual(v.open(f), b"frame")
        with self.assertRaises(crypto.CryptoError):
            v.open(f)

    def test_reflection_rejected(self):
        a = crypto.SecureChannel(self.key, crypto.DIR_AGENT_TO_VIEWER)
        with self.assertRaises(crypto.CryptoError):
            a.open(a.seal(b"my own frame"))

    def test_tamper_rejected(self):
        a = crypto.SecureChannel(self.key, crypto.DIR_AGENT_TO_VIEWER)
        v = crypto.SecureChannel(self.key, crypto.DIR_VIEWER_TO_AGENT)
        bad = bytearray(a.seal(b"important"))
        bad[20] ^= 0x40
        with self.assertRaises(Exception):
            v.open(bytes(bad))

    def test_out_of_order_within_window_is_accepted(self):
        a = crypto.SecureChannel(self.key, crypto.DIR_AGENT_TO_VIEWER)
        v = crypto.SecureChannel(self.key, crypto.DIR_VIEWER_TO_AGENT)
        frames = [a.seal(f"f{i}".encode()) for i in range(5)]
        for f in reversed(frames):                       # deliver backwards
            v.open(f)

    def test_password_verifier_never_stores_the_password(self):
        h = crypto.hash_password("Rest4urant-POS!")
        self.assertNotIn("Rest4urant", str(h))
        key = crypto.pbkdf2("Rest4urant-POS!", bytes.fromhex(h["salt"]), h["iterations"])
        self.assertEqual(key.hex(), h["key"])

    def test_proof_is_bound_to_both_public_keys(self):
        h = crypto.hash_password("hunter2hunter2")
        k = bytes.fromhex(h["key"])
        good = crypto.auth_proof(k, self.sid, self.vpub, self.apub)
        _, evil = crypto.generate_keypair()
        self.assertFalse(crypto.constant_time_eq(good, crypto.auth_proof(k, self.sid, self.vpub, evil)))
        self.assertFalse(crypto.constant_time_eq(good, crypto.auth_proof(k, self.sid + 1, self.vpub, self.apub)))
        self.assertNotEqual(good, crypto.auth_ack(k, self.sid, self.vpub, self.apub))


class TestDamageDetection(unittest.TestCase):
    def test_identical_frames_have_no_damage(self):
        a = np.full((256, 256, 3), 40, dtype=np.uint8)
        self.assertEqual(int(capture.changed_grid(a, a.copy(), 64).sum()), 0)

    def test_single_pixel_marks_exactly_one_tile(self):
        a = np.zeros((256, 256, 3), dtype=np.uint8)
        b = a.copy()
        b[70, 70] = 255
        grid = capture.changed_grid(b, a, 64)
        self.assertEqual(int(grid.sum()), 1)
        self.assertTrue(grid[1, 1])

    def test_non_multiple_dimensions_are_padded(self):
        a = np.zeros((100, 150, 3), dtype=np.uint8)
        b = a.copy()
        b[99, 149] = 255
        grid = capture.changed_grid(b, a, 64)
        self.assertEqual(grid.shape, (2, 3))
        self.assertTrue(grid[1, 2])

    def test_horizontal_run_merges_into_one_rect(self):
        g = np.zeros((4, 6), dtype=bool)
        g[1, 1:5] = True
        rects = capture.grid_to_rects(g, 64, 384, 256)
        self.assertEqual(rects, [(64, 64, 256, 64)])

    def test_rectangle_merges_in_both_directions(self):
        g = np.zeros((4, 6), dtype=bool)
        g[1:3, 2:4] = True
        rects = capture.grid_to_rects(g, 64, 384, 256)
        self.assertEqual(rects, [(128, 64, 128, 128)])

    def test_rects_are_clipped_to_the_frame(self):
        g = np.zeros((2, 2), dtype=bool)
        g[1, 1] = True
        rects = capture.grid_to_rects(g, 64, 100, 100)
        self.assertEqual(rects, [(64, 64, 36, 36)])

    def test_disjoint_regions_stay_separate(self):
        g = np.zeros((5, 5), dtype=bool)
        g[0, 0] = True
        g[4, 4] = True
        rects = capture.grid_to_rects(g, 64, 320, 320)
        self.assertEqual(len(rects), 2)


class TestEncoder(unittest.TestCase):
    def test_first_frame_is_a_keyframe_then_deltas(self):
        src = capture.NullScreenSource(320, 240)
        enc = capture.TileEncoder()
        first = enc.encode(src.grab())
        self.assertIsNotNone(first)
        self.assertTrue(first["flags"] & 0x01)
        second = enc.encode(src.grab())
        self.assertIsNotNone(second)
        self.assertFalse(second["flags"] & 0x01)
        self.assertLess(second["bytes"], first["bytes"])

    def test_static_screen_produces_nothing(self):
        enc = capture.TileEncoder()
        frame = np.full((240, 320, 3), 77, dtype=np.uint8)
        self.assertIsNotNone(enc.encode(frame))
        self.assertIsNone(enc.encode(frame.copy()))
        self.assertIsNone(enc.encode(frame.copy()))

    def test_downscaling_respects_max_width(self):
        enc = capture.TileEncoder()
        enc.set_profile("speed", max_width=640)
        out = enc.encode(np.random.randint(0, 255, (1080, 1920, 3), dtype=np.uint8))
        self.assertEqual(out["w"], 640)
        self.assertEqual(out["h"], 360)

    def test_resolution_change_forces_a_keyframe(self):
        enc = capture.TileEncoder()
        enc.encode(np.zeros((240, 320, 3), dtype=np.uint8))
        out = enc.encode(np.zeros((480, 640, 3), dtype=np.uint8))
        self.assertTrue(out["flags"] & 0x01)

    def test_heavy_change_falls_back_to_a_full_frame(self):
        enc = capture.TileEncoder()
        enc.encode(np.zeros((256, 256, 3), dtype=np.uint8))
        out = enc.encode(np.random.randint(0, 255, (256, 256, 3), dtype=np.uint8))
        self.assertEqual(len(out["tiles"]), 1)
        self.assertTrue(out["flags"] & 0x01)

    def test_auto_mode_backs_off_when_congested(self):
        enc = capture.TileEncoder()
        enc.set_profile("auto")
        start = enc.profile.jpeg_quality
        for _ in range(6):
            enc.nudge(congested=True)
        self.assertLess(enc.profile.jpeg_quality, start)
        for _ in range(30):
            enc.nudge(congested=False)
        self.assertGreater(enc.profile.jpeg_quality, 30)

    def test_wire_roundtrip_of_a_real_frame(self):
        src = capture.NullScreenSource(320, 240)
        enc = capture.TileEncoder()
        out = enc.encode(src.grab())
        blob = P.pack_tile_frame(1, out["codec"], out["flags"], out["seq"],
                                 out["w"], out["h"], out["tiles"])
        back = P.unpack_tile_frame(blob)
        self.assertEqual(len(back["tiles"]), len(out["tiles"]))
        self.assertEqual(back["tiles"][0][4], out["tiles"][0][4])


class TestInputRouting(unittest.TestCase):
    def setUp(self):
        self.backend = RecordingInput(screen=(0, 0, 3840, 1080))
        self.router = InputRouter(self.backend)

    def test_normalised_coords_map_to_the_streamed_monitor(self):
        self.router.set_monitor(0, 0, 1920, 1080)
        self.router.handle({"k": "m", "x": 0.5, "y": 0.5})
        self.assertEqual(self.backend.events[-1], {"type": "move", "x": 960, "y": 540})

    def test_second_monitor_offset_is_applied(self):
        self.router.set_monitor(1920, 0, 1920, 1080)
        self.router.handle({"k": "m", "x": 0.0, "y": 0.0})
        self.assertEqual(self.backend.events[-1], {"type": "move", "x": 1920, "y": 0})
        self.router.handle({"k": "m", "x": 1.0, "y": 1.0})
        self.assertEqual(self.backend.events[-1], {"type": "move", "x": 3839, "y": 1079})

    def test_out_of_range_coords_are_clamped(self):
        self.router.set_monitor(0, 0, 1920, 1080)
        self.router.handle({"k": "m", "x": -5, "y": 99})
        self.assertEqual(self.backend.events[-1], {"type": "move", "x": 0, "y": 1079})

    def test_buttons_and_wheel(self):
        self.router.set_monitor(0, 0, 1000, 1000)
        self.router.handle({"k": "md", "b": 2, "x": 0.1, "y": 0.2})
        self.router.handle({"k": "mu", "b": 2, "x": 0.1, "y": 0.2})
        self.router.handle({"k": "w", "dy": -120, "x": 0.5, "y": 0.5})
        kinds = [e["type"] for e in self.backend.events]
        self.assertEqual(kinds, ["button", "button", "wheel"])
        self.assertEqual(self.backend.events[0]["b"], 2)
        self.assertEqual(self.backend.events[2]["dy"], -120)

    def test_combo_presses_and_releases_in_reverse_order(self):
        self.router.handle({"k": "combo", "keys": ["ControlLeft", "AltLeft", "Delete"]})
        seq = [(e["code"], e["down"]) for e in self.backend.events]
        self.assertEqual(seq, [("ControlLeft", True), ("AltLeft", True), ("Delete", True),
                               ("Delete", False), ("AltLeft", False), ("ControlLeft", False)])

    def test_unknown_key_codes_are_ignored(self):
        self.router.handle({"k": "kd", "c": "TotallyMadeUpKey"})
        self.assertEqual(self.backend.events, [])

    def test_disabled_router_drops_everything(self):
        self.router.enabled = False
        self.router.handle({"k": "md", "b": 0, "x": 0.5, "y": 0.5})
        self.router.handle({"k": "txt", "s": "should not appear"})
        self.assertEqual(self.backend.events, [])

    def test_release_all_clears_held_keys(self):
        self.router.handle({"k": "kd", "c": "ShiftLeft"})
        self.router.handle({"k": "kd", "c": "KeyA"})
        self.backend.events.clear()
        self.router.release_all()
        released = {e["code"] for e in self.backend.events if e["down"] is False}
        self.assertEqual(released, {"ShiftLeft", "KeyA"})

    def test_scancode_table_covers_the_common_keys(self):
        for code in ["KeyA", "Digit0", "Enter", "Escape", "Tab", "Space", "Backspace",
                     "ArrowUp", "Delete", "F1", "F12", "ControlLeft", "ControlRight",
                     "AltLeft", "AltRight", "ShiftLeft", "MetaLeft", "NumpadEnter",
                     "Home", "End", "PageUp", "PageDown", "Insert", "Semicolon", "Slash"]:
            self.assertIn(code, SCANCODES, code)
        # extended keys must be flagged, or arrows/nav act like numpad keys
        self.assertEqual(SCANCODES["ArrowUp"][1], 1)
        self.assertEqual(SCANCODES["Numpad8"][1], 0)
        self.assertEqual(SCANCODES["ControlRight"][1], 1)
        self.assertEqual(SCANCODES["ControlLeft"][1], 0)


class TestFileService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ctl = []
        self.data = {}
        self.fs = FileService(self.ctl.append,
                              lambda x, s, c: self.data.setdefault(x, []).append((s, c)))

    def _last(self, op=None):
        if op:
            return next(c for c in reversed(self.ctl) if c.get("op") == op)
        return self.ctl[-1]

    def test_listing_reports_sizes_and_types(self):
        os.makedirs(os.path.join(self.tmp, "sub"))
        with open(os.path.join(self.tmp, "a.txt"), "w") as fh:
            fh.write("hello")
        self.fs.handle({"op": "list", "path": self.tmp})
        entries = {e["n"]: e for e in self._last("list-result")["entries"]}
        self.assertTrue(entries["sub"]["d"])
        self.assertFalse(entries["a.txt"]["d"])
        self.assertEqual(entries["a.txt"]["s"], 5)

    def test_download_then_upload_preserves_bytes(self):
        payload = os.urandom(400_000)
        src = os.path.join(self.tmp, "blob.bin")
        with open(src, "wb") as fh:
            fh.write(payload)
        self.fs.handle({"op": "get", "xferId": 1, "path": src})
        for _ in range(300):
            self.fs.handle({"op": "ack", "xferId": 1, "seq": 10 ** 6})
            if any(c.get("op") == "done" for c in self.ctl):
                break
            time.sleep(0.02)
        got = b"".join(c for _s, c in sorted(self.data[1]))
        self.assertEqual(got, payload)

        dst = os.path.join(self.tmp, "copy.bin")
        self.fs.handle({"op": "put", "xferId": 2, "path": dst, "size": len(payload)})
        for i in range(0, len(payload), P.FILE_CHUNK_SIZE):
            self.fs.on_data(2, i // P.FILE_CHUNK_SIZE + 1, payload[i:i + P.FILE_CHUNK_SIZE])
        self.fs.handle({"op": "done", "xferId": 2})
        with open(dst, "rb") as fh:
            self.assertEqual(fh.read(), payload)
        self.assertFalse(os.path.exists(dst + ".aegispart"))

    def test_jail_blocks_paths_outside_it(self):
        jailed = FileService(self.ctl.append, lambda *_: None, jail=self.tmp)
        self.ctl.clear()
        jailed.handle({"op": "list", "path": "/etc"})
        self.assertEqual(self._last()["op"], "error")
        jailed.handle({"op": "list", "path": os.path.join(self.tmp, "..")})
        self.assertEqual(self._last()["op"], "error")

    def test_jail_allows_paths_inside_it(self):
        os.makedirs(os.path.join(self.tmp, "inner"), exist_ok=True)
        jailed = FileService(self.ctl.append, lambda *_: None, jail=self.tmp)
        self.ctl.clear()
        jailed.handle({"op": "list", "path": os.path.join(self.tmp, "inner")})
        self.assertEqual(self._last()["op"], "list-result")

    def test_read_only_blocks_mutations(self):
        victim = os.path.join(self.tmp, "keepme.txt")
        with open(victim, "w") as fh:
            fh.write("x")
        ro = FileService(self.ctl.append, lambda *_: None, read_only=True)
        for op in ({"op": "delete", "path": victim},
                   {"op": "mkdir", "path": os.path.join(self.tmp, "nope")},
                   {"op": "put", "xferId": 9, "path": victim, "size": 1},
                   {"op": "rename", "path": victim, "to": victim + "2"}):
            self.ctl.clear()
            ro.handle(op)
            self.assertEqual(self._last()["op"], "error", op)
        self.assertTrue(os.path.exists(victim))

    def test_upload_chunks_arriving_out_of_order_are_reassembled(self):
        """A client that encrypts frames concurrently can emit them shuffled.
        Appending blindly corrupted the file with no error anywhere."""
        payload = bytes(range(256)) * 3000
        dst = os.path.join(self.tmp, "shuffled.bin")
        chunks = [(i // P.FILE_CHUNK_SIZE + 1, payload[i:i + P.FILE_CHUNK_SIZE])
                  for i in range(0, len(payload), P.FILE_CHUNK_SIZE)]
        self.assertGreater(len(chunks), 3, "need several chunks for this to mean anything")

        self.fs.handle({"op": "put", "xferId": 1, "path": dst, "size": len(payload)})
        for seq, chunk in sorted(chunks, key=lambda c: (c[0] * 7) % len(chunks)):
            self.fs.on_data(1, seq, chunk)
        self.fs.handle({"op": "done", "xferId": 1})
        with open(dst, "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_duplicate_upload_chunk_is_ignored(self):
        payload = b"x" * (P.FILE_CHUNK_SIZE * 2 + 17)
        dst = os.path.join(self.tmp, "dup.bin")
        chunks = [(i // P.FILE_CHUNK_SIZE + 1, payload[i:i + P.FILE_CHUNK_SIZE])
                  for i in range(0, len(payload), P.FILE_CHUNK_SIZE)]
        self.fs.handle({"op": "put", "xferId": 2, "path": dst, "size": len(payload)})
        for seq, chunk in chunks:
            self.fs.on_data(2, seq, chunk)
        self.fs.on_data(2, 1, chunks[0][1])           # replay
        self.fs.handle({"op": "done", "xferId": 2})
        with open(dst, "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_upload_with_a_permanently_missing_chunk_errors(self):
        payload = b"y" * (P.FILE_CHUNK_SIZE * 4)
        dst = os.path.join(self.tmp, "gap.bin")
        chunks = [(i // P.FILE_CHUNK_SIZE + 1, payload[i:i + P.FILE_CHUNK_SIZE])
                  for i in range(0, len(payload), P.FILE_CHUNK_SIZE)]
        self.ctl.clear()
        self.fs.handle({"op": "put", "xferId": 3, "path": dst, "size": len(payload)})
        for seq, chunk in chunks:
            if seq != 2:
                self.fs.on_data(3, seq, chunk)
        self.fs.handle({"op": "done", "xferId": 3})
        self.assertEqual(self._last()["op"], "error")
        self.assertIn("never arrived", self._last()["message"])
        self.assertFalse(os.path.exists(dst), "a truncated file must not be published")
        self.assertFalse(os.path.exists(dst + ".aegispart"))

    def test_missing_file_reports_an_error_not_a_crash(self):
        self.ctl.clear()
        self.fs.handle({"op": "get", "xferId": 5, "path": os.path.join(self.tmp, "ghost")})
        self.assertEqual(self._last()["op"], "error")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "agent.json")

    def test_roundtrip_and_permission_merge(self):
        c = Config(self.path)
        c["relayUrl"] = "wss://desk.example.com"
        c["permissions"]["shell"] = False
        c.save()
        c2 = Config(self.path)
        self.assertEqual(c2["relayUrl"], "wss://desk.example.com")
        self.assertFalse(c2.perm("shell"))
        self.assertTrue(c2.perm("input"))          # untouched default survives

    def test_password_is_hashed_not_stored(self):
        c = Config(self.path)
        c.set_unattended_password("Rest4urant-POS!")
        with open(self.path) as fh:
            raw = fh.read()
        self.assertNotIn("Rest4urant", raw)
        self.assertTrue(c.has_unattended_password)
        self.assertEqual(len(c.unattended_key()), 32)

    def test_weak_password_refused(self):
        c = Config(self.path)
        with self.assertRaises(ValueError):
            c.set_unattended_password("short")

    def test_clearing_the_password(self):
        c = Config(self.path)
        c.set_unattended_password("longenoughpassword")
        c.set_unattended_password(None)
        self.assertFalse(c.has_unattended_password)

    def test_capabilities_track_permissions(self):
        c = Config(self.path)
        c["permissions"]["shell"] = False
        c["permissions"]["files"] = False
        caps = c.capabilities()
        self.assertIn("screen", caps)
        self.assertNotIn("shell", caps)
        self.assertNotIn("files", caps)

    def test_config_file_is_not_world_readable(self):
        if os.name == "nt":
            self.skipTest("POSIX modes only")
        c = Config(self.path)
        c.save()
        self.assertEqual(os.stat(self.path).st_mode & 0o077, 0)

    def test_corrupt_config_falls_back_to_defaults(self):
        with open(self.path, "w") as fh:
            fh.write("{not json at all")
        c = Config(self.path)
        self.assertTrue(c.perm("input"))


class TestRelayUrlNormalisation(unittest.TestCase):
    def test_forms_people_actually_paste(self):
        from aegis_agent.client import normalise_relay_url as n
        cases = {
            "relay.example.com": "wss://relay.example.com/ws/agent",
            "https://relay.example.com": "wss://relay.example.com/ws/agent",
            "http://192.168.1.50:7443": "ws://192.168.1.50:7443/ws/agent",
            "wss://x.fly.dev/": "wss://x.fly.dev/ws/agent",
            "wss://x.fly.dev/ws/agent": "wss://x.fly.dev/ws/agent",
        }
        for raw, expect in cases.items():
            self.assertEqual(n(raw), expect, raw)

    def test_empty_url_is_an_error(self):
        from aegis_agent.client import normalise_relay_url as n
        with self.assertRaises(ValueError):
            n("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
