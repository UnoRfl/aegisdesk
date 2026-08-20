"""
Relay connection: register, keep alive, broker sessions, reconnect forever.

The receive callback must never block -- a consent dialog can sit on screen
for 45 seconds -- so every session request is handled on its own thread.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import ssl
import threading
import time
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse

import websocket                                             # websocket-client

from . import AGENT_VERSION, crypto, protocol as P, sysinfo
from .session import RemoteSession, b64e
from .ui import HeadlessUI

log = logging.getLogger("aegis.client")


def normalise_relay_url(raw: str) -> str:
    """Accept anything a human might paste and return a ws(s):// agent URL."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("relay URL is empty")
    if "://" not in raw:
        raw = "wss://" + raw
    u = urlparse(raw)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(u.scheme, "wss")
    path = u.path.rstrip("/")
    if not path.endswith("/ws/agent"):
        path = path + "/ws/agent"
    return urlunparse((scheme, u.netloc, path, "", "", ""))


class AgentClient:
    def __init__(self, cfg, ui=None, prefer_null_input: bool = False,
                 insecure_tls: bool = False, ephemeral: bool = False):
        self.cfg = cfg
        self.ui = ui or HeadlessUI()
        self.prefer_null_input = prefer_null_input
        self.insecure_tls = insecure_tls
        self.ephemeral = ephemeral          # quick support: leaves no fleet entry
        self.url = normalise_relay_url(cfg.get("relayUrl", ""))

        self.ws: Optional[websocket.WebSocketApp] = None
        self.sessions: Dict[int, RemoteSession] = {}
        self.connected = threading.Event()
        self.stopping = threading.Event()
        self.registered = threading.Event()
        self._send_lock = threading.Lock()
        self._backoff = float(cfg.get("reconnectMinSec", 2))
        self.last_error = ""
        self.relay_version = ""

    # ================================================================ status

    def status(self) -> dict:
        return {"deviceId": self.cfg.get("deviceId"), "name": self.cfg.device_name,
                "connected": self.connected.is_set(), "sessions": len(self.sessions),
                "relay": self.url, "relayVersion": self.relay_version,
                "lastError": self.last_error, "version": AGENT_VERSION}

    def support_state(self) -> dict:
        """What the quick-support window renders."""
        live = [s for s in self.sessions.values() if s.authed and not s.closed.is_set()]
        baked = {}
        try:
            from . import support as _sup
            baked = _sup.baked()
        except Exception:                                    # noqa: BLE001
            pass
        return {
            "deviceId": self.cfg.get("deviceId"),
            "code": self.cfg.get("supportCode"),
            "online": self.registered.is_set(),
            "connected": bool(live),
            "operator": live[0].operator if live else None,
            "error": self._friendly_error(),
            "brand": baked.get("brand") or "Remote Support",
            "supportPhone": baked.get("supportPhone"),
            "stop": self.stopping.is_set(),
        }

    def _friendly_error(self) -> Optional[str]:
        """Turn socket noise into something a non-technical person can act on."""
        e = (self.last_error or "").lower()
        if not e:
            return None
        if "bad_enroll_key" in e:
            return "This support tool is out of date. Ask for a new copy."
        if "getaddrinfo" in e or "name or service not known" in e or "nodename" in e:
            return "Cannot find the support server. Check your internet connection."
        if "refused" in e or "timed out" in e or "timeout" in e:
            return "Cannot reach the support server. Check your internet connection."
        if "certificate" in e or "ssl" in e:
            return "Secure connection failed. Ask your helper to check the server."
        return "Trying to reach the support server..."

    def new_support_code(self) -> str:
        from . import support as _sup
        code = _sup.new_code()
        self.cfg.set_support_code(code)
        log.info("issued a new session code")
        return code

    # ================================================================ sending

    def send_json(self, obj) -> bool:
        ws = self.ws
        if not ws or not self.connected.is_set():
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(obj, separators=(",", ":")))
            return True
        except Exception as exc:                             # noqa: BLE001
            log.debug("control send failed: %s", exc)
            return False

    def send_binary(self, data: bytes) -> bool:
        ws = self.ws
        if not ws or not self.connected.is_set():
            return False
        try:
            with self._send_lock:
                ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
            return True
        except Exception as exc:                             # noqa: BLE001
            log.debug("data send failed: %s", exc)
            return False

    # ================================================================ run loop

    def run_forever(self):
        while not self.stopping.is_set():
            started = time.time()
            self._connect_once()
            if self.stopping.is_set():
                break
            # a connection that lasted a while means the relay is healthy;
            # reset the backoff so a transient blip reconnects fast
            if time.time() - started > 60:
                self._backoff = float(self.cfg.get("reconnectMinSec", 2))
            delay = min(float(self.cfg.get("reconnectMaxSec", 60)),
                        self._backoff * (1.6 + random.random() * 0.4))
            self._backoff = delay
            log.info("reconnecting in %.0fs", delay)
            self.stopping.wait(delay)

    def _connect_once(self):
        log.info("connecting to %s", self.url)
        # on_data (not on_message) so the WebSocket opcode is explicit: with
        # skip_utf8_validation the library hands text frames back as raw bytes,
        # which is indistinguishable from a binary data-plane frame otherwise.
        self.ws = websocket.WebSocketApp(
            self.url,
            on_open=self._on_open, on_data=self._on_data,
            on_error=self._on_error, on_close=self._on_close,
            header={"User-Agent": f"AegisDesk-Agent/{AGENT_VERSION}"})
        sslopt = {"cert_reqs": ssl.CERT_NONE} if self.insecure_tls else None
        if self.insecure_tls:
            log.warning("TLS certificate verification is DISABLED for this connection")
        try:
            self.ws.run_forever(ping_interval=0, sslopt=sslopt,
                                skip_utf8_validation=True,
                                reconnect=0)
        except Exception as exc:                             # noqa: BLE001
            self.last_error = str(exc)
            log.warning("connection ended: %s", exc)

    def stop(self):
        self.stopping.set()
        for sid in list(self.sessions):
            self._close_session(sid, "agent_shutdown")
        ws = self.ws
        if ws:
            try:
                ws.close()
            except Exception:                                # noqa: BLE001
                pass

    # ================================================================ callbacks

    def _on_open(self, _ws):
        self.connected.set()
        self.last_error = ""
        log.info("connected; registering")
        report = sysinfo.full_report()
        self.send_json({
            "t": "register",
            "deviceId": self.cfg.get("deviceId"),
            "deviceToken": self.cfg.get("deviceToken"),
            "enrollKey": self.cfg.get("enrollKey") or "",
            "name": self.cfg.device_name,
            "os": report["os"], "arch": report["arch"],
            "agentVersion": AGENT_VERSION,
            "unattended": self.cfg.has_unattended_password,
            "caps": self.cfg.capabilities(),
            "ephemeral": self.ephemeral,
        })
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="aegis-hb").start()

    def _on_close(self, _ws, code, reason):
        self.connected.clear()
        self.registered.clear()
        log.info("disconnected (%s %s)", code, reason)
        for sid in list(self.sessions):
            self._close_session(sid, "relay_disconnected")

    def _on_error(self, _ws, err):
        self.last_error = str(err)
        log.warning("socket error: %s", err)

    def _on_data(self, _ws, data, opcode, _cont):
        if opcode == websocket.ABNF.OPCODE_BINARY:
            self._on_data_frame(bytes(data))
        elif opcode == websocket.ABNF.OPCODE_TEXT:
            if isinstance(data, (bytes, bytearray)):
                data = bytes(data).decode("utf-8", "replace")
            self._on_control(data)

    def _on_control(self, message: str):
        try:
            msg = json.loads(message)
        except Exception:                                    # noqa: BLE001
            log.debug("unparseable control frame: %r", message[:120])
            return
        t = msg.get("t")

        if t == "registered":
            first_time = not self.cfg.get("deviceId")
            self.cfg["deviceId"] = msg["deviceId"]
            self.cfg["deviceToken"] = msg["deviceToken"]
            self.relay_version = msg.get("relayVersion", "")
            if self.cfg.get("enrollKey"):
                self.cfg["enrollKey"] = ""       # one-time use; don't keep it on disk
            self.cfg.save()
            self.registered.set()
            self._backoff = float(self.cfg.get("reconnectMinSec", 2))
            banner = "ENROLLED" if first_time else "online"
            log.info("%s -- device ID %s (relay v%s)", banner, msg["deviceId"], self.relay_version)
            if first_time and not self.ephemeral:
                print(f"\n  This computer's AegisDesk ID is:  {msg['deviceId']}\n")
                self.ui.notify("AegisDesk ready", f"This computer's ID is {msg['deviceId']}.")

        elif t == "session-request":
            threading.Thread(target=self._handle_session_request, args=(msg,),
                             daemon=True, name="aegis-consent").start()

        elif t == "session-close":
            self._close_session(int(msg.get("sid", 0)), msg.get("reason", "relay"))

        elif t == "ping":
            self.send_json({"t": "pong", "ts": msg.get("ts")})

        elif t == "error":
            self.last_error = f"{msg.get('code')}: {msg.get('message')}"
            log.error("relay says: %s", self.last_error)
            if msg.get("code") in ("bad_enroll_key", "bad_device_token", "deenrolled"):
                log.error("this is not recoverable by retrying -- fix the config and restart")

    def _on_data_frame(self, buf: bytes):
        try:
            sid, sealed = P.unpack_outer(buf)
        except P.ProtocolError:
            return
        s = self.sessions.get(sid)
        if s:
            s.handle_frame(sealed)

    # ================================================================ sessions

    def _handle_session_request(self, msg: dict):
        sid = int(msg["sid"])
        operator = str(msg.get("operatorLabel") or msg.get("operator") or "unknown")
        log.info("[%s] session requested by %s", sid, operator)

        needs_password = self.cfg.has_unattended_password
        require_consent = bool(self.cfg.get("requireConsent", True))
        if needs_password and self.cfg.get("unattendedBypassesConsent", True):
            require_consent = False

        if require_consent:
            allowed = self.ui.ask_consent(
                operator, int(self.cfg.get("consentTimeoutSec", 45)),
                self.cfg.get("consentDefault", "deny") == "allow")
            if not allowed:
                log.info("[%s] declined at the device", sid)
                self.send_json({"t": "session-reject", "sid": sid, "reason": "declined_by_user"})
                return

        try:
            session = RemoteSession(
                sid=sid, viewer_pub_b64=msg["pub"], operator=operator, cfg=self.cfg,
                send_binary=self.send_binary, on_closed=self._on_session_closed,
                ui=self.ui, prefer_null_input=self.prefer_null_input)
        except Exception as exc:                             # noqa: BLE001
            log.error("[%s] could not set up session: %s", sid, exc)
            self.send_json({"t": "session-reject", "sid": sid, "reason": "agent_error"})
            return

        self.sessions[sid] = session
        accept = {"t": "session-accept", "sid": sid, "pub": b64e(session.pub),
                  "authRequired": session.auth_required}
        if session.auth_required:
            accept["salt"] = b64e(self.cfg.unattended_salt())
            accept["iterations"] = self.cfg.unattended_iterations()
        self.send_json(accept)
        session.begin()

    def _on_session_closed(self, sid: int, reason: str):
        self.sessions.pop(sid, None)
        self.send_json({"t": "session-closed", "sid": sid, "reason": reason})

    def _close_session(self, sid: int, reason: str):
        s = self.sessions.pop(sid, None)
        if s:
            s.close(reason)

    # ================================================================ heartbeat

    def _heartbeat_loop(self):
        period = max(10, int(self.cfg.get("heartbeatSec", 30)))
        while self.connected.is_set() and not self.stopping.is_set():
            self.send_json({"t": "heartbeat", "metrics": sysinfo.metrics(),
                            "unattended": self.cfg.has_unattended_password,
                            "sessions": len(self.sessions)})
            self.stopping.wait(period)
