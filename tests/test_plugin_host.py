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

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked_with.append((headers or {}).get("If-None-Match"))
        if url.endswith("/api/agent/v1/plugins"):
            if self.config_status != 200:
                return Resp(self.config_status)
            if (headers or {}).get("If-None-Match") == f'"{self.etag}"':
                return Resp(304)
            return Resp(200, {"plugins": self.plugins, "etag": self.etag})
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
    assert ctx._send_some() == 1 and ctx.backlog() == 0
    url, body = hosted.posts[-1]
    assert url.endswith("/api/agent/v1/plugins/hallthing/stats") and body == {"payload": {"steam": {"hit_bytes": 5}}}
    hosted.post_status = 422                   # refused: dropped, not retried forever
    ctx.send("stats", {"steam": 1})
    deadline = time.time() + 5
    while time.time() < deadline and ctx.health_.report()["metrics"].get("rejected") != 1:
        ctx._wake.set()
        time.sleep(0.05)
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
