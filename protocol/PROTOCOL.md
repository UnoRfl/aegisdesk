# AegisDesk Wire Protocol v1

Three parties:

| Party  | Role |
|--------|------|
| Agent  | Runs on the controlled machine. Captures screen, injects input. |
| Relay  | Dumb broker. Knows *who* is talking to *whom*, never *what* they say. |
| Viewer | Browser app the operator uses. |

Everything rides on a single WebSocket per party. Two planes share that socket:

* **Control plane** — UTF-8 JSON text frames. The relay reads and acts on these.
* **Data plane** — binary frames. The relay forwards them byte-for-byte and
  cannot read them (AES-256-GCM, key known only to agent + viewer).

---

## 1. Control plane

### 1.1 Agent -> Relay  (`wss://relay/ws/agent`)

```jsonc
// First frame. enrollKey only needed the very first time.
{ "t":"register", "deviceId":"123456789"|null, "deviceToken":"...", "enrollKey":"...",
  "name":"POS-01", "os":"Windows 11 (10.0.22631)", "arch":"AMD64",
  "agentVersion":"1.0.0", "unattended":true,
  "caps":["screen","input","clipboard","files","shell","sysinfo","multimon"] }

{ "t":"session-accept", "sid":123456, "pub":"<b64 P-256 raw pubkey>",
  "authRequired":true, "salt":"<b64 16B>", "iterations":250000 }
{ "t":"session-reject", "sid":123456, "reason":"declined_by_user" }
{ "t":"session-closed", "sid":123456 }
{ "t":"heartbeat", "metrics":{ "cpu":12.4, "mem":48.1, "uptime":81234 } }
{ "t":"pong", "ts":1690000000000 }
```

### 1.2 Relay -> Agent

```jsonc
{ "t":"registered", "deviceId":"123456789", "deviceToken":"...", "serverTime":169... }
{ "t":"session-request", "sid":123456, "operator":"racquel",
  "operatorLabel":"Racquel (iPhone, 24.5.6.7)", "pub":"<b64 viewer pubkey>",
  "unattendedRequested":true }
{ "t":"session-close", "sid":123456, "reason":"viewer_gone" }
{ "t":"config", "pingIntervalMs":20000 }
{ "t":"error", "code":"bad_enroll_key", "message":"..." }
{ "t":"ping", "ts":169... }
```

### 1.3 Viewer -> Relay  (`wss://relay/ws/viewer`)

```jsonc
{ "t":"auth", "token":"<operator token from POST /api/login>" }
{ "t":"connect", "deviceId":"123456789", "pub":"<b64 viewer pubkey>", "label":"iPhone" }
{ "t":"close", "sid":123456 }
{ "t":"pong", "ts":169... }
```

### 1.4 Relay -> Viewer

```jsonc
{ "t":"authed", "operator":"racquel", "role":"admin" }
{ "t":"devices", "devices":[ { "deviceId":"123456789", "name":"POS-01",
    "online":true, "os":"...", "group":"Front of house", "tags":["pos"],
    "lastSeen":169..., "unattended":true, "inSession":false, "metrics":{...} } ] }
{ "t":"session-open", "sid":123456, "deviceId":"123456789",
  "agentPub":"<b64>", "authRequired":true, "salt":"<b64>", "iterations":250000 }
{ "t":"session-denied", "sid":123456, "reason":"declined_by_user"|"offline"|"timeout" }
{ "t":"session-close", "sid":123456, "reason":"agent_gone" }
{ "t":"error", "code":"...", "message":"..." }
```

---

## 2. Data plane

### 2.1 Outer envelope (what the relay sees)

```
byte  0        : 0xD1                  magic
bytes 1..4     : sid, uint32 big-endian
bytes 5..16    : nonce, 12 bytes  (4-byte direction prefix || 8-byte counter BE)
bytes 17..end  : AES-256-GCM ciphertext || 16-byte tag
```

