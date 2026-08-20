#!/usr/bin/env node
'use strict';
/**
 * AegisDesk relay server.
 *
 * Responsibilities:
 *   - keep a registry of enrolled agents and which ones are online
 *   - authenticate operators, serve the browser viewer + admin console
 *   - broker sessions between a viewer and an agent
 *   - forward encrypted data-plane frames byte-for-byte
 *
 * It deliberately cannot decrypt session traffic. Keep it that way.
 */
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { WebSocketServer } = require('ws');

const { Store } = require('./lib/store');
const {
  signToken, verifyToken, verifyPassword, randomDigits, randomKey, log, setLogLevel,
} = require('./lib/util');
const {
  Router, sendJson, sendText, readJson, serveStatic, RateLimiter, clientIp,
} = require('./lib/http');

const VERSION = '1.0.0';
const DATA_MAGIC = 0xd1;
const CONSENT_TIMEOUT_MS = 60_000;
const PING_INTERVAL_MS = 20_000;
const PING_TIMEOUT_MS = 50_000;

// ---------------------------------------------------------------- config

function loadConfig() {
  const argv = process.argv.slice(2);
  const cli = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) continue;
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) cli[key] = true;
    else { cli[key] = next; i++; }
  }
  let file = {};
  const cfgPath = cli.config || process.env.AEGIS_CONFIG || path.join(__dirname, 'config.json');
  try { file = JSON.parse(fs.readFileSync(cfgPath, 'utf8')); }
  catch (e) { if (e.code !== 'ENOENT') log('warn', `config ${cfgPath}: ${e.message}`); }

  const pick = (name, envName, dflt) =>
    cli[name] !== undefined ? cli[name]
      : process.env[envName] !== undefined ? process.env[envName]
        : file[name] !== undefined ? file[name] : dflt;

  const cfg = {
    port: Number(pick('port', 'PORT', 7443)),
    host: String(pick('host', 'AEGIS_HOST', '0.0.0.0')),
    dataDir: String(pick('data-dir', 'AEGIS_DATA_DIR', path.join(__dirname, 'data'))),
    publicDir: String(pick('public-dir', 'AEGIS_PUBLIC_DIR', path.join(__dirname, 'public'))),
    tlsCert: pick('tls-cert', 'AEGIS_TLS_CERT', null),
    tlsKey: pick('tls-key', 'AEGIS_TLS_KEY', null),
    maxFrameBytes: Number(pick('max-frame-bytes', 'AEGIS_MAX_FRAME', 8 * 1024 * 1024)),
    sessionTokenHours: Number(pick('session-hours', 'AEGIS_SESSION_HOURS', 12)),
    trustProxy: pick('trust-proxy', 'AEGIS_TRUST_PROXY', false) === true
      || String(pick('trust-proxy', 'AEGIS_TRUST_PROXY', 'false')) === 'true',
    allowOpenEnroll: String(pick('allow-open-enroll', 'AEGIS_OPEN_ENROLL', 'false')) === 'true',
    logLevel: String(pick('log', 'AEGIS_LOG', 'info')),

    // one-shot setup mode, for installers and headless provisioning
    init: cli.init === true,
    resetAdmin: cli['reset-admin'] === true,
    adminUser: String(pick('admin-user', 'AEGIS_ADMIN_USER', 'admin')),
    adminPassword: pick('admin-password', 'AEGIS_ADMIN_PASSWORD', null),
  };
  return cfg;
}

const cfg = loadConfig();
setLogLevel(cfg.logLevel);

let store;
try {
  store = new Store(cfg.dataDir);
} catch (e) {
  // Overwhelmingly this is a data dir that cannot be created: a path mangled by
  // a shell that did not quote a space, or a location needing admin rights.
  // A raw stack trace here is useless -- the window it printed into is usually
  // already gone.
  process.stderr.write(
    `\nAegisDesk relay could not start.\n\n` +
    `  Could not use the data directory:\n    ${cfg.dataDir}\n\n` +
    `  ${e.message}\n\n` +
    `  If that path looks truncated, something dropped the quotes around it --\n` +
    `  omit --data-dir entirely and the relay will use its own folder.\n` +
    `  Otherwise pick a writable location, e.g. --data-dir ./data\n\n`);
  process.exit(1);
}

