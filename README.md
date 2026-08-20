# AegisDesk

A self-hosted remote support and device management tool. Same job as AnyDesk or
TeamViewer — see a screen, take over the mouse and keyboard, move files, run a
command, check on a machine — except you run the server, there is no
subscription, no device limit, and no seat count.

Built for exactly the situation it came from: one person doing IT for a
restaurant group, plus a few managers who need to look in on the back-office
machines without walking over to them.

```
  ┌──────────────┐         ┌──────────────┐         ┌──────────────┐
  │   Viewer     │  wss    │    Relay     │  wss    │    Agent     │
  │  (browser,   │◄───────►│  (your VPS,  │◄───────►│  (POS / back │
  │  any device) │         │  NAS or PC)  │         │  office PC)  │
  └──────────────┘         └──────────────┘         └──────────────┘
        │                         │                        │
        └───── AES-256-GCM, keys from ECDH ────────────────┘
               the relay forwards bytes it cannot read
```

---

## What it does

**Screen and control**
- Live screen streaming with damage-region encoding: only the parts of the
  screen that changed get sent. A static POS screen costs a few hundred bytes
  a second; a full redraw costs a normal JPEG.
- Multi-monitor, with per-session monitor switching.
- Mouse (all 5 buttons), wheel, horizontal wheel.
- Keyboard at **scancode** level, so layouts, games, UAC prompts and nested RDP
  sessions all behave. Unicode text injection for anything the scancode table
  can't express.
- Ctrl+Alt+Del, lock workstation, pause video, force keyframe.
- Adaptive quality: JPEG quality, resolution and frame rate move on their own
  as the link gets better or worse. Four manual presets too.

**Everything around it**
- Two-way file transfer, chunked with flow control, drag-and-drop in the
  browser. Remote file browser with mkdir/rename/delete.
- Two-way clipboard sync.
- Remote shell — a real pty on macOS/Linux, PowerShell or cmd on Windows.
- System inventory: OS, CPU, RAM, disks, IPs, battery, uptime.
- Process list with end-task, and the Windows service list.
- Fleet view with live CPU/RAM/disk bars, groups, tags, search.
- Operator accounts with roles, and an append-only audit log of every login,
  session and enrollment.

**Compatibility — the main reason to build your own**
- **Viewer:** any modern browser. Your laptop, a phone, an iPad, a borrowed
  machine at the other restaurant. Nothing to install, and the touch handling
  is real: tap to click, long-press to right-click, two-finger scroll,
  on-screen modifier keys for Ctrl/Alt/Win/F-keys.
- **Agent:** Windows 10/11 primarily; also works on macOS and Linux/X11.
- **Relay:** anywhere Node 18+ runs. A spare PC, a NAS, a $5 VPS, Fly.io's
  free tier, or inside a Tailscale network with no public exposure at all.

---

## Two ways in

Same split every mainstream tool has, because both situations are real.

| | **Support tool** | **Permanent agent** |
|---|---|---|
| For | someone who needs help right now | a machine you manage |
| They do | double-click one file, read out two numbers | nothing, ever |
| Installs | nothing, needs no admin rights | runs at logon |
| Leaves behind | nothing at all | a config file and a tray icon |
| You connect | when they give you the code | any time |
| Good for | managers, staff laptops, the other site | POS terminals, back office |

Build the support tool by double-clicking `BUILD-SUPPORT-TOOL.bat` — it bakes
your relay address and key into a single `.exe` you can email or publish on
GitHub Releases. See **[HELPING-PEOPLE.md](HELPING-PEOPLE.md)**, which is the
page to read if the people you support aren't technical.

---

## Quick start

**On Windows, double-click `START.bat`.** It installs what's missing, starts the
relay and an agent on this PC, opens the viewer and prints your login. That gets
you a working system on one machine in a few minutes, which is the right way to
see what this is before deploying it. `STOP.bat` shuts it down;
`CONSENT-MODE.bat` restarts it in ask-permission-every-time mode.

