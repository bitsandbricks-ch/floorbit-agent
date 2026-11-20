"""The agent's own key, which signs every request to Floorbit (Floorbit's docs/agent.md, "Identity",
bitsandbricks-ch/floorbit#1178).

The bearer key alone was the agent: whoever copied it could be the agent. Now each request also
carries ``Floorbit-Agent-Signature``, made with a P-256 key that is created here and never leaves
this machine (``tls/identity-key.pem``, 0600). Floorbit pins its public key the first time the agent
enrols it and checks every request against the pin; the bearer key stays as the second factor.

- **Enrol:** at hello, when Floorbit has no key pinned for this agent, the agent sends a signing
  request (``POST /api/agent/v1/identity``) and keeps the client certificate it gets
  (``tls/identity.pem``). The same happens 30 days before the certificate ends, signed with the key.
- **Clock:** a signature is good for five minutes around Floorbit's time. The agent takes that time
  from the ``Date`` of Floorbit's answers, so a box without NTP signs in time anyway.
- **Lost state:** a new machine (or a lost state folder) has a new key that Floorbit doesn't know.
  The agent says so and keeps asking; an admin resets its identity on the agent's page.
"""
import base64
import email.utils
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

HEADER = 'Floorbit-Agent-Signature'
SCHEME = 'floorbit-agent-v1'
KEY_FILE = 'identity-key.pem'
CERT_FILE = 'identity.pem'
SLACK = 2                   # seconds of difference to Floorbit's Date that are just its rounding


def message(method, target, stamp, nonce, agent_id, body):
    """What is signed; Floorbit's agent_identity.message builds the same bytes."""
    if isinstance(body, str):
        body = body.encode()
    digest = hashlib.sha256(body or b'').hexdigest()
    return '\n'.join((SCHEME, method.upper(), target, stamp, nonce, agent_id, digest)).encode()


def _target(url):
    parts = urlsplit(url)
    return (parts.path or '/') + (f'?{parts.query}' if parts.query else '')


def agent_id_of(key):
    """The id in ``fba_<id>_<secret>``."""
    parts = str(key or '').split('_')
    return parts[1] if len(parts) >= 3 and parts[0] == 'fba' else ''


class Identity:
    def __init__(self, root, log=print, now=time.time):
        self.dir = os.path.join(root, 'tls')
        self.log = log
        self.now = now
        self.offset = 0.0         # Floorbit's clock minus ours
        self._key = None
        self._lock = threading.Lock()

    @property
    def key_path(self):
        return os.path.join(self.dir, KEY_FILE)

    @property
    def cert_path(self):
        return os.path.join(self.dir, CERT_FILE)

    def key(self):
        """The private key, made the first time it's needed."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        with self._lock:
            if self._key is not None:
                return self._key
            try:
                with open(self.key_path, 'rb') as f:
                    self._key = serialization.load_pem_private_key(f.read(), None)
            except (OSError, ValueError):
                from .hosted import _write_private
                self._key = ec.generate_private_key(ec.SECP256R1())
                _write_private(self.key_path, self._key.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()).decode())
                self.log('[agent] made this agent\'s own key; Floorbit pins it when the agent enrols')
            return self._key

    def has_key(self):
        return self._key is not None or os.path.exists(self.key_path)

    def fingerprint(self):
        from cryptography.hazmat.primitives import serialization
        der = self.key().public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
        return hashlib.sha256(der).hexdigest()[:32]

    # ── signing ──────────────────────────────────────────────────────────────────────────────────

    def sign(self, method, url, body, agent_id):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        stamp = f'{self.now() + self.offset:.3f}'
        nonce = secrets.token_urlsafe(18)
        sig = self.key().sign(message(method, _target(url), stamp, nonce, agent_id, body), ec.ECDSA(hashes.SHA256()))
        return f't={stamp},n={nonce},s={base64.b64encode(sig).decode()}'

    def learn_clock(self, resp):
        """Floorbit's time from an answer's ``Date`` (whole seconds)."""
        raw = getattr(resp, 'headers', {}).get('Date') if resp is not None else None
        if not raw:
            return
        try:
            theirs = email.utils.parsedate_to_datetime(raw).timestamp()
        except (TypeError, ValueError):
            return
        off = theirs - self.now()
        # Date has whole seconds and the answer took a moment: only a real difference counts.
        if abs(off - self.offset) > SLACK:
            if abs(off) > 60 and abs(self.offset) <= 60:
                self.log(f'[agent] this machine\'s clock is {abs(off):.0f} s off Floorbit\'s; the agent signs with '
                         'Floorbit\'s time. Turn on NTP here.')
            self.offset = off if abs(off) > SLACK else 0.0

    def auth(self, key_of):
        """A ``requests`` auth that signs each request; ``key_of()`` gives the bearer key now."""
        import requests

        identity = self

        class Sign(requests.auth.AuthBase):
            def __call__(self, r):
                r.headers[HEADER] = identity.sign(r.method, r.url, r.body, agent_id_of(key_of()))
                r.register_hook('response', lambda resp, *a, **kw: identity.learn_clock(resp))
                return r
        return Sign()

    # ── the client certificate ───────────────────────────────────────────────────────────────────

    def not_after(self):
        from cryptography import x509
        try:
            with open(self.cert_path, 'rb') as f:
                return x509.load_pem_x509_certificates(f.read())[0].not_valid_after_utc.timestamp()
        except (OSError, ValueError, IndexError):
            return None

    def wanted(self, block):
        """Does hello's ``identity`` block call for enrolling (or renewing)?"""
        if not isinstance(block, dict):
            return False
        if not block.get('pinned'):
            return True
        if not block.get('signed'):
            return False          # a key Floorbit doesn't know: enrolling again would be refused
        ends = block.get('not_after') or self.not_after() or 0
        return ends - (self.now() + self.offset) < float(block.get('renew_days') or 30) * 86400

    def enrol(self, agent, path='/api/agent/v1/identity'):
        """Send the signing request; keep the certificate. Returns the fingerprint Floorbit pinned."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID

        from .hosted import _write_private
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                            f'agent {agent_id_of(agent.key)}')]))
               .sign(self.key(), hashes.SHA256()))
        resp = agent.http.post(f'{agent.url}{path}', headers=agent._headers(json_body=True), timeout=30,
                               data=json.dumps({'csr': csr.public_bytes(serialization.Encoding.PEM).decode()}))
        if resp.status_code in (400, 409):
            try:
                why = resp.json().get('error')
            except ValueError:
                why = None
            raise RuntimeError(why or f'HTTP {resp.status_code}')
        body = agent._check(resp)
        pem = str(body.get('cert') or '')
        if 'BEGIN CERTIFICATE' not in pem:
            raise RuntimeError('Floorbit sent no certificate.')
        _write_private(self.cert_path, pem)
        self.log(f'[agent] Floorbit pinned this agent\'s own key ({body.get("fingerprint") or self.fingerprint()}); '
                 'every request is signed with it from now on')
        return body.get('fingerprint')

    def report(self):
        if not self.has_key():
            return None
        return {'fingerprint': self.fingerprint(), 'not_after': self.not_after()}
