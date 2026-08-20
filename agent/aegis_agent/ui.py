"""
Local presence UI: the consent prompt, the on-screen session banner and the
tray icon.

These are the parts that make the tool honest -- someone sitting at the
machine can always see that a session is live and can always end it. They are
driven from a single Tk-owning thread because tkinter is not thread-safe;
other threads post callables onto a queue that the Tk thread drains.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger("aegis.ui")

try:
    import tkinter as tk
    from tkinter import font as tkfont
    _HAVE_TK = True
except Exception:                                            # noqa: BLE001
    tk = None                                                # type: ignore
    _HAVE_TK = False

BG = "#11161d"
BG2 = "#1a212b"
WARN = "#d29922"
FG = "#e8eef7"
MUTED = "#8fa0b5"
ACCENT = "#2f81f7"
DANGER = "#d93a3a"
OK = "#2ea043"


class UIBase:
    available = False
    def start(self): ...
    def stop(self): ...
    def ask_consent(self, operator: str, timeout: int, default_allow: bool) -> bool:
        return default_allow
    def show_banner(self, sid: int, operator: str, on_disconnect: Callable[[], None]): ...
    def hide_banner(self, sid: int): ...
    def notify(self, title: str, message: str): ...
    def show_support(self, get_state, on_new_code, on_quit): ...


class HeadlessUI(UIBase):
    """No display: decisions fall back to the configured default and every
    event is written to the log instead of the screen."""
    available = False

    def ask_consent(self, operator, timeout, default_allow):
        log.warning("consent requested by %s but no desktop UI is available; "
                    "auto-%s per configuration", operator, "allowing" if default_allow else "denying")
        return default_allow

    def show_banner(self, sid, operator, on_disconnect):
        log.info("[session %s] %s connected (no banner: headless)", sid, operator)

    def hide_banner(self, sid):
        log.info("[session %s] disconnected", sid)

    def notify(self, title, message):
        log.info("notify: %s -- %s", title, message)

    def show_support(self, get_state, on_new_code, on_quit):
        """No desktop: print the numbers and keep printing status changes.
        Only reachable when running from a terminal -- the shipped executable
        is windowed and always has Tk."""
        import time as _t
        from . import support as _s
        last = None
        print()
        while True:
            st = get_state()
            if st.get("stop"):
                break
            line = (st.get("deviceId"), st.get("code"), st.get("connected"), st.get("operator"))
            if line != last:
                last = line
                print("=" * 56)
                print("  Read these two numbers to the person helping you:")
                print()
                print(f"     Your ID:       {_s.group_id(st.get('deviceId'))}")
                print(f"     Session code:  {_s.group_code(st.get('code'))}")
                print()
                if st.get("connected"):
                    print(f"  CONNECTED -- {st.get('operator')} can see this screen.")
                else:
                    print("  Waiting for a connection...  (Ctrl+C to stop)")
                print("=" * 56)
            _t.sleep(1)


class TkUI(UIBase):
    available = True

    def __init__(self):
        self._q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._root: Optional["tk.Tk"] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._banners = {}

    # ------------------------------------------------------------------ thread
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="aegis-ui")
        self._thread.start()
        if not self._ready.wait(timeout=8):
            log.warning("UI thread did not come up in time")

    def _run(self):
        try:
            self._root = tk.Tk()
            self._root.withdraw()
            self._root.title("AegisDesk")
            self._ready.set()
            self._pump()
            self._root.mainloop()
        except Exception as exc:                             # noqa: BLE001
            log.warning("UI thread failed (%s); falling back to headless behaviour", exc)
            self.available = False
            self._ready.set()

    def _pump(self):
        try:
            while True:
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception as exc:                     # noqa: BLE001
                    log.debug("UI task failed: %s", exc)
        except queue.Empty:
            pass
        if self._root and not self._stopping.is_set():
            self._root.after(60, self._pump)

    def _post(self, fn: Callable[[], None]):
        self._q.put(fn)

    def stop(self):
        self._stopping.set()
        if self._root:
            self._post(lambda: self._root.quit())

    # ------------------------------------------------------------------ consent
    def ask_consent(self, operator: str, timeout: int, default_allow: bool) -> bool:
        if not self.available or not self._root:
            return default_allow
        result = {"allow": None}
        done = threading.Event()

        def build():
            try:
                win = tk.Toplevel(self._root)
                win.title("Remote support request")
                win.configure(bg=BG)
                win.attributes("-topmost", True)
                win.resizable(False, False)
                try:
                    win.attributes("-alpha", 0.99)
                except Exception:                            # noqa: BLE001
                    pass
                w, h = 430, 220
                sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(40, (sh - h) // 3)}")

                title_font = tkfont.Font(family="Segoe UI", size=13, weight="bold")
                body_font = tkfont.Font(family="Segoe UI", size=10)
                small_font = tkfont.Font(family="Segoe UI", size=9)

                tk.Label(win, text="Someone wants to connect to this computer",
                         bg=BG, fg=FG, font=title_font, wraplength=390,
                         justify="left").pack(anchor="w", padx=22, pady=(20, 6))
                tk.Label(win, text=operator, bg=BG, fg=ACCENT, font=body_font,
                         justify="left").pack(anchor="w", padx=22)
                tk.Label(win, text="They will be able to see your screen and control this\n"
                                   "computer until you disconnect.",
                         bg=BG, fg=MUTED, font=small_font, justify="left"
                         ).pack(anchor="w", padx=22, pady=(8, 0))

                countdown = tk.Label(win, text="", bg=BG, fg=MUTED, font=small_font)
                countdown.pack(anchor="w", padx=22, pady=(8, 0))

                row = tk.Frame(win, bg=BG)
                row.pack(side="bottom", fill="x", padx=18, pady=16)

                def finish(allow: bool):
                    if result["allow"] is None:
                        result["allow"] = allow
                    done.set()
                    try:
                        win.destroy()
                    except Exception:                        # noqa: BLE001
                        pass

                tk.Button(row, text="Allow", command=lambda: finish(True),
                          bg=OK, fg="white", relief="flat", font=body_font,
                          activebackground="#3fbf5a", padx=22, pady=7, cursor="hand2"
                          ).pack(side="right", padx=(8, 0))
                tk.Button(row, text="Decline", command=lambda: finish(False),
                          bg="#2a323d", fg=FG, relief="flat", font=body_font,
                          activebackground="#3a4450", padx=18, pady=7, cursor="hand2"
                          ).pack(side="right")

                win.protocol("WM_DELETE_WINDOW", lambda: finish(False))
                deadline = time.time() + timeout

                def tick():
                    left = int(round(deadline - time.time()))
                    if left <= 0:
                        countdown.config(text="No answer -- "
                                              + ("allowing" if default_allow else "declining"))
                        finish(default_allow)
                        return
                    countdown.config(text=f"Declines automatically in {left}s if you do nothing.")
                    win.after(500, tick)

                tick()
                try:
                    win.lift()
                    win.focus_force()
                    win.bell()
                except Exception:                            # noqa: BLE001
                    pass
            except Exception as exc:                         # noqa: BLE001
                log.warning("consent dialog failed (%s)", exc)
                result["allow"] = default_allow
                done.set()

        self._post(build)
        if not done.wait(timeout=timeout + 8):
            log.warning("consent dialog timed out without an answer")
            return default_allow
        return bool(result["allow"])

    # ------------------------------------------------------------------ banner
    def show_banner(self, sid: int, operator: str, on_disconnect: Callable[[], None]):
        if not self.available or not self._root:
            return

        def build():
            try:
                win = tk.Toplevel(self._root)
                win.overrideredirect(True)
                win.attributes("-topmost", True)
                win.configure(bg="#1b2430", highlightbackground=ACCENT, highlightthickness=1)
                sw = win.winfo_screenwidth()
                w, h = 320, 40
                win.geometry(f"{w}x{h}+{sw - w - 18}+18")
                f = tkfont.Font(family="Segoe UI", size=9)
                dot = tk.Label(win, text="●", bg="#1b2430", fg=DANGER, font=("Segoe UI", 12))
                dot.pack(side="left", padx=(10, 4))
                tk.Label(win, text=f"Remote session — {operator}"[:38],
                         bg="#1b2430", fg=FG, font=f).pack(side="left")
                tk.Button(win, text="Disconnect", command=on_disconnect, bg=DANGER, fg="white",
                          relief="flat", font=f, padx=8, pady=2, cursor="hand2"
                          ).pack(side="right", padx=8)

                blink = {"on": True}

                def pulse():
                    if sid not in self._banners:
                        return
                    blink["on"] = not blink["on"]
                    try:
                        dot.config(fg=DANGER if blink["on"] else "#5a2222")
                        win.after(700, pulse)
                    except Exception:                        # noqa: BLE001
                        pass

                self._banners[sid] = win
                pulse()
            except Exception as exc:                         # noqa: BLE001
                log.debug("banner failed: %s", exc)

        self._post(build)

    def hide_banner(self, sid: int):
        def kill():
            win = self._banners.pop(sid, None)
            if win:
                try:
                    win.destroy()
                except Exception:                            # noqa: BLE001
                    pass
        self._post(kill)

    # ------------------------------------------------------------------ toast
    def notify(self, title: str, message: str):
        if not self.available or not self._root:
            log.info("notify: %s -- %s", title, message)
            return

        def build():
            try:
                win = tk.Toplevel(self._root)
                win.overrideredirect(True)
                win.attributes("-topmost", True)
                win.configure(bg="#1b2430", highlightbackground="#2f3b4a", highlightthickness=1)
                sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                w, h = 330, 74
                win.geometry(f"{w}x{h}+{sw - w - 18}+{sh - h - 70}")
                tk.Label(win, text=title, bg="#1b2430", fg=FG,
                         font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
                tk.Label(win, text=message, bg="#1b2430", fg=MUTED, font=("Segoe UI", 9),
                         wraplength=300, justify="left").pack(anchor="w", padx=12)
                win.after(5000, lambda: win.destroy())
            except Exception as exc:                         # noqa: BLE001
                log.debug("toast failed: %s", exc)

        self._post(build)


    # ------------------------------------------------------------------ support
    def show_support(self, get_state, on_new_code, on_quit):
        """The whole interface a helped person ever sees.

        Two big numbers to read aloud, a live status line, and a Quit button.
        No settings, no jargon, nothing to configure. It blocks until the
        window is closed, which is also what ends the session.
        """
        if not self.available or not self._root:
            return HeadlessUI().show_support(get_state, on_new_code, on_quit)

        from . import support as sup
        closed = threading.Event()
        widgets = {}

        def build():
            try:
                win = tk.Toplevel(self._root)
                win.title("Remote Support")
                win.configure(bg=BG)
                win.resizable(False, False)
                win.attributes("-topmost", True)
                w, h = 460, 430
                sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(30, (sh - h) // 3)}")

                f_title = tkfont.Font(family="Segoe UI", size=15, weight="bold")
                f_label = tkfont.Font(family="Segoe UI", size=9)
                f_big = tkfont.Font(family="Consolas", size=27, weight="bold")
                f_body = tkfont.Font(family="Segoe UI", size=10)
                f_small = tkfont.Font(family="Segoe UI", size=8)

                state = get_state()
                brand = state.get("brand") or "Remote Support"

                tk.Label(win, text=brand, bg=BG, fg=FG, font=f_title
                         ).pack(anchor="w", padx=26, pady=(22, 2))
                tk.Label(win, text="Read these two numbers to the person helping you.",
                         bg=BG, fg=MUTED, font=f_body, wraplength=400, justify="left"
                         ).pack(anchor="w", padx=26, pady=(0, 16))

                def big_field(caption):
                    box = tk.Frame(win, bg=BG2, highlightbackground="#2a3543", highlightthickness=1)
                    box.pack(fill="x", padx=26, pady=(0, 12))
                    tk.Label(box, text=caption, bg=BG2, fg=MUTED, font=f_label
                             ).pack(anchor="w", padx=16, pady=(10, 0))
                    val = tk.Label(box, text="...", bg=BG2, fg=FG, font=f_big)
                    val.pack(anchor="w", padx=14, pady=(0, 12))
                    return val

                widgets["id"] = big_field("YOUR ID")
                widgets["code"] = big_field("SESSION CODE")

                status_row = tk.Frame(win, bg=BG)
                status_row.pack(fill="x", padx=26, pady=(4, 0))
                widgets["dot"] = tk.Label(status_row, text="\u25cf", bg=BG, fg=MUTED,
                                          font=("Segoe UI", 13))
                widgets["dot"].pack(side="left", padx=(0, 8))
                widgets["status"] = tk.Label(status_row, text="Starting...", bg=BG, fg=MUTED,
                                             font=f_body, anchor="w", justify="left",
                                             wraplength=360)
                widgets["status"].pack(side="left")

                phone = state.get("supportPhone")
                if phone:
                    tk.Label(win, text=f"Need to reach us? {phone}", bg=BG, fg=MUTED,
                             font=f_small).pack(anchor="w", padx=26, pady=(12, 0))

                btn_row = tk.Frame(win, bg=BG)
                btn_row.pack(side="bottom", fill="x", padx=22, pady=18)

                def quit_now():
                    closed.set()
                    try:
                        on_quit()
                    except Exception:                            # noqa: BLE001
                        pass
                    try:
                        win.destroy()
                    except Exception:                            # noqa: BLE001
                        pass

                tk.Button(btn_row, text="Quit", command=quit_now, bg="#3a1717", fg="#ff9a9a",
                          relief="flat", font=f_body, padx=20, pady=7, cursor="hand2",
                          activebackground="#4c1c1c").pack(side="right")
                tk.Button(btn_row, text="New code", command=lambda: on_new_code(),
                          bg="#2a323d", fg=FG, relief="flat", font=f_body, padx=16, pady=7,
                          cursor="hand2", activebackground="#3a4450").pack(side="right", padx=(0, 8))

                def copy_both():
                    st = get_state()
                    text = f"ID {sup.group_id(st.get('deviceId'))}   code {sup.group_code(st.get('code'))}"
                    try:
                        win.clipboard_clear()
                        win.clipboard_append(text)
                    except Exception:                            # noqa: BLE001
                        pass

                tk.Button(btn_row, text="Copy", command=copy_both, bg="#2a323d", fg=FG,
                          relief="flat", font=f_body, padx=16, pady=7, cursor="hand2",
                          activebackground="#3a4450").pack(side="left")

                win.protocol("WM_DELETE_WINDOW", quit_now)
                widgets["win"] = win

                def refresh():
                    if closed.is_set():
                        return
                    try:
                        st = get_state()
                        widgets["id"].config(text=sup.group_id(st.get("deviceId")))
                        widgets["code"].config(text=sup.group_code(st.get("code")) or "...")
                        if st.get("connected"):
                            widgets["dot"].config(fg=DANGER)
                            widgets["status"].config(
                                text=f"Connected \u2014 {st.get('operator')} can see this screen.",
                                fg=FG)
                        elif st.get("online"):
                            widgets["dot"].config(fg=OK)
                            widgets["status"].config(text="Ready. Waiting for a connection.", fg=MUTED)
                        else:
                            widgets["dot"].config(fg=WARN)
                            widgets["status"].config(
                                text=st.get("error") or "Connecting to the support server...", fg=MUTED)
                        widgets["win"].after(700, refresh)
                    except Exception:                            # noqa: BLE001
                        pass

                refresh()
                try:
                    win.lift()
                    win.focus_force()
                except Exception:                                # noqa: BLE001
                    pass
            except Exception as exc:                             # noqa: BLE001
                log.error("support window failed: %s", exc)
                closed.set()

        self._post(build)
        closed.wait()


def open_ui(enabled: bool = True) -> UIBase:
    if not enabled or not _HAVE_TK:
        return HeadlessUI()
    import os
    if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return HeadlessUI()
    ui = TkUI()
    ui.start()
    if not ui.available:
        return HeadlessUI()
    return ui


# ====================================================================== tray

class TrayIcon:
    """Optional pystray icon. Right-click menu shows status and can quit the
    agent or copy the device ID."""

    def __init__(self, get_status: Callable[[], dict], on_quit: Callable[[], None],
                 on_copy_id: Optional[Callable[[], None]] = None):
        self.get_status = get_status
        self.on_quit = on_quit
        self.on_copy_id = on_copy_id
        self._icon = None
        self._thread = None

    def _image(self, active: bool):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ring = (47, 129, 247, 255) if not active else (217, 58, 58, 255)
        d.ellipse((4, 4, 60, 60), outline=ring, width=6)
        d.ellipse((22, 22, 42, 42), fill=ring)
        return img

    def start(self):
        try:
            import pystray                                   # type: ignore
            from pystray import Menu, MenuItem                # type: ignore
        except Exception as exc:                             # noqa: BLE001
            log.info("tray icon unavailable (%s)", exc)
            return

        def title():
            s = self.get_status()
            base = f"AegisDesk — ID {s.get('deviceId') or 'not enrolled'}"
            if s.get("sessions"):
                return base + f" — {s['sessions']} session(s) active"
            return base + (" — online" if s.get("connected") else " — offline")

        items = [MenuItem(lambda _i: title(), None, enabled=False)]
        if self.on_copy_id:
            items.append(MenuItem("Copy my ID", lambda: self.on_copy_id()))
        items += [Menu.SEPARATOR, MenuItem("Quit AegisDesk", lambda: self._quit())]

        self._icon = pystray.Icon("aegisdesk", self._image(False), title(), Menu(*items))

        def run():
            try:
                self._icon.run()
            except Exception as exc:                         # noqa: BLE001
                log.info("tray loop ended: %s", exc)

        self._thread = threading.Thread(target=run, daemon=True, name="aegis-tray")
        self._thread.start()

        def refresh():
            while self._icon:
                try:
                    s = self.get_status()
                    self._icon.title = title()
                    self._icon.icon = self._image(bool(s.get("sessions")))
                except Exception:                            # noqa: BLE001
                    pass
                time.sleep(3)

        threading.Thread(target=refresh, daemon=True, name="aegis-tray-refresh").start()

    def _quit(self):
        try:
            self.on_quit()
        finally:
            self.stop()

    def stop(self):
        icon, self._icon = self._icon, None
        if icon:
            try:
                icon.stop()
            except Exception:                                # noqa: BLE001
                pass
