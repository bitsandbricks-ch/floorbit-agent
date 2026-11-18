"""The plugin host against a fake hosted Floorbit: config, start/stop, sends through the disk
spool, calls and actions, health in the report and /metrics. No network."""
import gzip
import json
import socket
import textwrap
import time
import urllib.request

import pytest

import floorbit_agent as agent_mod
from floorbit_agent.metrics import Metrics
from floorbit_agent.plugin_host import PluginHost

AGENT_PY = """
import time
from floorbit.plugins.agent import AgentPlugin as Base
from floorbit.plugins.base import PluginError

class AgentPlugin(Base):
    started = 0

    def start(self):
        AgentPlugin.started += 1
        if self.settings.get('fail'):
            raise RuntimeError('no log file')
        self.ctx.counter('lines', 3)
        self.ctx.gauge('disk_used_bytes', 1024)
        self.ctx.health('warning', 'cache 91% full')

    def check(self):
        return 'reading ' + str(self.settings.get('path'))

    def call_status(self, args):
        return {'up': True, 'asked': args}

    def action_clear(self, params):
        return {'message': 'cleared ' + params.get('what', 'all')}
"""


class Spec:
    def __init__(self, path, pid="hallthing"):
        self.id, self.path, self.error = pid, path, ""
        self.agent = {"module": "agent.py"}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHosted:
    def __init__(self, plugins=None):
        self.plugins = plugins or {}
        self.etag = "e1"
        self.posts = []
        self.post_status = 200
        self.config_status = 200
        self.asked_with = []
        self.ha = None

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked_with.append((headers or {}).get("If-None-Match"))
        if url.endswith("/api/agent/v1/plugins"):
            if self.config_status != 200:
                return Resp(self.config_status)
            if (headers or {}).get("If-None-Match") == f'"{self.etag}"':
                return Resp(304)
            return Resp(200, {"plugins": self.plugins, "etag": self.etag,
                              **({"ha": self.ha} if self.ha is not None else {})})
        if url.endswith("/api/agent/v1/plugins/hallthing/seats"):
            return Resp(200, {"data": {"seat_of": {"192.0.2.10": "A1"}}})
        if "/api/agent/v1/plugins/" in url:
            return Resp(409, {"error": "Traffic is turned off on this Floorbit."})
        return Resp(200, {"calls": []})

    def post(self, url, data=None, headers=None, timeout=None):
        if self.post_status is None:
            raise ConnectionError("uplink down")
        raw = gzip.decompress(data) if (headers or {}).get("Content-Encoding") == "gzip" else data
        self.posts.append((url, json.loads(raw)))
        return Resp(self.post_status, {"ok": True})


CONFIG = {"version": "1.0", "settings": {"path": "/logs/access.log"},
          "sends": {"stats": {"privacy": "aggregate"}}, "calls": ["status"], "actions": ["clear"]}


@pytest.fixture
def setup(tmp_path):
    folder = tmp_path / "hallthing"
    folder.mkdir()
    (folder / "agent.py").write_text(textwrap.dedent(AGENT_PY))
    hosted = FakeHosted({"hallthing": dict(CONFIG)})
    agent = agent_mod.Agent("https://floor.example.com", "fba_x_y", lambda m, a: [], http=hosted, log=lambda *_: None)
    lines = []
    host = PluginHost(agent, {"hallthing": Spec(folder)}, str(tmp_path / "state"), env={}, log=lines.append)
    agent.plugins = host
    yield host, hosted, agent, lines
    host.stop()


def test_it_runs_what_the_hosted_side_says(setup):
    host, hosted, _agent, lines = setup
    assert host.pull() and list(host.running) == ["hallthing"]
    assert host.pull() and hosted.asked_with[-1] == '"e1"'     # 304: nothing restarts
    plugin_cls = type(host.running["hallthing"].plugin)
    assert plugin_cls.started == 1
    # New settings: restarted with them.
    hosted.plugins = {"hallthing": {**CONFIG, "settings": {"path": "/other.log"}}}
    hosted.etag = "e2"
    host.pull()
    assert plugin_cls.started == 2 and host.running["hallthing"].ctx.settings == {"path": "/other.log"}
    # Turned off on the hosted side: stopped.
    hosted.plugins, hosted.etag = {}, "e3"
    host.pull()
    assert host.running == {} and any("stopped (turned off" in line for line in lines)


