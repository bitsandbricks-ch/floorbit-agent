"""The agent against a fake hosted Floorbit: no network, no Floorbit server."""
import gzip
import json
import time

import pytest

import floorbit_agent as agent_mod
from floorbit_agent.syslog_relay import Relay, TurnedOff


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHost:
    """What the agent needs from requests.Session; records what it sent."""

    def __init__(self, hello=None, status=200):
        self.hello = hello if hello is not None else {"name": "Spring LAN", "server_version": "0.1.0",
                                                       "encodings": ["gzip"], "protocol": 1}
        self.status = status
        self.posts = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.last_headers = headers
        if url.endswith("/hello"):
            return Resp(self.status, self.hello if self.status == 200 else {"error": "no"})
        return Resp(self.status, {"calls": []})

    def post(self, url, data=None, headers=None, timeout=None):
        raw = gzip.decompress(data) if (headers or {}).get("Content-Encoding") == "gzip" else data
        self.posts.append((url, dict(headers or {}), json.loads(raw)))
        return Resp(self.status, {"taken": 1, "error": "Syslog is turned off"})


def make(host):
    return agent_mod.Agent("https://floor.example.com", "fba_x_y", lambda m, a: [], http=host, log=lambda *_: None)


def test_it_says_who_it_is_and_which_protocol():
    host = FakeHost()
    a = make(host)
    assert a.hello()["name"] == "Spring LAN"
    assert host.last_headers["Floorbit-Agent-Protocol"] == str(agent_mod.PROTOCOL)
    assert host.last_headers["Floorbit-Agent-Key"] == "fba_x_y"


def test_gzip_only_after_hello_says_so():
    host = FakeHost()
    a = make(host)
    big = [{"raw": "<14>" + "x" * 100, "from": "192.0.2.9", "at": 1}] * 100
    a.send_syslog(big)
    assert "Content-Encoding" not in host.posts[-1][1]
    a.hello()
    a.send_syslog(big)
    assert host.posts[-1][1]["Content-Encoding"] == "gzip" and len(host.posts[-1][2]["lines"]) == 100
    old = make(FakeHost(hello={"name": "Old", "server_version": "0.0.1"}))   # no "encodings": plain
    old.hello()
    old.send_syslog(big)
    assert "Content-Encoding" not in old.http.posts[-1][1]


@pytest.mark.parametrize("status", [401, 426])
def test_a_refusal_stops_the_agent(status):
    with pytest.raises(agent_mod.Refused):
        make(FakeHost(status=status)).run()


def test_syslog_off_on_the_host_drops_the_backlog():
    a = make(FakeHost(status=409))
    relay = Relay(a.send_syslog, 0, log=lambda *_: None)
    relay.receive(b"<14>hello", "192.0.2.9")
    with pytest.raises(TurnedOff):
        relay.flush_once()
    assert not relay.lines


def test_the_backlog_waits_and_keeps_the_oldest():
    sent, up = [], {"ok": False}

    def send(lines, dropped):
        if not up["ok"]:
            raise RuntimeError("no route to host")
        sent.append((len(lines), dropped))

    relay = Relay(send, 0, backlog=3, log=lambda *_: None)
    for n in range(5):
        relay.receive(f"<14>line {n}".encode(), "192.0.2.9")
    with pytest.raises(RuntimeError):
        relay.flush_once()
    up["ok"] = True
    assert relay.flush_once() == 3 and sent == [(3, 2)]


def test_stopping_sends_what_waits():
    sent = []
    relay = Relay(lambda lines, dropped: sent.extend(lines), 0, log=lambda *_: None)
    relay.receive(b"<14>last words", "192.0.2.9")
    relay.stop()
    assert [x["raw"] for x in sent] == ["<14>last words"]


def test_health(tmp_path):
    beat = tmp_path / "beat"
    assert agent_mod.health(str(beat), out=lambda *_: None) == 1
    a = make(FakeHost())
    a.heartbeat = str(beat)
    a.beat()
    assert agent_mod.health(str(beat), out=lambda *_: None) == 0
    later = time.time() + agent_mod.HEALTHY_FOR + 5
    assert agent_mod.health(str(beat), now=later, out=lambda *_: None) == 1


def test_relays_refuse_a_bad_port():
    a = make(FakeHost())
    assert agent_mod.start_syslog(a, "") is None
    assert agent_mod.start_syslog(a, "not a port", out=lambda *_: None) is False


def test_its_own_version():
    assert agent_mod.agent_version() == (agent_mod.pathlib.Path(__file__).parent.parent / "VERSION").read_text().strip()


def test_main_knows_its_commands(monkeypatch, tmp_path):
    assert agent_mod.main(["delete-everything"]) == 2
    monkeypatch.delenv("FLOORBIT_URL", raising=False)
    assert agent_mod.main([]) == 2
    monkeypatch.setenv("FLOORBIT_AGENT_HEARTBEAT", str(tmp_path / "beat"))
    assert agent_mod.main(["health"]) == 1