// ---------------------------------------------------------------- live state

/** deviceId -> ws */
const agents = new Map();
/** ws -> { operator, role, label, sessions:Set<sid> } */
const viewers = new Map();
/** sid -> session */
const sessions = new Map();

const loginLimiter = new RateLimiter(10, 5 * 60_000);
const enrollLimiter = new RateLimiter(20, 60 * 60_000);

function newSid() {
  for (;;) {
    const sid = crypto.randomBytes(4).readUInt32BE(0);
    if (sid !== 0 && !sessions.has(sid)) return sid;
  }
}

function send(ws, obj) {
  if (!ws || ws.readyState !== 1) return false;
  try { ws.send(JSON.stringify(obj)); return true; } catch { return false; }
}

function deviceListPayload() {
  const inSession = new Map();
  for (const s of sessions.values()) {
    if (s.state === 'open') inSession.set(s.deviceId, (inSession.get(s.deviceId) || 0) + 1);
  }
  return Object.values(store.devices()).map((d) => {
    const ws = agents.get(d.deviceId);
    return {
      deviceId: d.deviceId, name: d.name, os: d.os, arch: d.arch,
      agentVersion: d.agentVersion, caps: d.caps,
      group: d.group, tags: d.tags, notes: d.notes,
      online: !!ws, lastSeen: d.lastSeen, lastIp: d.lastIp,
      unattended: !!d.unattended, enrolledAt: d.enrolledAt,
      ephemeral: !!d.ephemeral,
      sessions: inSession.get(d.deviceId) || 0,
      metrics: ws ? ws.metrics || null : null,
    };
  }).sort((a, b) =>
    (b.online - a.online) ||
    (b.ephemeral - a.ephemeral) ||
    a.name.localeCompare(b.name));
}

let broadcastTimer = null;
function broadcastDevices() {
  if (broadcastTimer) return;
  broadcastTimer = setTimeout(() => {
    broadcastTimer = null;
    const payload = { t: 'devices', devices: deviceListPayload() };
    const msg = JSON.stringify(payload);
    for (const ws of viewers.keys()) {
      if (ws.readyState === 1) { try { ws.send(msg); } catch { /* ignore */ } }
    }
  }, 150);
  if (broadcastTimer.unref) broadcastTimer.unref();
}

function closeSession(sid, reason, notify = 'both') {
  const s = sessions.get(sid);
  if (!s) return;
  sessions.delete(sid);
  if (s.consentTimer) clearTimeout(s.consentTimer);
  const meta = viewers.get(s.viewerWs);
  if (meta) meta.sessions.delete(sid);
  if (s.agentWs) (s.agentWs.sessions || new Set()).delete(sid);
  if (notify === 'both' || notify === 'agent') send(s.agentWs, { t: 'session-close', sid, reason });
  if (notify === 'both' || notify === 'viewer') send(s.viewerWs, { t: 'session-close', sid, reason });
  store.audit({
    kind: 'session-end', sid, deviceId: s.deviceId, operator: s.operator, reason,
    durationMs: Date.now() - s.createdAt, bytesToViewer: s.bytesAV, bytesToAgent: s.bytesVA,
  });
  log('info', `session ${sid} closed (${reason}) device=${s.deviceId} op=${s.operator} ` +
    `${(s.bytesAV / 1048576).toFixed(1)}MB down / ${(s.bytesVA / 1024).toFixed(0)}KB up`);
  broadcastDevices();
}

// ---------------------------------------------------------------- HTTP API

const router = new Router();

function auth(req, requiredRole) {
  const hdr = req.headers.authorization || '';
  const token = hdr.startsWith('Bearer ') ? hdr.slice(7) : null;
  const payload = token && verifyToken(token, store.tokenSecret);
  if (!payload || payload.k !== 'operator') return null;
  const op = store.getOperator(payload.u);
  if (!op) return null;
  const rank = { viewer: 0, operator: 1, admin: 2 };
  if (requiredRole && rank[op.role] < rank[requiredRole]) return { forbidden: true };
  return { username: op.username, role: op.role };
}

