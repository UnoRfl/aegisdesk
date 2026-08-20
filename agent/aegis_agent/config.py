"""Agent configuration: where it lives, what it contains, how it is saved."""
from __future__ import annotations

import json
import logging
import os
import platform
import socket
import stat
import tempfile
from typing import Any, Dict, Optional

from . import crypto
from . import AGENT_VERSION

log = logging.getLogger("aegis.config")
IS_WINDOWS = platform.system() == "Windows"

DEFAULTS: Dict[str, Any] = {
    "relayUrl": "",
    "enrollKey": "",
    "deviceId": None,
    "deviceToken": None,
    "name": "",
    "group": "",

    # Access control
    "requireConsent": True,          # show an Accept/Decline prompt on the device
    "consentTimeoutSec": 45,
    "consentDefault": "deny",        # what happens when nobody answers: deny | allow
    "unattended": None,              # {kdf, iterations, salt, key} once a password is set
    "unattendedBypassesConsent": True,

    # What an authenticated operator is allowed to do
    "permissions": {
        "view": True,
        "input": True,
        "clipboard": True,
        "files": True,
        "filesReadOnly": False,
        "fileJail": None,            # e.g. "C:\\\\POS-Exports" to confine browsing
        "shell": True,
        "sysinfo": True,
        "processes": True,
        "lockWorkstation": True,
    },

    # Presence / transparency -- deliberately not switchable off from the network
    "showBanner": True,
    "showTray": True,
    "notifyOnConnect": True,
    "beepOnConnect": False,

    # Streaming
    "defaultQuality": "auto",
    "maxWidth": 1600,
    "maxFps": 24,
    "monitor": 1,

    # Housekeeping
    "logLevel": "info",
    "reconnectMinSec": 2,
    "reconnectMaxSec": 60,
    "heartbeatSec": 30,
    "sessionLog": True,
}


def config_dir() -> str:
    env = os.environ.get("AEGISDESK_DIR")
    if env:
        return env
    if IS_WINDOWS:
        base = os.environ.get("ProgramData") or os.path.expanduser("~")
        return os.path.join(base, "AegisDesk")
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Application Support/AegisDesk")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "aegisdesk")


def config_path() -> str:
    return os.path.join(config_dir(), "agent.json")


def log_path() -> str:
    return os.path.join(config_dir(), "agent.log")


def session_log_path() -> str:
    return os.path.join(config_dir(), "sessions.jsonl")


class Config:
    """Agent configuration.

    `in_memory=True` gives a config that never touches the disk: used by quick
    support sessions, which are meant to leave no trace on the helped person's
    computer -- no device identity, no stored code, nothing to clean up.
    """

    def __init__(self, path: Optional[str] = None, in_memory: bool = False):
        self.in_memory = in_memory
        self.path = path or config_path()
        self.data: Dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        if not in_memory:
            self.load()

    # ------------------------------------------------------------------ io
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:                             # noqa: BLE001
            log.warning("config %s is unreadable (%s); using defaults", self.path, exc)
            return
        for k, v in disk.items():
            if k == "permissions" and isinstance(v, dict):
                self.data["permissions"].update(v)
            else:
                self.data[k] = v

    def save(self):
        if self.in_memory:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".agent-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:                            # noqa: BLE001
                    pass
        self._tighten_permissions()

    def _tighten_permissions(self):
        """The config holds the device token and the password verifier, so it
        should not be world-readable."""
        try:
            if IS_WINDOWS:
                import subprocess
                user = os.environ.get("USERNAME", "")
                subprocess.run(["icacls", self.path, "/inheritance:r",
                                "/grant:r", "SYSTEM:F", "/grant:r", "Administrators:F"]
                               + ([f"/grant:r", f"{user}:F"] if user else []),
                               capture_output=True, check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception as exc:                             # noqa: BLE001
            log.debug("could not tighten config permissions: %s", exc)

    # ------------------------------------------------------------------ access
    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    def perm(self, name: str) -> bool:
        return bool(self.data.get("permissions", {}).get(name, False))

    @property
    def device_name(self) -> str:
        return self.data.get("name") or socket.gethostname()

    # ------------------------------------------------------------------ password
    @property
    def has_unattended_password(self) -> bool:
        u = self.data.get("unattended")
        return bool(u and u.get("key") and u.get("salt"))

    def set_unattended_password(self, password: Optional[str]):
        if not password:
            self.data["unattended"] = None
        else:
            if len(password) < 8:
                raise ValueError("unattended password must be at least 8 characters")
            self.data["unattended"] = crypto.hash_password(password)
        self.save()

    def set_support_code(self, code: str):
        """Install a one-time session code as the access credential.

        Shown on screen and read aloud, so it is shorter than a password a
        person would type. It is only ever held in memory, is valid only while
        the support window is open, and can only be attempted by someone who
        already holds an operator account on the relay.
        """
        self.data["unattended"] = crypto.hash_password(code)
        self.data["supportCode"] = code       # in-memory only, for the on-screen display
        if not self.in_memory:
            raise RuntimeError("a support code must never be written to disk")

    def unattended_key(self) -> Optional[bytes]:
        u = self.data.get("unattended")
        if not u:
            return None
        return bytes.fromhex(u["key"])

    def unattended_salt(self) -> Optional[bytes]:
        u = self.data.get("unattended")
        return bytes.fromhex(u["salt"]) if u else None

    def unattended_iterations(self) -> int:
        u = self.data.get("unattended") or {}
        return int(u.get("iterations", crypto.DEFAULT_PBKDF2_ITERATIONS))

    # ------------------------------------------------------------------ misc
    def capabilities(self) -> list:
        caps = ["screen"]
        p = self.data.get("permissions", {})
        for name, cap in (("input", "input"), ("clipboard", "clipboard"), ("files", "files"),
                          ("shell", "shell"), ("sysinfo", "sysinfo"), ("processes", "processes")):
            if p.get(name):
                caps.append(cap)
        caps.append("multimon")
        return caps

    def summary(self) -> Dict[str, Any]:
        return {
            "configPath": self.path,
            "relayUrl": self.data.get("relayUrl"),
            "deviceId": self.data.get("deviceId"),
            "name": self.device_name,
            "agentVersion": AGENT_VERSION,
            "unattended": self.has_unattended_password,
            "requireConsent": self.data.get("requireConsent"),
            "permissions": self.data.get("permissions"),
        }
