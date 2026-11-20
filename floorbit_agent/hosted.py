"""What the hosted Floorbit sets for this agent on its page (Floorbit's docs/agent.md, "Settings from
Floorbit"): the agent's own settings, a new key to fetch, and a certificate signed by Floorbit's
agents CA. They come with the plugin config (``GET /api/agent/v1/plugins``, its ``agent`` block).

- **Settings:** each one has the environment variable the agent read before, and that variable
  still wins when it is set. What Floorbit says is kept in the state folder, so the agent starts
  with it next time; the ones read only at start say so (``pending``) until then.
- **Not from Floorbit:** what the hall allows (actions, plugin updates, the networks the agent may
  reach, ``LOCAL_ONLY``) and the machine itself (URL, first key, heartbeat). A stolen Floorbit
  login must not widen what the agent does.
- **Key:** asked to roll it, the agent fetches a new key (``POST /api/agent/v1/key``) and keeps it
  in the state folder; it wins over ``FLOORBIT_AGENT_KEY`` from then on.
- **Certificate:** the agent makes its own key (it never leaves this machine), sends a signing
  request (``POST /api/agent/v1/cert``) and serves the plugins' HTTPS listeners with what comes
  back. ``FLOORBIT_AGENT_TLS_CERT`` set by the hall wins.
"""
import contextlib
import json
import os
import time

# key -> (environment variable, takes effect without a restart)
FIELDS = {
    'lan_url': ('FLOORBIT_AGENT_LAN_URL', True),
    'metrics': ('FLOORBIT_AGENT_METRICS', False),
    'metrics_token': ('FLOORBIT_AGENT_METRICS_TOKEN', False),
    'buffer_max_mb': ('FLOORBIT_AGENT_BUFFER_MAX_MB', False),
    'buffer_max_hours': ('FLOORBIT_AGENT_BUFFER_MAX_HOURS', False),
    'telemetry_port': ('FLOORBIT_TELEMETRY_PORT', False),
    'vrrp_vips': ('FLOORBIT_AGENT_VRRP_VIPS', False),
    'vrrp_interface': ('FLOORBIT_AGENT_VRRP_INTERFACE', False),
    'vrrp_prefer': ('FLOORBIT_AGENT_VRRP_PREFER', False),
    'vrrp_id': ('FLOORBIT_AGENT_VRRP_ID', False),
    'vrrp_peers': ('FLOORBIT_AGENT_VRRP_PEERS', False),
    'log_level': ('FLOORBIT_AGENT_LOG_LEVEL', True),
    'log_summary': ('FLOORBIT_AGENT_LOG_SUMMARY', False),
}
# Other names the hall may have used for the same setting; they win too.
ALIASES = {'FLOORBIT_AGENT_LOG_LEVEL': ('LOG_LEVEL',)}
SECRETS = ('metrics_token',)
LOCAL_ONLY = ('FLOORBIT_AGENT_ALLOW_ACTIONS', 'FLOORBIT_AGENT_PLUGIN_UPDATES', 'FLOORBIT_AGENT_SWITCH_NETS',
              'FLOORBIT_AGENT_GAME_NETS', 'FLOORBIT_AGENT_PROBE_NETS')
SETTINGS_FILE = 'agent-settings.json'
KEY_FILE = 'agent-key'
TLS_DIR = 'tls'
CA_NAME = 'Floorbit local agents CA'
RENEW_DAYS = 30
CERT_RETRY = 600          # seconds between tries after the hosted side said no
# The tenant's Let's Encrypt certificate (floorbit/public_cert.py): its key comes sealed to ours.
SEAL_ALG = 'ECDH-ES+A256GCM'
SEAL_INFO = b'floorbit agent tls key v1'
# Where the Let's Encrypt pair is handed to the listeners (server_context), next to the CA's pair in
# FLOORBIT_AGENT_TLS_CERT/_KEY. Set by the agent itself, not by the hall.
PUBLIC_CERT_ENV, PUBLIC_KEY_ENV = 'FLOORBIT_AGENT_TLS_PUBLIC_CERT', 'FLOORBIT_AGENT_TLS_PUBLIC_KEY'


