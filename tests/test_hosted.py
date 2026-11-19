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

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, headers, json.loads(data) if data else None))
        if url.endswith("/api/agent/v1/key"):
            return Resp(self.key_status, {"key": "fba_0a1b2c3d_" + "n" * 30} if self.key_status == 200 else
                        {"error": "No new key was asked for this agent."})
        if url.endswith("/api/agent/v1/cert"):
            if self.cert_status != 200:
                return Resp(self.cert_status, {"error": "List the names first."})
            return Resp(200, {"cert": sign(json.loads(data)["csr"])})
        return Resp(200, {})


CA = {}


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
    assert s.take({"metrics": "0.0.0.0:9700", "lan_url": "http://192.0.2.10:8908", "vrrp_vips": ["192.0.2.10/24"],
                   "metrics_token": "example-token", "allow_actions": "*"})
    assert env["FLOORBIT_AGENT_METRICS"] == "127.0.0.1:9600"                  # the hall's own
    assert env["FLOORBIT_AGENT_LAN_URL"] == "http://192.0.2.10:8908"
    assert env["FLOORBIT_AGENT_VRRP_VIPS"] == "192.0.2.10/24"
    assert env["FLOORBIT_AGENT_ALLOW_ACTIONS"] == "lancache.*"                # never from Floorbit
    got = s.report()
    assert got["values"]["metrics"] == {"source": "env", "value": "127.0.0.1:9600"}
    assert got["values"]["lan_url"] == {"source": "floorbit", "value": "http://192.0.2.10:8908"}
    assert got["values"]["metrics_token"] == {"source": "floorbit", "set": True}
    assert got["values"]["buffer_max_mb"] == {"source": "default"}
    assert got["values"]["FLOORBIT_AGENT_ALLOW_ACTIONS"] == {"source": "env", "value": "lancache.*"}
    assert got["pending"] == ["metrics_token", "vrrp_vips"]                   # lan_url works at once
    assert "example-token" not in json.dumps(got)
    assert oct(os.stat(tmp_path / hosted.SETTINGS_FILE).st_mode & 0o777) == "0o600"
    # The next start takes them from the file, nothing pending; cleared ones go again.
    env2 = {}
    again = hosted.Settings(str(tmp_path), env2, log=lambda *_: None)
    assert env2["FLOORBIT_AGENT_VRRP_VIPS"] == "192.0.2.10/24" and again.report()["pending"] == []
    assert not again.take({"metrics": "0.0.0.0:9700", "lan_url": "http://192.0.2.10:8908",
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


