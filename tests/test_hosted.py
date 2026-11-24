"""What Floorbit sets on the agent's page (hosted.py): settings with the env winning, a key the
agent fetches itself, a certificate signed for it. Against a fake hosted Floorbit; no network."""
import json
import os

import pytest

import floorbit_agent as agent_mod
from floorbit_agent import hosted
from tests.test_plugin_host import Resp


class Hosted:
    def __init__(self):
        self.posts = []
        self.key_status = 200
        self.cert_status = 200
        self.public_status = 200
        self.public_name = "club.agent.example.com"

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, headers, json.loads(data) if data else None))
        if url.endswith("/api/agent/v1/key"):
            return Resp(self.key_status, {"key": "fba_0a1b2c3d_" + "n" * 30} if self.key_status == 200 else
                        {"error": "No new key was asked for this agent."})
        if url.endswith("/api/agent/v1/cert") and json.loads(data).get("public"):
            if self.public_status != 200:
                return Resp(self.public_status, {"error": "The agents' Let's Encrypt certificate isn't there yet."})
            return Resp(200, public_bundle(json.loads(data)["csr"], self.public_name))
        if url.endswith("/api/agent/v1/cert"):
            if self.cert_status != 200:
                return Resp(self.cert_status, {"error": "List the names first."})
            return Resp(200, {"cert": sign(json.loads(data)["csr"])})
        return Resp(200, {})


CA = {}


def public_bundle(csr_pem, name):
    """A stand-in for floorbit/public_cert.py: a certificate for ``name`` and its key, sealed to the
    key in the agent's request."""
    import base64
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder().subject_name(subject)
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake Let's Encrypt")]))
            .public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    agent_pub = x509.load_pem_x509_csr(csr_pem.encode()).public_key()
    eph = ec.generate_private_key(ec.SECP256R1())
    epk = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    secret = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk, info=hosted.SEAL_INFO).derive(
        eph.exchange(ec.ECDH(), agent_pub))
    nonce = os.urandom(12)
    data = AESGCM(secret).encrypt(nonce, key_pem, hosted.SEAL_INFO)
    enc = base64.b64encode
    return {"name": name, "cert": cert.public_bytes(serialization.Encoding.PEM).decode(),
            "key": {"alg": hosted.SEAL_ALG, "epk": enc(epk).decode(), "nonce": enc(nonce).decode(),
                    "data": enc(data).decode()}}


