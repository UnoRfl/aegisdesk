'use strict';
/** Minimal router + static file server. Zero dependencies on purpose. */
const fs = require('fs');
const path = require('path');
const { log } = require('./util');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
};

const SECURITY_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
  'Permissions-Policy': 'geolocation=(), microphone=(), camera=()',
};

class Router {
  constructor() { this.routes = []; }
  add(method, pattern, handler) {
    // pattern: '/api/devices/:id'
    const parts = pattern.split('/').filter(Boolean);
    this.routes.push({ method, parts, handler });
    return this;
  }
  get(p, h) { return this.add('GET', p, h); }
  post(p, h) { return this.add('POST', p, h); }
  patch(p, h) { return this.add('PATCH', p, h); }
  delete(p, h) { return this.add('DELETE', p, h); }

  match(method, pathname) {
    const parts = pathname.split('/').filter(Boolean);
    for (const r of this.routes) {
      if (r.method !== method || r.parts.length !== parts.length) continue;
      const params = {};
      let ok = true;
      for (let i = 0; i < r.parts.length; i++) {
        const pat = r.parts[i];
        if (pat.startsWith(':')) params[pat.slice(1)] = decodeURIComponent(parts[i]);
        else if (pat !== parts[i]) { ok = false; break; }
      }
      if (ok) return { handler: r.handler, params };
    }
    return null;
  }
}

function sendJson(res, status, obj) {
  const body = Buffer.from(JSON.stringify(obj));
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
    ...SECURITY_HEADERS,
  });
  res.end(body);
}

function sendText(res, status, text) {
  const body = Buffer.from(String(text));
  res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8', 'Content-Length': body.length, ...SECURITY_HEADERS });
  res.end(body);
}

async function readBody(req, limit = 1024 * 512) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) { reject(new Error('body too large')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

async function readJson(req) {
  const buf = await readBody(req);
  if (!buf.length) return {};
  try { return JSON.parse(buf.toString('utf8')); }
  catch { throw Object.assign(new Error('invalid JSON body'), { status: 400 }); }
}

function serveStatic(root, urlPath, res) {
  let rel = decodeURIComponent(urlPath.split('?')[0]);
  if (rel === '/' || rel === '') rel = '/index.html';
  // defeat traversal: resolve then confirm the result is still inside root
  const full = path.resolve(root, '.' + rel);
  if (!full.startsWith(path.resolve(root) + path.sep) && full !== path.resolve(root)) {
    sendText(res, 403, 'Forbidden');
    return true;
  }
  let st;
  try { st = fs.statSync(full); } catch { return false; }
  if (st.isDirectory()) return false;
  const ext = path.extname(full).toLowerCase();
  const headers = {
    'Content-Type': MIME[ext] || 'application/octet-stream',
    'Content-Length': st.size,
    'Cache-Control': ext === '.html' ? 'no-cache' : 'public, max-age=300',
    ...SECURITY_HEADERS,
  };
  res.writeHead(200, headers);
  fs.createReadStream(full).pipe(res);
  return true;
}

/** Sliding-window rate limiter keyed by string (usually IP or IP+user). */
class RateLimiter {
  constructor(max, windowMs) { this.max = max; this.windowMs = windowMs; this.hits = new Map(); }
  check(key) {
    const now = Date.now();
    let arr = this.hits.get(key);
    if (!arr) { arr = []; this.hits.set(key, arr); }
    while (arr.length && now - arr[0] > this.windowMs) arr.shift();
    if (arr.length >= this.max) return false;
    arr.push(now);
    if (this.hits.size > 5000) {
      for (const [k, v] of this.hits) { if (!v.length || now - v[v.length - 1] > this.windowMs) this.hits.delete(k); }
    }
    return true;
  }
  reset(key) { this.hits.delete(key); }
}

function clientIp(req) {
  const xff = req.headers['x-forwarded-for'];
  if (xff) return String(xff).split(',')[0].trim();
  return (req.socket && req.socket.remoteAddress) || '?';
}

module.exports = { Router, sendJson, sendText, readJson, readBody, serveStatic, RateLimiter, clientIp, SECURITY_HEADERS, log };
