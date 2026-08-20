"""System inventory and lightweight metrics, so the fleet view can show
something useful without opening a screen session."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List

try:
    import psutil                                            # type: ignore
    _HAVE_PSUTIL = True
except Exception:                                            # noqa: BLE001
    psutil = None                                            # type: ignore
    _HAVE_PSUTIL = False

_BOOT = time.time()


def os_label() -> str:
    if platform.system() == "Windows":
        rel = platform.win32_ver()
        try:
            build = int(platform.version().split(".")[-1])
        except Exception:                                    # noqa: BLE001
            build = 0
        name = "Windows 11" if build >= 22000 else f"Windows {rel[0]}"
        edition = ""
        try:
            edition = platform.win32_edition() or ""
        except Exception:                                    # noqa: BLE001
            pass
        return f"{name} {edition} ({platform.version()})".replace("  ", " ").strip()
    if platform.system() == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    try:
        import distro                                        # type: ignore
        return f"{distro.name()} {distro.version()}"
    except Exception:                                        # noqa: BLE001
        return f"{platform.system()} {platform.release()}"


def metrics() -> Dict[str, Any]:
    out: Dict[str, Any] = {"uptime": int(time.time() - _BOOT)}
    if _HAVE_PSUTIL:
        try:
            out["cpu"] = round(psutil.cpu_percent(interval=None), 1)
            vm = psutil.virtual_memory()
            out["mem"] = round(vm.percent, 1)
            out["memTotalMb"] = round(vm.total / 1048576)
            du = psutil.disk_usage(os.path.abspath(os.sep))
            out["disk"] = round(du.percent, 1)
            out["diskFreeGb"] = round(du.free / 1073741824, 1)
            out["sysUptime"] = int(time.time() - psutil.boot_time())
            bat = getattr(psutil, "sensors_battery", lambda: None)()
            if bat:
                out["battery"] = round(bat.percent)
                out["charging"] = bool(bat.power_plugged)
        except Exception:                                    # noqa: BLE001
            pass
    else:
        try:
            total, used, free = shutil.disk_usage(os.path.abspath(os.sep))
            out["disk"] = round(used * 100 / total, 1)
            out["diskFreeGb"] = round(free / 1073741824, 1)
        except Exception:                                    # noqa: BLE001
            pass
        try:
            out["cpu"] = round(os.getloadavg()[0] * 100 / max(1, os.cpu_count() or 1), 1)
        except Exception:                                    # noqa: BLE001
            pass
    return out


def _local_ips() -> List[str]:
    ips = set()
    try:
        for fam, _, _, _, addr in socket.getaddrinfo(socket.gethostname(), None):
            if fam in (socket.AF_INET, socket.AF_INET6):
                ips.add(addr[0])
    except Exception:                                        # noqa: BLE001
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("1.1.1.1", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:                                        # noqa: BLE001
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def full_report(extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    rep: Dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": os_label(),
        "platform": platform.platform(),
        "arch": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
        "domain": os.environ.get("USERDOMAIN", ""),
        "cores": os.cpu_count(),
        "ips": _local_ips(),
        "agentPid": os.getpid(),
        "metrics": metrics(),
    }
    if _HAVE_PSUTIL:
        try:
            rep["disks"] = [
                {"device": p.device, "mount": p.mountpoint, "fs": p.fstype,
                 "totalGb": round(psutil.disk_usage(p.mountpoint).total / 1073741824, 1),
                 "freeGb": round(psutil.disk_usage(p.mountpoint).free / 1073741824, 1)}
                for p in psutil.disk_partitions(all=False)[:12]
                if not p.opts.startswith("cdrom")
            ]
        except Exception:                                    # noqa: BLE001
            pass
        try:
            rep["bootTime"] = int(psutil.boot_time())
        except Exception:                                    # noqa: BLE001
            pass
    if extra:
        rep.update(extra)
    return rep


def process_list(limit: int = 200) -> List[Dict[str, Any]]:
    if not _HAVE_PSUTIL:
        return []
    rows = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_info", "cpu_percent"]):
        try:
            info = p.info
            rows.append({
                "pid": info["pid"], "name": info["name"] or "?",
                "user": (info["username"] or "").split("\\")[-1],
                "memMb": round((info["memory_info"].rss if info["memory_info"] else 0) / 1048576, 1),
                "cpu": round(info["cpu_percent"] or 0.0, 1),
            })
        except Exception:                                    # noqa: BLE001
            continue
    rows.sort(key=lambda r: r["memMb"], reverse=True)
    return rows[:limit]


def kill_process(pid: int) -> Dict[str, Any]:
    if not _HAVE_PSUTIL:
        return {"ok": False, "error": "psutil not installed"}
    try:
        p = psutil.Process(int(pid))
        name = p.name()
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:                                    # noqa: BLE001
            p.kill()
        return {"ok": True, "pid": pid, "name": name}
    except Exception as exc:                                 # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def list_services(limit: int = 300) -> List[Dict[str, Any]]:
    """Windows services; empty list elsewhere."""
    if platform.system() != "Windows" or not _HAVE_PSUTIL:
        return []
    out = []
    try:
        for s in psutil.win_service_iter():
            try:
                d = s.as_dict()
                out.append({"name": d.get("name"), "display": d.get("display_name"),
                            "status": d.get("status"), "start": d.get("start_type")})
            except Exception:                                # noqa: BLE001
                continue
    except Exception:                                        # noqa: BLE001
        return []
    return out[:limit]