router.post('/api/login', async (req, res) => {
  const ip = clientIp(req);
  if (!loginLimiter.check(ip)) return sendJson(res, 429, { error: 'too many attempts, wait 5 minutes' });
  const body = await readJson(req);
  const op = store.getOperator(body.username || '');
  const ok = op && verifyPassword(body.password || '', op.password);
  if (!ok) {
    store.audit({ kind: 'login-fail', username: String(body.username || '').slice(0, 64), ip });
    log('warn', `failed login for "${body.username}" from ${ip}`);
    return sendJson(res, 401, { error: 'invalid username or password' });
  }
  loginLimiter.reset(ip);
  op.lastLogin = Date.now();
  store.save();
  const exp = Date.now() + cfg.sessionTokenHours * 3600_000;
  const token = signToken({ k: 'operator', u: op.username, r: op.role, exp }, store.tokenSecret);
  store.audit({ kind: 'login', username: op.username, ip });
  log('info', `operator ${op.username} logged in from ${ip}`);
  sendJson(res, 200, { token, username: op.username, role: op.role, expiresAt: exp });
});

router.get('/api/me', (req, res) => {
  const a = auth(req);
  if (!a || a.forbidden) return sendJson(res, 401, { error: 'unauthorized' });
  sendJson(res, 200, { username: a.username, role: a.role, version: VERSION });
});

router.post('/api/password', async (req, res) => {
  const a = auth(req);
  if (!a || a.forbidden) return sendJson(res, 401, { error: 'unauthorized' });
  const body = await readJson(req);
  const op = store.getOperator(a.username);
  if (!verifyPassword(body.current || '', op.password)) return sendJson(res, 403, { error: 'current password is wrong' });
  try { store.setOperatorPassword(a.username, body.next || ''); }
  catch (e) { return sendJson(res, 400, { error: e.message }); }
  store.audit({ kind: 'password-change', username: a.username });
  sendJson(res, 200, { ok: true });
});

router.get('/api/devices', (req, res) => {
  const a = auth(req);
  if (!a || a.forbidden) return sendJson(res, 401, { error: 'unauthorized' });
  sendJson(res, 200, { devices: deviceListPayload() });
});

router.patch('/api/devices/:id', async (req, res, params) => {
  const a = auth(req, 'operator');
  if (!a) return sendJson(res, 401, { error: 'unauthorized' });
  if (a.forbidden) return sendJson(res, 403, { error: 'operator role required' });
  const d = store.getDevice(params.id);
  if (!d) return sendJson(res, 404, { error: 'no such device' });
  const body = await readJson(req);
  const patch = {};
  if (typeof body.name === 'string') patch.name = body.name.slice(0, 64).trim() || d.deviceId;
  if (typeof body.group === 'string') patch.group = body.group.slice(0, 64).trim();
  if (typeof body.notes === 'string') patch.notes = body.notes.slice(0, 2000);
  if (Array.isArray(body.tags)) patch.tags = body.tags.slice(0, 16).map((t) => String(t).slice(0, 32));
  Object.assign(d, patch);
  store.save();
  store.audit({ kind: 'device-edit', deviceId: d.deviceId, by: a.username, patch });
  broadcastDevices();
  sendJson(res, 200, { ok: true });
});

router.delete('/api/devices/:id', (req, res, params) => {
  const a = auth(req, 'admin');
  if (!a) return sendJson(res, 401, { error: 'unauthorized' });
  if (a.forbidden) return sendJson(res, 403, { error: 'admin role required' });
  if (!store.getDevice(params.id)) return sendJson(res, 404, { error: 'no such device' });
  for (const [sid, s] of sessions) if (s.deviceId === params.id) closeSession(sid, 'device_removed');
  const ws = agents.get(params.id);
  if (ws) { send(ws, { t: 'error', code: 'deenrolled', message: 'This device was removed from the fleet.' }); ws.close(4003, 'deenrolled'); }
  store.removeDevice(params.id);
  store.audit({ kind: 'device-remove', deviceId: params.id, by: a.username });
  broadcastDevices();
  sendJson(res, 200, { ok: true });
});

router.get('/api/audit', (req, res) => {
  const a = auth(req, 'admin');
  if (!a) return sendJson(res, 401, { error: 'unauthorized' });
  if (a.forbidden) return sendJson(res, 403, { error: 'admin role required' });
  const limit = Math.min(1000, Number(new URL(req.url, 'http://x').searchParams.get('limit') || 200));
  sendJson(res, 200, { entries: store.readAudit(limit) });
});