Direction prefix: `0x00000001` agent->viewer, `0x00000002` viewer->agent.
The counter starts at 1 and increments per frame. A receiver MUST reject a
counter it has already seen (replay protection) and MUST reject a frame whose
direction prefix matches its own sending direction (reflection protection).

The relay validates only: `byte0 == 0xD1`, the sid belongs to this socket's
session, and `len <= maxFrameBytes`. It then writes the identical buffer to the
peer socket.

### 2.2 Key agreement

1. Viewer generates an ephemeral P-256 keypair, sends the raw (uncompressed,
   65-byte) public key in `connect`.
2. Agent generates its own ephemeral P-256 keypair, replies in `session-accept`.
3. Both compute `Z = ECDH(priv, peerPub)` (32 bytes), then

```
salt = SHA256( "aegisdesk-v1-salt" || sid_be32 || viewerPub || agentPub )
key  = HKDF-SHA256(ikm=Z, salt=salt, info="aegisdesk-v1-data", len=32)
```

The relay could substitute its own public keys (it is the one relaying them),
so a relay operator *can* MITM an unauthenticated session. Password
authentication (2.3) closes that hole: the proof is bound to both public keys,
so a substituted key produces an invalid proof and the agent hangs up.

### 2.3 Password authentication (inside the encrypted channel)

Only when the agent has an unattended password set (`authRequired: true`).

```
K = PBKDF2-HMAC-SHA256(password, salt, iterations, dkLen=32)

viewer proof = HMAC-SHA256(K, "aegisdesk-auth-v1"  || sid_be32 || viewerPub || agentPub)
agent  proof = HMAC-SHA256(K, "aegisdesk-auth-ack" || sid_be32 || viewerPub || agentPub)
```

The agent stores only `K` (plus salt and iteration count) on disk — never the
password. It compares proofs in constant time, and returns its own proof so the
viewer can confirm it is talking to the real agent and not the relay.

### 2.4 Inner messages (after decryption)

```
byte 0 : channel id
rest   : channel payload
```

| ID   | Name           | Direction | Payload |
|------|----------------|-----------|---------|
| 0x01 | `AUTH_CHALLENGE` | A->V | JSON `{salt, iterations, attemptsLeft}` |
| 0x02 | `AUTH_RESPONSE`  | V->A | JSON `{proof}` |
| 0x03 | `AUTH_RESULT`    | A->V | JSON `{ok, proof?, reason?, attemptsLeft?}` |
| 0x10 | `SCREEN_INFO`    | A->V | JSON `{monitors:[{id,w,h,x,y,primary}], active, w, h, scale}` |
| 0x11 | `TILE_FRAME`     | A->V | binary, see 2.5 |
| 0x12 | `CURSOR`         | A->V | JSON `{x, y, visible, shape}` |
| 0x20 | `INPUT`          | V->A | JSON, see 2.6 |
| 0x30 | `CLIPBOARD`      | both | JSON `{text}` |
| 0x40 | `FILE_CTL`       | both | JSON, see 2.7 |
| 0x41 | `FILE_DATA`      | both | binary `uint32 xferId, uint32 seq, bytes` |
| 0x50 | `SHELL_CTL`      | V->A | JSON `{op:"start"|"stdin"|"resize"|"kill", ...}` |
| 0x51 | `SHELL_OUT`      | A->V | binary `uint8 stream(1=out,2=err,3=exit), bytes` |
| 0x60 | `SYSINFO`        | both | JSON — request `{}` / reply `{...}` |
| 0x70 | `CONTROL`        | V->A | JSON, see 2.8 |
| 0x71 | `STATUS`         | A->V | JSON `{level, message}` |
| 0x7E | `PING`           | both | binary `uint64 tsMillis` |
| 0x7F | `PONG`           | both | binary `uint64 tsMillis` (echo) |

### 2.5 TILE_FRAME

