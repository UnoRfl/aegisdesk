"""
File browsing and two-way transfer.

Transfers are chunked (128 KiB) with a sliding window of 32 unacknowledged
chunks, so a big upload can't outrun the socket and blow up memory on either
end. Paths are normalised and, when a jail is configured, confined to it.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import protocol as P

log = logging.getLogger("aegis.files")
IS_WINDOWS = platform.system() == "Windows"
SEP = "\\" if IS_WINDOWS else "/"
MAX_LIST = 4000


def _drives() -> List[str]:
    if not IS_WINDOWS:
        return ["/"]
    out = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                out.append(f"{letter}:\\")
    except Exception:                                        # noqa: BLE001
        out = ["C:\\"]
    return out


def _special_dirs() -> List[dict]:
    home = os.path.expanduser("~")
    cands = [("Desktop", os.path.join(home, "Desktop")),
             ("Documents", os.path.join(home, "Documents")),
             ("Downloads", os.path.join(home, "Downloads")),
             ("Home", home)]
    if IS_WINDOWS:
        cands += [("ProgramData", os.environ.get("ProgramData", r"C:\ProgramData")),
                  ("Program Files", os.environ.get("ProgramFiles", r"C:\Program Files")),
                  ("Temp", os.environ.get("TEMP", r"C:\Windows\Temp")),
                  ("Windows", os.environ.get("SystemRoot", r"C:\Windows"))]
    else:
        cands += [("/etc", "/etc"), ("/var/log", "/var/log"), ("/tmp", "/tmp")]
    seen, out = set(), []
    for label, path in cands:
        if path and os.path.isdir(path) and path not in seen:
            seen.add(path)
            out.append({"label": label, "path": path})
    return out


@dataclass
class Transfer:
    xfer_id: int
    path: str
    direction: str                  # "down" (agent->viewer) or "up" (viewer->agent)
    size: int = 0
    seq: int = 0
    acked: int = 0
    handle: object = None
    cancelled: bool = False
    started: float = field(default_factory=time.time)
    written: int = 0
    expect_seq: int = 1
    pending: Dict[int, bytes] = field(default_factory=dict)


class FileService:
    """
    send_ctl(obj)        -> emit a FILE_CTL JSON message
    send_data(xid, seq, chunk) -> emit a FILE_DATA binary message
    """

    def __init__(self, send_ctl: Callable[[dict], None],
                 send_data: Callable[[int, int, bytes], None],
                 jail: Optional[str] = None, read_only: bool = False):
        self.send_ctl = send_ctl
        self.send_data = send_data
        self.jail = os.path.abspath(jail) if jail else None
        self.read_only = read_only
        self.transfers: Dict[int, Transfer] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------ path safety
    def _resolve(self, path: str) -> str:
        p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path or "."))))
        if self.jail:
            real = os.path.realpath(p)
            if real != self.jail and not real.startswith(self.jail + os.sep):
                raise PermissionError(f"path is outside the allowed folder ({self.jail})")
        return p

    def _guard_write(self):
        if self.read_only:
            raise PermissionError("this agent is configured read-only for file transfer")

    # ------------------------------------------------------------ dispatch
    def handle(self, msg: dict):
        op = msg.get("op")
        try:
            fn = getattr(self, f"_op_{str(op).replace('-', '_')}", None)
            if fn is None:
                self.send_ctl({"op": "error", "message": f"unknown file op {op!r}"})
                return
            fn(msg)
        except Exception as exc:                             # noqa: BLE001
            log.info("file op %s failed: %s", op, exc)
            self.send_ctl({"op": "error", "xferId": msg.get("xferId"),
                           "path": msg.get("path"), "message": str(exc)})

    # ------------------------------------------------------------ browsing
    def _op_roots(self, _msg):
        self.send_ctl({"op": "roots-result", "sep": SEP, "drives": _drives(),
                       "special": _special_dirs(), "home": os.path.expanduser("~"),
                       "cwd": os.getcwd(), "readOnly": self.read_only,
                       "jail": self.jail})

    def _op_list(self, msg):
        path = self._resolve(msg.get("path") or os.path.expanduser("~"))
        entries = []
        with os.scandir(path) as it:
            for i, e in enumerate(it):
                if i >= MAX_LIST:
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                    entries.append({
                        "n": e.name, "d": e.is_dir(follow_symlinks=False),
                        "s": 0 if e.is_dir(follow_symlinks=False) else st.st_size,
                        "m": int(st.st_mtime),
                        "l": stat.S_ISLNK(st.st_mode),
                        "h": e.name.startswith(".") or bool(getattr(st, "st_file_attributes", 0) & 0x2),
                    })
                except (OSError, PermissionError):
                    entries.append({"n": e.name, "d": False, "s": 0, "m": 0, "err": True})
        entries.sort(key=lambda x: (not x["d"], x["n"].lower()))
        parent = os.path.dirname(path.rstrip(os.sep)) or None
        self.send_ctl({"op": "list-result", "path": path, "parent": parent,
                       "sep": SEP, "entries": entries, "truncated": len(entries) >= MAX_LIST})

    def _op_mkdir(self, msg):
        self._guard_write()
        path = self._resolve(msg["path"])
        os.makedirs(path, exist_ok=True)
        self.send_ctl({"op": "ok", "action": "mkdir", "path": path})

    def _op_delete(self, msg):
        self._guard_write()
        path = self._resolve(msg["path"])
        if os.path.isdir(path) and not os.path.islink(path):
            if msg.get("recursive"):
                shutil.rmtree(path)
            else:
                os.rmdir(path)
        else:
            os.remove(path)
        self.send_ctl({"op": "ok", "action": "delete", "path": path})

    def _op_rename(self, msg):
        self._guard_write()
        src = self._resolve(msg["path"])
        dst = self._resolve(msg["to"])
        os.replace(src, dst)
        self.send_ctl({"op": "ok", "action": "rename", "path": src, "to": dst})

    # ------------------------------------------------------------ download
    def _op_get(self, msg):
        xid = int(msg["xferId"])
        path = self._resolve(msg["path"])
        size = os.path.getsize(path)
        fh = open(path, "rb")
        t = Transfer(xid, path, "down", size=size, handle=fh)
        with self._lock:
            self.transfers[xid] = t
        self.send_ctl({"op": "get-begin", "xferId": xid, "size": size,
                       "name": os.path.basename(path), "chunk": P.FILE_CHUNK_SIZE})
        threading.Thread(target=self._pump_down, args=(t,), daemon=True,
                         name=f"aegis-send-{xid}").start()

    def _pump_down(self, t: Transfer):
        try:
            while not t.cancelled and not self._stop.is_set():
                if t.seq - t.acked >= P.FILE_WINDOW:
                    time.sleep(0.004)
                    continue
                chunk = t.handle.read(P.FILE_CHUNK_SIZE)
                if not chunk:
                    break
                t.seq += 1
                self.send_data(t.xfer_id, t.seq, chunk)
            if not t.cancelled:
                self.send_ctl({"op": "done", "xferId": t.xfer_id, "chunks": t.seq,
                               "elapsedMs": int((time.time() - t.started) * 1000)})
        except Exception as exc:                             # noqa: BLE001
            self.send_ctl({"op": "error", "xferId": t.xfer_id, "message": str(exc)})
        finally:
            self._finish(t.xfer_id)

    def _op_ack(self, msg):
        t = self.transfers.get(int(msg.get("xferId", -1)))
        if t:
            t.acked = max(t.acked, int(msg.get("seq", 0)))

    # ------------------------------------------------------------ upload
    def _op_put(self, msg):
        self._guard_write()
        xid = int(msg["xferId"])
        path = self._resolve(msg["path"])
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fh = open(path + ".aegispart", "wb")
        t = Transfer(xid, path, "up", size=int(msg.get("size", 0)), handle=fh)
        with self._lock:
            self.transfers[xid] = t
        self.send_ctl({"op": "put-ready", "xferId": xid, "chunk": P.FILE_CHUNK_SIZE,
                       "window": P.FILE_WINDOW})

    def on_data(self, xfer_id: int, seq: int, chunk: bytes):
        """Append chunks strictly in sequence.

        A chunk that arrives early is held until its predecessors land. The
        transport is ordered, but a client that encrypts frames concurrently
        can emit them out of order, and appending blindly would corrupt the
        file with no error anywhere. This is cheap insurance.
        """
        t = self.transfers.get(xfer_id)
        if not t or t.direction != "up" or t.cancelled:
            return
        if seq < t.expect_seq:
            log.debug("ignoring duplicate chunk %d for transfer %d", seq, xfer_id)
            return
        try:
            if seq > t.expect_seq:
                if len(t.pending) >= P.FILE_WINDOW * 2:
                    raise IOError(
                        f"chunk {t.expect_seq} never arrived; {len(t.pending)} later "
                        f"chunks are buffered, giving up")
                t.pending[seq] = chunk
                return

            t.handle.write(chunk)
            t.written += len(chunk)
            t.expect_seq += 1
            while t.expect_seq in t.pending:
                buffered = t.pending.pop(t.expect_seq)
                t.handle.write(buffered)
                t.written += len(buffered)
                t.expect_seq += 1
            t.seq = t.expect_seq - 1

            if t.seq % (P.FILE_WINDOW // 2) == 0:
                self.send_ctl({"op": "ack", "xferId": xfer_id, "seq": t.seq})
        except Exception as exc:                             # noqa: BLE001
            self.send_ctl({"op": "error", "xferId": xfer_id, "message": str(exc)})
            self._finish(xfer_id, keep_partial=False)

    def _op_done(self, msg):
        """Viewer finished sending an upload."""
        xid = int(msg["xferId"])
        t = self.transfers.get(xid)
        if not t or t.direction != "up":
            return
        if t.pending:
            missing = t.expect_seq
            self.send_ctl({"op": "error", "xferId": xid,
                           "message": f"upload incomplete: chunk {missing} never arrived"})
            self._finish(xid, keep_partial=False)
            return
        try:
            t.handle.flush()
            os.fsync(t.handle.fileno())
        except Exception:                                    # noqa: BLE001
            pass
        try:
            t.handle.close()
        except Exception:                                    # noqa: BLE001
            pass
        t.handle = None
        try:
            os.replace(t.path + ".aegispart", t.path)
            self.send_ctl({"op": "put-complete", "xferId": xid, "path": t.path,
                           "bytes": t.written})
            log.info("received %s (%.1f KB)", t.path, t.written / 1024)
        except Exception as exc:                             # noqa: BLE001
            self.send_ctl({"op": "error", "xferId": xid, "message": str(exc)})
        finally:
            self._finish(xid)

    def _op_cancel(self, msg):
        xid = int(msg.get("xferId", -1))
        t = self.transfers.get(xid)
        if t:
            t.cancelled = True
            self._finish(xid, keep_partial=False)
            self.send_ctl({"op": "cancelled", "xferId": xid})

    # ------------------------------------------------------------ teardown
    def _finish(self, xfer_id: int, keep_partial: bool = True):
        with self._lock:
            t = self.transfers.pop(xfer_id, None)
        if not t:
            return
        if t.handle:
            try:
                t.handle.close()
            except Exception:                                # noqa: BLE001
                pass
        if t.direction == "up" and not keep_partial:
            try:
                os.remove(t.path + ".aegispart")
            except Exception:                                # noqa: BLE001
                pass

    def shutdown(self):
        self._stop.set()
        for xid in list(self.transfers):
            self._finish(xid, keep_partial=False)