router.get('/api/operators', (req, res) => {
  const a = auth(req, 'admin');
  if (!a) return sendJson(res, 401, { error: 'unauthorized' });
  if (a.forbidden) return sendJson(res, 403, { error: 'admin role required' });
  sendJson(res, 200, {
    operators: Object.values(store.operators()).map((o) => ({
      username: o.username, role: o.role, createdAt: o.createdAt, lastLogin: o.lastLogin,
    })),
  });
});

router.post('/api/operators', async (req, res) => {
  const a = auth(req, 'admin');
  if (!a) return sendJson(res, 401, { error: 'unauthorized' });
  if (a.forbidden) return sendJson(res, 403, { error: 'admin role required' });
  const body = await readJson(req);
  try {
    const op = store.addOperator(body.username, body.password, body.role || 'operator');
    store.audit({ kind: 'operator-add', username: op.username, role: op.role, by: a.username });
    sendJson(res, 200, { ok: true, username: op.username, role: op.role });
  } catch (e) { sendJson(res, 400, { error: e.message }); }
});

router.delete('/api/operators/:username', (req, res, params) => {
  const a = auth(req, 'admin');
  if (!a) return sendJson(res, 401, { error: 'unauthorized' });
  if (a.forbidden) return sendJson(res, 403, { error: 'admin role required' });
  try {
    store.removeOperator(params.username);
    store.audit({ kind: 'operator-remove', username: params.username, by: a.username });
    sendJson(res, 200, { ok: true });
  } catch (e) { sendJson(res, 400, { error: e.message }); }
});

router.get('/api/enroll-key', (req, res) => {
  const a = auth(req, 'admin');
  if (!a) return sendJson(res, 401, { error: 'unauthorized' });
  if (a.forbidden) return sendJson(res, 403, { error: 'admin role required' });
  sendJson(res, 200, { enrollKey: store.enrollKey });
});

router.post('/api/enroll-key/rotate', (req, res) => {
  const a = auth(req, 'admin');
  if (!a) return sendJson(res, 401, { error: 'unauthorized' });
  if (a.forbidden) return sendJson(res, 403, { error: 'admin role required' });
  const key = store.rotateEnrollKey();
  store.audit({ kind: 'enroll-key-rotate', by: a.username });
  sendJson(res, 200, { enrollKey: key });
});

router.get('/healthz', (req, res) => {
  sendJson(res, 200, {
    ok: true, version: VERSION,
    devices: Object.keys(store.devices()).length,
    online: agents.size, sessions: sessions.size,
    uptimeSec: Math.round(process.uptime()),
  });
});

// ---------------------------------------------------------------- HTTP server

async function handleRequest(req, res) {
  const url = new URL(req.url, 'http://placeholder');
  const m = router.match(req.method, url.pathname);
  if (m) {
    try { await m.handler(req, res, m.params); }
    catch (e) {
      log('error', `${req.method} ${url.pathname}: ${e.stack || e.message}`);
      if (!res.headersSent) sendJson(res, e.status || 500, { error: e.message || 'internal error' });
    }
    return;
  }
  if (req.method === 'GET' || req.method === 'HEAD') {
    if (serveStatic(cfg.publicDir, url.pathname, res)) return;
    // SPA fallback so /admin and /connect/123 work on refresh
    if (!url.pathname.startsWith('/api/') && serveStatic(cfg.publicDir, '/index.html', res)) return;
  }
  sendText(res, 404, 'Not found');
}

let server;
if (cfg.tlsCert && cfg.tlsKey) {
  server = https.createServer({
    cert: fs.readFileSync(cfg.tlsCert), key: fs.readFileSync(cfg.tlsKey),
  }, handleRequest);
} else {
  server = http.createServer(handleRequest);
}

// ---------------------------------------------------------------- WebSocket

const agentWss = new WebSocketServer({ noServer: true, maxPayload: cfg.maxFrameBytes });
const viewerWss = new WebSocketServer({ noServer: true, maxPayload: cfg.maxFrameBytes });

