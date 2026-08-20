"""
Quick support sessions.

The permanent agent is right for a POS terminal you own. It is the wrong shape
for "the manager at the other restaurant can't print and I need to look at her
screen right now" -- that person is not going to run PowerShell, paste an
enrollment key, or set an access password.

So: one file, no install, no admin rights, nothing to configure. They
double-click it, read two numbers off the screen, and close it when you are
done. Nothing is written to their disk and the device disappears from the
fleet list the moment the window closes.
"""
from __future__ import annotations

import os
import secrets
import socket
from typing import Optional

CODE_DIGITS = 8


def new_code() -> str:
    """A one-time session code. Eight digits: long enough that guessing it is
    not a strategy, short enough to read down a phone line without mistakes."""
    return "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))


def group_code(code: str) -> str:
    """1234 5678 -- chunked because people read chunks, not strings."""
    code = str(code)
    return f"{code[:4]} {code[4:]}" if len(code) == 8 else code


def group_id(device_id: Optional[str]) -> str:
    """123 456 789"""
    d = str(device_id or "")
    if len(d) == 9:
        return f"{d[:3]} {d[3:6]} {d[6:]}"
    return d or "connecting..."


def default_session_name() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    host = socket.gethostname()
    return f"{user} on {host}".strip() if user else host


# --------------------------------------------------------------------------
# Build-time configuration.
#
# build-support-exe.ps1 writes aegis_agent/_baked.py so the shipped
# executable already knows which relay to call and needs no arguments. Without
# it (running from source) the values come from the command line.
# --------------------------------------------------------------------------

def baked() -> dict:
    try:
        from . import _baked                                  # type: ignore
    except Exception:                                         # noqa: BLE001
        return {}
    return {
        "relayUrl": getattr(_baked, "RELAY_URL", "") or "",
        "enrollKey": getattr(_baked, "ENROLL_KEY", "") or "",
        "brand": getattr(_baked, "BRAND", "") or "",
        "supportPhone": getattr(_baked, "SUPPORT_PHONE", "") or "",
    }
