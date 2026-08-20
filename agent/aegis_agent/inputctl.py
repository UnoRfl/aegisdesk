"""
Input injection.

Windows uses SendInput through ctypes -- no third-party dependency, and
scancode-level key events so remote typing behaves like a real keyboard.
Other platforms fall back to pynput. Headless test runs get a recorder that
just logs what would have happened.
"""
from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Dict, List, Optional, Tuple

from .keymap import MODIFIER_CODES, PYNPUT_CHARS, PYNPUT_SPECIAL, SCANCODES

log = logging.getLogger("aegis.input")
IS_WINDOWS = platform.system() == "Windows"


class InputBackend:
    name = "none"
    def move(self, vx: int, vy: int): ...
    def button(self, index: int, down: bool, vx: int, vy: int): ...
    def wheel(self, dx: int, dy: int, vx: int, vy: int): ...
    def key(self, code: str, down: bool) -> bool: ...
    def text(self, s: str): ...
    def virtual_screen(self) -> Tuple[int, int, int, int]: return (0, 0, 1920, 1080)
    def cursor_pos(self) -> Optional[Tuple[int, int]]: return None
    def lock_workstation(self) -> bool: return False
    def release_all(self): ...


# ==================================================================== Windows

class WindowsInput(InputBackend):
    name = "win32-sendinput"

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)

        ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

        self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT, self.INPUT_UNION = \
            MOUSEINPUT, KEYBDINPUT, INPUT, INPUT_UNION

        self.INPUT_MOUSE, self.INPUT_KEYBOARD = 0, 1
        self.MOUSEEVENTF = {
            "move": 0x0001, "absolute": 0x8000, "virtualdesk": 0x4000,
            "ldown": 0x0002, "lup": 0x0004, "rdown": 0x0008, "rup": 0x0010,
            "mdown": 0x0020, "mup": 0x0040, "xdown": 0x0080, "xup": 0x0100,
            "wheel": 0x0800, "hwheel": 0x1000,
        }
        self.KEYEVENTF_EXTENDEDKEY = 0x0001
        self.KEYEVENTF_KEYUP = 0x0002
        self.KEYEVENTF_UNICODE = 0x0004
        self.KEYEVENTF_SCANCODE = 0x0008
        self._down: set[str] = set()
        self._lock = threading.Lock()
        # DPI awareness: without this, coordinates on a scaled display are wrong
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:                                       # noqa: BLE001
            try:
                self.user32.SetProcessDPIAware()
            except Exception:                                   # noqa: BLE001
                pass

    def _send(self, *inputs):
        n = len(inputs)
        arr = (self.INPUT * n)(*inputs)
        sent = self.user32.SendInput(n, arr, self.ctypes.sizeof(self.INPUT))
        if sent != n:
            err = self.ctypes.get_last_error()
            log.debug("SendInput sent %d/%d (err %s)", sent, n, err)
        return sent == n

    def virtual_screen(self):
        SM = self.user32.GetSystemMetrics
        return (SM(76), SM(77), max(1, SM(78)), max(1, SM(79)))   # X, Y, CX, CY of virtual screen

    def cursor_pos(self):
        from ctypes import wintypes
        pt = wintypes.POINT()
        if self.user32.GetCursorPos(self.ctypes.byref(pt)):
            return (pt.x, pt.y)
        return None

    def _abs(self, vx: int, vy: int):
        x0, y0, w, h = self.virtual_screen()
        nx = int(round((vx - x0) * 65535 / max(1, w - 1)))
        ny = int(round((vy - y0) * 65535 / max(1, h - 1)))
        return max(0, min(65535, nx)), max(0, min(65535, ny))

    def _mouse(self, flags: int, vx: Optional[int] = None, vy: Optional[int] = None, data: int = 0):
        f = flags
        dx = dy = 0
        if vx is not None and vy is not None:
            dx, dy = self._abs(vx, vy)
            f |= self.MOUSEEVENTF["move"] | self.MOUSEEVENTF["absolute"] | self.MOUSEEVENTF["virtualdesk"]
        mi = self.MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, f, 0, 0)
        return self.INPUT(self.INPUT_MOUSE, self.INPUT_UNION(mi=mi))

    def move(self, vx: int, vy: int):
        self._send(self._mouse(0, vx, vy))

    def button(self, index: int, down: bool, vx: int, vy: int):
        M = self.MOUSEEVENTF
        table = {0: ("ldown", "lup", 0), 1: ("mdown", "mup", 0),
                 2: ("rdown", "rup", 0), 3: ("xdown", "xup", 1), 4: ("xdown", "xup", 2)}
        if index not in table:
            return
        dn, up, xdata = table[index]
        self._send(self._mouse(M[dn if down else up], vx, vy, xdata))

    def wheel(self, dx: int, dy: int, vx: int, vy: int):
        M = self.MOUSEEVENTF
        events = []
        if dy:
            events.append(self._mouse(M["wheel"], vx, vy, self._i32(-dy)))
        if dx:
            events.append(self._mouse(M["hwheel"], vx, vy, self._i32(dx)))
        if events:
            self._send(*events)

    @staticmethod
    def _i32(v: int) -> int:
        v = int(v)
        return v & 0xFFFFFFFF if v >= 0 else (v + (1 << 32)) & 0xFFFFFFFF

    def _kb(self, scan: int, extended: int, up: bool):
        flags = self.KEYEVENTF_SCANCODE | (self.KEYEVENTF_KEYUP if up else 0)
        if extended:
            flags |= self.KEYEVENTF_EXTENDEDKEY
        ki = self.KEYBDINPUT(0, scan & 0xFFFF, flags, 0, 0)
        return self.INPUT(self.INPUT_KEYBOARD, self.INPUT_UNION(ki=ki))

    def key(self, code: str, down: bool) -> bool:
        entry = SCANCODES.get(code)
        if not entry:
            return False
        scan, ext = entry
        with self._lock:
            if down:
                self._down.add(code)
            else:
                self._down.discard(code)
        return self._send(self._kb(scan, ext, not down))

    def text(self, s: str):
        events = []
        for ch in s:
            for unit in self._utf16_units(ch):
                events.append(self._unicode(unit, False))
                events.append(self._unicode(unit, True))
            if len(events) >= 64:
                self._send(*events)
                events = []
        if events:
            self._send(*events)

    @staticmethod
    def _utf16_units(ch: str) -> List[int]:
        b = ch.encode("utf-16-le")
        return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]

    def _unicode(self, unit: int, up: bool):
        flags = self.KEYEVENTF_UNICODE | (self.KEYEVENTF_KEYUP if up else 0)
        ki = self.KEYBDINPUT(0, unit & 0xFFFF, flags, 0, 0)
        return self.INPUT(self.INPUT_KEYBOARD, self.INPUT_UNION(ki=ki))

    def lock_workstation(self) -> bool:
        try:
            return bool(self.user32.LockWorkStation())
        except Exception:                                       # noqa: BLE001
            return False

    def release_all(self):
        with self._lock:
            stuck = list(self._down)
            self._down.clear()
        for code in stuck:
            entry = SCANCODES.get(code)
            if entry:
                self._send(self._kb(entry[0], entry[1], True))
        if stuck:
            log.info("released %d stuck key(s): %s", len(stuck), ", ".join(stuck))


