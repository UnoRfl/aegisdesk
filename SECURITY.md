# Security design

What this document is for: if you're going to put software on other people's
computers that can see their screen and move their mouse, you should be able to
explain exactly what protects them. This is that explanation.

---

## Threat model

**Defended against**

| Adversary | Defence |
|---|---|
| Someone on the same wifi | TLS to the relay, and AES-256-GCM inside it. Two independent layers. |
| A malicious or compromised **relay** | The relay never holds a session key. It can drop or delay traffic (denial of service) but cannot read or forge session content, and if a device has an access password it cannot MITM the handshake either — see below. |
| Someone who steals a device ID | Device IDs are not credentials. Connecting also needs an operator account on the relay, and the device's own access password if it has one. |
| Someone who steals the relay's `state.json` | It holds scrypt password hashes and device tokens. Tokens let you impersonate an *agent* (offer a screen), not view one. Rotate them by de-enrolling the device. |
| Brute-forcing an access password | 5 attempts per session with increasing delay, then disconnect. Every attempt is logged on the agent. |
| Brute-forcing an operator login | 10 attempts per IP per 5 minutes. Every failure is logged with the IP. |
| Replayed or reflected frames | Per-direction nonce counters, a 4096-frame replay window, and rejection of any frame whose direction prefix matches the receiver's own send direction. |
| Tampered frames | AES-GCM authentication tag. A single flipped bit fails the tag and the frame is dropped. |
| Path traversal in the file browser | Paths are resolved with `realpath` and, when a jail is configured, must be inside it. Static file serving in the relay does the same. |
| Unauthorised enrollment | New agents need the enrollment key. Rate-limited to 20 attempts per IP per hour. |

**Not defended against**

- **A compromised agent machine.** If someone has admin on that computer, they
  can read `agent.json`, which holds the device token and the password
  verifier. Nothing in userland can prevent that. Treat the agent's config as a
  secret at rest.
- **A compromised viewer machine.** Same reasoning — a keylogger on your laptop
  sees your operator password.
- **A relay operator running a modified relay** who is willing to be caught.
  They can deny service, and on a device with **no** access password they can
  substitute their own ECDH key and MITM the session, because there is nothing
  else binding the handshake. This is why the access password matters: with one
  set, key substitution is detected and refused.
- **Traffic analysis.** Frame sizes and timing leak coarse activity (typing vs
  video vs idle). Padding this out was judged not worth the bandwidth.

---

## Session establishment, step by step

```
viewer                          relay                          agent
  |                               |                              |
  |-- POST /api/login ----------->|                              |
  |<-- HMAC-signed token ---------|                              |
  |                               |                              |
  |-- ws: auth(token) ----------->|                              |
  |-- ws: connect(deviceId, Pv) ->|                              |
  |                               |-- session-request(sid, Pv) -->|
  |                               |                              | consent prompt
  |                               |                              | (unless a password
  |                               |                              |  is configured)
  |                               |<-- session-accept(Pa, salt) --|
  |<-- session-open(Pa, salt) ----|                              |
  |                               |                              |
  |   both sides now compute:                                    |
  |     Z    = ECDH(P-256)                                       |
  |     salt = SHA256("aegisdesk-v1-salt" || sid || Pv || Pa)     |
  |     K    = HKDF-SHA256(Z, salt, "aegisdesk-v1-data", 32)      |
  |                               |                              |
  |== AES-256-GCM from here on; the relay forwards opaque bytes ==|
  |                               |                              |
  |-- AUTH_RESPONSE(proof) ------>|----------------------------->|  verify
  |<-- AUTH_RESULT(ok, ack) ------|<-----------------------------|  constant-time
  |                               |                              |
  |<-- TILE_FRAME, SCREEN_INFO ---|<-----------------------------|
  |-- INPUT, FILE_CTL, ... ------>|----------------------------->|
```

### The password proof, and why it's shaped that way

The obvious design is "send the password over the encrypted channel and compare
it." That fails against the relay, because the relay is the one that carries
both public keys during the handshake — it can hand each side its own key,
establish two separate sessions, and read everything.

So the proof is an HMAC over a transcript that includes both public keys:

```
K     = PBKDF2-HMAC-SHA256(password, salt, 250000, 32)

viewer -> agent:  HMAC(K, "aegisdesk-auth-v1"  || sid || Pv || Pa)
agent -> viewer:  HMAC(K, "aegisdesk-auth-ack" || sid || Pv || Pa)
```

