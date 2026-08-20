'use strict';
const crypto = require('crypto');

function b64url(buf) {
  return Buffer.from(buf).toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function unb64url(s) {
  return Buffer.from(String(s).replace(/-/g, '+').replace(/_/g, '/'), 'base64');
}

/** Compact HMAC-signed token: b64url(json).b64url(mac) */
function signToken(payload, secret) {
  const body = b64url(JSON.stringify(payload));
  const mac = b64url(crypto.createHmac('sha256', secret).update(body).digest());
  return `${body}.${mac}`;
}
function verifyToken(token, secret) {
  if (typeof token !== 'string') return null;
  const dot = token.indexOf('.');
  if (dot < 1) return null;
  const body = token.slice(0, dot);
  const mac = token.slice(dot + 1);
  const want = b64url(crypto.createHmac('sha256', secret).update(body).digest());
  if (mac.length !== want.length) return null;
  if (!crypto.timingSafeEqual(Buffer.from(mac), Buffer.from(want))) return null;
  let payload;
  try { payload = JSON.parse(unb64url(body).toString('utf8')); } catch { return null; }
  if (payload.exp && Date.now() > payload.exp) return null;
  return payload;
}

/** scrypt password hashing: "scrypt$N$r$p$salt$hash" */
function hashPassword(password, N = 16384, r = 8, p = 1) {
  const salt = crypto.randomBytes(16);
  const dk = crypto.scryptSync(String(password), salt, 32, { N, r, p, maxmem: 64 * 1024 * 1024 });
  return `scrypt$${N}$${r}$${p}$${salt.toString('hex')}$${dk.toString('hex')}`;
}
function verifyPassword(password, stored) {
  try {
    const [scheme, N, r, p, saltHex, hashHex] = String(stored).split('$');
    if (scheme !== 'scrypt') return false;
    const dk = crypto.scryptSync(String(password), Buffer.from(saltHex, 'hex'), 32,
      { N: +N, r: +r, p: +p, maxmem: 64 * 1024 * 1024 });
    const want = Buffer.from(hashHex, 'hex');
    return dk.length === want.length && crypto.timingSafeEqual(dk, want);
  } catch { return false; }
}

function randomDigits(n) {
  let out = '';
  while (out.length < n) {
    for (const b of crypto.randomBytes(n * 2)) {
      if (b < 250) { out += String(b % 10); if (out.length === n) break; }
    }
  }
  // never start with 0 -- keeps it a clean n-digit number people can read aloud
  if (out[0] === '0') out = '1' + out.slice(1);
  return out;
}

function randomKey(bytes = 24) { return b64url(crypto.randomBytes(bytes)); }

function nowMs() { return Date.now(); }

const LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
let logLevel = LEVELS.info;
function setLogLevel(name) { if (name in LEVELS) logLevel = LEVELS[name]; }
function log(level, ...args) {
  if (LEVELS[level] > logLevel) return;
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const stream = level === 'error' || level === 'warn' ? process.stderr : process.stdout;
  stream.write(`${ts} [${level.toUpperCase().padEnd(5)}] ${args.map(String).join(' ')}\n`);
}

module.exports = {
  b64url, unb64url, signToken, verifyToken, hashPassword, verifyPassword,
  randomDigits, randomKey, nowMs, setLogLevel, log,
};
