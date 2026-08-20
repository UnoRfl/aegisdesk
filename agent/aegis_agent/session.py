"""
One remote-control session.

Owns the encrypted channel, the capture/encode loop, and the service objects
(input, files, shell, clipboard, sysinfo). Everything a viewer can do to this
machine goes through `handle_frame`, and every permission check lives here.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Callable, Optional

from . import crypto, protocol as P
from .capture import TileEncoder, open_screen_source
from .clipboard import Clipboard
from .files import FileService
from .inputctl import InputRouter, open_input_backend
from .shell import ShellSession
from . import sysinfo

log = logging.getLogger("aegis.session")

MAX_AUTH_ATTEMPTS = 5
CLIPBOARD_POLL_SEC = 1.5


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


class RemoteSession:
    def __init__(self, sid: int, viewer_pub_b64: str, operator: str, cfg,
                 send_binary: Callable[[bytes], None],
                 on_closed: Callable[[int, str], None],
                 ui=None, prefer_null_input: bool = False):
        self.sid = sid
        self.operator = operator
        self.cfg = cfg
        self._send_binary = send_binary
        self._on_closed = on_closed
        self.ui = ui
        self.started_at = time.time()
        self.closed = threading.Event()

        self.viewer_pub = b64d(viewer_pub_b64)
        self.priv, self.pub = crypto.generate_keypair()
        key = crypto.derive_key(self.priv, self.viewer_pub, sid, self.viewer_pub, self.pub)
        self.channel = crypto.SecureChannel(key, crypto.DIR_AGENT_TO_VIEWER)

        self.auth_required = cfg.has_unattended_password
        self.authed = not self.auth_required
        self.auth_attempts = 0

        self._send_lock = threading.Lock()
        self._capture_thread: Optional[threading.Thread] = None
        self._clip_thread: Optional[threading.Thread] = None
        self.encoder = TileEncoder()
        self.encoder.set_profile(cfg.get("defaultQuality", "auto"),
                                 cfg.get("maxWidth", 1600), cfg.get("maxFps", 24))
        self.monitor_id = int(cfg.get("monitor", 1))
        self.paused = False
        self._keyframe_pending = True

        self.input = InputRouter(open_input_backend(prefer_null=prefer_null_input))
        self.input.enabled = cfg.perm("input")
        self.clipboard = Clipboard()
        self._last_clip = None
        self.files: Optional[FileService] = None
        self.shell: Optional[ShellSession] = None

        self.rtt_ms = 0.0
        self.bytes_sent = 0
        self.frames_sent = 0
        self._congested = False

    # ================================================================ lifecycle

    def begin(self):
        """Called after the relay confirms the session is open."""
        if self.auth_required:
            self.send_json(P.CH_AUTH_CHALLENGE, {
                "salt": b64e(self.cfg.unattended_salt()),
                "iterations": self.cfg.unattended_iterations(),
                "attemptsLeft": MAX_AUTH_ATTEMPTS - self.auth_attempts,
            })
            log.info("[%s] awaiting password from %s", self.sid, self.operator)
        else:
            self._go_live()

    def _go_live(self):
        self.authed = True
        if self.cfg.get("showBanner", True) and self.ui:
            self.ui.show_banner(self.sid, self.operator, lambda: self.close("ended_at_device"))
        if self.cfg.get("notifyOnConnect", True) and self.ui:
            self.ui.notify("Remote session started", f"{self.operator} is connected to this computer.")
        self.send_json(P.CH_STATUS, {"level": "info", "message": "connected"})
        self._start_capture()
        if self.cfg.perm("clipboard"):
            self._start_clipboard_watch()
        self._audit("session-open")
        log.info("[%s] live: operator=%s input=%s files=%s shell=%s",
                 self.sid, self.operator, self.input.enabled,
                 self.cfg.perm("files"), self.cfg.perm("shell"))

    def close(self, reason: str = "closed"):
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.input.release_all()
        except Exception:                                    # noqa: BLE001
            pass
        if self.shell:
            try:
                self.shell.kill()
            except Exception:                                # noqa: BLE001
                pass
        if self.files:
            self.files.shutdown()
        if self.ui:
            self.ui.hide_banner(self.sid)
        self._audit("session-close", reason=reason)
        log.info("[%s] closed (%s) after %.0fs, %.1f MB sent, %d frames",
                 self.sid, reason, time.time() - self.started_at,
                 self.bytes_sent / 1048576, self.frames_sent)
        try:
            self._on_closed(self.sid, reason)
        except Exception:                                    # noqa: BLE001
            pass

    def _audit(self, kind: str, **extra):
        if not self.cfg.get("sessionLog", True):
            return
        from .config import session_log_path
        rec = {"ts": int(time.time()), "kind": kind, "sid": self.sid,
               "operator": self.operator, "device": self.cfg.get("deviceId"), **extra}
        try:
            path = session_log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as exc:                             # noqa: BLE001
            log.debug("session log write failed: %s", exc)

    # ================================================================ sending

    def _send_inner(self, payload: bytes):
        if self.closed.is_set():
            return
        try:
            with self._send_lock:
                sealed = self.channel.seal(payload)
                frame = P.pack_outer(self.sid, sealed)
            self._send_binary(frame)
            self.bytes_sent += len(frame)
        except Exception as exc:                             # noqa: BLE001
            log.debug("[%s] send failed: %s", self.sid, exc)

    def send_json(self, channel: int, obj):
        self._send_inner(P.pack_json(channel, obj))

    def send_raw(self, channel: int, payload: bytes):
        self._send_inner(P.pack_raw(channel, payload))

    # ================================================================ receiving

    def handle_frame(self, sealed: bytes):
        try:
            plain = self.channel.open(sealed)
        except Exception as exc:                             # noqa: BLE001
            log.warning("[%s] dropping undecryptable frame: %s", self.sid, exc)
            return
        try:
            channel, payload = P.split_inner(plain)
        except P.ProtocolError:
            return

        if not self.authed:
            if channel == P.CH_AUTH_RESPONSE:
                self._handle_auth(payload)
            elif channel == P.CH_PING:
                self.send_raw(P.CH_PONG, payload)
            else:
                log.debug("[%s] ignoring channel 0x%02x before auth", self.sid, channel)
            return

        try:
            self._dispatch(channel, payload)
        except Exception as exc:                             # noqa: BLE001
            log.warning("[%s] channel 0x%02x handler failed: %s", self.sid, channel, exc)

    def _handle_auth(self, payload: bytes):
        self.auth_attempts += 1
        try:
            msg = P.parse_json(payload)
            given = b64d(msg.get("proof", ""))
        except Exception:                                    # noqa: BLE001
            given = b""
        key = self.cfg.unattended_key() or b""
        expected = crypto.auth_proof(key, self.sid, self.viewer_pub, self.pub)
        if given and crypto.constant_time_eq(given, expected):
            ack = crypto.auth_ack(key, self.sid, self.viewer_pub, self.pub)
            self.send_json(P.CH_AUTH_RESULT, {"ok": True, "proof": b64e(ack)})
            log.info("[%s] password accepted for %s", self.sid, self.operator)
            self._audit("auth-ok")
            if self.cfg.get("requireConsent", True) and not self.cfg.get("unattendedBypassesConsent", True):
                if not self._ask_consent():
                    self.send_json(P.CH_STATUS, {"level": "error", "message": "declined at the device"})
                    self.close("declined_by_user")
                    return
            self._go_live()
            return

        left = MAX_AUTH_ATTEMPTS - self.auth_attempts
        # A one-off support session has a code read aloud, not a typed password.
        # Saying the right word matters when the operator is relaying it by phone.
        noun = "code" if self.cfg.get("supportCode") else "password"
        log.warning("[%s] wrong %s from %s (%d attempt(s) left)",
                    self.sid, noun, self.operator, left)
        self._audit("auth-fail", attemptsLeft=left)
        if left <= 0:
            self.send_json(P.CH_AUTH_RESULT, {"ok": False, "reason": f"too many wrong {noun}s", "attemptsLeft": 0})
            time.sleep(0.5)
            self.close("auth_failed")
            return
        # linear backoff so a stolen device ID can't be brute-forced quickly
        time.sleep(min(4.0, 0.6 * self.auth_attempts))
        self.send_json(P.CH_AUTH_RESULT, {"ok": False, "reason": f"wrong {noun}", "attemptsLeft": left})

    def _ask_consent(self) -> bool:
        if not self.ui:
            return self.cfg.get("consentDefault", "deny") == "allow"
        return self.ui.ask_consent(self.operator,
                                   int(self.cfg.get("consentTimeoutSec", 45)),
                                   self.cfg.get("consentDefault", "deny") == "allow")

    # ---------------------------------------------------------------- dispatch

    def _dispatch(self, channel: int, payload: bytes):
        if channel == P.CH_INPUT:
            if not self.cfg.perm("input"):
                return
            self.input.handle(P.parse_json(payload))

        elif channel == P.CH_CONTROL:
            self._control(P.parse_json(payload))

        elif channel == P.CH_CLIPBOARD:
            if not self.cfg.perm("clipboard"):
                return
            text = str(P.parse_json(payload).get("text", ""))
            self._last_clip = text
            self.clipboard.set(text)

        elif channel == P.CH_FILE_CTL:
            if not self.cfg.perm("files"):
                self.send_json(P.CH_FILE_CTL, {"op": "error", "message": "file transfer is disabled on this device"})
                return
            self._file_service().handle(P.parse_json(payload))

        elif channel == P.CH_FILE_DATA:
            if not self.cfg.perm("files"):
                return
            xid, seq, chunk = P.unpack_file_data(payload)
            self._file_service().on_data(xid, seq, chunk)

        elif channel == P.CH_SHELL_CTL:
            if not self.cfg.perm("shell"):
                self.send_raw(P.CH_SHELL_OUT, P.pack_shell_out(
                    P.STREAM_STDERR, b"remote shell is disabled on this device\r\n"))
                return
            self._shell_ctl(P.parse_json(payload))

        elif channel == P.CH_SYSINFO:
            self._sysinfo(P.parse_json(payload))

        elif channel == P.CH_PING:
            self.send_raw(P.CH_PONG, payload)

        elif channel == P.CH_PONG:
            sent = P.unpack_ts(payload)
            self.rtt_ms = max(0.0, time.time() * 1000 - sent)

        else:
            log.debug("[%s] unhandled channel 0x%02x", self.sid, channel)

    # ---------------------------------------------------------------- control

    def _control(self, msg: dict):
        op = msg.get("op")
        if op == "quality":
            self.encoder.set_profile(str(msg.get("mode", "auto")),
                                     msg.get("maxWidth"), msg.get("maxFps"))
            self.send_json(P.CH_STATUS, {"level": "info",
                                         "message": f"quality: {self.encoder.profile.name}"})
        elif op == "keyframe":
            self.encoder.request_keyframe()
        elif op == "monitor":
            self.monitor_id = int(msg.get("id", 1))
            self._keyframe_pending = True
            self.encoder.request_keyframe()
        elif op == "pause":
            self.paused = bool(msg.get("on"))
            if not self.paused:
                self.encoder.request_keyframe()
        elif op == "cad":
            if self.cfg.perm("input"):
                self.input.handle({"k": "combo", "keys": ["ControlLeft", "AltLeft", "Delete"]})
                self.send_json(P.CH_STATUS, {"level": "info", "message":
                               "Ctrl+Alt+Del sent (needs an elevated agent to reach the secure desktop)"})
        elif op == "lock":
            if self.cfg.perm("lockWorkstation") and self.input.backend.lock_workstation():
                self.send_json(P.CH_STATUS, {"level": "info", "message": "workstation locked"})
            else:
                self.send_json(P.CH_STATUS, {"level": "warn", "message": "lock is not available here"})
        elif op == "blank":
            self.send_json(P.CH_STATUS, {"level": "warn",
                                         "message": "privacy screen is not supported in this build"})
        elif op == "disconnect":
            self.close("closed_by_viewer")

    # ---------------------------------------------------------------- services

    def _file_service(self) -> FileService:
        if self.files is None:
            self.files = FileService(
                send_ctl=lambda o: self.send_json(P.CH_FILE_CTL, o),
                send_data=lambda x, s, c: self.send_raw(P.CH_FILE_DATA, P.pack_file_data(x, s, c)),
                jail=self.cfg.get("permissions", {}).get("fileJail"),
                read_only=self.cfg.perm("filesReadOnly"))
        return self.files

    def _shell_ctl(self, msg: dict):
        op = msg.get("op")
        if op == "start":
            if self.shell and self.shell.alive:
                return
            self.shell = ShellSession(
                on_output=lambda stream, data: self.send_raw(
                    P.CH_SHELL_OUT, P.pack_shell_out(stream, data)))
            self.shell.start(int(msg.get("cols", 100)), int(msg.get("rows", 30)))
            self._audit("shell-start")
        elif op == "stdin" and self.shell:
            self.shell.write(str(msg.get("data", "")).encode("utf-8", "replace"))
        elif op == "resize" and self.shell:
            self.shell.resize(int(msg.get("cols", 100)), int(msg.get("rows", 30)))
        elif op == "kill" and self.shell:
            self.shell.kill()
            self.shell = None

    def _sysinfo(self, msg: dict):
        op = msg.get("op", "report")
        if op == "report":
            if not self.cfg.perm("sysinfo"):
                return
            self.send_json(P.CH_SYSINFO, {"op": "report-result",
                                          "report": sysinfo.full_report({"encoder": self._encoder_name()})})
        elif op == "processes":
            if not self.cfg.perm("processes"):
                return
            self.send_json(P.CH_SYSINFO, {"op": "processes-result", "processes": sysinfo.process_list()})
        elif op == "services":
            self.send_json(P.CH_SYSINFO, {"op": "services-result", "services": sysinfo.list_services()})
        elif op == "kill":
            if not self.cfg.perm("processes"):
                return
            res = sysinfo.kill_process(int(msg.get("pid", -1)))
            self._audit("process-kill", pid=msg.get("pid"), ok=res.get("ok"))
            self.send_json(P.CH_SYSINFO, {"op": "kill-result", **res})
        elif op == "stats":
            self.send_json(P.CH_SYSINFO, {"op": "stats-result", "stats": self.stats()})

    @staticmethod
    def _encoder_name():
        from .capture import encoder_name
        return encoder_name()

    def stats(self) -> dict:
        return {"sid": self.sid, "operator": self.operator,
                "uptimeSec": round(time.time() - self.started_at, 1),
                "bytesSent": self.bytes_sent, "frames": self.frames_sent,
                "rttMs": round(self.rtt_ms, 1),
                "quality": self.encoder.profile.name,
                "jpegQuality": self.encoder.profile.jpeg_quality,
                "maxWidth": self.encoder.profile.max_width,
                "maxFps": self.encoder.profile.max_fps,
                "encoder": self._encoder_name(),
                "congested": self._congested,
                **self.encoder.stats.snapshot()}

    # ---------------------------------------------------------------- clipboard

    def _start_clipboard_watch(self):
        def loop():
            while not self.closed.is_set():
                try:
                    txt = self.clipboard.get()
                    if txt and txt != self._last_clip:
                        self._last_clip = txt
                        self.send_json(P.CH_CLIPBOARD, {"text": txt})
                except Exception:                            # noqa: BLE001
                    pass
                self.closed.wait(CLIPBOARD_POLL_SEC)
        self._clip_thread = threading.Thread(target=loop, daemon=True, name=f"aegis-clip-{self.sid}")
        self._clip_thread.start()

    # ---------------------------------------------------------------- capture

    def _start_capture(self):
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True,
                                                name=f"aegis-cap-{self.sid}")
        self._capture_thread.start()

    def _capture_loop(self):
        src = None
        try:
            src = open_screen_source()          # mss must be created in its own thread
            self._announce_screen(src)
            last_info = time.time()
            last_cursor = None
            while not self.closed.is_set():
                frame_start = time.time()
                interval = 1.0 / max(1, self.encoder.profile.max_fps)

                if self.paused:
                    self.closed.wait(0.15)
                    continue

                try:
                    frame = src.grab(self.monitor_id)
                except Exception as exc:                     # noqa: BLE001
                    log.warning("[%s] capture failed (%s); re-opening screen", self.sid, exc)
                    try:
                        src.refresh()
                    except Exception:                        # noqa: BLE001
                        pass
                    self.closed.wait(0.5)
                    self.encoder.request_keyframe()
                    continue

                out = self.encoder.encode(frame)
                if out:
                    payload = P.pack_tile_frame(self.monitor_id, out["codec"], out["flags"],
                                                out["seq"], out["w"], out["h"], out["tiles"])
                    t_send = time.time()
                    self.send_raw(P.CH_TILE_FRAME, payload)
                    send_ms = (time.time() - t_send) * 1000
                    self.frames_sent += 1
                    # If pushing the frame into the socket took a big slice of
                    # our frame budget, the link is the bottleneck: back off.
                    self._congested = send_ms > interval * 1000 * 0.5
                    self.encoder.nudge(self._congested)

                # cursor position rides along cheaply so the viewer can draw it
                pos = self.input.backend.cursor_pos()
                if pos and pos != last_cursor:
                    last_cursor = pos
                    mx, my, mw, mh = self.input.monitor_rect
                    self.send_json(P.CH_CURSOR, {
                        "x": round((pos[0] - mx) / max(1, mw), 4),
                        "y": round((pos[1] - my) / max(1, mh), 4), "visible": True})

                if time.time() - last_info > 5:
                    last_info = time.time()
                    self._announce_screen(src, quiet=True)

                elapsed = time.time() - frame_start
                if elapsed < interval:
                    self.closed.wait(interval - elapsed)
        except Exception as exc:                             # noqa: BLE001
            log.error("[%s] capture loop died: %s", self.sid, exc, exc_info=True)
            self.send_json(P.CH_STATUS, {"level": "error", "message": f"capture stopped: {exc}"})
        finally:
            if src:
                src.close()

    def _announce_screen(self, src, quiet: bool = False):
        mons = src.monitors
        active = next((m for m in mons if m.id == self.monitor_id), mons[0])
        self.monitor_id = active.id
        self.input.set_monitor(active.x, active.y, active.width, active.height)
        info = {"monitors": [m.as_dict() for m in mons], "active": active.id,
                "w": active.width, "h": active.height,
                "maxWidth": self.encoder.profile.max_width,
                "quality": self.encoder.profile.name,
                "permissions": self.cfg.get("permissions", {}),
                "encoder": self._encoder_name(),
                "hostname": sysinfo.full_report()["hostname"] if not quiet else None}
        if quiet:
            info.pop("hostname", None)
        self.send_json(P.CH_SCREEN_INFO, info)
