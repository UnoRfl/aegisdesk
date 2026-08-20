# Helping people who aren't technical

There are two ways to get onto a computer, and picking the right one is most of
the battle. Mainstream tools have the same split — TeamViewer calls them
QuickSupport and Host, AnyDesk calls them the portable client and unattended
access.

| | **Support tool** | **Permanent agent** |
|---|---|---|
| For | A person who needs help right now | A machine you manage |
| They do | Double-click one file, read out two numbers | Nothing, ever |
| Installs | Nothing | Runs at logon |
| Admin rights | Not needed | Needed once to install |
| Leaves behind | Nothing at all | A config file and a tray icon |
| You connect | When they give you the code | Any time |
| In your fleet list | Only while their window is open | Always |
| Good for | Managers, staff laptops, the other restaurant, a supplier | POS terminals, back office, kiosks |

The rest of this page is about the support tool, because that's the one your
staff will actually touch.

---

## Building it (once)

Double-click **BUILD-SUPPORT-TOOL.bat**.

It reads the enrollment key out of your own relay, asks what address people
should reach you on, and produces one file:

```
agent\dist\AegisDesk-Support.exe
```

Your relay address and key are compiled into it, so the person receiving it has
nothing to type. Rebuild only if you change relay address or rotate the key.

> **You need a real relay address first.** Not `localhost` — that only works on
> your own PC. Tailscale is the easy answer and takes about five minutes;
> QUICKSTART.md Part 2 walks through it. The builder refuses a localhost
> address rather than handing you an .exe that can't work.

**Treat the .exe as moderately sensitive.** Anyone holding it can offer *their*
screen to your relay. It does **not** let them view anyone else's screen — that
needs an operator account and password. Worst case, someone spams your fleet
list with sessions you can just ignore; rotate the key (Admin → Rotate key) and
rebuild.

---

## Getting it to people

Pick whichever fits how your team already works.

**Email or text it.** Simplest. Some mail providers strip `.exe` attachments —
zip it first if yours does.

**GitHub Releases.** The standard way free tools do this, and it gives you a
permanent link you can put on a card by the till:

```cmd
gh release create v1.0 agent\dist\AegisDesk-Support.exe ^
  --title "Remote Support" ^
  --notes "Double-click to start a support session."
```

The download link is then predictable:

```
https://github.com/UnoRfl/aegisdesk/releases/latest/download/AegisDesk-Support.exe
```

That link always points at your newest build. Upload a new .exe under a new tag
and it follows automatically — nobody has to be told about the update.

A short link (Bitly, or a `support.yourdomain.com` redirect) makes it something
you can say out loud on the phone. That's worth more than it sounds when you're
talking someone through it.

**Shared drive or USB.** Fine too. Nothing phones home to check for updates, so
an old copy keeps working until you rotate the key.

---

## What the other person does

The entire experience, start to finish:

1. Download the file
2. Double-click it
3. Read you the two numbers on the screen
4. Close the window when you're done

That's it. No install, no account, no password to invent, no admin prompt, no
settings. What they see:

```
  ┌────────────────────────────────────────────┐
  │  Racquel IT Support                        │
  │  Read these two numbers to the person      │
  │  helping you.                              │
  │                                            │
  │  ┌──────────────────────────────────────┐  │
  │  │ YOUR ID                              │  │
  │  │ 504 020 965                          │  │
  │  └──────────────────────────────────────┘  │
  │  ┌──────────────────────────────────────┐  │
  │  │ SESSION CODE                         │  │
  │  │ 4017 6569                            │  │
  │  └──────────────────────────────────────┘  │
  │                                            │
  │  ● Ready. Waiting for a connection.        │
  │                                            │
  │  Need to reach us? (555) 123-4567          │
  │                                            │
  │  [ Copy ]              [ New code ] [Quit] │
  └────────────────────────────────────────────┘
```

Once you connect, the status line turns red and says who's connected. A banner
also parks itself in the corner of their screen with a **Disconnect** button, so
they can end it themselves at any moment without finding the window again.

**Message you can copy and paste to them:**

> Hi — to let me look at your screen, download this file and double-click it:
> https://github.com/UnoRfl/aegisdesk/releases/latest/download/AegisDesk-Support.exe
>
> A small window will open with two numbers. Read them to me over the phone and
> I'll connect. Nothing gets installed, and closing the window ends it. If
> Windows warns you about the file, choose "More info" then "Run anyway".

---

## Your side

Open your relay in a browser. A waiting session appears **at the top** of the
list with a blue **WAITING FOR HELP** badge and the person's name (`Maria on
FRONT-DESK`), so you can tell instantly which entry is the person on the phone.

Click **Connect**, type the code they read you, and you're in. Everything works
the same as a permanent agent: screen, keyboard and mouse, files, clipboard,
shell, system info.

When they close their window the entry vanishes from your list. You don't clean
anything up, and your fleet list doesn't silt up with dozens of dead one-off
entries.

---

## Why a code rather than an Accept button

For a one-off session the code *is* the consent. They had to read it to you, so
they know exactly who's connecting and when — and unlike an Accept prompt, they
can't click it by reflex without understanding what they agreed to.

It also solves a practical problem: a support session usually starts with the
person unable to work the computer properly. Asking them to find and click a
dialog they can't describe is exactly the wrong move.

The code is eight digits, generated fresh each time the window opens, held only
in memory, and only usable by someone who already has an operator account on
your relay. **New code** issues a different one if they've read it out to the
wrong person.

For permanent agents on machines you own, the trade-off flips — nobody's sitting
there, so those use a stored access password instead. And for a staff laptop you
manage but don't own, you can install the permanent agent with *no* password, so
it prompts for Accept on screen every time. All three modes exist because all
three situations are real.

---

## Common questions from the other end

**"Windows says it's not safe."** SmartScreen warns about any executable without
a paid code-signing certificate. **More info → Run anyway.** If you want that
warning gone, an EV code-signing certificate is a few hundred dollars a year;
for internal use most people just tell staff to expect it.

**"Does it stay on my computer?"** No. It writes nothing — no settings, no
identity, no logs. Delete the download and there's no trace.

**"Can you see my screen after I close it?"** No. The window closing drops the
connection and removes the entry from the helper's list entirely.

**"Do I need to be an administrator?"** No. It runs as a normal user. It can't
show the Windows login screen or click through a UAC prompt for that reason —
if you need to fix something that requires admin, they'll have to type the
admin password while you watch, or you use a permanent agent installed with
`--elevated`.

**"My antivirus quarantined it."** Unsigned executables that capture the screen
and inject input look exactly like the malicious kind, because functionally they
are the same thing — the difference is consent. Add an exclusion, or sign the
binary.
