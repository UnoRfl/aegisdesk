// Verifies that the browser-side crypto (WebCrypto, exercised here through
// Node's identical implementation) agrees with the Python agent.
// Reads a JSON job on argv[2], prints JSON to stdout.
const job = JSON.parse(process.argv[2]);
const subtle = globalThis.crypto.subtle;
const b64 = (b) => Buffer.from(b).toString('base64');
const unb64 = (s) => new Uint8Array(Buffer.from(s, 'base64'));

const P256 = { name: 'ECDH', namedCurve: 'P-256' };

async function importPub(raw) {
  return subtle.importKey('raw', raw, P256, false, []);
}

async function run() {
  if (job.op === 'gen') {
    const kp = await subtle.generateKey(P256, true, ['deriveBits']);
    const pub = new Uint8Array(await subtle.exportKey('raw', kp.publicKey));
    const priv = new Uint8Array(await subtle.exportKey('pkcs8', kp.privateKey));
    return { pub: b64(pub), priv: b64(priv) };
  }
  if (job.op === 'derive' || job.op === 'seal' || job.op === 'open' || job.op === 'proof') {
    const priv = await subtle.importKey('pkcs8', unb64(job.priv), P256, false, ['deriveBits']);
    const peer = await importPub(unb64(job.peerPub));
    const shared = new Uint8Array(await subtle.deriveBits({ name: 'ECDH', public: peer }, priv, 256));

    const sidBytes = new Uint8Array(4);
    new DataView(sidBytes.buffer).setUint32(0, job.sid, false);
    const saltInput = new Uint8Array([
      ...new TextEncoder().encode('aegisdesk-v1-salt'),
      ...sidBytes, ...unb64(job.viewerPub), ...unb64(job.agentPub),
    ]);
    const salt = new Uint8Array(await subtle.digest('SHA-256', saltInput));
    const hkdfKey = await subtle.importKey('raw', shared, 'HKDF', false, ['deriveBits']);
    const keyBits = new Uint8Array(await subtle.deriveBits(
      { name: 'HKDF', hash: 'SHA-256', salt, info: new TextEncoder().encode('aegisdesk-v1-data') },
      hkdfKey, 256));

    if (job.op === 'derive') return { key: b64(keyBits) };

    const aes = await subtle.importKey('raw', keyBits, 'AES-GCM', false, ['encrypt', 'decrypt']);
    if (job.op === 'seal') {
      const nonce = new Uint8Array(12);
      const dv = new DataView(nonce.buffer);
      dv.setUint32(0, job.direction, false);
      dv.setBigUint64(4, BigInt(job.counter), false);
      const ct = new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv: nonce, tagLength: 128 },
        aes, new TextEncoder().encode(job.plaintext)));
      return { frame: b64(new Uint8Array([...nonce, ...ct])) };
    }
    if (job.op === 'open') {
      const framed = unb64(job.frame);
      const nonce = framed.slice(0, 12);
      const pt = new Uint8Array(await subtle.decrypt({ name: 'AES-GCM', iv: nonce, tagLength: 128 },
        aes, framed.slice(12)));
      return { plaintext: new TextDecoder().decode(pt) };
    }
    if (job.op === 'proof') {
      const pw = new TextEncoder().encode(job.password);
      const pwKey = await subtle.importKey('raw', pw, 'PBKDF2', false, ['deriveBits']);
      const dk = new Uint8Array(await subtle.deriveBits(
        { name: 'PBKDF2', hash: 'SHA-256', salt: unb64(job.authSalt), iterations: job.iterations },
        pwKey, 256));
      const hk = await subtle.importKey('raw', dk, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
      const msg = new Uint8Array([
        ...new TextEncoder().encode('aegisdesk-auth-v1'),
        ...sidBytes, ...unb64(job.viewerPub), ...unb64(job.agentPub),
      ]);
      const sig = new Uint8Array(await subtle.sign('HMAC', hk, msg));
      return { dk: b64(dk), proof: b64(sig) };
    }
  }
  throw new Error('unknown op ' + job.op);
}
run().then((r) => process.stdout.write(JSON.stringify(r)))
  .catch((e) => { process.stdout.write(JSON.stringify({ error: String(e) })); process.exitCode = 1; });