# ==================================================================== pynput

class PynputInput(InputBackend):
    name = "pynput"

    def __init__(self):
        from pynput import keyboard, mouse           # type: ignore
        self.kb = keyboard.Controller()
        self.ms = mouse.Controller()
        self.Key = keyboard.Key
        self.Button = mouse.Button
        self._down: set[str] = set()
        self._screen = self._probe_screen()

    @staticmethod
    def _probe_screen():
        try:
            import mss                               # type: ignore
            with mss.mss() as s:
                m = s.monitors[0]
                return (m["left"], m["top"], m["width"], m["height"])
        except Exception:                            # noqa: BLE001
            return (0, 0, 1920, 1080)

    def virtual_screen(self):
        return self._screen

    def cursor_pos(self):
        try:
            return tuple(self.ms.position)           # type: ignore
        except Exception:                            # noqa: BLE001
            return None

    def move(self, vx, vy):
        self.ms.position = (int(vx), int(vy))

    def _btn(self, index):
        return {0: self.Button.left, 1: self.Button.middle, 2: self.Button.right}.get(index)

    def button(self, index, down, vx, vy):
        b = self._btn(index)
        if b is None:
            return
        self.ms.position = (int(vx), int(vy))
        (self.ms.press if down else self.ms.release)(b)

    def wheel(self, dx, dy, vx, vy):
        self.ms.position = (int(vx), int(vy))
        self.ms.scroll(int(dx / 120) if dx else 0, int(-dy / 120) if dy else 0)

    def _resolve(self, code):
        if code in PYNPUT_SPECIAL:
            return getattr(self.Key, PYNPUT_SPECIAL[code], None)
        return PYNPUT_CHARS.get(code)

    def key(self, code, down):
        k = self._resolve(code)
        if k is None:
            return False
        try:
            (self.kb.press if down else self.kb.release)(k)
        except Exception as exc:                     # noqa: BLE001
            log.debug("pynput key %s failed: %s", code, exc)
            return False
        if down:
            self._down.add(code)
        else:
            self._down.discard(code)
        return True

    def text(self, s):
        try:
            self.kb.type(s)
        except Exception as exc:                     # noqa: BLE001
            log.debug("pynput type failed: %s", exc)

    def release_all(self):
        for code in list(self._down):
            self.key(code, False)
        self._down.clear()