def test_a_plugin_that_fails_or_is_missing_is_reported_not_fatal(setup):
    host, hosted, _agent, _lines = setup
    hosted.plugins = {"hallthing": {**CONFIG, "settings": {"fail": True}}, "newer": dict(CONFIG)}
    host.pull()
    report = host.report()
    assert report["hallthing"] == {"status": "error", "message": "RuntimeError: no log file", "metrics": {},
                                   "errors": [], "running": False}
    assert "update the agent" in report["newer"]["message"]
    out = []
    assert host.check(out.append) is False and any("✗ plugin newer" in line for line in out)


def test_an_older_hosted_floorbit_is_left_alone(setup):
    host, hosted, _agent, _lines = setup
    hosted.config_status = 404
    assert host.pull() is False and host.supported is False and host.running == {}


def test_sends_are_spooled_and_checked(setup):
    host, hosted, _agent, _lines = setup
    host.pull()
    ctx = host.running["hallthing"].ctx
    from floorbit.plugins.base import PluginError
    with pytest.raises(PluginError, match="aggregate-only"):
        ctx.send("stats", {"client": "192.0.2.10"})
    with pytest.raises(PluginError, match="does not list"):
        ctx.send("other", {})
    hosted.post_status = None                  # uplink down: kept on disk
    ctx.send("stats", {"steam": {"hit_bytes": 5}})
    deadline = time.time() + 5
    while time.time() < deadline and not ctx.backlog():
        time.sleep(0.05)
    assert ctx.backlog() == 1 and hosted.posts == []
    hosted.post_status = 200
    ctx._send_some()                           # the sender thread may be the one that sends it
    assert ctx.backlog() == 0 and len(hosted.posts) == 1
    url, body = hosted.posts[-1]
    assert url.endswith("/api/agent/v1/plugins/hallthing/stats") and body == {"payload": {"steam": {"hit_bytes": 5}}}
    hosted.post_status = 422                   # refused: dropped, not retried forever
    ctx.send("stats", {"steam": 1})
    ctx._send_some()                           # don't wait out the sender's retry pause
    assert ctx.health_.report()["metrics"].get("rejected") == 1
    assert ctx.backlog() == 0 and "refused stats (HTTP 422)" in ctx.health_.report()["errors"][-1]["error"]


def test_calls_and_actions(setup):
    host, _hosted, _agent, _lines = setup
    host.pull()
    from floorbit.plugins.base import PluginError
    assert host.call(["hallthing", "call", "status", {"x": 1}]) == {"up": True, "asked": {"x": 1}}
    with pytest.raises(PluginError, match="no agent call reboot"):
        host.call(["hallthing", "call", "reboot", {}])
    with pytest.raises(PluginError, match="not running"):
        host.call(["other", "call", "status", {}])
    with pytest.raises(PluginError, match="FLOORBIT_AGENT_ALLOW_ACTIONS"):
        host.call(["hallthing", "action", "clear", {}])
    for allowed in ("hallthing.clear", "hallthing.*", "*"):
        host.env = {"FLOORBIT_AGENT_ALLOW_ACTIONS": f"other.x, {allowed}"}
        assert host.call(["hallthing", "action", "clear", {"what": "steam"}]) == {"message": "cleared steam"}
    host.env = {"FLOORBIT_AGENT_ALLOW_ACTIONS": "other.*"}
    with pytest.raises(PluginError):
        host.call(["hallthing", "action", "clear", {}])


def test_calls_come_through_the_tunnel_methods(setup):
    host, _hosted, _agent, _lines = setup
    host.pull()
    execute = agent_mod.local_executor(None, {"host": host})
    assert execute("plugin", ["hallthing", "call", "status", {}])["up"] is True
    assert execute("plugins_reload", []) == {"ok": True} and host._reload.is_set()
    from floorbit.plugins.base import PluginError
    with pytest.raises(PluginError, match="runs no plugins"):
        agent_mod.local_executor(None, {})("plugin", [])


def test_health_goes_into_the_report_and_metrics(setup):
    host, _hosted, agent, _lines = setup
    host.pull()
    report = agent.report()["plugins"]["hallthing"]
    assert report["status"] == "warning" and report["message"] == "cache 91% full" and report["running"]
    assert report["metrics"] == {"backlog": 0, "disk_used_bytes": 1024, "lines": 3}
    m = Metrics()
    m.collect(host.collect)
    text = m.render()
    assert 'floorbit_agent_plugin_lines{plugin="hallthing"} 3' in text
    assert 'floorbit_agent_plugin_up{plugin="hallthing"} 1' in text
    out = []
    assert host.check(out.append) and out == ["  ✓ plugin hallthing: reading /logs/access.log"]