The rest of this section is what that script automates — and what you'll do on
the other computers.

Three pieces. Start the relay once, then enroll each computer.

### 1. Relay (once)

```bash
cd relay
npm install
node server.js --port 7443
```

First run prints an admin password and an enrollment key — **copy both**:

```
==================================================================
  AegisDesk first run -- an admin account was created for you

    username : admin
    password : D0HaP3fPEq-tmRdX

  Change it after you log in (Settings -> Change password).
  Enrollment key for installing agents:

    i6J45dQS_LFL14o91WyrsHo6pjLmMned
==================================================================
```

Open `http://localhost:7443` and sign in.

> The browser needs Web Crypto, which browsers only expose on `https://` or
> `http://localhost`. Testing on localhost works out of the box. For anything
> else, see [Exposing it safely](#exposing-it-safely) — it's one command with
> Docker Compose.

### 2. Agent (on each computer you want to support)

Windows, one line, from an elevated PowerShell in the `agent` folder:

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1 `
  -Relay wss://desk.myrestaurant.com `
  -EnrollKey i6J45dQS_LFL14o91WyrsHo6pjLmMned `
  -Name "POS-01" `
  -Password "Front0fH0use!"
```

That installs Python if it's missing, installs dependencies, enrolls the
machine, and registers it to start at logon. Drop `-Password` if you want
someone at that machine to approve every session instead.

Manually, or on macOS/Linux:

```bash
cd agent
pip install -r requirements.txt
python -m aegis_agent setup          # asks for relay, key, name, password
python -m aegis_agent install        # start at logon
python -m aegis_agent run
```

### 3. Connect

Open the relay in a browser, click the device, and you're in. On a phone,
add it to your home screen and it behaves like an app.

Useful before you go anywhere near a network:

```bash
python -m aegis_agent selftest    # benchmarks capture, checks encoders and input
python -m aegis_agent status      # shows config, device ID, what's detected
```

---

## Security

The relay is a **blind** byte forwarder. It brokers who talks to whom and then
forwards frames it has no key for.

| | |
|---|---|
| **Transport** | TLS to the relay (via your reverse proxy or `--tls-cert`). |
| **Session** | AES-256-GCM. Key from an ephemeral P-256 ECDH per session, through HKDF-SHA256. New keys every session; nothing is reused. |
| **Replay** | Per-direction nonce counters plus a 4096-frame sliding window. Reflected and replayed frames are dropped. |
| **Device auth** | Agents get a 256-bit device token at enrollment, checked in constant time on every reconnect. New enrollments need the enrollment key. |
| **Access password** | PBKDF2-SHA256, 250 000 iterations. The agent stores only the derived key, never the password. |
| **Support codes** | Eight digits, fresh per session, held only in memory and never written to disk. Only attemptable by someone who already holds an operator account on the relay. |
| **Anti-MITM** | The password proof is an HMAC bound to the session ID **and both ECDH public keys**. A relay operator who swaps in their own keys produces an invalid proof and gets hung up on — and the agent returns its own proof so the viewer can confirm the far end is genuine. Verified by test. |
| **Brute force** | 5 password attempts per session with increasing delay, then the agent drops the connection. Login and enrollment are rate-limited at the relay. |
| **Audit** | Append-only JSONL on the relay (logins, sessions, enrollments, bytes, durations) and on each agent (its own sessions, auth results, shell starts, process kills). |
| **Least privilege** | Per-device switches for input, clipboard, files, shell, sysinfo, process control. File browsing can be jailed to one folder or set read-only. |

### It is visible, by design

This is a support tool, not a surveillance tool, and the code reflects that:

- A consent dialog is the **default**. Unattended access is opt-in per device.
- Whenever a session is live, a red pulsing banner sits on the remote screen
  with a **Disconnect** button. Anyone at the machine can end the session.
- The tray icon shows the device ID and session count.
- Every session is logged on both ends.

There is no hidden mode, no invisible install, and no way to turn the banner
off from the network side. If you need to watch someone without their
knowledge, this is the wrong software.

---

## Exposing it safely

Pick one. In rough order of how much I'd trust it on a restaurant network:

**A. Tailscale (best, and free for personal use)**
Install Tailscale on the relay host and on your laptop/phone. Run the relay
bound to the tailnet address. Nothing is on the public internet at all, and
`https://` comes free via Tailscale Serve:

```bash
tailscale serve --bg 7443
```

**B. Docker Compose + Caddy (public hostname, automatic HTTPS)**
Point a DNS A record at the host, then:

```bash
cd relay
DOMAIN=desk.myrestaurant.com docker compose up -d
```

Caddy fetches a Let's Encrypt certificate on its own and renews it.

**C. Fly.io free tier**

```bash
cd relay
fly launch --no-deploy --name my-aegisdesk
fly volumes create aegis_data --size 1
fly deploy
```

`auto_stop_machines` is deliberately off in `fly.toml` — a sleeping relay shows
every device as offline.

**D. Self-signed cert on the LAN**
Works, but browsers will bury Web Crypto behind a warning page and mobile
Safari is especially stubborn. Fine for a quick test; use A or B for real.

Whatever you pick: **do not** set `allowOpenEnroll`. It lets any machine that
can reach the relay enroll itself.

---

## Configuration

### Relay

CLI flag, env var, or `relay/config.json` (see `config.example.json`) — in that
order of precedence.

| Flag | Env | Default | |
|---|---|---|---|
| `--port` | `PORT` | `7443` | |
| `--host` | `AEGIS_HOST` | `0.0.0.0` | |
| `--data-dir` | `AEGIS_DATA_DIR` | `./data` | devices, operators, audit log |
| `--tls-cert` / `--tls-key` | `AEGIS_TLS_CERT` / `_KEY` | none | direct TLS instead of a proxy |
| `--session-hours` | `AEGIS_SESSION_HOURS` | `12` | operator token lifetime |
| `--trust-proxy` | `AEGIS_TRUST_PROXY` | `false` | honour `X-Forwarded-For` in audit logs |
| `--allow-open-enroll` | `AEGIS_OPEN_ENROLL` | `false` | **leave this alone** |
| `--log` | `AEGIS_LOG` | `info` | `debug` when something's wrong |
| `--init` | — | — | provision and exit, printing `AEGIS_*=value` lines an installer can parse |
| `--reset-admin` | — | — | with `--init`, also set a fresh admin password |
| `--admin-password` | `AEGIS_ADMIN_PASSWORD` | — | with `--init`, set a specific one |

### Agent

`%ProgramData%\AegisDesk\agent.json` on Windows,
`~/.config/aegisdesk/agent.json` elsewhere. Mode `0600` / SYSTEM+Admins ACL,
because it holds the device token and the password verifier.

```jsonc
{
  "relayUrl": "wss://desk.myrestaurant.com",
  "name": "POS-01",

  "requireConsent": true,          // someone must click Allow
  "consentTimeoutSec": 45,
  "consentDefault": "deny",        // what happens if nobody answers
  "unattendedBypassesConsent": true,

  "permissions": {
    "input": true,                 // false = look, don't touch
    "clipboard": true,
    "files": true,
    "filesReadOnly": false,
    "fileJail": null,              // e.g. "C:\\POS-Exports" to confine browsing
    "shell": true,
    "sysinfo": true,
    "processes": true,
    "lockWorkstation": true
  },

  "showBanner": true,              // on-screen "session active" banner
  "showTray": true,
  "defaultQuality": "auto",        // auto | speed | balanced | quality
  "maxWidth": 1600,
  "maxFps": 24,
  "monitor": 1
}
```

A useful pattern for a POS terminal you only ever need to read from:

```jsonc
"permissions": { "input": false, "shell": false, "files": true,
                 "filesReadOnly": true, "fileJail": "C:\\POS-Exports" }
```

### Agent commands

```
python -m aegis_agent setup      configure relay, name, password
python -m aegis_agent support    one-off session: shows an ID and a code, installs nothing
python -m aegis_agent run        connect and serve  (--no-ui, --no-input, --insecure-tls)
python -m aegis_agent password   set or clear the unattended password
python -m aegis_agent status     config + detected capabilities
python -m aegis_agent install    start at logon  (--elevated for UAC/Ctrl-Alt-Del)
python -m aegis_agent uninstall  remove the autostart entry
python -m aegis_agent selftest   benchmark capture and check encoders
```

---

## Performance

The whole design is about not sending pixels twice.

Each frame is compared to the last one on a 64×64 tile grid with a single
vectorised numpy comparison. Changed tiles are merged into as few rectangles as
possible — horizontal runs first, then vertically — and only those rectangles
get encoded. If more than 55% of the screen changed, it sends one full frame
instead, because that's cheaper than 40 JPEG headers.

Measured on the synthetic test pattern at 1280×720, one moving 100×100 block:

| | |
|---|---|
| Keyframe (first frame) | 8.6 KB |
| Steady-state frame | 1.15 KB |
| Encode time per frame | 0.7 ms (OpenCV) |
| Streaming rate | 25 fps, ~33 KB/s |
| Completely static screen | **0 bytes** — nothing is sent at all |

Install `opencv-python-headless` and JPEG encoding gets 3–6× faster than
Pillow. The agent uses it automatically if present and falls back silently if
not.

Tuning knobs, roughly in order of effect: `maxWidth` (halving it quarters the
pixels), quality preset, `maxFps`.

---

## Known limits

Being straight about what this doesn't do:

- **Login screen / UAC.** The agent runs in the interactive user session, so it
  can't show you the Windows login screen or the secure desktop behind a UAC
  prompt. This isn't laziness — a Windows *service* runs in session 0 and can't
  capture the user's desktop at all, so a service-based agent would show you a
  black screen. `install --elevated` gets UAC prompts to work within a
  logged-in session; nothing here reaches the pre-login screen.
- **No H.264/hardware encoding.** Tiled JPEG is simple, dependency-light and
  fine for desktop work. Watching video over it is not a good time.
- **No audio.**
- **Wayland.** Capture on Wayland is unreliable; X11 works.
- **Windows has no pty**, so the remote shell there is pipe-based. Line-oriented
  tools are fine; full-screen TUIs aren't.
- **No wake-on-LAN, no session recording, no unattended reboot-and-reconnect**
  yet. All are additions, not redesigns.

---

## Testing

```bash
bash scripts/run-all-tests.sh          # unit + interop + end-to-end
npm install playwright                  # optional, adds the real-browser suite
```

Five layers, all of which pass on a clean checkout:

1. **59 unit tests** — wire framing, damage detection and rectangle merging,
   encoder behaviour, input coordinate mapping across multiple monitors,
   scancode table, file-service path jail and read-only enforcement, config
   handling.
2. **Python ↔ browser crypto interop** — the same ECDH, HKDF, AES-GCM, PBKDF2
   and HMAC vectors are computed by the Python agent and by WebCrypto, and
   compared byte for byte. If these pass, the browser and the agent genuinely
   agree.
3. **28 end-to-end checks** — boots a real relay, enrolls a real agent, and
   drives a full session over the wire: ECDH handshake, a deliberately wrong
   password (rejected), the right one (accepted, with the anti-MITM ack
   verified), live video, input, quality change, ping/pong, a 700 KB file
   downloaded and byte-compared, a shell command executed, a system report, a
   forged frame survived, and a clean teardown.
4. **16 quick-support checks** — starts a support session the way the shipped
   `.exe` does, connects with the code a person would read aloud, then verifies
   the two promises that model makes: the session de-enrols itself when the
   window closes, and nothing whatsoever was written to the helped person's
   disk.
5. **18 browser checks** — drives the actual viewer in a real Chromium
   (`npm install playwright` to enable). Logs in, opens a session through
   WebCrypto, reads pixels back off the canvas to prove frames decoded, exercises
   the file browser, terminal, system-info panel and admin console, then repeats
   the whole thing at iPhone viewport with touch emulation. Screenshots land in
   `screenshots/`.

---

## Layout

```
START.bat  STOP.bat  CONSENT-MODE.bat     one-click Windows setup
BUILD-SUPPORT-TOOL.bat                   builds the .exe you send to people
tools/
  setup-and-run.ps1    what START.bat actually runs
  stop.ps1             process shutdown, matched by command line
  build-support-tool.ps1  bakes relay + key into AegisDesk-Support.exe

relay/
  server.js            HTTP + WebSocket relay, session brokering
  lib/util.js          tokens, scrypt password hashing, IDs, logging
  lib/store.js         JSON persistence, audit log
  lib/http.js          router, static files, rate limiter
  public/index.html    the entire viewer + admin console, one file
  Dockerfile  docker-compose.yml  Caddyfile  fly.toml

agent/
  aegis_agent/
    client.py          relay connection, reconnect, session brokering
    session.py         one session: crypto, capture loop, permissions
    capture.py         screen capture, damage detection, tile encoding
    inputctl.py        SendInput / pynput backends, coordinate mapping
    keymap.py          DOM code -> PS/2 scancode
    crypto.py          ECDH, HKDF, AES-GCM, PBKDF2 proofs
    protocol.py        wire framing
    support.py         one-off sessions: codes, no disk writes
    files.py shell.py clipboard.py sysinfo.py ui.py config.py autostart.py
  install-windows.ps1  build-exe.ps1  build-support-exe.ps1  uninstall-windows.ps1

protocol/PROTOCOL.md   full wire specification
tests/                 unit, interop, end-to-end and real-browser suites
```

---

## Everyday recipes

**Add a manager who can connect but not administer**
Admin → Operators → add with role `operator`.

**Let a manager see a screen but never touch it**
Set `"input": false` in that agent's config and restart it. The viewer's
toolbar reflects it.

**Rotate the enrollment key after a laptop goes missing**
Admin → Rotate key. Enrolled devices keep working; new installs need the new
key. To cut off the missing machine specifically, delete it from the fleet list
— its device token stops working immediately and its session is killed.

**Ship it without Python on the target machines**
Run `build-exe.ps1` once on a Windows box, copy `dist\AegisDeskAgent.exe`
around, then `AegisDeskAgent.exe setup … && AegisDeskAgent.exe install`.

**Find out what happened last Tuesday**
Admin → Audit log, or `relay/data/audit.jsonl` and each agent's
`sessions.jsonl`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "This page needs Web Crypto" | You're on plain `http://` to a non-localhost host. Use HTTPS or Tailscale Serve. |
| Device never appears | Check `agent.json`'s `relayUrl`, then the agent log (`%ProgramData%\AegisDesk\agent.log`). Wrong enrollment key logs `bad_enroll_key`. |
| Black screen | The agent is running as a service or in session 0. Use `install` (logon task), not a service wrapper. |
| Keyboard types the wrong characters | Almost always a stuck modifier. Click the canvas to refocus, or press the softkey modifiers to clear them; the agent also releases stuck keys when a session ends. |
| Connects, then drops after ~40s | A proxy is eating WebSocket frames. Caddy config in this repo handles it; for nginx you need `proxy_http_version 1.1` and the `Upgrade`/`Connection` headers. |
| Sluggish over 4G | Set quality to Speed, or drop `maxWidth` to 1280. |
| `no JPEG encoder` | `pip install opencv-python-headless Pillow` |
| Consent dialog never shows | No desktop UI available (headless or a service). It then falls back to `consentDefault`, which is `deny`. |

---

## License

MIT. See [LICENSE](LICENSE).

Whatever you build on it, keep the consent prompt and the session banner
honest. The reason a tool like this is acceptable to have on a coworker's
computer is that they can always see it and always end it.