def _write_private(path, text):
    """Written whole and swapped in, readable only by the agent: it holds a key or a token."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _as_env(value):
    if isinstance(value, list):
        return ','.join(str(v) for v in value)
    return str(value)


def _relevel(env):
    # The log level is read at startup (logs.setup); setting it up again takes the new level now.
    try:
        from . import logs
    except ImportError:
        return
    setup = getattr(logs, 'setup', None)
    if setup:
        setup(env)


class Settings:
    def __init__(self, root, env=None, log=print):
        self.env = os.environ if env is None else env
        self.root = root
        self.log = log
        # What the hall set itself, before anything from Floorbit went in: that wins.
        self.own = {var: next((str(self.env.get(v) or '').strip() for v in (var, *ALIASES.get(var, ()))
                              if str(self.env.get(v) or '').strip()), '')
                    for var, _ in FIELDS.values()}
        self.values = self._load()
        self.pending = set()
        self._apply(self.values, starting=True)

    def _path(self):
        return os.path.join(self.root, SETTINGS_FILE)

    def _load(self):
        try:
            with open(self._path(), encoding='utf-8') as f:
                found = json.load(f)
            return found if isinstance(found, dict) else {}
        except (OSError, ValueError):
            return {}

    def _apply(self, values, starting=False):
        for key, (var, live) in FIELDS.items():
            if self.own[var]:
                continue
            value = values.get(key)
            wanted = _as_env(value) if value not in (None, '', []) else ''
            if wanted == str(self.env.get(var) or ''):
                continue
            if wanted:
                self.env[var] = wanted
            else:
                self.env.pop(var, None)
            if not starting and not live:
                self.pending.add(key)
            if not starting and key == 'log_level':
                _relevel(self.env)

    def take(self, settings):
        """Floorbit's settings for this agent. True when they changed."""
        if not isinstance(settings, dict):
            return False
        settings = {k: v for k, v in settings.items() if k in FIELDS}
        if settings == self.values:
            return False
        try:
            _write_private(self._path(), json.dumps(settings))
        except OSError as exc:
            self.log(f'[agent] cannot keep the settings from Floorbit in {self._path()}: {exc}')
        self.values = settings
        self._apply(settings)
        if self.pending:
            self.log(f'[agent] new settings from Floorbit; at the next start: {", ".join(sorted(self.pending))}')
        else:
            self.log('[agent] new settings from Floorbit')
        return True

    def report(self):
        values = {}
        for key, (var, _) in FIELDS.items():
            if self.own[var]:
                source, value = 'env', self.own[var]
            elif self.values.get(key) not in (None, '', []):
                source, value = 'floorbit', _as_env(self.values[key])
            else:
                source, value = 'default', ''
            row = {'source': source}
            if key in SECRETS:
                if value:
                    row['set'] = True
            elif value:
                row['value'] = value
            values[key] = row
        for var in LOCAL_ONLY:
            value = str(self.env.get(var) or '').strip()
            values[var] = {'source': 'env', 'value': value} if value else {'source': 'default'}
        return {'values': values, 'pending': sorted(self.pending)}


# ── the key ─────────────────────────────────────────────────────────────────────────────────────