server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, 'http://placeholder');
  if (url.pathname === '/ws/agent') {
    agentWss.handleUpgrade(req, socket, head, (ws) => agentWss.emit('connection', ws, req));
  } else if (url.pathname === '/ws/viewer') {
    viewerWss.handleUpgrade(req, socket, head, (ws) => viewerWss.emit('connection', ws, req));
  } else {
    socket.destroy();
  }
});

// ---- agent socket ----
agentWss.on('connection', (ws, req) => {
  const ip = clientIp(req);
  ws.isAlive = true;
  ws.deviceId = null;
  ws.sessions = new Set();
  ws.metrics = null;
  const killTimer = setTimeout(() => {
    if (!ws.deviceId) { send(ws, { t: 'error', code: 'register_timeout', message: 'no register frame' }); ws.close(4000, 'register timeout'); }
  }, 15_000);

  ws.on('message', (raw, isBinary) => {
    if (isBinary) return handleDataFrame(ws, raw, 'agent');
    let msg;
    try { msg = JSON.parse(raw.toString('utf8')); } catch { return; }
    if (!msg || typeof msg.t !== 'string') return;

    if (msg.t === 'register') {
      if (ws.deviceId) return;
      let device = msg.deviceId ? store.getDevice(String(msg.deviceId)) : null;
      if (device) {
        // re-registration: constant-time token check
        const a = Buffer.from(String(msg.deviceToken || ''));
        const b = Buffer.from(String(device.deviceToken));
        const ok = a.length === b.length && crypto.timingSafeEqual(a, b);
        if (!ok) {
          store.audit({ kind: 'agent-auth-fail', deviceId: device.deviceId, ip });
          send(ws, { t: 'error', code: 'bad_device_token', message: 'device token rejected' });
          return ws.close(4001, 'bad device token');
        }
      } else {
        if (!enrollLimiter.check(ip)) {
          send(ws, { t: 'error', code: 'rate_limited', message: 'too many enrollments from this address' });
          return ws.close(4029, 'rate limited');
        }
        const keyOk = cfg.allowOpenEnroll
          || (typeof msg.enrollKey === 'string' && msg.enrollKey.length === store.enrollKey.length
            && crypto.timingSafeEqual(Buffer.from(msg.enrollKey), Buffer.from(store.enrollKey)));
        if (!keyOk) {
          store.audit({ kind: 'enroll-fail', ip, name: String(msg.name || '').slice(0, 64) });
          log('warn', `enrollment rejected from ${ip} (bad key)`);
          send(ws, { t: 'error', code: 'bad_enroll_key', message: 'enrollment key rejected' });
          return ws.close(4002, 'bad enroll key');
        }
        let id;
        do { id = randomDigits(9); } while (store.getDevice(id));
        device = store.addDevice(id, randomKey(32), { ...msg, ip });
        device.ephemeral = !!msg.ephemeral;
        store.save();
        store.audit({ kind: msg.ephemeral ? 'support-session-start' : 'enroll',
                      deviceId: id, name: device.name, os: device.os, ip });
        log('info', `${msg.ephemeral ? 'support session' : 'enrolled new device'} ${id} ` +
                    `"${device.name}" (${device.os}) from ${ip}`);
      }

      const existing = agents.get(device.deviceId);
      if (existing && existing !== ws) {
        send(existing, { t: 'error', code: 'replaced', message: 'another agent connected with this ID' });
        existing.close(4008, 'replaced');
      }
      ws.deviceId = device.deviceId;
      ws.ephemeral = !!msg.ephemeral || !!device.ephemeral;
      clearTimeout(killTimer);
      agents.set(device.deviceId, ws);
      store.touchDevice(device.deviceId, {
        name: (msg.name || device.name).slice(0, 64), os: msg.os || device.os,
        arch: msg.arch || device.arch, agentVersion: msg.agentVersion || '',
        caps: Array.isArray(msg.caps) ? msg.caps : device.caps,
        unattended: !!msg.unattended, lastIp: ip,
      });
      send(ws, {
        t: 'registered', deviceId: device.deviceId, deviceToken: device.deviceToken,
        serverTime: Date.now(), relayVersion: VERSION, pingIntervalMs: PING_INTERVAL_MS,
      });
      log('info', `agent online: ${device.deviceId} "${store.getDevice(device.deviceId).name}" from ${ip}`);
      broadcastDevices();
      return;
    }

    if (!ws.deviceId) return;

    switch (msg.t) {
      case 'heartbeat': {
        ws.metrics = msg.metrics && typeof msg.metrics === 'object' ? msg.metrics : null;
        if (typeof msg.unattended === 'boolean') store.touchDevice(ws.deviceId, { unattended: msg.unattended });
        else store.touchDevice(ws.deviceId, {});
        broadcastDevices();
        break;
      }
      case 'pong': ws.isAlive = true; break;
      case 'session-accept': {
        const s = sessions.get(msg.sid);
        if (!s || s.agentWs !== ws || s.state !== 'pending') return;
        if (s.consentTimer) { clearTimeout(s.consentTimer); s.consentTimer = null; }
        s.state = 'open';
        s.openedAt = Date.now();
        send(s.viewerWs, {
          t: 'session-open', sid: msg.sid, deviceId: s.deviceId, agentPub: msg.pub,
          authRequired: !!msg.authRequired, salt: msg.salt || null,
          iterations: msg.iterations || 250000,
        });
        store.audit({ kind: 'session-start', sid: msg.sid, deviceId: s.deviceId, operator: s.operator, authRequired: !!msg.authRequired });
        log('info', `session ${msg.sid} open: ${s.operator} -> ${s.deviceId}`);
        broadcastDevices();
        break;
      }
      case 'session-reject': {
        const s = sessions.get(msg.sid);
        if (!s || s.agentWs !== ws) return;
        send(s.viewerWs, { t: 'session-denied', sid: msg.sid, reason: String(msg.reason || 'declined').slice(0, 64) });
        store.audit({ kind: 'session-denied', sid: msg.sid, deviceId: s.deviceId, operator: s.operator, reason: msg.reason });
        log('info', `session ${msg.sid} denied by ${s.deviceId}: ${msg.reason}`);
        sessions.delete(msg.sid);
        if (s.consentTimer) clearTimeout(s.consentTimer);
        const meta = viewers.get(s.viewerWs);
        if (meta) meta.sessions.delete(msg.sid);
        ws.sessions.delete(msg.sid);
        break;
      }
      case 'session-closed':
        if (sessions.has(msg.sid) && sessions.get(msg.sid).agentWs === ws) closeSession(msg.sid, 'closed_by_agent', 'viewer');
        break;
      default: break;
    }
  });

  ws.on('close', () => {
    clearTimeout(killTimer);
    if (ws.deviceId && agents.get(ws.deviceId) === ws) {
      agents.delete(ws.deviceId);
      // A quick-support agent is not a fleet member. It exists for one visit
      // and leaves nothing behind when the person closes the window, so the
      // device list does not silt up with dead one-off entries.
      if (ws.ephemeral) {
        store.removeDevice(ws.deviceId);
        store.audit({ kind: 'support-session-end', deviceId: ws.deviceId });
        log('info', `support agent gone, de-enrolled: ${ws.deviceId}`);
      } else {
        store.touchDevice(ws.deviceId, {});
        log('info', `agent offline: ${ws.deviceId}`);
      }
    }
    for (const sid of Array.from(ws.sessions)) closeSession(sid, 'agent_gone', 'viewer');
    broadcastDevices();
  });
  ws.on('error', (e) => log('debug', `agent socket error: ${e.message}`));
});

