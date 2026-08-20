"""
Wire framing shared by the agent and (mirrored in JS) the viewer.

Outer frame on the WebSocket:
    0xD1 | sid uint32 BE | nonce 12B | AES-GCM ciphertext+tag

Inner message after decryption:
    channel uint8 | payload
"""
from __future__ import annotations

import json
import struct
from typing import Any, Tuple

DATA_MAGIC = 0xD1

# --- channels ---
CH_AUTH_CHALLENGE = 0x01
CH_AUTH_RESPONSE  = 0x02
CH_AUTH_RESULT    = 0x03
CH_SCREEN_INFO    = 0x10
CH_TILE_FRAME     = 0x11
CH_CURSOR         = 0x12
CH_INPUT          = 0x20
CH_CLIPBOARD      = 0x30
CH_FILE_CTL       = 0x40
CH_FILE_DATA      = 0x41
CH_SHELL_CTL      = 0x50
CH_SHELL_OUT      = 0x51
CH_SYSINFO        = 0x60
CH_CONTROL        = 0x70
CH_STATUS         = 0x71
CH_PING           = 0x7E
CH_PONG           = 0x7F

CHANNEL_NAMES = {v: k for k, v in list(globals().items()) if k.startswith("CH_")}

CODEC_JPEG = 1
CODEC_PNG = 2

FLAG_KEYFRAME = 0x01

FILE_CHUNK_SIZE = 128 * 1024
FILE_WINDOW = 32          # max unacked chunks in flight


class ProtocolError(Exception):
    pass


# ------------------------------------------------------------------ outer frame

def pack_outer(sid: int, sealed: bytes) -> bytes:
    return struct.pack(">BI", DATA_MAGIC, sid) + sealed


def unpack_outer(buf: bytes) -> Tuple[int, bytes]:
    if len(buf) < 5 or buf[0] != DATA_MAGIC:
        raise ProtocolError("not a data-plane frame")
    (sid,) = struct.unpack_from(">I", buf, 1)
    return sid, buf[5:]


# ------------------------------------------------------------------ inner messages

def pack_json(channel: int, obj: Any) -> bytes:
    return bytes((channel,)) + json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")


def pack_raw(channel: int, payload: bytes) -> bytes:
    return bytes((channel,)) + payload


def split_inner(plain: bytes) -> Tuple[int, bytes]:
    if not plain:
        raise ProtocolError("empty inner message")
    return plain[0], plain[1:]


def parse_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ProtocolError(f"bad JSON payload: {exc}") from exc


# ------------------------------------------------------------------ tile frames

_TILE_HDR = ">BBBHHHH"
_TILE_HDR_LEN = struct.calcsize(_TILE_HDR)      # 11
_TILE_ENT = ">HHHHI"
_TILE_ENT_LEN = struct.calcsize(_TILE_ENT)      # 12


def pack_tile_frame(monitor_id: int, codec: int, flags: int, seq: int,
                    frame_w: int, frame_h: int, tiles) -> bytes:
    """tiles: iterable of (x, y, w, h, image_bytes)."""
    tiles = list(tiles)
    out = bytearray(struct.pack(_TILE_HDR, monitor_id & 0xFF, codec, flags,
                                seq & 0xFFFF, frame_w, frame_h, len(tiles)))
    for x, y, w, h, data in tiles:
        out += struct.pack(_TILE_ENT, x, y, w, h, len(data))
        out += data
    return bytes(out)


def unpack_tile_frame(payload: bytes):
    """Mirror of pack_tile_frame -- used by tests and any Python viewer."""
    monitor_id, codec, flags, seq, fw, fh, count = struct.unpack_from(_TILE_HDR, payload, 0)
    off = _TILE_HDR_LEN
    tiles = []
    for _ in range(count):
        x, y, w, h, n = struct.unpack_from(_TILE_ENT, payload, off)
        off += _TILE_ENT_LEN
        tiles.append((x, y, w, h, payload[off:off + n]))
        off += n
    if off != len(payload):
        raise ProtocolError(f"tile frame length mismatch: consumed {off} of {len(payload)}")
    return {"monitor": monitor_id, "codec": codec, "flags": flags, "seq": seq,
            "w": fw, "h": fh, "tiles": tiles}


# ------------------------------------------------------------------ file data

def pack_file_data(xfer_id: int, seq: int, chunk: bytes) -> bytes:
    return struct.pack(">II", xfer_id, seq) + chunk


def unpack_file_data(payload: bytes):
    xfer_id, seq = struct.unpack_from(">II", payload, 0)
    return xfer_id, seq, payload[8:]


# ------------------------------------------------------------------ shell

STREAM_STDOUT = 1
STREAM_STDERR = 2
STREAM_EXIT = 3


def pack_shell_out(stream: int, data: bytes) -> bytes:
    return bytes((stream,)) + data


# ------------------------------------------------------------------ ping

def pack_ts(channel: int, ts_ms: int) -> bytes:
    return bytes((channel,)) + struct.pack(">Q", ts_ms & 0xFFFFFFFFFFFFFFFF)


def unpack_ts(payload: bytes) -> int:
    return struct.unpack_from(">Q", payload, 0)[0]