def stored_key(root):
    """The key fetched from Floorbit, or ''."""
    try:
        with open(os.path.join(root, KEY_FILE), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def drop_stored_key(root):
    """The stored key was refused (the agent was enrolled again): put it aside, start with the env's."""
    path = os.path.join(root, KEY_FILE)
    with contextlib.suppress(OSError):
        os.replace(path, f'{path}.refused')


def fetch_key(agent, root, log=print):
    """Floorbit asked for a new key: fetch it, keep it, use it. False when that didn't work; the
    agent asks again with its next config."""
    resp = agent.http.post(f'{agent.url}/api/agent/v1/key', headers=agent._headers(json_body=True), timeout=30)
    if resp.status_code == 409:
        return False
    body = agent._check(resp)
    key = str(body.get('key') or '').strip()
    if not key.startswith('fba_'):
        log('[agent] Floorbit sent no usable key')
        return False
    try:
        _write_private(os.path.join(root, KEY_FILE), key)
    except OSError as exc:
        # It works until the agent restarts; then FLOORBIT_AGENT_KEY is the old one, which stops
        # working once this one was used. Said loudly, so someone fixes the folder first.
        log(f'[agent] cannot keep the new key in {root}: {exc}. Make the state folder writable, '
            'or enrol the agent again after its next restart.')
    agent.key = key
    agent.key_source = 'stored'
    os.environ['FLOORBIT_AGENT_KEY'] = key      # the in-process plugins check they run with the key
    log(f'[agent] fetched a new key from Floorbit; kept in {os.path.join(root, KEY_FILE)}')
    return True


def open_sealed(private_key, box):
    """The key Floorbit sealed to ours (floorbit/public_cert.py seal_to): ECDH P-256, HKDF-SHA256,
    AES-256-GCM."""
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    if not isinstance(box, dict) or box.get('alg') != SEAL_ALG:
        raise ValueError('Floorbit sent the key in a form this agent does not know.')
    epk = base64.b64decode(box['epk'])
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), epk)
    secret = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk, info=SEAL_INFO).derive(
        private_key.exchange(ec.ECDH(), peer))
    return AESGCM(secret).decrypt(base64.b64decode(box['nonce']), base64.b64decode(box['data']), SEAL_INFO).decode()


# ── the certificate ─────────────────────────────────────────────────────────────────────────────

