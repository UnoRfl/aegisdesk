"""Make the agent come back after a reboot.

On Windows this registers a *scheduled task that runs at logon*, not a
service. That is deliberate: a Windows service runs in session 0 and cannot
see or capture the interactive desktop, so a service-based agent would show
you a black screen. The trade-off is documented in the README -- the agent
starts when someone logs in, and reconnects on its own after that.
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import textwrap

log = logging.getLogger("aegis.autostart")
IS_WINDOWS = platform.system() == "Windows"
TASK_NAME = "AegisDeskAgent"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" run'
    pyw = sys.executable
    if IS_WINDOWS:
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(cand):
            pyw = cand                     # pythonw = no console window
    return f'"{pyw}" -m aegis_agent run'


def install() -> tuple[bool, str]:
    if IS_WINDOWS:
        return _install_windows()
    if platform.system() == "Linux":
        return _install_systemd()
    if platform.system() == "Darwin":
        return _install_launchd()
    return False, "unsupported platform"


def uninstall() -> tuple[bool, str]:
    if IS_WINDOWS:
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    if platform.system() == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", "aegisdesk-agent"],
                       capture_output=True, check=False)
        path = os.path.expanduser("~/.config/systemd/user/aegisdesk-agent.service")
        if os.path.exists(path):
            os.remove(path)
        return True, "systemd user unit removed"
    if platform.system() == "Darwin":
        path = os.path.expanduser("~/Library/LaunchAgents/com.aegisdesk.agent.plist")
        subprocess.run(["launchctl", "unload", path], capture_output=True, check=False)
        if os.path.exists(path):
            os.remove(path)
        return True, "launch agent removed"
    return False, "unsupported platform"


def _install_windows() -> tuple[bool, str]:
    cmd = _launch_command()
    # /RL LIMITED keeps it in the user's own token; use --elevated for HIGHEST
    args = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON",
            "/TR", cmd, "/F", "/RL", "LIMITED"]
    r = subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    # also restart it if it ever dies
    subprocess.run(["schtasks", "/Change", "/TN", TASK_NAME, "/RI", "5", "/DU", "9999:59"],
                   capture_output=True, check=False, creationflags=NO_WINDOW)
    return True, f'scheduled task "{TASK_NAME}" created (runs at logon)'


def _install_windows_elevated() -> tuple[bool, str]:
    cmd = _launch_command()
    args = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON",
            "/TR", cmd, "/F", "/RL", "HIGHEST"]
    r = subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)
    return r.returncode == 0, (r.stderr or r.stdout).strip() or "created (elevated)"


def _install_systemd() -> tuple[bool, str]:
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    path = os.path.join(unit_dir, "aegisdesk-agent.service")
    exe = sys.executable
    unit = textwrap.dedent(f"""\
        [Unit]
        Description=AegisDesk remote support agent
        After=graphical-session.target

        [Service]
        Type=simple
        ExecStart={exe} -m aegis_agent run
        Restart=always
        RestartSec=5
        Environment=PYTHONUNBUFFERED=1

        [Install]
        WantedBy=default.target
        """)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "aegisdesk-agent"],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or f"unit written to {path}").strip()


def _install_launchd() -> tuple[bool, str]:
    d = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "com.aegisdesk.agent.plist")
    plist = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict>
          <key>Label</key><string>com.aegisdesk.agent</string>
          <key>ProgramArguments</key>
          <array><string>{sys.executable}</string><string>-m</string>
                 <string>aegis_agent</string><string>run</string></array>
          <key>RunAtLoad</key><true/>
          <key>KeepAlive</key><true/>
        </dict></plist>
        """)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(plist)
    r = subprocess.run(["launchctl", "load", path], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or f"launch agent written to {path}").strip()


def is_installed() -> bool:
    if IS_WINDOWS:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, creationflags=NO_WINDOW)
        return r.returncode == 0
    if platform.system() == "Linux":
        return os.path.exists(os.path.expanduser("~/.config/systemd/user/aegisdesk-agent.service"))
    if platform.system() == "Darwin":
        return os.path.exists(os.path.expanduser("~/Library/LaunchAgents/com.aegisdesk.agent.plist"))
    return False