def sign(csr_pem):
    """A stand-in for Floorbit's agents CA (floorbit/agent_ca.py)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    if not CA:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosted.CA_NAME)])
        now = datetime.datetime.now(datetime.UTC)
        CA["key"], CA["cert"] = key, (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                                      .public_key(key.public_key()).serial_number(1).not_valid_before(now)
                                      .not_valid_after(now + datetime.timedelta(days=3650))
                                      .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                                      .sign(key, hashes.SHA256()))
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(CA["cert"].subject)
            .public_key(csr.public_key()).serial_number(2).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("agent.example.com")]), critical=False)
            .sign(CA["key"], hashes.SHA256()))
    pem = serialization.Encoding.PEM
    return (cert.public_bytes(pem) + CA["cert"].public_bytes(pem)).decode()


@pytest.fixture
def agent(tmp_path):
    a = agent_mod.Agent("https://floor.example.com", "fba_0a1b2c3d_" + "o" * 30, lambda m, args: [],
                        http=Hosted(), log=lambda *_: None)
    return a


def test_settings_from_floorbit_go_in_but_the_env_wins(tmp_path):
    env = {"FLOORBIT_AGENT_METRICS": "127.0.0.1:9600", "FLOORBIT_AGENT_ALLOW_ACTIONS": "lancache.*"}
    s = hosted.Settings(str(tmp_path), env, log=lambda *_: None)
    assert s.take({"metrics": "0.0.0.0:9700", "log_level": "debug", "vrrp_vips": ["192.0.2.10/24"],
                   "metrics_token": "example-token", "allow_actions": "*"})
    assert env["FLOORBIT_AGENT_METRICS"] == "127.0.0.1:9600"                  # the hall's own
    assert env["FLOORBIT_AGENT_LOG_LEVEL"] == "debug"
    assert env["FLOORBIT_AGENT_VRRP_VIPS"] == "192.0.2.10/24"
    assert env["FLOORBIT_AGENT_ALLOW_ACTIONS"] == "lancache.*"                # never from Floorbit
    got = s.report()
    assert got["values"]["metrics"] == {"source": "env", "value": "127.0.0.1:9600"}
    assert got["values"]["log_level"] == {"source": "floorbit", "value": "debug"}
    assert got["values"]["metrics_token"] == {"source": "floorbit", "set": True}
    assert got["values"]["buffer_max_mb"] == {"source": "default"}
    assert got["values"]["FLOORBIT_AGENT_ALLOW_ACTIONS"] == {"source": "env", "value": "lancache.*"}
    assert got["pending"] == ["metrics_token", "vrrp_vips"]                   # the log level works at once
    assert "example-token" not in json.dumps(got)
    assert oct(os.stat(tmp_path / hosted.SETTINGS_FILE).st_mode & 0o777) == "0o600"
    # The next start takes them from the file, nothing pending; cleared ones go again.
    env2 = {}
    again = hosted.Settings(str(tmp_path), env2, log=lambda *_: None)
    assert env2["FLOORBIT_AGENT_VRRP_VIPS"] == "192.0.2.10/24" and again.report()["pending"] == []
    assert not again.take({"metrics": "0.0.0.0:9700", "log_level": "debug",
                           "vrrp_vips": ["192.0.2.10/24"], "metrics_token": "example-token"})
    again.take({})
    assert "FLOORBIT_AGENT_VRRP_VIPS" not in env2


def test_the_agent_fetches_and_keeps_its_new_key(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOORBIT_AGENT_KEY", agent.key)
    agent.hosted = hosted.Hosted(str(tmp_path), env={}, log=lambda *_: None)
    agent.take_hosted({"settings": {}, "roll_key": True})
    assert agent.key.endswith("n" * 30) and agent.key_source == "stored"
    assert agent.http.posts[0][1]["Floorbit-Agent-Key"].endswith("o" * 30)    # asked with the old one
    assert hosted.stored_key(str(tmp_path)) == agent.key
    assert oct(os.stat(tmp_path / hosted.KEY_FILE).st_mode & 0o777) == "0o600"
    assert agent.report()["settings"]["key"] == "stored"
    agent.http.key_status = 409                       # asked already: nothing to fetch
    assert hosted.fetch_key(agent, str(tmp_path), log=lambda *_: None) is False
    hosted.drop_stored_key(str(tmp_path))
    assert hosted.stored_key(str(tmp_path)) == ""


def test_the_agent_gets_a_certificate_and_its_key_stays_here(agent, tmp_path):
    env = {}
    agent.hosted = hosted.Hosted(str(tmp_path), env=env, log=lambda *_: None)
    restarted = []
    agent.plugins = type("P", (), {"restart_tls": lambda self: restarted.append(1), "report": lambda self: {}})()
    block = {"names": ["agent.example.com"], "asked": 0}
    agent.take_hosted({"settings": {}, "cert": block})
    url, _, body = agent.http.posts[-1]
    assert url.endswith("/api/agent/v1/cert") and "PRIVATE" not in body["csr"]
    assert env["FLOORBIT_AGENT_TLS_CERT"].endswith("agent.pem") and restarted == [1]
    assert oct(os.stat(env["FLOORBIT_AGENT_TLS_KEY"]).st_mode & 0o777) == "0o600"
    report = agent.report()["settings"]["cert"]
    assert report["names"] == ["agent.example.com"] and report["floorbit_ca"] and report["source"] == "floorbit"
    # Same names, far from the end, not asked again: nothing to do. Asked after it: a new one.
    cert = agent.hosted.cert
    assert not cert.wanted(block)
    assert cert.wanted({**block, "asked": os.path.getmtime(cert.cert_path) + 10})
    assert cert.wanted({"names": ["other.example.com"]})
    # Restarted, the agent serves with it from the start.
    env2 = {}
    hosted.Certificate(str(tmp_path), env2)
    assert env2["FLOORBIT_AGENT_TLS_KEY"] == env["FLOORBIT_AGENT_TLS_KEY"]


def test_a_certificate_the_hall_set_wins_and_a_refusal_waits(agent, tmp_path):
    hall = {"FLOORBIT_AGENT_TLS_CERT": "/certs/agent.pem", "FLOORBIT_AGENT_TLS_KEY": "/certs/agent-key.pem"}
    cert = hosted.Certificate(str(tmp_path), hall)
    assert not cert.wanted({"names": ["agent.example.com"]})
    agent.hosted = hosted.Hosted(str(tmp_path / "b"), env={}, log=lambda *_: None)
    agent.http.cert_status = 400
    agent.take_hosted({"settings": {}, "cert": {"names": ["agent.example.com"]}})
    asked = len(agent.http.posts)
    agent.take_hosted({"settings": {}, "cert": {"names": ["agent.example.com"]}})
    assert len(agent.http.posts) == asked                     # not again within CERT_RETRY




def test_the_tenant_s_lets_encrypt_certificate_comes_next_to_the_ca_s_its_key_sealed(agent, tmp_path):
    env = {}
    agent.hosted = hosted.Hosted(str(tmp_path), env=env, log=lambda *_: None)
    restarted = []
    agent.plugins = type("P", (), {"restart_tls": lambda self: restarted.append(1), "report": lambda self: {}})()
    # Not there yet: the CA's serves meanwhile.
    agent.http.public_status = 409
    ca_block = {"names": ["agent.example.com"], "asked": 0}
    agent.take_hosted({"settings": {}, "cert": ca_block, "public_cert": {"name": "club.agent.example.com", "asked": 1}})
    assert [b.get("public") for _, _, b in agent.http.posts] == [None, True]
    assert env["FLOORBIT_AGENT_TLS_KEY"].endswith("agent-key.pem") and restarted == [1]
    assert hosted.PUBLIC_KEY_ENV not in env
    agent.hosted.cert.public_failed_at = 0
    agent.http.public_status = 200
    block = {"name": "club.agent.example.com", "asked": 1}
    agent.take_hosted({"settings": {}, "cert": ca_block, "public_cert": block})
    _, _, body = agent.http.posts[-1]
    assert body["public"] is True and "PRIVATE" not in body["csr"]
    # Both pairs: the CA's stays, the Let's Encrypt one comes next to it.
    assert env["FLOORBIT_AGENT_TLS_KEY"].endswith("agent-key.pem")
    assert env[hosted.PUBLIC_KEY_ENV].endswith("public-key.pem") and restarted == [1, 1]
    assert oct(os.stat(env[hosted.PUBLIC_KEY_ENV]).st_mode & 0o777) == "0o600"
    with open(env[hosted.PUBLIC_KEY_ENV]) as f:
        assert "BEGIN PRIVATE KEY" in f.read()
    report = agent.report()["settings"]["cert"]
    assert report["floorbit_ca"] and report["public"]["names"] == ["club.agent.example.com"]
    cert = agent.hosted.cert
    assert not cert.public_wanted(block) and not cert.wanted(ca_block)
    assert cert.public_wanted({**block, "asked": os.path.getmtime(cert.public_cert_path) + 10})   # renewed
    # Restarted, it serves both; the domain gone, only the CA's.
    env2 = {}
    hosted.Certificate(str(tmp_path), env2)
    assert env2[hosted.PUBLIC_KEY_ENV].endswith("public-key.pem")
    assert env2["FLOORBIT_AGENT_TLS_KEY"].endswith("agent-key.pem")
    agent.take_hosted({"settings": {}, "cert": ca_block})
    assert hosted.PUBLIC_KEY_ENV not in env and not cert.public and restarted == [1, 1, 1]
    assert env["FLOORBIT_AGENT_TLS_KEY"].endswith("agent-key.pem")


def _served(context, name):
    """The common name of the certificate a TLS server with ``context`` shows a browser asking for
    ``name`` (None: no SNI)."""
    import contextlib
    import ssl

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname, client.verify_mode = False, ssl.CERT_NONE
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    c = client.wrap_bio(c_in, c_out, server_hostname=name)
    s = context.wrap_bio(s_in, s_out, server_side=True)
    for _ in range(10):
        for side in (c, s):
            with contextlib.suppress(ssl.SSLWantReadError):
                side.do_handshake()
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if c.version() and s.version():
            break
    leaf = x509.load_der_x509_certificate(c.getpeercert(binary_form=True))
    return leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def test_listeners_serve_lets_encrypt_by_its_name_and_the_ca_s_to_the_rest(agent, tmp_path):
    env = {}
    agent.hosted = hosted.Hosted(str(tmp_path), env=env, log=lambda *_: None)
    agent.take_hosted({"settings": {}, "cert": {"names": ["192.0.2.10"]},
                       "public_cert": {"name": "club.agent.example.com", "asked": 1}})
    context = hosted.server_context(env)
    assert _served(context, "club.agent.example.com") == "club.agent.example.com"
    assert _served(context, "CLUB.agent.example.com.") == "club.agent.example.com"
    # By IP (no SNI), by a name on the CA's certificate, by any other name: the CA's.
    assert _served(context, None) == "192.0.2.10"
    assert _served(context, "agent.example.com") == "192.0.2.10"
    assert _served(context, "other.agent.example.com") == "192.0.2.10"
    # Expired (the clock moved past it): the CA's for its name too.
    late = hosted.server_context(env, now=lambda: 4e9)
    assert _served(late, "club.agent.example.com") == "192.0.2.10"
    # A broken Let's Encrypt pair never stops the listeners.
    with open(env[hosted.PUBLIC_KEY_ENV], "w") as f:
        f.write("not a key")
    assert _served(hosted.server_context(env), "club.agent.example.com") == "192.0.2.10"


def test_wildcard_names_match_one_label():
    assert hosted._name_matches("a.example.com", ["*.example.com"])
    assert not hosted._name_matches("a.b.example.com", ["*.example.com"])
    assert not hosted._name_matches("example.com", ["*.example.com"])


def test_a_sealed_key_for_another_agent_is_refused(agent, tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec
    box = public_bundle(_csr_of(ec.generate_private_key(ec.SECP256R1())), "club.agent.example.com")["key"]
    with pytest.raises(Exception):  # noqa: B017  # AES-GCM: InvalidTag
        hosted.open_sealed(ec.generate_private_key(ec.SECP256R1()), box)
    with pytest.raises(ValueError):
        hosted.open_sealed(ec.generate_private_key(ec.SECP256R1()), {"alg": "none"})


def _csr_of(key):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")])).sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def test_log_level_from_floorbit_now_and_hall_alias_wins(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hosted, "_relevel", lambda env: calls.append(env.get("FLOORBIT_AGENT_LOG_LEVEL")))
    env = {}
    s = hosted.Settings(str(tmp_path), env, log=lambda *_: None)
    assert s.take({"log_level": "debug", "log_summary": 60})
    assert env["FLOORBIT_AGENT_LOG_LEVEL"] == "debug" and calls == ["debug"]   # taken now
    assert s.pending == {"log_summary"}                                        # read at the next start

    hall = {"LOG_LEVEL": "warning"}
    s = hosted.Settings(str(tmp_path / "b"), hall, log=lambda *_: None)
    s.take({"log_level": "debug"})
    assert "FLOORBIT_AGENT_LOG_LEVEL" not in hall
    assert s.report()["values"]["log_level"] == {"source": "env", "value": "warning"}