def test_listeners_stop_with_the_plugin(setup):
    host, _hosted, _agent, _lines = setup
    host.pull()
    ctx = host.running["hallthing"].ctx
    got = []
    udp = ctx.listen_udp(0, lambda data, peer: got.append(data), host="127.0.0.1")
    http = ctx.listen_http(0, lambda method, path, headers, body: (204, b""), host="127.0.0.1")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(b"hello", ("127.0.0.1", udp))
    req = urllib.request.Request(f"http://127.0.0.1:{http}/write", data=b"cpu value=1", method="POST")
    assert urllib.request.urlopen(req, timeout=5).status == 204
    deadline = time.time() + 5
    while time.time() < deadline and not got:
        time.sleep(0.05)
    assert got == [b"hello"]
    host.stop()
    with pytest.raises(OSError):
        urllib.request.urlopen(req, timeout=2)


def test_failed_logins_are_not_tried_again_until_cleared(setup, tmp_path):
    host, _hosted, agent, _lines = setup
    host.pull()
    ctx = host.running["hallthing"].ctx
    assert ctx.may_login("sw-a-01", "ssh-ops", 3)
    ctx.auth_failed("sw-a-01", "ssh-ops", 3, "Authentication failed")
    ctx.auth_failed("sw-a-01", "ssh-ops", 3, "again")          # said once
    assert not ctx.may_login("sw-a-01", "ssh-ops", 3)
    assert ctx.may_login("sw-a-01", "ssh-ops", 4)               # the password was edited
    assert ctx.may_login("sw-a-02", "ssh-ops", 3)
    blocked = agent.report()["plugins"]["hallthing"]["auth_blocked"]
    assert [(b["device"], b["credential"], b["rev"]) for b in blocked] == [("sw-a-01", "ssh-ops", "3")]
    assert agent._report_at is None                              # reported at once
    # Kept on disk: a restart doesn't try it either.
    from floorbit.plugins.agent import AuthBlocks
    assert not AuthBlocks(ctx.auth.path).may_login("sw-a-01", "ssh-ops", 3)
    assert host.call(["hallthing", "unblock", "", {"device": "sw-a-01", "credential": "ssh-ops"}]) == {"cleared": 1}
    assert ctx.may_login("sw-a-01", "ssh-ops", 3) and agent.report()["plugins"]["hallthing"]["auth_blocked"] == []


def _self_signed(folder):
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent.example.com")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    return str(folder / "cert.pem"), str(folder / "key.pem")


def test_a_listener_can_serve_tls_when_the_hall_gave_a_certificate(setup, tmp_path):
    import ssl
    host, _hosted, _agent, lines = setup
    host.pull()
    ctx = host.running["hallthing"].ctx
    plain = ctx.listen_http(0, lambda *a: (204, b""), host="127.0.0.1", tls=True)      # no cert: plain
    assert ctx.tls is False and any("plain HTTP" in line for line in lines)
    assert urllib.request.urlopen(f"http://127.0.0.1:{plain}/", timeout=5).status == 204
    cert, key = _self_signed(tmp_path)
    host.env = {"FLOORBIT_AGENT_TLS_CERT": cert, "FLOORBIT_AGENT_TLS_KEY": key}
    port = ctx.listen_http(0, lambda m, p, h, b: (200, h.get("Authorization", "")), host="127.0.0.1", tls=True)
    assert ctx.tls is True
    trust = ssl.create_default_context(cafile=cert)
    req = urllib.request.Request(f"https://127.0.0.1:{port}/write", data=b"x", headers={"Authorization": "Token t"})
    assert urllib.request.urlopen(req, timeout=5, context=trust).read() == b"Token t"
    with pytest.raises(OSError):        # plain HTTP to a TLS listener gets nowhere
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
    host.env = {"FLOORBIT_AGENT_TLS_CERT": str(tmp_path / "missing.pem"), "FLOORBIT_AGENT_TLS_KEY": key}
    from floorbit.plugins.base import PluginError
    with pytest.raises(PluginError, match="cannot use the TLS certificate"):
        ctx.listen_http(0, lambda *a: (204, b""), host="127.0.0.1", tls=True)


class FakeRegistry:
    def __init__(self, store, on=()):
        self.settings_store = store
        self.specs = {pid: object() for pid in ("librenms", "netbox", "pihole")}
        self.on = set(on)

    def enabled(self, spec):
        return any(self.specs[p] is spec for p in self.on)

    def set_enabled(self, pid, on):
        (self.on.add if on else self.on.discard)(pid)


