'use strict';
/**
 * Tiny JSON-file store. Atomic writes (tmp + rename), debounced so a busy
 * fleet doesn't hammer the disk. Good for hundreds of devices, which is
 * about 100x more than a restaurant group needs.
 */
const fs = require('fs');
const path = require('path');
const { hashPassword, randomKey, log } = require('./util');

class Store {
  constructor(dataDir) {
    this.dir = dataDir;
    fs.mkdirSync(this.dir, { recursive: true });
    this.file = path.join(this.dir, 'state.json');
    this.auditFile = path.join(this.dir, 'audit.jsonl');
    this.state = this._load();
    this._dirty = false;
    this._timer = null;
    this._flushBound = () => this.flush();
    process.on('exit', this._flushBound);
  }

  _load() {
    let s = null;
    try {
      s = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    } catch (e) {
      if (e.code !== 'ENOENT') log('warn', `state.json unreadable (${e.message}); starting fresh`);
    }
    if (!s || typeof s !== 'object') s = {};
    s.devices = s.devices || {};
    s.operators = s.operators || {};
    s.settings = s.settings || {};
    if (!s.settings.enrollKey) s.settings.enrollKey = randomKey(24);
    if (!s.settings.tokenSecret) s.settings.tokenSecret = randomKey(32);
    return s;
  }

  save() {
    this._dirty = true;
    if (this._timer) return;
    this._timer = setTimeout(() => { this._timer = null; this.flush(); }, 400);
    if (this._timer.unref) this._timer.unref();
  }

  flush() {
    if (!this._dirty) return;
    this._dirty = false;
    const tmp = `${this.file}.tmp`;
    try {
      fs.writeFileSync(tmp, JSON.stringify(this.state, null, 2));
      fs.renameSync(tmp, this.file);
    } catch (e) {
      log('error', `failed to persist state: ${e.message}`);
    }
  }

  // ---- settings ----
  get enrollKey() { return this.state.settings.enrollKey; }
  get tokenSecret() { return this.state.settings.tokenSecret; }
  rotateEnrollKey() {
    this.state.settings.enrollKey = randomKey(24);
    this.save();
    return this.state.settings.enrollKey;
  }

  // ---- operators ----
  operators() { return this.state.operators; }
  getOperator(username) { return this.state.operators[String(username).toLowerCase()] || null; }
  addOperator(username, password, role = 'operator') {
    const u = String(username).toLowerCase().trim();
    if (!/^[a-z0-9._-]{2,32}$/.test(u)) throw new Error('username must be 2-32 chars of a-z 0-9 . _ -');
    if (String(password).length < 8) throw new Error('password must be at least 8 characters');
    if (!['admin', 'operator', 'viewer'].includes(role)) throw new Error('role must be admin, operator or viewer');
    this.state.operators[u] = {
      username: u, role, password: hashPassword(password),
      createdAt: Date.now(), lastLogin: null,
    };
    this.save();
    return this.state.operators[u];
  }
  setOperatorPassword(username, password) {
    const op = this.getOperator(username);
    if (!op) throw new Error('no such operator');
    if (String(password).length < 8) throw new Error('password must be at least 8 characters');
    op.password = hashPassword(password);
    this.save();
  }
  removeOperator(username) {
    const u = String(username).toLowerCase();
    const admins = Object.values(this.state.operators).filter((o) => o.role === 'admin');
    if (admins.length === 1 && admins[0].username === u) throw new Error('cannot remove the last admin');
    delete this.state.operators[u];
    this.save();
  }

  // ---- devices ----
  devices() { return this.state.devices; }
  getDevice(id) { return this.state.devices[String(id)] || null; }
  addDevice(deviceId, deviceToken, info) {
    this.state.devices[deviceId] = {
      deviceId, deviceToken,
      name: info.name || deviceId,
      os: info.os || 'unknown', arch: info.arch || '',
      agentVersion: info.agentVersion || '',
      caps: info.caps || [],
      group: '', tags: [], notes: '',
      enrolledAt: Date.now(), lastSeen: Date.now(), lastIp: info.ip || '',
      unattended: !!info.unattended,
    };
    this.save();
    return this.state.devices[deviceId];
  }
  touchDevice(deviceId, patch) {
    const d = this.getDevice(deviceId);
    if (!d) return null;
    Object.assign(d, patch, { lastSeen: Date.now() });
    this.save();
    return d;
  }
  removeDevice(deviceId) { delete this.state.devices[String(deviceId)]; this.save(); }

  // ---- audit ----
  audit(event) {
    const line = JSON.stringify({ ts: Date.now(), ...event });
    try { fs.appendFileSync(this.auditFile, line + '\n'); }
    catch (e) { log('warn', `audit write failed: ${e.message}`); }
  }
  readAudit(limit = 200) {
    try {
      const txt = fs.readFileSync(this.auditFile, 'utf8');
      const lines = txt.split('\n').filter(Boolean);
      return lines.slice(-limit).map((l) => { try { return JSON.parse(l); } catch { return null; } })
        .filter(Boolean).reverse();
    } catch { return []; }
  }
}

module.exports = { Store };