def read_cert(path):
    """(names, not_after, issuer CN) of a certificate file, or None."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    try:
        with open(path, 'rb') as f:
            cert = x509.load_pem_x509_certificates(f.read())[0]
    except (OSError, ValueError, IndexError):
        return None
    try:
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n) for n in sans.get_values_for_type(x509.DNSName)] + \
                [str(n) for n in sans.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    issuer = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    return names, cert.not_valid_after_utc.timestamp(), issuer[0].value if issuer else ''


def _name_matches(name, names):
    name = name.rstrip('.').lower()
    for n in names:
        n = n.lower()
        if n == name or (n.startswith('*.') and name.count('.') == n.count('.') and name.endswith(n[1:])):
            return True
    return False


def server_context(env, now=time.time):
    """The TLS context for the agent's HTTPS listeners, or None without a certificate.

    Two pairs, chosen per connection by the name the browser asks for (SNI): the tenant's Let's
    Encrypt certificate for its own name while it is valid, the CA's (or the hall's) for everything
    else: the agent's address, a name from the CA certificate, no name at all. So a laptop that came
    by the public name gets a certificate it trusts, and crew that came by IP still get one that
    matches. Raises OSError or ssl.SSLError when the CA's pair can't be loaded."""
    import ssl

    def context(cert, key):
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        c.minimum_version = ssl.TLSVersion.TLSv1_2
        c.load_cert_chain(cert, key)
        return c
    public = None
    pcert, pkey = str(env.get(PUBLIC_CERT_ENV) or '').strip(), str(env.get(PUBLIC_KEY_ENV) or '').strip()
    found = read_cert(pcert) if pcert and pkey else None
    if found and found[1] > now():
        try:
            public = context(pcert, pkey)
        except (OSError, ssl.SSLError):
            public = None    # a broken pair: the CA's serves everyone
    cert = str(env.get('FLOORBIT_AGENT_TLS_CERT') or '').strip()
    key = str(env.get('FLOORBIT_AGENT_TLS_KEY') or '').strip()
    if not cert or not key:
        return public
    default = context(cert, key)
    if public is not None:
        names, not_after = found[0], found[1]

        def pick(sock, server_name, _context):
            # Expired while the listener ran: the CA's again, until a renewed one restarts it.
            if server_name and _name_matches(server_name, names) and now() < not_after:
                sock.context = public
        default.sni_callback = pick
    return default


class Certificate:
    def __init__(self, root, env=None, log=print, now=time.time):
        self.env = os.environ if env is None else env
        self.dir = os.path.join(root, TLS_DIR)
        self.log = log
        self.now = now
        self.hall_set = bool(str(self.env.get('FLOORBIT_AGENT_TLS_CERT') or '').strip())
        self.failed_at = 0
        self.public_failed_at = 0
        if not self.hall_set:
            self._use()

    @property
    def cert_path(self):
        return os.path.join(self.dir, 'agent.pem')

    @property
    def key_path(self):
        """The agent's own key: signs its requests, opens the shared key. Never leaves this machine."""
        return os.path.join(self.dir, 'agent-key.pem')

    @property
    def public_cert_path(self):
        """The tenant's Let's Encrypt certificate, served next to the CA's (server_context)."""
        return os.path.join(self.dir, 'public.pem')

    @property
    def shared_key_path(self):
        """The tenant's Let's Encrypt key, shared by its agents."""
        return os.path.join(self.dir, 'public-key.pem')

    @property
    def public(self):
        return os.path.exists(self.public_cert_path) and os.path.exists(self.shared_key_path)

    def _use(self):
        if os.path.exists(self.cert_path) and os.path.exists(self.key_path):
            self.env['FLOORBIT_AGENT_TLS_CERT'] = self.cert_path
            self.env['FLOORBIT_AGENT_TLS_KEY'] = self.key_path
        if self.public:
            self.env[PUBLIC_CERT_ENV], self.env[PUBLIC_KEY_ENV] = self.public_cert_path, self.shared_key_path
        else:
            self.env.pop(PUBLIC_CERT_ENV, None)
            self.env.pop(PUBLIC_KEY_ENV, None)

    def _read(self, path=None):
        return read_cert(path or self.cert_path)

    def wanted(self, block):
        """Does Floorbit's ``{"names", "asked"}`` call for a new certificate?"""
        if self.hall_set or not isinstance(block, dict) or not block.get('names'):
            return False
        if self.now() - self.failed_at < CERT_RETRY:
            return False
        found = self._read()
        if found is None:
            return True
        names, not_after, _ = found
        try:
            issued = os.path.getmtime(self.cert_path)
        except OSError:
            issued = 0
        return (sorted(names) != sorted(str(n) for n in block['names'])
                or not_after - self.now() < RENEW_DAYS * 86400
                or (block.get('asked') or 0) > issued)

    def fetch(self, agent, block):
        """Make a key (once), ask Floorbit to sign it, serve with the answer. True when it changed."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        try:
            with open(self.key_path, 'rb') as f:
                key = serialization.load_pem_private_key(f.read(), None)
        except (OSError, ValueError):
            key = ec.generate_private_key(ec.SECP256R1())
            _write_private(self.key_path, key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()).decode())
        names = [str(n) for n in block['names']]
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64])]))
               .sign(key, hashes.SHA256()))
        resp = agent.http.post(f'{agent.url}/api/agent/v1/cert', headers=agent._headers(json_body=True),
                               data=json.dumps({'csr': csr.public_bytes(serialization.Encoding.PEM).decode()}),
                               timeout=30)
        if resp.status_code == 400:
            self.failed_at = self.now()
            try:
                why = resp.json().get('error')
            except ValueError:
                why = None
            self.log(f'[agent] Floorbit did not sign the certificate: {why or "HTTP 400"}')
            return False
        pem = str(agent._check(resp).get('cert') or '')
        if 'BEGIN CERTIFICATE' not in pem:
            self.failed_at = self.now()
            return False
        _write_private(self.cert_path, pem)
        self._use()
        self.log(f'[agent] new certificate from Floorbit for {", ".join(names)}')
        return True

    # ── the tenant's Let's Encrypt certificate ──

    def _own_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        try:
            with open(self.key_path, 'rb') as f:
                key = serialization.load_pem_private_key(f.read(), None)
            if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
                return key
        except (OSError, ValueError):
            pass
        key = ec.generate_private_key(ec.SECP256R1())
        _write_private(self.key_path, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
        return key

    def public_wanted(self, block):
        """Does Floorbit's ``{"name", "asked"}`` call for its Let's Encrypt certificate?"""
        if self.hall_set or not isinstance(block, dict) or not block.get('name'):
            return False
        if self.now() - self.public_failed_at < CERT_RETRY:
            return False
        found = self._read(self.public_cert_path) if self.public else None
        if found is None:
            return True
        try:
            issued = os.path.getmtime(self.public_cert_path)
        except OSError:
            issued = 0
        return found[0] != [str(block['name'])] or (block.get('asked') or 0) > issued

    def fetch_public(self, agent, block):
        """Fetch the tenant's certificate and its key, sealed to our own key. True when it changed."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID
        key = self._own_key()
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(block['name'])[:64])]))
               .sign(key, hashes.SHA256()))
        resp = agent.http.post(f'{agent.url}/api/agent/v1/cert', headers=agent._headers(json_body=True),
                               data=json.dumps({'csr': csr.public_bytes(serialization.Encoding.PEM).decode(),
                                                'public': True}), timeout=30)
        if resp.status_code in (400, 409):
            self.public_failed_at = self.now()
            try:
                why = resp.json().get('error')
            except ValueError:
                why = None
            self.log(f'[agent] no Let\'s Encrypt certificate from Floorbit yet: {why or resp.status_code}')
            return False
        body = agent._check(resp)
        pem = str(body.get('cert') or '')
        shared = open_sealed(key, body.get('key'))
        leaf = x509.load_pem_x509_certificates(pem.encode())[0]
        shared_key = serialization.load_pem_private_key(shared.encode(), None)
        if leaf.public_key().public_numbers() != shared_key.public_key().public_numbers():
            self.public_failed_at = self.now()
            self.log('[agent] the Let\'s Encrypt certificate and its key from Floorbit do not match; kept the old one')
            return False
        _write_private(self.shared_key_path, shared)
        _write_private(self.public_cert_path, pem)
        self._use()
        self.log(f'[agent] Let\'s Encrypt certificate from Floorbit for {block["name"]}')
        return True

    def drop_public(self):
        """Floorbit has no Let's Encrypt certificate for us any more: only the CA's serves. True when
        there was one."""
        if not os.path.exists(self.public_cert_path) and not os.path.exists(self.shared_key_path):
            return False
        for path in (self.public_cert_path, self.shared_key_path):
            with contextlib.suppress(OSError):
                os.remove(path)
        self._use()
        self.log('[agent] Floorbit has no Let\'s Encrypt certificate for us any more; serving the CA\'s')
        return True

    def report(self):
        path = str(self.env.get('FLOORBIT_AGENT_TLS_CERT') or '').strip()
        if not path:
            return None
        found = self._read(path)
        if found is None:
            return None
        names, not_after, issuer = found
        public = read_cert(self.public_cert_path) if not self.hall_set and self.public else None
        return {'names': names, 'not_after': not_after, 'source': 'env' if self.hall_set else 'floorbit',
                'floorbit_ca': issuer == CA_NAME,
                # Served to browsers that ask for its name (server_context); the CA's to the rest.
                'public': {'names': public[0], 'not_after': public[1]} if public else None}



class Hosted:
    """The three together, for the agent (Agent.take_hosted)."""
    def __init__(self, root, env=None, log=print):
        self.root = root
        self.settings = Settings(root, env, log)
        self.cert = Certificate(root, env, log)
        self.log = log