```
uint8   monitorId
uint8   codec        1 = JPEG, 2 = PNG (used for tiny/flat tiles)
uint8   flags        bit0 = keyframe (viewer should clear its canvas first)
uint16  frameSeq
uint16  frameW       full logical frame size, post-scaling
uint16  frameH
uint16  tileCount
repeat tileCount times:
    uint16 x, uint16 y, uint16 w, uint16 h, uint32 byteLen, bytes image
```

### 2.6 INPUT

```jsonc
{ "k":"m",  "x":0.5123, "y":0.4410 }                  // move, normalized 0..1
{ "k":"md", "b":0, "x":0.5, "y":0.4 }                 // button down 0=L 1=M 2=R 3=X1 4=X2
{ "k":"mu", "b":0, "x":0.5, "y":0.4 }
{ "k":"w",  "dx":0, "dy":-120, "x":0.5, "y":0.4 }     // wheel, WHEEL_DELTA units
{ "k":"kd", "c":"KeyA", "r":false }                   // c = DOM KeyboardEvent.code
{ "k":"ku", "c":"KeyA" }
{ "k":"txt","s":"héllo" }                             // unicode injection
{ "k":"combo","keys":["ControlLeft","AltLeft","Delete"] }
```

### 2.7 FILE_CTL

```jsonc
{ "op":"list", "path":"C:\\Users" }
{ "op":"list-result", "path":"...", "sep":"\\", "entries":[{"n":"x.txt","d":false,"s":123,"m":169...}] }
{ "op":"roots" } / { "op":"roots-result", "roots":[...], "home":"C:\\Users\\pos" }
{ "op":"get", "xferId":7, "path":"C:\\a.txt" }
{ "op":"get-begin", "xferId":7, "size":12345, "name":"a.txt" }
{ "op":"put", "xferId":8, "path":"C:\\b.txt", "size":999 }
{ "op":"put-ready", "xferId":8 }
{ "op":"ack", "xferId":7, "seq":16 }        // flow control, every 16 chunks
{ "op":"done", "xferId":7 }
{ "op":"cancel", "xferId":7 }
{ "op":"error", "xferId":7, "message":"..." }
{ "op":"mkdir", "path":"..." } / { "op":"delete", "path":"..." } / { "op":"rename", "path":"...", "to":"..." }
```

Chunk size is 128 KiB. The sender may have at most 32 unacknowledged chunks
in flight.

### 2.8 CONTROL

```jsonc
{ "op":"quality", "mode":"auto"|"speed"|"balanced"|"quality", "maxWidth":1600, "maxFps":30 }
{ "op":"monitor", "id":1 }
{ "op":"keyframe" }                  // resend everything
{ "op":"cad" }                       // Ctrl+Alt+Del (Windows, needs elevation)
{ "op":"lock" }                      // lock the remote workstation
{ "op":"blank", "on":true }          // privacy screen, if supported
{ "op":"pause", "on":true }          // stop sending frames
{ "op":"disconnect" }
```

---

## 3. HTTP API (relay)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/login` | — | `{username,password}` -> `{token, role, expiresAt}` |
| GET  | `/api/me` | token | current operator |
| GET  | `/api/devices` | token | fleet list |
| PATCH| `/api/devices/:id` | token | `{name,group,tags,notes}` |
| DELETE | `/api/devices/:id` | admin | de-enroll |
| GET  | `/api/audit?limit=200` | admin | session + auth audit log |
| GET  | `/api/operators` | admin | list |
| POST | `/api/operators` | admin | `{username,password,role}` |
| DELETE | `/api/operators/:username` | admin | remove |
| POST | `/api/password` | token | `{current,next}` change own password |
| GET  | `/api/enroll-key` | admin | current enrollment key |
| POST | `/api/enroll-key/rotate` | admin | new enrollment key |
| GET  | `/healthz` | — | `{ok:true, devices:n, online:n}` |

Tokens are `base64url(payload).base64url(HMAC-SHA256(secret, payload))`.
