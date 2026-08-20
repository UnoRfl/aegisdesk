"""
Remote shell.

Windows gets a pipe-backed cmd.exe or PowerShell (Windows has no pty, so
line-oriented tools work and full-screen TUIs do not). Unix gets a real pty,
so top/nano/htop behave.
"""
from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import threading
from typing import Callable, Optional

log = logging.getLogger("aegis.shell")
IS_WINDOWS = platform.system() == "Windows"

STREAM_STDOUT, STREAM_STDERR, STREAM_EXIT = 1, 2, 3
READ_SIZE = 32768


def default_shell() -> list:
    if IS_WINDOWS:
        pwsh = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            r"System32\WindowsPowerShell\v1.0\powershell.exe")
        if os.path.exists(pwsh):
            return [pwsh, "-NoLogo", "-NoProfile", "-NoExit", "-Command", "-"]
        return [os.environ.get("COMSPEC", "cmd.exe")]
    for cand in ("/bin/bash", "/bin/sh"):
        if os.path.exists(cand):
            return [cand, "-i"]
    return ["/bin/sh"]


class ShellSession:
    def __init__(self, on_output: Callable[[int, bytes], None], cwd: Optional[str] = None,
                 shell_cmd: Optional[list] = None, env_extra: Optional[dict] = None):
        self.on_output = on_output
        self.cmd = shell_cmd or default_shell()
        self.cwd = cwd or os.path.expanduser("~")
        self.proc: Optional[subprocess.Popen] = None
        self.master_fd = None
        self._threads = []
        self._alive = threading.Event()
        self._env = {**os.environ, "TERM": "xterm-256color", **(env_extra or {})}

    # ------------------------------------------------------------------ start
    def start(self, cols: int = 100, rows: int = 30):
        if self.proc:
            return
        if IS_WINDOWS:
            self._start_pipes()
        else:
            self._start_pty(cols, rows)
        self._alive.set()
        log.info("shell started: %s (pid %s)", self.cmd[0], self.proc.pid if self.proc else "?")

    def _start_pipes(self):
        creationflags = 0
        if IS_WINDOWS:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=self._env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, creationflags=creationflags)
        for stream, tag in ((self.proc.stdout, STREAM_STDOUT), (self.proc.stderr, STREAM_STDERR)):
            t = threading.Thread(target=self._reader_pipe, args=(stream, tag),
                                 daemon=True, name=f"aegis-shell-{tag}")
            t.start()
            self._threads.append(t)
        threading.Thread(target=self._waiter, daemon=True, name="aegis-shell-wait").start()

    def _start_pty(self, cols, rows):
        import pty
        import fcntl
        import struct
        import termios
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:                                     # noqa: BLE001
            pass
        self.proc = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=self._env,
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=os.setsid, close_fds=True)
        os.close(slave)
        self.master_fd = master
        t = threading.Thread(target=self._reader_pty, daemon=True, name="aegis-shell-pty")
        t.start()
        self._threads.append(t)
        threading.Thread(target=self._waiter, daemon=True, name="aegis-shell-wait").start()

    # ------------------------------------------------------------------ io
    def _reader_pipe(self, stream, tag):
        try:
            while True:
                data = stream.read(1)
                if not data:
                    break
                # opportunistically drain whatever else is buffered
                try:
                    import io
                    if isinstance(stream, io.BufferedReader):
                        extra = stream.read1(READ_SIZE - 1)
                        if extra:
                            data += extra
                except Exception:                            # noqa: BLE001
                    pass
                self.on_output(tag, data)
        except Exception as exc:                             # noqa: BLE001
            log.debug("shell reader ended: %s", exc)

    def _reader_pty(self):
        try:
            while True:
                data = os.read(self.master_fd, READ_SIZE)
                if not data:
                    break
                self.on_output(STREAM_STDOUT, data)
        except OSError:
            pass
        except Exception as exc:                             # noqa: BLE001
            log.debug("pty reader ended: %s", exc)

    def _waiter(self):
        try:
            code = self.proc.wait()
        except Exception:                                    # noqa: BLE001
            code = -1
        self._alive.clear()
        try:
            self.on_output(STREAM_EXIT, str(code).encode())
        except Exception:                                    # noqa: BLE001
            pass
        log.info("shell exited with code %s", code)

    def write(self, data: bytes):
        if not self.proc:
            return
        try:
            if self.master_fd is not None:
                os.write(self.master_fd, data)
            elif self.proc.stdin:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except Exception as exc:                             # noqa: BLE001
            log.debug("shell write failed: %s", exc)

    def resize(self, cols: int, rows: int):
        if self.master_fd is None:
            return
        try:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
        except Exception:                                    # noqa: BLE001
            pass

    @property
    def alive(self) -> bool:
        return self._alive.is_set() and self.proc is not None and self.proc.poll() is None

    def kill(self):
        p = self.proc
        if not p:
            return
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:                                    # noqa: BLE001
            try:
                p.kill()
            except Exception:                                # noqa: BLE001
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:                                # noqa: BLE001
                pass
            self.master_fd = None
        self._alive.clear()
