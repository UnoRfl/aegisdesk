"""
Screen capture, damage detection and tile encoding.

The expensive part of any remote desktop tool is deciding what *not* to send.
Each frame is compared to the previous one on a tile grid using a single
vectorised numpy comparison, adjacent changed tiles are merged into
rectangles, and only those rectangles are JPEG-encoded. On a mostly-static
screen (someone typing an order into a POS) that is a few kilobytes a frame
instead of a few hundred.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

log = logging.getLogger("aegis.capture")

# -------- optional accelerators ----------------------------------------------
try:
    import cv2  # type: ignore
    _HAVE_CV2 = True
except Exception:                                       # noqa: BLE001
    cv2 = None                                          # type: ignore
    _HAVE_CV2 = False

try:
    from PIL import Image  # type: ignore
    _HAVE_PIL = True
except Exception:                                       # noqa: BLE001
    Image = None                                        # type: ignore
    _HAVE_PIL = False

try:
    import mss  # type: ignore
    _HAVE_MSS = True
except Exception:                                       # noqa: BLE001
    mss = None                                          # type: ignore
    _HAVE_MSS = False


TILE = 64
MAX_RECTS = 48
FULL_FRAME_RATIO = 0.55     # if more than this fraction changed, just resend everything


def encoder_name() -> str:
    if _HAVE_CV2:
        return "opencv"
    if _HAVE_PIL:
        return "pillow"
    return "none"


# ============================================================== screen sources

@dataclass
class Monitor:
    id: int
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    def as_dict(self):
        return {"id": self.id, "x": self.x, "y": self.y, "w": self.width,
                "h": self.height, "primary": self.primary}


class ScreenSource:
    """Thread-confined mss wrapper. Create and use from a single thread."""

    def __init__(self):
        if not _HAVE_MSS:
            raise RuntimeError("mss is not installed -- pip install mss")
        self._sct = mss.mss()
        self._monitors = self._enumerate()

    def _enumerate(self) -> List[Monitor]:
        out: List[Monitor] = []
        raw = self._sct.monitors
        # raw[0] is the union of all screens; raw[1:] are the individual ones
        for idx, m in enumerate(raw[1:], start=1):
            out.append(Monitor(id=idx, x=m["left"], y=m["top"],
                               width=m["width"], height=m["height"], primary=(idx == 1)))
        if len(out) > 1:
            union = raw[0]
            out.append(Monitor(id=0, x=union["left"], y=union["top"],
                               width=union["width"], height=union["height"], primary=False))
        return out

    @property
    def monitors(self) -> List[Monitor]:
        return self._monitors

    def refresh(self):
        try:
            self._sct.close()
        except Exception:                               # noqa: BLE001
            pass
        self._sct = mss.mss()
        self._monitors = self._enumerate()

    def get(self, monitor_id: int) -> Monitor:
        for m in self._monitors:
            if m.id == monitor_id:
                return m
        return self._monitors[0]

    def grab(self, monitor_id: int) -> np.ndarray:
        """Return an HxWx3 BGR uint8 array."""
        raw = self._sct.monitors
        idx = monitor_id if 0 <= monitor_id < len(raw) else 1
        shot = self._sct.grab(raw[idx])
        arr = np.frombuffer(shot.bgra, dtype=np.uint8)
        arr = arr.reshape(shot.height, shot.width, 4)
        return arr[:, :, :3]        # BGRA -> BGR, drops the useless alpha

    def close(self):
        try:
            self._sct.close()
        except Exception:                               # noqa: BLE001
            pass


class NullScreenSource(ScreenSource):
    """Synthetic screen for headless CI: a moving gradient plus a clock bar.
    Lets the whole pipeline be exercised without a display."""

    def __init__(self, width=1280, height=720):
        self._w, self._h = width, height
        self._monitors = [Monitor(1, 0, 0, width, height, True)]
        self._t = 0

    def refresh(self):
        pass

    def grab(self, monitor_id: int = 1) -> np.ndarray:
        self._t += 1
        yy, xx = np.mgrid[0:self._h, 0:self._w]
        frame = np.zeros((self._h, self._w, 3), dtype=np.uint8)
        frame[:, :, 0] = (xx // 6) % 256
        frame[:, :, 1] = (yy // 4) % 256
        frame[:, :, 2] = 64
        # a small block that moves every frame -> guarantees a damage rect
        bx = (self._t * 17) % max(1, self._w - 120)
        by = (self._t * 7) % max(1, self._h - 120)
        frame[by:by + 100, bx:bx + 100] = 255
        return frame

    def close(self):
        pass


def open_screen_source() -> ScreenSource:
    try:
        src = ScreenSource()
        if not src.monitors:
            raise RuntimeError("no monitors reported")
        return src
    except Exception as exc:                            # noqa: BLE001
        log.warning("no real display available (%s); using synthetic screen", exc)
        return NullScreenSource()


# ============================================================== encoding

def _encode_jpeg(bgr: np.ndarray, quality: int) -> bytes:
    if _HAVE_CV2:
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality),
                                             int(cv2.IMWRITE_JPEG_OPTIMIZE), 1])
        if ok:
            return buf.tobytes()
    if _HAVE_PIL:
        import io
        img = Image.fromarray(bgr[:, :, ::-1])          # BGR -> RGB
        bio = io.BytesIO()
        img.save(bio, format="JPEG", quality=int(quality), optimize=False, subsampling=1)
        return bio.getvalue()
    raise RuntimeError("no JPEG encoder available -- install opencv-python-headless or Pillow")


def _encode_png(bgr: np.ndarray) -> bytes:
    if _HAVE_CV2:
        ok, buf = cv2.imencode(".png", bgr, [int(cv2.IMWRITE_PNG_COMPRESSION), 6])
        if ok:
            return buf.tobytes()
    if _HAVE_PIL:
        import io
        bio = io.BytesIO()
        Image.fromarray(bgr[:, :, ::-1]).save(bio, format="PNG", optimize=False)
        return bio.getvalue()
    raise RuntimeError("no PNG encoder available")


def _resize(bgr: np.ndarray, w: int, h: int) -> np.ndarray:
    if w == bgr.shape[1] and h == bgr.shape[0]:
        return bgr
    if _HAVE_CV2:
        interp = cv2.INTER_AREA if w < bgr.shape[1] else cv2.INTER_LINEAR
        return cv2.resize(bgr, (w, h), interpolation=interp)
    if _HAVE_PIL:
        img = Image.fromarray(bgr[:, :, ::-1]).resize((w, h), Image.BILINEAR)
        return np.asarray(img)[:, :, ::-1]
    # last resort: nearest-neighbour via slicing
    yi = (np.arange(h) * bgr.shape[0] // h).clip(0, bgr.shape[0] - 1)
    xi = (np.arange(w) * bgr.shape[1] // w).clip(0, bgr.shape[1] - 1)
    return bgr[yi][:, xi]


# ============================================================== damage detection

def changed_grid(cur: np.ndarray, prev: np.ndarray, tile: int = TILE) -> np.ndarray:
    """Boolean (rows, cols) grid of tiles that differ between two frames."""
    h, w = cur.shape[:2]
    gh = (h + tile - 1) // tile
    gw = (w + tile - 1) // tile
    ph, pw = gh * tile, gw * tile

    diff = np.any(cur != prev, axis=2)                  # HxW bool
    if ph != h or pw != w:
        padded = np.zeros((ph, pw), dtype=bool)
        padded[:h, :w] = diff
        diff = padded
    return diff.reshape(gh, tile, gw, tile).any(axis=(1, 3))


def grid_to_rects(grid: np.ndarray, tile: int, w: int, h: int) -> List[Tuple[int, int, int, int]]:
    """Merge changed tiles into as few pixel rectangles as possible:
    horizontal runs first, then vertical merge of identical runs."""
    gh, gw = grid.shape
    runs_by_row: List[List[Tuple[int, int]]] = []
    for r in range(gh):
        row = grid[r]
        runs: List[Tuple[int, int]] = []
        c = 0
        while c < gw:
            if row[c]:
                start = c
                while c < gw and row[c]:
                    c += 1
                runs.append((start, c))
            else:
                c += 1
        runs_by_row.append(runs)

    rects: List[Tuple[int, int, int, int]] = []
    open_rects = {}                                     # (c0,c1) -> (row_start, row_end)
    for r in range(gh):
        current = set(runs_by_row[r])
        for span in list(open_rects.keys()):
            if span not in current:
                r0, r1 = open_rects.pop(span)
                rects.append((span[0], r0, span[1], r1))
        for span in current:
            if span in open_rects:
                open_rects[span] = (open_rects[span][0], r + 1)
            else:
                open_rects[span] = (r, r + 1)
    for span, (r0, r1) in open_rects.items():
        rects.append((span[0], r0, span[1], r1))

    out = []
    for c0, r0, c1, r1 in rects:
        x = c0 * tile
        y = r0 * tile
        rw = min(c1 * tile, w) - x
        rh = min(r1 * tile, h) - y
        if rw > 0 and rh > 0:
            out.append((x, y, rw, rh))
    out.sort(key=lambda r: (r[1], r[0]))
    return out


# ============================================================== the encoder

@dataclass
class EncodeStats:
    frames: int = 0
    keyframes: int = 0
    tiles: int = 0
    bytes_out: int = 0
    encode_ms_total: float = 0.0
    capture_ms_total: float = 0.0
    skipped_identical: int = 0

    def snapshot(self):
        f = max(1, self.frames)
        return {"frames": self.frames, "keyframes": self.keyframes, "tiles": self.tiles,
                "bytesOut": self.bytes_out, "skipped": self.skipped_identical,
                "avgEncodeMs": round(self.encode_ms_total / f, 2),
                "avgCaptureMs": round(self.capture_ms_total / f, 2)}


@dataclass
class QualityProfile:
    name: str = "balanced"
    jpeg_quality: int = 62
    max_width: int = 1600
    max_fps: int = 24

    @staticmethod
    def preset(name: str) -> "QualityProfile":
        table = {
            "speed":     QualityProfile("speed", 38, 1280, 30),
            "balanced":  QualityProfile("balanced", 62, 1600, 24),
            "quality":   QualityProfile("quality", 84, 3840, 20),
            "auto":      QualityProfile("auto", 62, 1600, 24),
        }
        return table.get(name, table["balanced"])


class TileEncoder:
    """Stateful per-session encoder. Not thread-safe; drive it from one thread."""

    def __init__(self, tile: int = TILE):
        self.tile = tile
        self.prev: Optional[np.ndarray] = None
        self.seq = 0
        self.profile = QualityProfile.preset("auto")
        self.auto = True
        self.stats = EncodeStats()
        self._force_keyframe = True
        self._last_shape: Optional[Tuple[int, int]] = None

    # -- controls -----------------------------------------------------------
    def set_profile(self, name: str, max_width: Optional[int] = None, max_fps: Optional[int] = None):
        self.auto = (name == "auto")
        self.profile = QualityProfile.preset(name)
        if max_width:
            self.profile.max_width = max(320, min(7680, int(max_width)))
        if max_fps:
            self.profile.max_fps = max(1, min(60, int(max_fps)))
        self.request_keyframe()

    def request_keyframe(self):
        self._force_keyframe = True
        self.prev = None

    def nudge(self, congested: bool):
        """Adaptive step, called once per frame when in auto mode."""
        if not self.auto:
            return
        p = self.profile
        if congested:
            p.jpeg_quality = max(30, p.jpeg_quality - 6)
            if p.jpeg_quality <= 36:
                p.max_width = max(960, int(p.max_width * 0.85))
                p.max_fps = max(6, p.max_fps - 3)
        else:
            if p.jpeg_quality < 72:
                p.jpeg_quality = min(72, p.jpeg_quality + 2)
            elif p.max_width < 1920:
                p.max_width = min(1920, int(p.max_width * 1.08) + 8)
            elif p.max_fps < 24:
                p.max_fps += 1

    # -- main entry ---------------------------------------------------------
    def encode(self, bgr: np.ndarray) -> Optional[dict]:
        """Return {'w','h','flags','tiles':[(x,y,w,h,bytes)],'bytes':n} or None
        when nothing at all changed."""
        t0 = time.perf_counter()
        h0, w0 = bgr.shape[:2]
        target_w = min(w0, self.profile.max_width)
        if target_w < w0:
            target_h = max(1, int(round(h0 * target_w / w0)))
            # keep dimensions even -- friendlier to JPEG chroma subsampling
            target_w -= target_w % 2
            target_h -= target_h % 2
            frame = _resize(bgr, target_w, target_h)
        else:
            frame = bgr
        h, w = frame.shape[:2]

        if self._last_shape != (h, w):
            self._last_shape = (h, w)
            self._force_keyframe = True
            self.prev = None

        keyframe = self._force_keyframe or self.prev is None
        if keyframe:
            rects = [(0, 0, w, h)]
        else:
            grid = changed_grid(frame, self.prev, self.tile)
            total = grid.size
            n_changed = int(grid.sum())
            if n_changed == 0:
                self.stats.skipped_identical += 1
                self.stats.capture_ms_total += (time.perf_counter() - t0) * 1000
                return None
            if n_changed / max(1, total) > FULL_FRAME_RATIO:
                rects = [(0, 0, w, h)]
                keyframe = True
            else:
                rects = grid_to_rects(grid, self.tile, w, h)
                if len(rects) > MAX_RECTS:
                    # too fragmented: coalesce into one bounding box
                    xs0 = min(r[0] for r in rects); ys0 = min(r[1] for r in rects)
                    xs1 = max(r[0] + r[2] for r in rects); ys1 = max(r[1] + r[3] for r in rects)
                    rects = [(xs0, ys0, xs1 - xs0, ys1 - ys0)]

        t1 = time.perf_counter()
        tiles = []
        total_bytes = 0
        q = self.profile.jpeg_quality
        for (x, y, rw, rh) in rects:
            sub = frame[y:y + rh, x:x + rw]
            if sub.size == 0:
                continue
            if rw * rh <= 1024 and not keyframe:
                data = _encode_png(sub)                 # tiny rects: PNG is smaller and lossless
                codec = 2
            else:
                data = _encode_jpeg(sub, q)
                codec = 1
            tiles.append((x, y, rw, rh, data, codec))
            total_bytes += len(data)

        if not tiles:
            return None

        # a frame is single-codec on the wire; if we mixed, re-encode the odd ones
        codecs = {t[5] for t in tiles}
        if len(codecs) > 1:
            tiles = [(x, y, rw, rh,
                      d if c == 1 else _encode_jpeg(frame[y:y + rh, x:x + rw], q), 1)
                     for (x, y, rw, rh, d, c) in tiles]
            total_bytes = sum(len(t[4]) for t in tiles)
            codec = 1
        else:
            codec = tiles[0][5]

        self.prev = frame.copy()
        self._force_keyframe = False
        self.seq = (self.seq + 1) & 0xFFFF
        self.stats.frames += 1
        self.stats.keyframes += 1 if keyframe else 0
        self.stats.tiles += len(tiles)
        self.stats.bytes_out += total_bytes
        self.stats.capture_ms_total += (t1 - t0) * 1000
        self.stats.encode_ms_total += (time.perf_counter() - t1) * 1000

        return {"w": w, "h": h, "seq": self.seq, "codec": codec,
                "flags": 0x01 if keyframe else 0x00,
                "tiles": [(x, y, rw, rh, d) for (x, y, rw, rh, d, _c) in tiles],
                "bytes": total_bytes}