// ---- viewer socket ----
viewerWss.on('connection', (ws, req) => {
  const ip = clientIp(req);
  ws.isAlive = true;
  const killTimer = setTimeout(() => {
    if (!viewers.has(ws)) { send(ws, { t: 'error', code: 'auth_timeout', message: 'no auth frame' }); ws.close(4000, 'auth timeout'); }
  }, 15_000);

  ws.on('message', (raw, isBinary) => {
    if (isBinary) return handleDataFrame(ws, raw, 'viewer');
    let msg;
    try { msg = JSON.parse(raw.toString('utf8')); } catch { return; }
    if (!msg || typeof msg.t !== 'string') return;

    if (msg.t === 'auth') {
      if (viewers.has(ws)) return;
      const payload = verifyToken(msg.token, store.tokenSecret);
      const op = payload && payload.k === 'operator' ? store.getOperator(payload.u) : null;
      if (!op) {
        send(ws, { t: 'error', code: 'bad_token', message: 'token invalid or expired' });
        return ws.close(4001, 'bad token');
      }
      clearTimeout(killTimer);
      viewers.set(ws, {
        operator: op.username, role: op.role,
        label: String(msg.label || '').slice(0, 48), ip, sessions: new Set(),
      });
      send(ws, { t: 'authed', operator: op.username, role: op.role, relayVersion: VERSION });
      send(ws, { t: 'devices', devices: deviceListPayload() });
      log('debug', `viewer ${op.username} connected from ${ip}`);
      return;
    }

    const meta = viewers.get(ws);
    if (!meta) return;

    switch (msg.t) {
      case 'pong': ws.isAlive = true; break;
      case 'refresh': send(ws, { t: 'devices', devices: deviceListPayload() }); break;
      case 'connect': {
        if (meta.role === 'viewer' && false) return; // reserved: read-only role gating
        const device = store.getDevice(String(msg.deviceId || ''));
        if (!device) return send(ws, { t: 'error', code: 'no_device', message: 'unknown device ID' });
        const agentWs = agents.get(device.deviceId);
        if (!agentWs) return send(ws, { t: 'session-denied', sid: 0, deviceId: device.deviceId, reason: 'offline' });
        if (meta.sessions.size >= 8) return send(ws, { t: 'error', code: 'too_many', message: 'too many open sessions' });
        if (typeof msg.pub !== 'string' || msg.pub.length < 40 || msg.pub.length > 512) {
          return send(ws, { t: 'error', code: 'bad_pub', message: 'invalid public key' });
        }
        const sid = newSid();
        const s = {
          sid, deviceId: device.deviceId, operator: meta.operator, viewerWs: ws, agentWs,
          state: 'pending', createdAt: Date.now(), openedAt: null, bytesAV: 0, bytesVA: 0,
          seenAV: 0, seenVA: 0, consentTimer: null,
        };
        sessions.set(sid, s);
        meta.sessions.add(sid);
        agentWs.sessions.add(sid);
        s.consentTimer = setTimeout(() => {
          if (sessions.get(sid) && sessions.get(sid).state === 'pending') {
            send(ws, { t: 'session-denied', sid, deviceId: device.deviceId, reason: 'timeout' });
            store.audit({ kind: 'session-timeout', sid, deviceId: device.deviceId, operator: meta.operator });
            sessions.delete(sid);
            meta.sessions.delete(sid);
            agentWs.sessions.delete(sid);
            send(agentWs, { t: 'session-close', sid, reason: 'timeout' });
          }
        }, CONSENT_TIMEOUT_MS);
        send(agentWs, {
          t: 'session-request', sid, operator: meta.operator,
          operatorLabel: meta.label ? `${meta.operator} (${meta.label})` : meta.operator,
          pub: msg.pub, ip: meta.ip, unattendedRequested: msg.unattended !== false,
        });
        log('info', `session ${sid} requested: ${meta.operator} -> ${device.deviceId}`);
        break;
      }
      case 'close':
        if (sessions.has(msg.sid) && sessions.get(msg.sid).viewerWs === ws) closeSession(msg.sid, 'closed_by_viewer', 'agent');
        break;
      default: break;
    }
  });

  ws.on('close', () => {
    clearTimeout(killTimer);
    const meta = viewers.get(ws);
    if (meta) { for (const sid of Array.from(meta.sessions)) closeSession(sid, 'viewer_gone', 'agent'); }
    viewers.delete(ws);
    broadcastDevices();
  });
  ws.on('error', (e) => log('debug', `viewer socket error: ${e.message}`));
});

