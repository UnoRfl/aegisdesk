"""Python agent <-> WebCrypto (browser) interop.

Node's WebCrypto is the same spec surface browsers implement, so if these
vectors round-trip here they round-trip in the viewer.
"""
import base64, json, os, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
from aegis_agent import crypto as C   # noqa: E402

HELPER = os.path.join(ROOT, "tests", "interop_webcrypto.mjs")
b64 = lambda b: base64.b64encode(b).decode()
unb64 = lambda s: base64.b64decode(s)


def js(job):
    out = subprocess.run(["node", HELPER, json.dumps(job)],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise AssertionError(f"node helper failed: {out.stdout} {out.stderr}")
    r = json.loads(out.stdout)
    if "error" in r:
        raise AssertionError(r["error"])
    return r


class TestInterop(unittest.TestCase):
    def setUp(self):
        self.sid = 0x1234ABCD
        self.viewer = js({"op": "gen"})
        self.apriv, self.apub = C.generate_keypair()
        self.vpub = unb64(self.viewer["pub"])

    def _js_common(self, op, **extra):
        return js({"op": op, "priv": self.viewer["priv"], "peerPub": b64(self.apub),
                   "sid": self.sid, "viewerPub": b64(self.vpub), "agentPub": b64(self.apub),
                   **extra})

    def test_ecdh_hkdf_agreement(self):
        py = C.derive_key(self.apriv, self.vpub, self.sid, self.vpub, self.apub)
        self.assertEqual(b64(py), self._js_common("derive")["key"])

    def test_browser_seals_agent_opens(self):
        key = C.derive_key(self.apriv, self.vpub, self.sid, self.vpub, self.apub)
        ch = C.SecureChannel(key, C.DIR_AGENT_TO_VIEWER)
        r = self._js_common("seal", direction=C.DIR_VIEWER_TO_AGENT, counter=1,
                            plaintext="click at 0.5,0.5 — éü")
        self.assertEqual(ch.open(unb64(r["frame"])).decode(), "click at 0.5,0.5 — éü")

    def test_agent_seals_browser_opens(self):
        key = C.derive_key(self.apriv, self.vpub, self.sid, self.vpub, self.apub)
        ch = C.SecureChannel(key, C.DIR_AGENT_TO_VIEWER)
        frame = ch.seal(b"tile frame payload \x00\x01\xff")
        r = self._js_common("open", frame=b64(frame))
        self.assertEqual(r["plaintext"].encode("utf-8", "surrogatepass")[:18], b"tile frame payload")

    def test_password_proof_matches(self):
        password = "Rest4urant-POS!"
        salt = C.new_salt()
        iters = 10_000          # low only to keep the test fast
        r = self._js_common("proof", password=password, authSalt=b64(salt), iterations=iters)
        dk = C.pbkdf2(password, salt, iters)
        self.assertEqual(b64(dk), r["dk"], "PBKDF2 output differs")
        proof = C.auth_proof(dk, self.sid, self.vpub, self.apub)
        self.assertEqual(b64(proof), r["proof"], "HMAC proof differs")

    def test_proof_is_bound_to_keys(self):
        password = "hunter2hunter2"
        salt, iters = C.new_salt(), 10_000
        r = self._js_common("proof", password=password, authSalt=b64(salt), iterations=iters)
        dk = C.pbkdf2(password, salt, iters)
        _, mitm_pub = C.generate_keypair()
        forged = C.auth_proof(dk, self.sid, self.vpub, mitm_pub)
        self.assertNotEqual(b64(forged), r["proof"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
