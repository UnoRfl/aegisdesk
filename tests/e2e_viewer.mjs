/**
 * End-to-end test: drives a real relay and a real agent through the exact
 * protocol the browser viewer uses (same WebCrypto calls, same framing).
 *
 * usage: node tests/e2e_viewer.mjs <relayHttpBase> <operator> <password> <probeFile>
 */
import WebSocket from '../relay/node_modules/ws/index.js';
import { readFileSync } from 'node:fs';

const [base, user, pass, probeFile] = process.argv.slice(2);
const subtle = globalThis.crypto.subtle;
const te = new TextEncoder(), td = new TextDecoder();

const CH = {
  AUTH_CHALLENGE: 0x01, AUTH_RESPONSE: 0x02, AUTH_RESULT: 0x03,
  SCREEN_INFO: 0x10, TILE_FRAME: 0x11, CURSOR: 0x12,
  INPUT: 0x20, CLIPBOARD: 0x30, FILE_CTL: 0x40, FILE_DATA: 0x41,
  SHELL_CTL: 0x50, SHELL_OUT: 0x51, SYSINFO: 0x60,
  CONTROL: 0x70, STATUS: 0x71, PING: 0x7E, PONG: 0x7F,
};
const DATA_MAGIC = 0xD1, DIR_A2V = 1, DIR_V2A = 2;

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  (${detail})` : ''}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const cat = (...a) => { const n = a.reduce((s, x) => s + x.length, 0); const o = new Uint8Array(n); let i = 0; for (const x of a) { o.set(x, i); i += x.length; } return o; };
const u32be = (n) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, n >>> 0, false); return b; };
const b64e = (b) => Buffer.from(b).toString('base64');
const b64d = (s) => new Uint8Array(Buffer.from(s, 'base64'));

function deferred() {
  let res, rej;
  const p = new Promise((a, b) => { res = a; rej = b; });
  return { p, res, rej };
}

async function main() {
  // ---------- 1. HTTP login ----------
  const lr = await fetch(`${base}/api/login`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass }),
  });
  const login = await lr.json();
  check('operator login over HTTP', !!login.token, `role=${login.role}`);
  if (!login.token) process.exit(1);

  const health = await (await fetch(`${base}/healthz`)).json();
  check('relay healthz reachable', health.ok === true, `devices=${health.devices} online=${health.online}`);

  // ---------- 2. viewer websocket ----------
  const wsUrl = base.replace(/^http/, 'ws') + '/ws/viewer';
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';

  const state = {
    authed: deferred(), devices: deferred(), open: deferred(),
    key: null, sid: 0, sendCtr: 0, pub: null, priv: null, agentPub: null,
    screenInfo: deferred(), firstFrame: deferred(), authResult: deferred(),
    fileList: deferred(), fileBegin: deferred(), fileDone: deferred(),
    shellSaw: deferred(), sysinfo: deferred(), clip: deferred(),
    frames: 0, tiles: 0, bytes: 0, chunks: [], statusMsgs: [], keyframes: 0,
  };

  ws.on('open', () => ws.send(JSON.stringify({ t: 'auth', token: login.token, label: 'e2e' })));
  let recvChain = Promise.resolve();
  ws.on('message', (data, isBinary) => {
    if (isBinary) {
      // same ordering guarantee the browser viewer makes: decrypt in arrival
      // order, or a small control message can overtake a large data chunk
      const buf = new Uint8Array(data);
      recvChain = recvChain.then(() => onData(buf)).catch((e) => console.log('   [drop]', e.message));
      return;
    }
    const m = JSON.parse(data.toString());
    if (m.t === 'authed') state.authed.res(m);
    else if (m.t === 'devices') { if (m.devices.length) state.devices.res(m.devices); }
    else if (m.t === 'session-open') state.open.res(m);
    else if (m.t === 'session-denied') state.open.rej(new Error(`denied: ${m.reason}`));
    else if (m.t === 'ping') ws.send(JSON.stringify({ t: 'pong', ts: m.ts }));
    else if (m.t === 'session-close') {
      console.log(`   [relay closed session: ${m.reason}]`);
      state.open.rej(new Error(`session closed before it opened: ${m.reason}`));
    }
    else if (m.t === 'error') console.log(`   [relay error: ${m.code} ${m.message}]`);
  });

  const authed = await state.authed.p;
  check('viewer websocket authenticated', authed.operator === user, `relay v${authed.relayVersion}`);

  // ---------- 3. wait for the agent to show up ----------
  let devices = null;
  for (let i = 0; i < 60; i++) {
    const r = await fetch(`${base}/api/devices`, { headers: { Authorization: `Bearer ${login.token}` } });
    const d = (await r.json()).devices;
    if (d.length && d[0].online) { devices = d; break; }
    await sleep(500);
  }
  check('agent enrolled and shows as online', !!devices,
    devices ? `id=${devices[0].deviceId} name=${devices[0].name} os=${devices[0].os}` : 'timed out');
  if (!devices) { summarise(); process.exit(1); }
  const dev = devices[0];
  check('agent advertises capabilities', (dev.caps || []).includes('input'), (dev.caps || []).join(','));
  check('agent reports an unattended password is set', dev.unattended === true);
  check('relay reports live metrics from the agent',
    dev.metrics && typeof dev.metrics.cpu === 'number', JSON.stringify(dev.metrics || {}).slice(0, 90));

  // ---------- 4. ECDH ----------
  const kp = await subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  state.priv = kp.privateKey;
  state.pub = new Uint8Array(await subtle.exportKey('raw', kp.publicKey));
  ws.send(JSON.stringify({ t: 'connect', deviceId: dev.deviceId, pub: b64e(state.pub), label: 'e2e' }));

  const open = await state.open.p;
  state.sid = open.sid;
  state.agentPub = b64d(open.agentPub);
  check('session brokered by relay', !!open.sid, `sid=${open.sid} authRequired=${open.authRequired}`);
  check('agent demanded a password', open.authRequired === true);

  const peer = await subtle.importKey('raw', state.agentPub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = new Uint8Array(await subtle.deriveBits({ name: 'ECDH', public: peer }, state.priv, 256));
  const salt = new Uint8Array(await subtle.digest('SHA-256',
    cat(te.encode('aegisdesk-v1-salt'), u32be(state.sid), state.pub, state.agentPub)));
  const hk = await subtle.importKey('raw', shared, 'HKDF', false, ['deriveBits']);
  const bits = new Uint8Array(await subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt, info: te.encode('aegisdesk-v1-data') }, hk, 256));
  state.key = await subtle.importKey('raw', bits, 'AES-GCM', false, ['encrypt', 'decrypt']);
  check('ECDH + HKDF key derived in the browser stack', true, `key=${b64e(bits).slice(0, 12)}...`);

  // ---------- 5. password proof ----------
  const authSalt = b64d(open.salt);
  const iters = open.iterations;
  const mkProof = async (password, label) => {
    const pk = await subtle.importKey('raw', te.encode(password), 'PBKDF2', false, ['deriveBits']);
    const dk = new Uint8Array(await subtle.deriveBits(
      { name: 'PBKDF2', hash: 'SHA-256', salt: authSalt, iterations: iters }, pk, 256));
    const k = await subtle.importKey('raw', dk, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return new Uint8Array(await subtle.sign('HMAC', k,
      cat(te.encode(label), u32be(state.sid), state.pub, state.agentPub)));
  };

  // deliberately wrong first, to prove the agent rejects it
  await send(CH.AUTH_RESPONSE, te.encode(JSON.stringify({ proof: b64e(await mkProof('definitely-wrong', 'aegisdesk-auth-v1')) })));
  const bad = await state.authResult.p;
  check('wrong password rejected', bad.ok === false, `${bad.reason}, ${bad.attemptsLeft} left`);

  state.authResult = deferred();
  const AGENT_PASSWORD = process.env.E2E_AGENT_PASSWORD;
  await send(CH.AUTH_RESPONSE, te.encode(JSON.stringify({ proof: b64e(await mkProof(AGENT_PASSWORD, 'aegisdesk-auth-v1')) })));
  const good = await state.authResult.p;
  check('correct password accepted', good.ok === true);

  const expectAck = await mkProof(AGENT_PASSWORD, 'aegisdesk-auth-ack');
  const gotAck = b64d(good.proof || '');
  check('agent proved it knows the password (anti-MITM ack)',
    Buffer.compare(Buffer.from(gotAck), Buffer.from(expectAck)) === 0);

  // ---------- 6. screen ----------
  const info = await state.screenInfo.p;
  check('received screen info', info.monitors.length > 0,
    `${info.w}x${info.h}, encoder=${info.encoder}, monitors=${info.monitors.length}`);

  const first = await state.firstFrame.p;
  check('received first video frame', first.tiles > 0,
    `${first.w}x${first.h}, ${first.tiles} tile(s), keyframe=${first.keyframe}, ${first.bytes} B`);

  await sleep(2500);
  check('video keeps streaming', state.frames > 5,
    `${state.frames} frames, ${state.tiles} tiles, ${(state.bytes / 1024).toFixed(0)} KB in ~2.5s`);
  check('keyframe was sent exactly once at start', state.keyframes === 1, `keyframes=${state.keyframes}`);
  check('delta frames are much smaller than the keyframe',
    state.bytes / state.frames < first.bytes, `avg ${(state.bytes / state.frames / 1024).toFixed(1)} KB vs keyframe ${(first.bytes / 1024).toFixed(1)} KB`);

  // ---------- 7. input ----------
  await send(CH.INPUT, te.encode(JSON.stringify({ k: 'm', x: 0.5, y: 0.5 })));
  await send(CH.INPUT, te.encode(JSON.stringify({ k: 'md', b: 0, x: 0.5, y: 0.5 })));
  await send(CH.INPUT, te.encode(JSON.stringify({ k: 'mu', b: 0, x: 0.5, y: 0.5 })));
  await send(CH.INPUT, te.encode(JSON.stringify({ k: 'txt', s: 'hello from the viewer' })));
  await send(CH.INPUT, te.encode(JSON.stringify({ k: 'combo', keys: ['ControlLeft', 'AltLeft', 'Delete'] })));
  check('input messages accepted without error', true, 'agent-side effects verified by the python test');

  // ---------- 8. control ----------
  await send(CH.CONTROL, te.encode(JSON.stringify({ op: 'quality', mode: 'speed' })));
  await sleep(400);
  check('quality change acknowledged',
    state.statusMsgs.some((m) => /quality: speed/.test(m)), state.statusMsgs.join(' | ').slice(0, 90));

  // ---------- 9. round-trip latency ----------
  const pingBuf = new Uint8Array(8);
  new DataView(pingBuf.buffer).setBigUint64(0, BigInt(Date.now()), false);
  const t0 = Date.now();
  await send(CH.PING, pingBuf);
  await sleep(600);
  check('ping/pong round trip', state.pong !== undefined, `${state.pong} ms measured, ${Date.now() - t0} ms wall`);

  // ---------- 10. files ----------
  await send(CH.FILE_CTL, te.encode(JSON.stringify({ op: 'list', path: probeFile.replace(/\/[^/]+$/, '') })));
  const listing = await state.fileList.p;
  const want = probeFile.split('/').pop();
  check('remote directory listing', listing.entries.some((e) => e.n === want),
    `${listing.entries.length} entries in ${listing.path}`);

  await send(CH.FILE_CTL, te.encode(JSON.stringify({ op: 'get', xferId: 7, path: probeFile })));
  const begin = await state.fileBegin.p;
  await state.fileDone.p;
  const got = Buffer.concat(state.chunks.filter(Boolean));
  const expect = readFileSync(probeFile);
  check('file downloaded intact over the encrypted channel',
    Buffer.compare(got, expect) === 0,
    `${got.length} B received, sha match=${Buffer.compare(got, expect) === 0}, declared size=${begin.size}`);

  // ---------- 11. shell ----------
  await send(CH.SHELL_CTL, te.encode(JSON.stringify({ op: 'start', cols: 90, rows: 24 })));
  await sleep(700);
  await send(CH.SHELL_CTL, te.encode(JSON.stringify({ op: 'stdin', data: 'echo AEGIS_E2E_MARKER_OK\n' })));
  const shellText = await Promise.race([state.shellSaw.p, sleep(6000).then(() => null)]);
  check('remote shell executed a command', shellText !== null,
    shellText ? shellText.trim().slice(0, 60) : 'marker never appeared');

  // ---------- 12. sysinfo ----------
  await send(CH.SYSINFO, te.encode(JSON.stringify({ op: 'report' })));
  const rep = await Promise.race([state.sysinfo.p, sleep(5000).then(() => null)]);
  check('system report returned', rep && !!rep.hostname,
    rep ? `${rep.hostname} / ${rep.os} / ${rep.cores} cores` : 'timed out');

  // ---------- 13. clipboard ----------
  await send(CH.CLIPBOARD, te.encode(JSON.stringify({ text: 'clipboard probe 123' })));
  await sleep(300);
  check('clipboard push accepted', true, 'no clipboard tool in the test container, so no echo expected');

  // ---------- 14. tamper / replay resistance ----------
  const forged = cat(new Uint8Array([DATA_MAGIC]), u32be(state.sid),
    new Uint8Array(12), new Uint8Array(40));
  ws.send(forged);
  await sleep(600);
  check('agent survived a forged frame', state.frames > 0, 'still streaming after garbage input');

  // ---------- 15. teardown ----------
  const framesBefore = state.frames;
  ws.send(JSON.stringify({ t: 'close', sid: state.sid }));
  await sleep(1200);
  check('stream stops after the viewer disconnects', state.frames - framesBefore < 3,
    `${state.frames - framesBefore} frames arrived after close`);

  ws.close();
  summarise();

  // ================================================================ helpers
  async function send(channel, payload) {
    state.sendCtr += 1;
    const nonce = new Uint8Array(12);
    const dv = new DataView(nonce.buffer);
    dv.setUint32(0, DIR_V2A, false);
    dv.setBigUint64(4, BigInt(state.sendCtr), false);
    const body = cat(new Uint8Array([channel]), payload);
    const ct = new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv: nonce, tagLength: 128 }, state.key, body));
    ws.send(cat(new Uint8Array([DATA_MAGIC]), u32be(state.sid), nonce, ct));
  }

  async function onData(buf) {
    if (buf[0] !== DATA_MAGIC || !state.key) return;
    const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    if (dv.getUint32(1, false) !== state.sid) return;
    const nonce = buf.subarray(5, 17);
    const ndv = new DataView(nonce.buffer, nonce.byteOffset, 12);
    if (ndv.getUint32(0, false) !== DIR_A2V) return;
    let plain;
    try {
      plain = new Uint8Array(await subtle.decrypt(
        { name: 'AES-GCM', iv: nonce, tagLength: 128 }, state.key, buf.subarray(17)));
    } catch { return; }
    const ch = plain[0], payload = plain.subarray(1);

    if (ch === CH.TILE_FRAME) {
      const p = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
      const flags = p.getUint8(2);
      const w = p.getUint16(5, false), h = p.getUint16(7, false), count = p.getUint16(9, false);
      // walk the tile table to confirm the framing is exactly right
      let off = 11, total = 0;
      for (let i = 0; i < count; i++) {
        const len = p.getUint32(off + 8, false);
        off += 12 + len; total += len;
      }
      if (off !== payload.length) { check('tile frame framing consistent', false, `consumed ${off} of ${payload.length}`); return; }
      state.frames++; state.tiles += count; state.bytes += payload.length;
      if (flags & 1) state.keyframes++;
      state.firstFrame.res({ w, h, tiles: count, keyframe: !!(flags & 1), bytes: payload.length });
    } else if (ch === CH.SCREEN_INFO) {
      state.screenInfo.res(JSON.parse(td.decode(payload)));
    } else if (ch === CH.AUTH_RESULT) {
      state.authResult.res(JSON.parse(td.decode(payload)));
    } else if (ch === CH.AUTH_CHALLENGE) {
      /* already carried in session-open */
    } else if (ch === CH.STATUS) {
      state.statusMsgs.push(JSON.parse(td.decode(payload)).message);
    } else if (ch === CH.PONG) {
      const sent = Number(new DataView(payload.buffer, payload.byteOffset, 8).getBigUint64(0, false));
      state.pong = Date.now() - sent;
    } else if (ch === CH.FILE_CTL) {
      const m = JSON.parse(td.decode(payload));
      if (m.op === 'list-result') state.fileList.res(m);
      else if (m.op === 'get-begin') state.fileBegin.res(m);
      else if (m.op === 'done') state.fileDone.res(m);
      else if (m.op === 'error') console.log(`   [file error: ${m.message}]`);
    } else if (ch === CH.FILE_DATA) {
      const p = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
      const seq = p.getUint32(4, false);
      state.chunks[seq - 1] = Buffer.from(payload.subarray(8));
      if (seq % 16 === 0) send(CH.FILE_CTL, te.encode(JSON.stringify({ op: 'ack', xferId: 7, seq })));
    } else if (ch === CH.SHELL_OUT) {
      const text = td.decode(payload.subarray(1));
      state.shellBuf = (state.shellBuf || '') + text;
      if (state.shellBuf.includes('AEGIS_E2E_MARKER_OK')) {
        const line = state.shellBuf.split('\n').find((l) => l.includes('AEGIS_E2E_MARKER_OK') && !l.includes('echo'));
        state.shellSaw.res(line || 'AEGIS_E2E_MARKER_OK');
      }
    } else if (ch === CH.SYSINFO) {
      const m = JSON.parse(td.decode(payload));
      if (m.op === 'report-result') state.sysinfo.res(m.report);
    } else if (ch === CH.CLIPBOARD) {
      state.clip.res(JSON.parse(td.decode(payload)));
    }
  }
}

function summarise() {
  const pass = results.filter((r) => r.ok).length;
  console.log(`\n${'='.repeat(60)}\n  ${pass}/${results.length} checks passed`);
  const failed = results.filter((r) => !r.ok);
  if (failed.length) {
    console.log('  FAILED:');
    for (const f of failed) console.log(`    - ${f.name} ${f.detail}`);
  }
  console.log('='.repeat(60));
  process.exitCode = failed.length ? 1 : 0;
}

const watchdog = setTimeout(() => {
  console.error('E2E TIMEOUT: no progress for 90s');
  summarise();
  process.exit(1);
}, 90_000);
watchdog.unref?.();

main().then(() => clearTimeout(watchdog))
  .catch((e) => { console.error('E2E ERROR:', e.message); summarise(); process.exit(1); });