/** Forward an opaque encrypted frame to the session peer. */
function handleDataFrame(ws, buf, side) {
  if (buf.length < 17 || buf[0] !== DATA_MAGIC) return;
  if (buf.length > cfg.maxFrameBytes) return;
  const sid = buf.readUInt32BE(1);
  const s = sessions.get(sid);
  if (!s || s.state !== 'open') return;
  let peer;
  if (side === 'agent') {
    if (s.agentWs !== ws) return;
    peer = s.viewerWs; s.bytesAV += buf.length;
  } else {
    if (s.viewerWs !== ws) return;
    peer = s.agentWs; s.bytesVA += buf.length;
  }
  if (peer && peer.readyState === 1) {
    try { peer.send(buf, { binary: true }); } catch { /* peer is going away */ }
  }
}

// ---------------------------------------------------------------- keepalive

const heartbeat = setInterval(() => {
  for (const wss of [agentWss, viewerWss]) {
    for (const ws of wss.clients) {
      if (ws.isAlive === false) { log('debug', 'terminating dead socket'); ws.terminate(); continue; }
      ws.isAlive = false;
      send(ws, { t: 'ping', ts: Date.now() });
    }
  }
}, PING_INTERVAL_MS);
if (heartbeat.unref) heartbeat.unref();
void PING_TIMEOUT_MS;

