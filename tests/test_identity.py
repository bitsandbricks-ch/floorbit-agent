"""The agent's own key signs every request (identity.py, bitsandbricks-ch/floorbit#1178). Against a
fake Floorbit that checks signatures the way Floorbit's agent_identity.py does; no network."""
import base64
import email.utils
import hashlib
import json
import os
import stat

import pytest
import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import floorbit_agent as agent_mod
from floorbit_agent import identity as ident
from tests.test_hosted import sign as ca_sign

KEY = "fba_0a1b2c3d_" + "o" * 30


def verify(public_key, method, target, header, body, agent_id="0a1b2c3d"):
    """Floorbit's side, written out again: the two must agree byte for byte."""
    parts = dict(p.split("=", 1) for p in header.split(","))
    digest = hashlib.sha256(body or b"").hexdigest()
    msg = "\n".join(("floorbit-agent-v1", method, target, parts["t"], parts["n"], agent_id, digest)).encode()
    public_key.verify(base64.b64decode(parts["s"]), msg, ec.ECDSA(hashes.SHA256()))
    return float(parts["t"]), parts["n"]


class Resp:
    def __init__(self, status=200, body=None, date=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = {"Date": date} if date else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Floorbit:
    """Pins the first key enrolled and wants every later hello signed with it."""
    def __init__(self, clock=None):
        self.pinned = None
        self.clock = clock
        self.enrolled = 0
        self.hellos = []

    def _date(self):
        return email.utils.formatdate(self.clock, usegmt=True) if self.clock else None

    def get(self, url, headers=None, timeout=None, params=None):
        assert url.endswith("/api/agent/v1/hello")
        signed = False
        if self.pinned and ident.HEADER in headers:
            stamp, _ = verify(self.pinned, "GET", "/api/agent/v1/hello", headers[ident.HEADER], b"")
            signed = self.clock is None or abs(stamp - self.clock) < 300
        self.hellos.append(signed)
        return Resp(200, {"name": "Hall", "protocols": [1, 2], "identity": {
            "pinned": self.pinned is not None, "signed": signed, "not_after": None, "renew_days": 30,
            "path": "/api/agent/v1/identity"}}, self._date())

    def post(self, url, data=None, headers=None, timeout=None):
        assert url.endswith("/api/agent/v1/identity")
        from cryptography import x509
        csr = x509.load_pem_x509_csr(json.loads(data)["csr"].encode())
        if self.pinned is not None:
            return Resp(409, {"error": "This agent already has a pinned key.", "code": "identity"})
        self.pinned = csr.public_key()
        self.enrolled += 1
        return Resp(200, {"cert": ca_sign(json.loads(data)["csr"]), "fingerprint": "f" * 32})


@pytest.fixture
def agent(tmp_path):
    a = agent_mod.Agent("https://floor.example.com", KEY, lambda m, args: [], http=Floorbit(), log=lambda *_: None)
    a.use_identity(ident.Identity(str(tmp_path), log=lambda *_: None))
    return a


def _sign_like_session(a, method, url, body=b""):
    """What the session's auth adds to a request (requests.Session.auth)."""
    req = requests.Request(method, url, data=body).prepare()
    a.signer(req)
    return req


def test_the_key_is_made_here_once_and_only_the_agent_can_read_it(tmp_path):
    one = ident.Identity(str(tmp_path), log=lambda *_: None)
    fp = one.fingerprint()
    assert stat.S_IMODE(os.stat(one.key_path).st_mode) == 0o600
    assert ident.Identity(str(tmp_path), log=lambda *_: None).fingerprint() == fp      # kept across restarts


def test_every_request_is_signed_over_method_path_query_and_body(agent):
    req = _sign_like_session(agent, "POST", "https://floor.example.com/api/agent/v1/answers?x=1", b'{"answers": []}')
    public = agent.identity.key().public_key()
    verify(public, "POST", "/api/agent/v1/answers?x=1", req.headers[ident.HEADER], b'{"answers": []}')
    with pytest.raises(InvalidSignature):     # another body: the signature doesn't hold
        verify(public, "POST", "/api/agent/v1/answers?x=1", req.headers[ident.HEADER], b'{"answers": [1]}')
    with pytest.raises(InvalidSignature):     # another path
        verify(public, "POST", "/api/agent/v1/syslog", req.headers[ident.HEADER], b'{"answers": []}')


def test_each_signature_has_its_own_nonce(agent):
    a = _sign_like_session(agent, "GET", "https://floor.example.com/api/agent/v1/calls?wait=25")
    b = _sign_like_session(agent, "GET", "https://floor.example.com/api/agent/v1/calls?wait=25")
    assert a.headers[ident.HEADER] != b.headers[ident.HEADER]


def test_the_session_signs_with_the_key_it_has_now(agent):
    agent.key = "fba_99999999_" + "n" * 30         # rolled: the id in the signature follows
    req = _sign_like_session(agent, "GET", "https://floor.example.com/api/agent/v1/hello")
    verify(agent.identity.key().public_key(), "GET", "/api/agent/v1/hello", req.headers[ident.HEADER], b"",
           agent_id="99999999")


def test_hello_enrols_the_key_once_then_signs_with_it(agent):
    floorbit = agent.http
    agent.hello()
    assert floorbit.enrolled == 1
    assert os.path.exists(agent.identity.cert_path)
    floorbit.get_orig = floorbit.get

    def signed_get(url, headers=None, timeout=None, params=None):
        headers = dict(headers or {})
        headers[ident.HEADER] = agent.identity.sign("GET", url, b"", "0a1b2c3d")
        return floorbit.get_orig(url, headers=headers, timeout=timeout)
    floorbit.get = signed_get
    agent.hello()
    assert floorbit.enrolled == 1 and floorbit.hellos[-1] is True


def test_a_wrong_clock_is_put_right_from_floorbits_date(agent, monkeypatch):
    floorbit = agent.http
    floorbit.clock = 1_900_000_000.0
    agent.identity.now = lambda: floorbit.clock - 3600         # this box is an hour behind
    agent.identity.learn_clock(Resp(200, {}, email.utils.formatdate(floorbit.clock, usegmt=True)))
    assert abs(agent.identity.offset - 3600) < 3
    stamp = float(agent.identity.sign("GET", "https://floor.example.com/x", b"", "0a1b2c3d").split(",")[0][2:])
    assert abs(stamp - floorbit.clock) < 3


def test_a_key_floorbit_does_not_know_waits_for_an_admin(agent):
    other = ec.generate_private_key(ec.SECP256R1())
    agent.http.pinned = other.public_key()          # pinned elsewhere: a new machine
    with pytest.raises(agent_mod.IdentityRefused, match="Reset the agent's identity"):
        agent.hello()
    assert agent.http.enrolled == 0                   # it doesn't try to take the pin over


def test_a_signature_refusal_is_not_a_fatal_key_refusal(agent):
    resp = Resp(401, {"error": "The agent's clock is off.", "code": "clock"})
    with pytest.raises(agent_mod.IdentityRefused):
        agent._check(resp)
    with pytest.raises(agent_mod.Refused):
        agent._check(Resp(401, {"error": "The agent key is wrong or was revoked."}))


def test_renews_30_days_before_the_end(tmp_path):
    i = ident.Identity(str(tmp_path), log=lambda *_: None, now=lambda: 1_000_000.0)
    assert i.wanted({"pinned": False})
    assert not i.wanted({"pinned": True, "signed": True, "not_after": 1_000_000 + 40 * 86400, "renew_days": 30})
    assert i.wanted({"pinned": True, "signed": True, "not_after": 1_000_000 + 20 * 86400, "renew_days": 30})
    assert not i.wanted({"pinned": True, "signed": False, "not_after": 0})


def test_the_link_upgrade_is_signed(agent):
    headers = agent._link_headers("wss://floor.example.com/api/agent/v2/link")
    verify(agent.identity.key().public_key(), "GET", "/api/agent/v2/link", headers[ident.HEADER], b"")


def test_the_front_door_signs_what_it_passes_on(agent):
    from floorbit_agent import front_door
    door = front_door.FrontDoor("https://floor.example.com", KEY, auth=agent.signer, log=lambda *_: None)
    req = door.http.prepare_request(requests.Request("POST", "https://floor.example.com/api/checkin",
                                                     data=b'{"pin": "1234"}'))
    verify(agent.identity.key().public_key(), "POST", "/api/checkin", req.headers[ident.HEADER], b'{"pin": "1234"}')


def test_the_public_key_is_p256(agent):
    key = agent.identity.key()
    assert isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1"
    with open(agent.identity.key_path, "rb") as f:
        assert serialization.load_pem_private_key(f.read(), None).public_key() == key.public_key()
