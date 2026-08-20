"""
Session cryptography for AegisDesk.

The relay never holds a key. Agent and viewer each generate an ephemeral
P-256 keypair per session, agree on a shared secret with ECDH, run it
through HKDF-SHA256 and use the result as an AES-256-GCM key.

Nonce layout is 4 bytes of direction prefix followed by an 8-byte big-endian
counter, so the two directions can never collide and every frame under a
given key gets a unique nonce.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CURVE = ec.SECP256R1()
DIR_AGENT_TO_VIEWER = 1
DIR_VIEWER_TO_AGENT = 2

HKDF_INFO = b"aegisdesk-v1-data"
SALT_PREFIX = b"aegisdesk-v1-salt"
AUTH_LABEL = b"aegisdesk-auth-v1"
AUTH_ACK_LABEL = b"aegisdesk-auth-ack"

DEFAULT_PBKDF2_ITERATIONS = 250_000


class CryptoError(Exception):
    pass


def generate_keypair():
    """Return (private_key, raw_public_bytes) -- 65-byte uncompressed point."""
    priv = ec.generate_private_key(CURVE)
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return priv, raw


def derive_key(priv, peer_raw_pub: bytes, sid: int, viewer_pub: bytes, agent_pub: bytes) -> bytes:
    """ECDH + HKDF. `viewer_pub`/`agent_pub` fix the transcript so a relay
    that swaps a key changes the salt and both sides end up with different
    keys (and, with password auth on, a failed proof)."""
    if len(peer_raw_pub) != 65 or peer_raw_pub[0] != 0x04:
        raise CryptoError("peer public key is not a 65-byte uncompressed P-256 point")
    peer = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, peer_raw_pub)
    shared = priv.exchange(ec.ECDH(), peer)
    salt = hashlib.sha256(SALT_PREFIX + struct.pack(">I", sid) + viewer_pub + agent_pub).digest()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=HKDF_INFO).derive(shared)


class SecureChannel:
    """AES-256-GCM sealing/opening with per-direction nonce counters and a
    replay window on the receive side."""

    def __init__(self, key: bytes, send_direction: int):
        if len(key) != 32:
            raise CryptoError("key must be 32 bytes")
        self._aead = AESGCM(key)
        self._send_dir = send_direction
        self._recv_dir = (DIR_VIEWER_TO_AGENT if send_direction == DIR_AGENT_TO_VIEWER
                          else DIR_AGENT_TO_VIEWER)
        self._send_ctr = 0
        self._recv_high = 0
        self._recv_seen: set[int] = set()

    @staticmethod
    def _nonce(direction: int, counter: int) -> bytes:
        return struct.pack(">IQ", direction, counter)

    def seal(self, plaintext: bytes) -> bytes:
        """Return nonce || ciphertext||tag."""
        self._send_ctr += 1
        if self._send_ctr >= (1 << 62):
            raise CryptoError("nonce counter exhausted -- reconnect")
        nonce = self._nonce(self._send_dir, self._send_ctr)
        return nonce + self._aead.encrypt(nonce, plaintext, None)

    def open(self, framed: bytes) -> bytes:
        """Take nonce || ciphertext, verify direction + replay, return plaintext."""
        if len(framed) < 12 + 16:
            raise CryptoError("frame too short")
        nonce, body = framed[:12], framed[12:]
        direction, counter = struct.unpack(">IQ", nonce)
        if direction != self._recv_dir:
            raise CryptoError("wrong direction prefix (reflected frame?)")
        if counter == 0:
            raise CryptoError("zero counter")
        if counter <= self._recv_high - 4096:
            raise CryptoError("counter too old (replay)")
        if counter in self._recv_seen:
            raise CryptoError("counter replay")
        plain = self._aead.decrypt(nonce, body, None)   # raises InvalidTag on tamper
        self._recv_seen.add(counter)
        if counter > self._recv_high:
            self._recv_high = counter
        if len(self._recv_seen) > 8192:
            cutoff = self._recv_high - 4096
            self._recv_seen = {c for c in self._recv_seen if c > cutoff}
        return plain


# ------------------------------------------------------------------ password auth

def pbkdf2(password: str, salt: bytes, iterations: int = DEFAULT_PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


def _auth_msg(label: bytes, sid: int, viewer_pub: bytes, agent_pub: bytes) -> bytes:
    return label + struct.pack(">I", sid) + viewer_pub + agent_pub


def auth_proof(key: bytes, sid: int, viewer_pub: bytes, agent_pub: bytes) -> bytes:
    return hmac.new(key, _auth_msg(AUTH_LABEL, sid, viewer_pub, agent_pub), hashlib.sha256).digest()


def auth_ack(key: bytes, sid: int, viewer_pub: bytes, agent_pub: bytes) -> bytes:
    return hmac.new(key, _auth_msg(AUTH_ACK_LABEL, sid, viewer_pub, agent_pub), hashlib.sha256).digest()


def constant_time_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def new_salt(n: int = 16) -> bytes:
    return os.urandom(n)


def hash_password(password: str, iterations: int = DEFAULT_PBKDF2_ITERATIONS,
                  salt: Optional[bytes] = None) -> dict:
    """Produce the on-disk verifier. The plaintext password is never stored."""
    salt = salt or new_salt()
    return {
        "kdf": "pbkdf2-sha256",
        "iterations": iterations,
        "salt": salt.hex(),
        "key": pbkdf2(password, salt, iterations).hex(),
    }