If the relay substitutes a key, the two sides disagree about `Pa` (or `Pv`), the
proofs don't match, and the agent hangs up. The agent's *return* proof closes
the other direction: the viewer knows it's talking to something that actually
holds the password verifier, not to a relay pretending. Both proofs use distinct
labels so neither can be replayed as the other.

The agent stores only `K` (plus the salt and iteration count) on disk. It never
sees or stores the password itself.

`tests/test_crypto_interop.py::test_proof_is_bound_to_keys` is the test that
holds this property in place, and the end-to-end suite verifies the return proof
on a live session.

---

## Nonce construction

12 bytes: a 4-byte direction prefix and an 8-byte big-endian counter.

```
agent -> viewer : 00 00 00 01 | counter
viewer -> agent : 00 00 00 02 | counter
```

Two consequences worth stating plainly:

1. The two directions can never collide on a nonce, which would be catastrophic
   for GCM.
2. A frame sent by A and bounced straight back at A carries A's own direction
   prefix, and is rejected before decryption is even attempted.

Counters start at 1. A receiver tracks the highest counter seen and a set of
recent ones; anything already seen, or more than 4096 behind the high-water
mark, is dropped. Out-of-order delivery inside that window is fine.

---

## Permissions

Every capability is a separate switch in the agent's config, checked on the
agent at the moment of use — not merely hidden in the viewer's UI:

```jsonc
"permissions": {
  "input": true,           // mouse and keyboard
  "clipboard": true,
  "files": true,
  "filesReadOnly": false,  // browse and download, no writes or deletes
  "fileJail": null,        // confine browsing to one folder
  "shell": true,
  "sysinfo": true,
  "processes": true,       // list and end tasks
  "lockWorkstation": true
}
```

A viewer that asks for a disabled capability gets an explicit refusal, and the
attempt is logged. Turning a permission off is a real boundary.

---

## Transparency guarantees

These are not configurable from the network, and that's deliberate:

- **Consent prompt** by default. Unattended access is opt-in, per device,
  requires a password of at least 8 characters, and shows up in the fleet list
  with a key icon so you can see at a glance which machines have it.
- **On-screen banner** whenever a session is live: pulsing red dot, the
  operator's name, and a **Disconnect** button that ends the session
  immediately from the remote side.
- **Tray icon** showing the device ID and active session count.
- **Two audit trails.** The relay records logins, sessions, enrollments, bytes
  transferred and durations. Each agent independently records its own sessions,
  auth successes and failures, shell starts and process kills. Neither can be
  edited from the viewer.

There is no stealth mode and no way to add one without editing the source. If
you fork this, please leave that alone.

---

## Operational recommendations

1. **Change the generated admin password** on first login.
2. **Give managers `operator`, not `admin`.** Admin can delete devices and
   rotate the enrollment key.
3. **Don't reuse access passwords across machines.** One per device means one
   compromised note doesn't open the whole fleet.
4. **Prefer Tailscale** over a public hostname. A relay nobody can reach is a
   relay nobody can attack.
5. **Never set `allowOpenEnroll`** on anything internet-facing.
6. **Set `trustProxy` only behind a proxy you control**, otherwise clients can
   forge `X-Forwarded-For` and poison your audit log.
7. **Back up `relay/data/`.** Losing it means re-enrolling every device.
8. **Read the audit log occasionally.** It's the control that catches the things
   the other controls missed.
9. **On staff-facing machines, leave consent on.** The tool being visible is
   what makes it acceptable to have installed.

---

## Cryptographic inventory

| Purpose | Algorithm | Notes |
|---|---|---|
| Key agreement | ECDH P-256 | Ephemeral per session, both sides |
| Key derivation | HKDF-SHA256 | Salt binds sid + both public keys |
| Session encryption | AES-256-GCM | 96-bit nonce, 128-bit tag |
| Access password | PBKDF2-HMAC-SHA256 | 250 000 iterations, 128-bit salt |
| Password proof | HMAC-SHA256 | Bound to sid + both public keys |
| Operator password | scrypt | N=16384, r=8, p=1 |
| Operator token | HMAC-SHA256 | Compact, expiring, server-secret-signed |
| Device token | 256-bit random | Constant-time comparison |
| Enrollment key | 192-bit random | Constant-time comparison, rotatable |

Python side: `cryptography` (OpenSSL). Browser side: WebCrypto.
Relay: Node's `crypto`. No hand-rolled primitives.

P-256 rather than X25519 for one practical reason: WebCrypto supports P-256
everywhere, and X25519 support is still uneven across browsers. The
interop test suite exists to prove the two stacks agree byte for byte.

---

## Reporting a problem

It's your repository. If you find something, open an issue on it — and if
someone else is running your relay, tell them before you publish.
