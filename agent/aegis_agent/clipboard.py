"""Clipboard read/write. Windows uses the Win32 API directly; other
platforms try pyperclip, then xclip/xsel/pbpaste."""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess

log = logging.getLogger("aegis.clipboard")
IS_WINDOWS = platform.system() == "Windows"
MAX_CLIP = 1024 * 1024      # 1 MiB -- clipboards larger than this are almost always a mistake


class Clipboard:
    def __init__(self):
        self.backend = "none"
        self._win = None
        if IS_WINDOWS:
            try:
                import ctypes
                self._win = ctypes
                self.backend = "win32"
                return
            except Exception:                                # noqa: BLE001
                pass
        try:
            import pyperclip                                 # type: ignore
            pyperclip.paste()
            self._pyperclip = pyperclip
            self.backend = "pyperclip"
            return
        except Exception:                                    # noqa: BLE001
            pass
        for tool in ("xclip", "xsel", "pbpaste", "wl-paste"):
            if shutil.which(tool):
                self.backend = tool
                return

    # ---------------------------------------------------------------- read
    def get(self) -> str:
        try:
            if self.backend == "win32":
                return self._win_get()
            if self.backend == "pyperclip":
                return str(self._pyperclip.paste() or "")[:MAX_CLIP]
            if self.backend == "xclip":
                return self._run(["xclip", "-selection", "clipboard", "-o"])
            if self.backend == "xsel":
                return self._run(["xsel", "--clipboard", "--output"])
            if self.backend == "pbpaste":
                return self._run(["pbpaste"])
            if self.backend == "wl-paste":
                return self._run(["wl-paste", "--no-newline"])
        except Exception as exc:                             # noqa: BLE001
            log.debug("clipboard read failed: %s", exc)
        return ""

    # ---------------------------------------------------------------- write
    def set(self, text: str) -> bool:
        text = str(text)[:MAX_CLIP]
        try:
            if self.backend == "win32":
                return self._win_set(text)
            if self.backend == "pyperclip":
                self._pyperclip.copy(text)
                return True
            cmds = {"xclip": ["xclip", "-selection", "clipboard"],
                    "xsel": ["xsel", "--clipboard", "--input"],
                    "pbpaste": ["pbcopy"],
                    "wl-paste": ["wl-copy"]}
            cmd = cmds.get(self.backend)
            if cmd:
                subprocess.run(cmd, input=text.encode("utf-8"), timeout=5, check=False)
                return True
        except Exception as exc:                             # noqa: BLE001
            log.debug("clipboard write failed: %s", exc)
        return False

    @staticmethod
    def _run(cmd) -> str:
        out = subprocess.run(cmd, capture_output=True, timeout=5, check=False)
        return out.stdout.decode("utf-8", "replace")[:MAX_CLIP]

    # ---------------------------------------------------------------- win32
    def _win_get(self) -> str:
        ctypes = self._win
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        if not u32.OpenClipboard(0):
            return ""
        try:
            if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            handle = u32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = k32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                k32.GlobalUnlock(handle)
        finally:
            u32.CloseClipboard()

    def _win_set(self, text: str) -> bool:
        ctypes = self._win
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        if not u32.OpenClipboard(0):
            return False
        try:
            u32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            ptr = k32.GlobalLock(handle)
            ctypes.memmove(ptr, buf, size)
            k32.GlobalUnlock(handle)
            return bool(u32.SetClipboardData(CF_UNICODETEXT, handle))
        finally:
            u32.CloseClipboard()