// ---------------------------------------------------------------- bootstrap

/**
 * `--init` provisions the relay and exits without listening: makes sure an
 * admin exists, then prints credentials as KEY=value lines an installer can
 * parse. Beats scraping them out of the first-run banner.
 *
 *   node server.js --init                        ensure admin, show enroll key
 *   node server.js --init --reset-admin          also set a new random password
 *   node server.js --init --admin-password ...   set a specific password
 */
function runInit() {
  const user = String(cfg.adminUser).toLowerCase();
  const existing = store.getOperator(user);
  let password = cfg.adminPassword && String(cfg.adminPassword) !== 'true'
    ? String(cfg.adminPassword) : null;
  let state;

  try {
    if (!existing) {
      password = password || randomKey(12);
      store.addOperator(user, password, 'admin');
      state = 'created';
    } else if (password || cfg.resetAdmin) {
      password = password || randomKey(12);
      store.setOperatorPassword(user, password);
      existing.role = 'admin';
      store.save();
      state = 'password-reset';
    } else {
      state = 'existing';
    }
  } catch (e) {
    process.stdout.write(`AEGIS_ERROR=${e.message}\n`);
    process.exit(2);
  }

  store.flush();
  const out = [
    `AEGIS_ADMIN_USER=${user}`,
    `AEGIS_ADMIN_PASSWORD=${password || ''}`,
    `AEGIS_ENROLL_KEY=${store.enrollKey}`,
    `AEGIS_STATE=${state}`,
    `AEGIS_DATA_DIR=${cfg.dataDir}`,
    `AEGIS_PORT=${cfg.port}`,
    `AEGIS_VERSION=${VERSION}`,
    '',
  ].join('\n');
  process.stdout.write(out);
  process.exit(0);
}

if (cfg.init || cfg.resetAdmin) {
  runInit();
}

function bootstrap() {
  if (Object.keys(store.operators()).length === 0) {
    const pw = randomKey(12);
    store.addOperator('admin', pw, 'admin');
    store.flush();
    const line = '='.repeat(66);
    process.stdout.write(
      `\n${line}\n  AegisDesk first run -- an admin account was created for you\n\n` +
      `    username : admin\n    password : ${pw}\n\n` +
      `  Change it after you log in (Settings -> Change password).\n` +
      `  Enrollment key for installing agents:\n\n    ${store.enrollKey}\n${line}\n\n`);
  }
}

bootstrap();

server.listen(cfg.port, cfg.host, () => {
  const scheme = cfg.tlsCert ? 'https' : 'http';
  log('info', `AegisDesk relay v${VERSION} listening on ${scheme}://${cfg.host}:${cfg.port}`);
  log('info', `data dir: ${cfg.dataDir}`);
  log('info', `${Object.keys(store.devices()).length} device(s) enrolled, ${Object.keys(store.operators()).length} operator(s)`);
  if (!cfg.tlsCert) log('warn', 'running without TLS -- put this behind Caddy/nginx or a Tailscale network before exposing it to the internet');
});

function shutdown(sig) {
  log('info', `${sig} received, shutting down`);
  clearInterval(heartbeat);
  for (const sid of Array.from(sessions.keys())) closeSession(sid, 'relay_shutdown');
  store.flush();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('uncaughtException', (e) => { log('error', `uncaught: ${e.stack || e.message}`); });
process.on('unhandledRejection', (e) => { log('error', `unhandled rejection: ${(e && e.stack) || e}`); });

module.exports = { server, store };
