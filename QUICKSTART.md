# Quick start

## The fast way: double-click START.bat

In the `aegisdesk` folder there is **START.bat**. Double-click it. That's the
whole thing.

It installs Node.js and Python if they're missing, installs both sets of
dependencies, provisions the relay, enrolls this PC, starts the relay and the
agent in their own windows, opens the viewer in your browser, and prints your
login on screen:

```
  ==================================================================
   Everything is running.

     Open:            http://localhost:7443
     Sign in as:      admin
     Password:        Xk9pQ2mR-vN4tL8w
     Device password:  TestPass123!   (asked when you click Connect)
  ==================================================================
```

First run takes 3–5 minutes, almost all of it `pip install` (numpy and opencv
are large). Later runs take seconds. It's safe to run again any time — it
reuses the device ID and leaves your admin password alone.

Three other double-clickables sit beside it:

| File | What it does |
|---|---|
| **START.bat** | Sets up and starts everything. Run this first. |
| **STOP.bat** | Shuts down the relay and agent. Only touches this project's processes. |
| **CONSENT-MODE.bat** | Restarts with no access password, so every connection asks permission on screen. Worth seeing once — it's what the other person sees. |

If Windows SmartScreen objects to a `.bat` from the internet: **More info →
Run anyway**. You can read all three in Notepad first; they're a few lines
each and just call the PowerShell script in `tools\`.

**A heads-up on what you'll see:** you're viewing the same screen your browser
is on, so the remote view shows the browser showing the remote view, forever.
That's normal and it means streaming works. Open Notepad and put it beside the
browser first — that gives you somewhere harmless to test clicking and typing.

Once that works, skip to **Part 2** below to make it reachable from your phone
and the other computers.

---

# Doing it by hand

Everything below is what START.bat automates. Worth reading once so you know
what's on your machine, and necessary for the other computers anyway.

---

## Part 1 — Start the relay

The relay is the middleman. It has to be reachable from both your phone/laptop
and the restaurant computers. Run it wherever that's true: a back-office PC
that's always on, a NAS, or a cheap VPS.

```bash
cd relay
npm install
node server.js --port 7443
```

You'll see something like:

```
==================================================================
  AegisDesk first run -- an admin account was created for you

    username : admin
    password : D0HaP3fPEq-tmRdX          <-- WRITE THIS DOWN

  Enrollment key for installing agents:

    i6J45dQS_LFL14o91WyrsHo6pjLmMned      <-- AND THIS
==================================================================
```

Open <http://localhost:7443> and sign in. Change the password under **Admin →
Change my password** while you're there.

Leave this running. To keep it running after you close the terminal:

```bash
# Linux
sudo npm install -g pm2 && pm2 start server.js --name aegisdesk && pm2 save

# Windows
npm install -g pm2 pm2-windows-startup
pm2 start server.js --name aegisdesk
pm2-startup install
pm2 save
```

---

## Part 2 — Make it reachable (pick one)

### Option A — Tailscale (recommended: free, private, no ports open)

On the relay machine and on your phone/laptop, install [Tailscale](https://tailscale.com/download)
and sign in with the same account. Then on the relay machine:

```bash
tailscale serve --bg 7443
tailscale status          # shows the https://<machine>.<tailnet>.ts.net URL
```

That URL is your relay address, it has a real certificate, and nothing is
exposed to the public internet. Use it for both the browser and the agents.

### Option B — Public hostname with automatic HTTPS

Point a DNS A record (e.g. `desk.myrestaurant.com`) at the relay host's public
IP, open ports 80 and 443, then:

```bash
cd relay
DOMAIN=desk.myrestaurant.com docker compose up -d
```

Caddy gets a Let's Encrypt certificate by itself. Your relay is
`https://desk.myrestaurant.com`.

### Option C — LAN only, for right now

Skip TLS and use `http://<relay-ip>:7443`. **The browser viewer will refuse to
run** — browsers only allow the encryption APIs on `https://` or
`http://localhost`. Fine for testing from the relay machine itself; move to A
or B before you rely on it.

---

## Part 3a — Helping someone who has nothing installed

This is the common case: a manager calls, can't print, and is not going to run
PowerShell. Build the support tool once:

**Double-click `BUILD-SUPPORT-TOOL.bat`.**

It pulls the enrollment key from your relay, asks for the address from Part 2,
and produces `agent\dist\AegisDesk-Support.exe`. Email that file to them, put
it on a shared drive, or publish it to GitHub Releases for a permanent link.

Their whole job: download it, double-click it, read you the two numbers on
screen, close it when you're done. Nothing installed, no admin rights, nothing
left behind. The session appears at the top of your fleet list with a blue
**WAITING FOR HELP** badge and disappears when they close the window.

Full detail, including the message to copy-paste to them and what to say when
Windows warns about the file: **[HELPING-PEOPLE.md](HELPING-PEOPLE.md)**.

---

## Part 3b — Install the permanent agent on a computer

On each Windows machine you want to support. Copy the `agent` folder over
(a USB stick is fine), then open PowerShell **as Administrator** in it:

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1 `
  -Relay wss://desk.myrestaurant.com `
  -EnrollKey i6J45dQS_LFL14o91WyrsHo6pjLmMned `
  -Name "POS-01" `
  -Password "Front0fH0use!"
```

Substitute your own relay URL, enrollment key, a name you'll recognise, and an
access password for that machine.

**About `-Password`:**

| | |
|---|---|
| **With** a password | You can connect any time without anyone touching that computer. Right for back-office and POS machines. |
| **Without** it | Someone at that computer must click **Allow** for every session. Right for staff laptops and anything personal. |

Either way, a red banner appears on the remote screen while you're connected,
with a Disconnect button.

The script takes a couple of minutes — it installs Python if needed, installs
dependencies, enrolls the machine, and sets it to start at logon. At the end it
prints the computer's 9-digit AegisDesk ID.

**macOS or Linux instead:**

```bash
cd agent
pip install -r requirements.txt
python -m aegis_agent setup      # it will ask you everything
python -m aegis_agent install
python -m aegis_agent run
```

On macOS, System Settings → Privacy & Security → grant **Screen Recording** and
**Accessibility** to your terminal or to Python. Without those, you get a black
screen and no input.

---

## Part 4 — Connect

Open your relay URL in any browser — laptop, phone, tablet, whatever's nearby.
Sign in. The machine appears in the list with a green dot and live CPU/RAM bars.
Click **Connect**.

If the device has an access password, you'll be asked for it. That check happens
between your browser and that computer directly; the relay can't see it and
can't fake it.

**On a phone:** tap = click, long-press = right-click, two fingers = scroll,
drag = drag. The **⌨** button brings up a row of Ctrl/Alt/Win/Esc/F-keys plus
your normal keyboard.

**Toolbar:** monitor picker, quality, scaling, Ctrl+Alt+Del, clipboard, files,
shell, system info, stats overlay, disconnect.

Add the relay to your phone's home screen and it opens like an app.

---

## Part 5 — Add the managers

**Admin → Operators**, add each person with role `operator`. They get their own
username and password and see the same fleet list. Keep `admin` for yourself —
that's the role that can add users, delete devices and rotate the enrollment
key.

If a manager should be able to look at a screen but never take control, set
that agent's config to `"input": false` and restart it.

---

## Checklist

- [ ] Relay running and reachable over `https://` or a tailnet
- [ ] Admin password changed from the generated one
- [ ] Enrollment key saved somewhere that isn't a sticky note
- [ ] Permanent agent installed on the machines you manage
- [ ] Support tool built and a link to it saved somewhere you can read aloud
- [ ] Access passwords set on unattended machines, left off personal ones
- [ ] Managers added as `operator`, not `admin`
- [ ] Tested from your phone on mobile data, not just on the shop wifi

---

## When something's wrong

```bash
# on the agent machine
python -m aegis_agent status      # is it enrolled? what did it detect?
python -m aegis_agent selftest    # can it actually capture and encode?
```

Agent log: `%ProgramData%\AegisDesk\agent.log` (Windows) or
`~/.config/aegisdesk/agent.log`.

Relay: `curl https://your-relay/healthz` and the console output.

The troubleshooting table in [README.md](README.md#troubleshooting) covers the
common ones — black screens, drops after 40 seconds, stuck modifier keys.
