/**
 * Connects to a waiting quick-support session the way the browser viewer does,
 * authenticating with the one-time code the helped person reads aloud.
 *
 * usage: node tests/support_check.mjs <relayBase> <operator> <password> <code>
 */
import WebSocket from '../relay/node_modules/ws/index.js';

const [base, user, pass, code] = process.argv.slice(2);
const subtle = globalThis.crypto.subtle;
const te = new TextEncoder(), td = new TextDecoder();
const DATA_MAGIC = 0xD1, DIR_A2V = 1, DIR_V2A = 2;
const CH = { AUTH_RESPONSE: 0x02, AUTH_RESULT: 0x03, SCREEN_INFO: 0x10, TILE_FRAME: 0x11, INPUT: 0x20 };

const results = [];
const check = (n, ok, d = '') => { results.push({ n, ok, d }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  (${d})` : ''}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const cat = (...a) => { const t = a.reduce((s, x) => s + x.length, 0); const o = new Uint8Array(t); let i = 0; for (const x of a) { o.set(x, i); i += x.length; } return o; };
const u32be = (n) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, n >>> 0, false); return b; };
const b64e = (b) => Buffer.from(b).toString('base64');
const b64d = (s) => new Uint8Array(Buffer.from(s, 'base64'));
function deferred() { let res, rej; const p = new Promise((a, b) => { res = a; rej = b; }); return { p, res, rej }; }

const login = await (await fetch(`${base}/api/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: user, password: pass }),
})).json();
check('operator signed in', !!login.token);

const devs = (await (await fetch(`${base}/api/devices`, {
  headers: { Authorization: `Bearer ${login.token}` },
})).json()).devices;

const dev = devs.find((d) => d.ephemeral);
check('support session appears in the fleet list', !!dev,
  dev ? `id=${dev.deviceId} name=${JSON.stringify(dev.name)}` : 'none found');
if (!dev) { process.exit(1); }
check('flagged as a one-off, not a fleet member', dev.ephemeral === true);
check('advertises that it needs a code', dev.unattended === true);
check('it is a throwaway with no group or tags', !dev.group && (!dev.tags || !dev.tags.length));

const ws = new WebSocket(base.replace(/^http/, 'ws') + '/ws/viewer');
ws.binaryType = 'arraybuffer';
const st = {
  open: deferred(), authRes: deferred(), screen: deferred(), frame: deferred(),
  sid: 0, key: null, sendCtr: 0, pub: null, priv: null, agentPub: null, frames: 0,
};
let recvChain = Promise.resolve();

ws.on('open', () => ws.send(JSON.stringify({ t: 'auth', token: login.token, label: 'support-test' })));
ws.on('message', (data, isBinary) => {
  if (isBinary) { const b = new Uint8Array(data); recvChain = recvChain.then(() => onData(b)).catch(() => {}); return; }
  const m = JSON.parse(data.toString());
  if (m.t === 'authed') {
    subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']).then(async (kp) => {
      st.priv = kp.privateKey;
      st.pub = new Uint8Array(await subtle.exportKey('raw', kp.publicKey));
      ws.send(JSON.stringify({ t: 'connect', deviceId: dev.deviceId, pub: b64e(st.pub) }));
    });
  } else if (m.t === 'session-open') st.open.res(m);
  else if (m.t === 'session-denied') st.open.rej(new Error(m.reason));
  else if (m.t === 'ping') ws.send(JSON.stringify({ t: 'pong', ts: m.ts }));
});

const open = await st.open.p;
st.sid = open.sid;
st.agentPub = b64d(open.agentPub);
check('session opened with no one clicking Accept', open.authRequired === true,
  'the code they read out is the consent');