# ==================================================================== headless

class RecordingInput(InputBackend):
    """No-op backend that remembers events -- used by the test suite and by
    any run with no usable display."""
    name = "recording"

    def __init__(self, screen=(0, 0, 1280, 720)):
        self.events: List[dict] = []
        self._screen = screen
        self._down: set[str] = set()

    def virtual_screen(self):
        return self._screen

    def cursor_pos(self):
        for e in reversed(self.events):
            if e["type"] in ("move", "button"):
                return (e["x"], e["y"])
        return (0, 0)

    def move(self, vx, vy):
        self.events.append({"type": "move", "x": vx, "y": vy})

    def button(self, index, down, vx, vy):
        self.events.append({"type": "button", "b": index, "down": down, "x": vx, "y": vy})

    def wheel(self, dx, dy, vx, vy):
        self.events.append({"type": "wheel", "dx": dx, "dy": dy, "x": vx, "y": vy})

    def key(self, code, down):
        if code not in SCANCODES:
            return False
        self.events.append({"type": "key", "code": code, "down": down})
        (self._down.add if down else self._down.discard)(code)
        return True

    def text(self, s):
        self.events.append({"type": "text", "s": s})

    def release_all(self):
        for c in list(self._down):
            self.key(c, False)


def open_input_backend(prefer_null: bool = False) -> InputBackend:
    if prefer_null:
        return RecordingInput()
    if IS_WINDOWS:
        try:
            return WindowsInput()
        except Exception as exc:                     # noqa: BLE001
            log.error("SendInput backend unavailable (%s)", exc)
    else:
        try:
            return PynputInput()
        except Exception as exc:                     # noqa: BLE001
            log.warning("pynput backend unavailable (%s); input will be ignored", exc)
    return RecordingInput()


# ==================================================================== dispatcher

class InputRouter:
    """Turns protocol INPUT messages into backend calls.

    Owns the mapping from normalized (0..1) coordinates inside the streamed
    monitor to absolute virtual-desktop pixels, and enforces the read-only
    and input-disabled switches.
    """

    def __init__(self, backend: InputBackend):
        self.backend = backend
        self.monitor_rect = (0, 0, 1920, 1080)      # x, y, w, h of the monitor being streamed
        self.enabled = True
        self.last_activity = 0.0
        self._combo_lock = threading.Lock()

    def set_monitor(self, x: int, y: int, w: int, h: int):
        self.monitor_rect = (x, y, max(1, w), max(1, h))

    def _to_virtual(self, nx: float, ny: float) -> Tuple[int, int]:
        x, y, w, h = self.monitor_rect
        nx = 0.0 if nx < 0 else (1.0 if nx > 1 else float(nx))
        ny = 0.0 if ny < 0 else (1.0 if ny > 1 else float(ny))
        return int(round(x + nx * (w - 1))), int(round(y + ny * (h - 1)))

    def handle(self, msg: dict) -> None:
        if not self.enabled:
            return
        k = msg.get("k")
        self.last_activity = time.time()
        try:
            if k == "m":
                self.backend.move(*self._to_virtual(msg.get("x", 0), msg.get("y", 0)))
            elif k in ("md", "mu"):
                vx, vy = self._to_virtual(msg.get("x", 0), msg.get("y", 0))
                self.backend.button(int(msg.get("b", 0)), k == "md", vx, vy)
            elif k == "w":
                vx, vy = self._to_virtual(msg.get("x", 0), msg.get("y", 0))
                self.backend.wheel(int(msg.get("dx", 0)), int(msg.get("dy", 0)), vx, vy)
            elif k in ("kd", "ku"):
                code = str(msg.get("c", ""))
                if not self.backend.key(code, k == "kd"):
                    log.debug("unmapped key code %r", code)
            elif k == "txt":
                s = str(msg.get("s", ""))[:4096]
                if s:
                    self.backend.text(s)
            elif k == "combo":
                keys = [str(c) for c in (msg.get("keys") or [])][:6]
                self._combo(keys)
        except Exception as exc:                     # noqa: BLE001
            log.warning("input dispatch failed (%s): %s", k, exc)

    def _combo(self, keys: List[str]):
        with self._combo_lock:
            pressed = []
            for c in keys:
                if self.backend.key(c, True):
                    pressed.append(c)
                time.sleep(0.012)
            time.sleep(0.03)
            for c in reversed(pressed):
                self.backend.key(c, False)
                time.sleep(0.008)

    def release_all(self):
        try:
            self.backend.release_all()
        except Exception:                            # noqa: BLE001
            pass
