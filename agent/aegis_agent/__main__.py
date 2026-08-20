"""AegisDesk agent command line."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import signal
import sys
import time

from . import AGENT_VERSION
from .config import Config, config_path, log_path, session_log_path


def setup_logging(level: str, to_file: bool = True, quiet: bool = False):
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    if not quiet:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if to_file:
        try:
            path = log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(path, maxBytes=4 * 1024 * 1024,
                                                      backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception as exc:                             # noqa: BLE001
            print(f"warning: cannot write log file ({exc})", file=sys.stderr)
    logging.getLogger("websocket").setLevel(logging.WARNING)


BANNER = r"""
   _              _     ___          _
  /_\  ___  __ _ (_)___|   \ ___ ___| |__
 / _ \/ -_)/ _` || (_-<| |) / -_|_-<| / /
/_/ \_\___|\__, ||_/__/|___/\___/__/|_\_\
           |___/            agent v{v}
""".strip("\n")


# ==================================================================== commands

def cmd_setup(args, cfg: Config):
    from .client import normalise_relay_url
    if args.relay:
        cfg["relayUrl"] = args.relay
    if not cfg.get("relayUrl"):
        cfg["relayUrl"] = input("Relay URL (e.g. wss://desk.myrestaurant.com): ").strip()
    try:
        normalise_relay_url(cfg["relayUrl"])
    except Exception as exc:                                 # noqa: BLE001
        print(f"error: {exc}")
        return 2
    if args.enroll_key:
        cfg["enrollKey"] = args.enroll_key
    elif not cfg.get("deviceId") and not cfg.get("enrollKey"):
        cfg["enrollKey"] = getpass.getpass("Enrollment key (from the relay admin page): ").strip()
    if args.name:
        cfg["name"] = args.name
    elif not cfg.get("name"):
        import socket
        suggested = socket.gethostname()
        entered = input(f"Friendly name for this computer [{suggested}]: ").strip()
        cfg["name"] = entered or suggested

    if args.password is not None:
        cfg.set_unattended_password(args.password or None)
    elif not cfg.has_unattended_password and not args.no_password_prompt:
        print("\nUnattended password (optional).")
        print("  Set one and this machine can be reached without anyone clicking Allow.")
        print("  Leave blank to require someone at the machine to approve every session.")
        pw = getpass.getpass("  Password (blank = consent required): ")
        if pw:
            again = getpass.getpass("  Repeat: ")
            if pw != again:
                print("error: passwords do not match")
                return 2
            cfg.set_unattended_password(pw)

    cfg.save()
    print(f"\nSaved to {cfg.path}")
    print(json.dumps(cfg.summary(), indent=2))
    print("\nNext:  python -m aegis_agent run"
          "\n   or: python -m aegis_agent install   (start automatically at logon)")
    return 0


def cmd_password(args, cfg: Config):
    if args.clear:
        cfg.set_unattended_password(None)
        print("Unattended password cleared -- sessions now require someone to click Allow.")
        return 0
    pw = args.set
    if pw is None:
        pw = getpass.getpass("New unattended password (blank to cancel): ")
        if not pw:
            print("cancelled")
            return 1
        if pw != getpass.getpass("Repeat: "):
            print("error: passwords do not match")
            return 2
    try:
        cfg.set_unattended_password(pw)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print("Unattended password set. Restart the agent for it to take effect on new sessions.")
    return 0


def cmd_status(args, cfg: Config):
    from . import autostart
    from .capture import encoder_name, _HAVE_MSS
    from .inputctl import open_input_backend
    from .ui import _HAVE_TK
    print(BANNER.format(v=AGENT_VERSION))
    print(json.dumps({
        **cfg.summary(),
        "autostartInstalled": autostart.is_installed(),
        "jpegEncoder": encoder_name(),
        "screenCapture": "mss" if _HAVE_MSS else "MISSING (pip install mss)",
        "inputBackend": open_input_backend().name,
        "desktopUi": bool(_HAVE_TK),
        "logFile": log_path(),
        "sessionLog": session_log_path(),
    }, indent=2))
    return 0


def cmd_install(args, cfg: Config):
    from . import autostart
    if not cfg.get("relayUrl"):
        print("Run `python -m aegis_agent setup` first.")
        return 2
    if args.elevated and autostart.IS_WINDOWS:
        ok, msg = autostart._install_windows_elevated()
    else:
        ok, msg = autostart.install()
    print(("OK: " if ok else "FAILED: ") + msg)
    if ok and autostart.IS_WINDOWS:
        print("\nThe agent starts when a user logs in. A Windows *service* cannot capture\n"
              "the interactive desktop (session 0 isolation), which is why a logon task is\n"
              "used instead. See README -> 'Login screen limitation'.")
    return 0 if ok else 1


def cmd_uninstall(args, cfg: Config):
    from . import autostart
    ok, msg = autostart.uninstall()
    print(("OK: " if ok else "FAILED: ") + msg)
    return 0 if ok else 1


def cmd_selftest(args, cfg: Config):
    from .capture import TileEncoder, encoder_name, open_screen_source
    from .inputctl import open_input_backend
    from . import protocol as P
    print(BANNER.format(v=AGENT_VERSION))
    print(f"\nencoder      : {encoder_name()}")
    src = open_screen_source()
    print(f"screen source: {type(src).__name__}")
    for m in src.monitors:
        print(f"  monitor {m.id}: {m.width}x{m.height} at ({m.x},{m.y})"
              + (" [primary]" if m.primary else ""))
    enc = TileEncoder()
    t0 = time.perf_counter()
    n, total, sent = 0, 0, 0
    while time.perf_counter() - t0 < 3.0:
        out = enc.encode(src.grab(src.monitors[0].id))
        n += 1
        if out:
            sent += 1
            total += out["bytes"]
            P.unpack_tile_frame(P.pack_tile_frame(1, out["codec"], out["flags"], out["seq"],
                                                  out["w"], out["h"], out["tiles"]))
    dt = time.perf_counter() - t0
    print(f"\ncaptured {n} frames in {dt:.1f}s -> {n/dt:.1f} fps")
    print(f"{sent} frames had changes, {total/1024:.0f} KB total "
          f"({total/max(1,sent)/1024:.1f} KB per changed frame)")
    print(f"estimated steady-state bandwidth: {total/dt/1024:.0f} KB/s")
    print(f"stats: {json.dumps(enc.stats.snapshot())}")
    b = open_input_backend()
    print(f"\ninput backend: {b.name}")
    print(f"virtual screen: {b.virtual_screen()}   cursor: {b.cursor_pos()}")
    src.close()
    if encoder_name() == "none":
        print("\nWARNING: no JPEG encoder. Run: pip install opencv-python-headless Pillow")
        return 1
    print("\nself-test OK")
    return 0


def cmd_support(args, cfg: Config):
    """Quick support session: one window, two numbers, nothing installed.

    Deliberately does not touch the machine it runs on -- no config file, no
    log file, no session log, no autostart entry, no admin rights. Closing the
    window ends the session and de-enrols the device at the relay.
    """
    import threading

    from . import support
    from .client import AgentClient, normalise_relay_url
    from .ui import open_ui

    baked = support.baked()
    relay = args.relay or baked.get("relayUrl") or ""
    enroll = args.enroll_key or baked.get("enrollKey") or ""

    if not relay:
        print("\n  This copy of the support tool has no server configured.\n"
              "  Ask whoever sent it to you for a new copy, or run:\n"
              "      aegis_agent support --relay <url> --enroll-key <key>\n")
        return 2
    try:
        normalise_relay_url(relay)
    except Exception as exc:                                 # noqa: BLE001
        print(f"  bad relay URL: {exc}")
        return 2

    # A throwaway configuration that exists only in memory.
    scfg = Config(in_memory=True)
    scfg["relayUrl"] = relay
    scfg["enrollKey"] = enroll
    scfg["name"] = args.name or support.default_session_name()
    scfg["requireConsent"] = False        # handing over the code IS the consent
    scfg["showTray"] = False              # the window is the whole interface
    scfg["sessionLog"] = False            # leave nothing on their disk
    scfg["showBanner"] = True             # but always show that a session is live
    scfg["heartbeatSec"] = 15
    scfg.set_support_code(support.new_code())

    ui = open_ui(enabled=True)
    client = AgentClient(scfg, ui=ui, ephemeral=True, insecure_tls=args.insecure_tls)

    net = threading.Thread(target=client.run_forever, daemon=True, name="aegis-support-net")
    net.start()

    try:
        ui.show_support(
            get_state=client.support_state,
            on_new_code=client.new_support_code,
            on_quit=client.stop,
        )
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()
        ui.stop()
    return 0


def cmd_run(args, cfg: Config):
    from .client import AgentClient
    from .ui import TrayIcon, open_ui

    if not cfg.get("relayUrl"):
        print("No relay configured. Run: python -m aegis_agent setup")
        return 2
    if not cfg.get("deviceId") and not cfg.get("enrollKey"):
        print("This device is not enrolled and no enrollment key is set.\n"
              "Run: python -m aegis_agent setup --relay <url> --enroll-key <key>")
        return 2

    log = logging.getLogger("aegis")
    print(BANNER.format(v=AGENT_VERSION))
    ui = open_ui(enabled=cfg.get("showBanner", True) and not args.no_ui)
    log.info("presence UI: %s", type(ui).__name__)

    client = AgentClient(cfg, ui=ui, prefer_null_input=args.no_input,
                         insecure_tls=args.insecure_tls)

    tray = None
    if cfg.get("showTray", True) and not args.no_ui:
        def copy_id():
            from .clipboard import Clipboard
            Clipboard().set(str(cfg.get("deviceId") or ""))
        tray = TrayIcon(client.status, on_quit=client.stop, on_copy_id=copy_id)
        tray.start()

    def handle_signal(signum, _frame):
        log.info("signal %s -- shutting down", signum)
        client.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except Exception:                                    # noqa: BLE001
            pass

    try:
        client.run_forever()
    except KeyboardInterrupt:
        client.stop()
    finally:
        if tray:
            tray.stop()
        ui.stop()
    log.info("agent stopped")
    return 0


# ==================================================================== argparse

def build_parser():
    p = argparse.ArgumentParser(
        prog="aegis_agent",
        description="AegisDesk agent -- lets an authorised operator view and control this computer.")
    p.add_argument("--config", help="path to agent.json (default: %s)" % config_path())
    p.add_argument("--log", default=None, help="log level: debug, info, warning, error")
    p.add_argument("--quiet", action="store_true", help="log to file only")
    p.add_argument("--version", action="version", version=f"AegisDesk agent {AGENT_VERSION}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", help="configure the relay, name and password")
    s.add_argument("--relay", help="relay URL, e.g. wss://desk.example.com")
    s.add_argument("--enroll-key", help="enrollment key from the relay admin page")
    s.add_argument("--name", help="friendly name shown in the fleet list")
    s.add_argument("--password", nargs="?", const="", help="set the unattended password non-interactively")
    s.add_argument("--no-password-prompt", action="store_true")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("run", help="connect to the relay and serve sessions")
    s.add_argument("--no-ui", action="store_true", help="no tray icon, banner or consent dialog")
    s.add_argument("--no-input", action="store_true", help="stream the screen but ignore all input")
    s.add_argument("--insecure-tls", action="store_true",
                   help="skip TLS verification (self-signed relay only -- not for the internet)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("support", help="one-off support session: shows an ID and a code, installs nothing")
    s.add_argument("--relay", help="relay URL (baked in when built as an executable)")
    s.add_argument("--enroll-key", help="enrollment key (baked in when built as an executable)")
    s.add_argument("--name", help="how this session appears in the helper's list")
    s.add_argument("--insecure-tls", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_support)

    s = sub.add_parser("password", help="set or clear the unattended password")
    s.add_argument("--set", nargs="?", const=None)
    s.add_argument("--clear", action="store_true")
    s.set_defaults(fn=cmd_password)

    s = sub.add_parser("status", help="show configuration and detected capabilities")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("install", help="start automatically at logon")
    s.add_argument("--elevated", action="store_true",
                   help="Windows: run with highest privileges (needed for UAC prompts / Ctrl+Alt+Del)")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("uninstall", help="remove the autostart entry")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("selftest", help="benchmark capture, check encoders and input backend")
    s.set_defaults(fn=cmd_selftest)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        args = parser.parse_args((argv or []) + ["run"]) if False else args
        parser.print_help()
        return 0
    cfg = Config(args.config)
    setup_logging(args.log or cfg.get("logLevel", "info"),
                  to_file=args.cmd == "run", quiet=args.quiet or args.cmd == "support")
    try:
        return args.fn(args, cfg)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