def test_plugins_without_an_agent_part_follow_the_hosted_side(tmp_path):
    import flask
    from floorbit.runtime import rt
    from floorbit.settings import SettingsStore

    from floorbit_agent.plugin_host import LocalPlugins
    store = SettingsStore(str(tmp_path / "data.json"), environ={"LIBRENMS_TOKEN": "hall-token"})
    registry = FakeRegistry(store, on={"netbox"})
    lines = []
    local = LocalPlugins(flask.Flask("t"), registry, str(tmp_path), pinned=["pihole"], log=lines.append)
    local.apply({"librenms": {"settings": {"librenms.url": "https://librenms.example.com", "librenms.token": "hosted"}},
                 "pihole": {"settings": {}}, "unifi": {"settings": {}}})
    assert registry.on == {"netbox", "librenms", "pihole"}
    # Settings from the hosted side, kept in memory; the agent's own env still wins.
    assert rt.librenms_url == "https://librenms.example.com" and rt.librenms_token == "hall-token"
    assert not (tmp_path / "data.json").exists()
    assert any("unifi, which this agent does not have" in line for line in lines)
    # Turned off there: off here, except what the hall pinned and what was on before.
    local.apply({})
    assert registry.on == {"netbox", "pihole"} and rt.librenms_url == ""
    # Remembered across a restart.
    local.apply({"librenms": {"settings": {}}})
    again = LocalPlugins(flask.Flask("t"), registry, str(tmp_path), log=lines.append)
    again.apply({})
    assert "librenms" not in registry.on


def test_a_latest_kind_keeps_only_the_newest_and_fetches_ask_the_hosted_part(setup):
    host, hosted, _agent, _lines = setup
    sends = {**CONFIG["sends"], "summary": {"privacy": "device", "latest": True}}
    hosted.plugins = {"hallthing": {**CONFIG, "sends": sends, "fetches": ["seats", "other"]}}
    host.pull()
    ctx = host.running["hallthing"].ctx
    hosted.post_status = None                       # away: only the newest summary waits
    for i in range(5):
        ctx.send("summary", {"n": i})
    ctx._send_some()
    assert ctx.backlog() == 1 and hosted.posts == []
    hosted.post_status = 200
    ctx._send_some()
    assert [b for u, b in hosted.posts if u.endswith("/summary")] == [{"payload": {"n": 4}}] and ctx.backlog() == 0
    assert ctx.fetch("seats") == {"seat_of": {"192.0.2.10": "A1"}}
    from floorbit.plugins.agent import HostedSaidNo
    from floorbit.plugins.base import PluginError
    with pytest.raises(HostedSaidNo, match="Traffic is turned off"):
        ctx.fetch("other")
    with pytest.raises(PluginError, match="does not list"):
        ctx.fetch("secrets")


class FakeVrrp:
    def __init__(self, first="MASTER"):
        from floorbit_agent.vrrp import instance
        self.states = {instance(1): first}


def test_split_brain_the_hosted_side_names_who_polls(setup):
    import floorbit_agent.plugin_host as ph
    host, hosted, _agent, lines = setup
    assert host.primary()                                  # no VRRP: always primary
    host.vrrp = FakeVrrp("MASTER")
    host.pull()
    assert host.primary()                                  # an older Floorbit sends no "ha"
    hosted.ha, hosted.etag = {"stand_by_for": "a1"}, "e2"
    host.pull()
    assert not host.primary()                              # both MASTER: the hosted side settles it
    assert any("stands by" in line for line in lines)
    assert host.pull() and not host.primary()              # 304 keeps the hint fresh
    later = time.monotonic() + ph.HA_HINT_FRESH + 1
    assert host.primary(now=later)                         # uplink gone too long: VRRP alone again
    hosted.ha, hosted.etag = {}, "e3"
    host.pull()
    assert host.primary() and any("no longer standing by" in line for line in lines)
    host.vrrp = FakeVrrp("BACKUP")
    hosted.ha, hosted.etag = {"stand_by_for": "a1"}, "e4"
    host.pull()
    assert not host.primary()                              # a VRRP backup never polls anyway


def test_a_bad_hint_is_ignored(setup):
    host, hosted, _agent, _lines = setup
    host.vrrp = FakeVrrp("MASTER")
    for bad in ("a1", {"stand_by_for": 5}, {"stand_by_for": ""}, ["x"]):
        hosted.ha, hosted.etag = bad, f"e-{bad!r}"
        host.pull()
        assert host.primary(), bad