const peer = await subtle.importKey('raw', st.agentPub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
const shared = new Uint8Array(await subtle.deriveBits({ name: 'ECDH', public: peer }, st.priv, 256));
const salt = new Uint8Array(await subtle.digest('SHA-256',
  cat(te.encode('aegisdesk-v1-salt'), u32be(st.sid), st.pub, st.agentPub)));
const hk = await subtle.importKey('raw', shared, 'HKDF', false, ['deriveBits']);
const bits = new Uint8Array(await subtle.deriveBits(
  { name: 'HKDF', hash: 'SHA-256', salt, info: te.encode('aegisdesk-v1-data') }, hk, 256));
st.key = await subtle.importKey('raw', bits, 'AES-GCM', false, ['encrypt', 'decrypt']);

async function proof(secret, label) {
  const pk = await subtle.importKey('raw', te.encode(secret), 'PBKDF2', false, ['deriveBits']);
  const dk = new Uint8Array(await subtle.deriveBits(
    { name: 'PBKDF2', hash: 'SHA-256', salt: b64d(open.salt), iterations: open.iterations }, pk, 256));
  const k = await subtle.importKey('raw', dk, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await subtle.sign('HMAC', k, cat(te.encode(label), u32be(st.sid), st.pub, st.agentPub)));
}

async function send(channel, payload) {
  st.sendCtr += 1;
  const nonce = new Uint8Array(12);
  const dv = new DataView(nonce.buffer);
  dv.setUint32(0, DIR_V2A, false);
  dv.setBigUint64(4, BigInt(st.sendCtr), false);
  const ct = new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv: nonce, tagLength: 128 },
    st.key, cat(new Uint8Array([channel]), payload)));
  ws.send(cat(new Uint8Array([DATA_MAGIC]), u32be(st.sid), nonce, ct));
}

async function onData(buf) {
  if (buf[0] !== DATA_MAGIC || !st.key) return;
  const nonce = buf.subarray(5, 17);
  if (new DataView(nonce.buffer, nonce.byteOffset, 12).getUint32(0, false) !== DIR_A2V) return;
  let plain;
  try {
    plain = new Uint8Array(await subtle.decrypt({ name: 'AES-GCM', iv: nonce, tagLength: 128 }, st.key, buf.subarray(17)));
  } catch { return; }
  const ch = plain[0], payload = plain.subarray(1);
  if (ch === CH.AUTH_RESULT) st.authRes.res(JSON.parse(td.decode(payload)));
  else if (ch === CH.SCREEN_INFO) st.screen.res(JSON.parse(td.decode(payload)));
  else if (ch === CH.TILE_FRAME) { st.frames++; st.frame.res(payload.length); }
}

// a wrong code must be refused even though the session is a one-off
await send(CH.AUTH_RESPONSE, te.encode(JSON.stringify({ proof: b64e(await proof('00000000', 'aegisdesk-auth-v1')) })));
const bad = await st.authRes.p;
check('wrong code refused', bad.ok === false, `${bad.reason}, ${bad.attemptsLeft} left`);

st.authRes = deferred();
await send(CH.AUTH_RESPONSE, te.encode(JSON.stringify({ proof: b64e(await proof(code, 'aegisdesk-auth-v1')) })));
const good = await st.authRes.p;
check('the code they read out unlocks the session', good.ok === true);
const ack = await proof(code, 'aegisdesk-auth-ack');
check('agent proved it holds the same code (anti-MITM)',
  Buffer.compare(Buffer.from(b64d(good.proof || '')), Buffer.from(ack)) === 0);

const info = await st.screen.p;
check('their screen is described', info.monitors.length > 0, `${info.w}x${info.h}`);
const first = await st.frame.p;
check('their screen is streaming', first > 0, `first frame ${first} B`);
await sleep(1500);
check('stream continues', st.frames > 3, `${st.frames} frames`);

await send(CH.INPUT, te.encode(JSON.stringify({ k: 'm', x: 0.5, y: 0.5 })));
check('input accepted', true);

ws.close();
const passed = results.filter((r) => r.ok).length;
console.log(`\n  ${passed}/${results.length} support-flow checks passed`);
process.exit(results.some((r) => !r.ok) ? 1 : 0);
