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
    # Protocol 1 until the hosted side says it knows more: an older Floorbit would answer 426.
    assert host.last_headers["Floorbit-Agent-Protocol"] == "1"
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


# ── Protocol 2: the WebSocket link, against a small hosted side on a local port ─────────────────

class LinkHost:
    """hello, the long poll, answers and the WebSocket link, on 127.0.0.1 (nothing else)."""

    def __init__(self, link=True, refuse_link=False):
        import threading

        from werkzeug.serving import make_server

        self.link, self.refuse_link = link, refuse_link
        self.calls = [{"id": "1", "method": "devices", "args": []}]
        self.answers, self.via = [], []
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def app(self, environ, start_response):
        import simple_websocket
        from werkzeug.wrappers import Request, Response

        req = Request(environ)
        if req.path == "/api/agent/v1/hello":
            body = {"name": "Spring LAN", "server_version": "0.2.0", "encodings": ["gzip"], "protocol": 2}
            if self.link:
                body.update(protocols=[1, 2], link="/api/agent/v2/link")
            return Response(json.dumps(body), mimetype="application/json")(environ, start_response)
        if req.path == "/api/agent/v2/link":
            if self.refuse_link:
                return Response("{}", status=401)(environ, start_response)
            if not self.link:
                return Response("{}", status=404)(environ, start_response)
            ws = simple_websocket.Server(environ)
            ws.send(json.dumps({"type": "hello", "name": "Spring LAN", "protocol": 2}))
            calls, self.calls = self.calls, []
            ws.send(json.dumps({"type": "calls", "calls": calls}))
            message = json.loads(ws.receive(timeout=5))
            self.answers += message["answers"]
            self.via.append("link")
            ws.close()
            return Response()(environ, start_response)
        if req.path == "/api/agent/v1/calls":
            calls, self.calls = self.calls, []
            if not calls:
                time.sleep(0.2)
            return Response(json.dumps({"calls": calls}), mimetype="application/json")(environ, start_response)
        if req.path == "/api/agent/v1/answers":
            raw = req.get_data()
            if req.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            self.answers += json.loads(raw)["answers"]
            self.via.append("poll")
            return Response('{"taken": 1}', mimetype="application/json")(environ, start_response)
        return Response("{}", status=404)(environ, start_response)

    def close(self):
        self.server.shutdown()


def run_against(host, until, seconds=10):
    import threading

    import requests

    http = requests.Session()
    http.trust_env = False                 # straight to 127.0.0.1, never through a proxy
    logged = []
    a = agent_mod.Agent(host.url, "fba_x_y", lambda m, args: [{"name": "sw-1"}], http=http, log=logged.append)
    errors = []

    def go():
        try:
            a.run()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not until() and not errors:
        time.sleep(0.05)
    a.stop()
    t.join(5)
    return a, logged, errors


def test_hello_picks_the_protocol_and_the_link():
    a = make(FakeHost(hello={"name": "New", "protocol": 2, "protocols": [1, 2], "link": "/api/agent/v2/link"}))
    a.hello()
    assert a.protocol == 2 and a.link_path == "/api/agent/v2/link"
    old = make(FakeHost())                          # a hosted Floorbit of protocol 1
    old.hello()
    assert old.protocol == 1 and old.link_path is None
    assert old._headers()["Floorbit-Agent-Protocol"] == "1"


def test_calls_come_over_the_link():
    host = LinkHost()
    try:
        _, logged, errors = run_against(host, lambda: host.answers)
    finally:
        host.close()
    assert not errors
    assert host.answers == [{"id": "1", "ok": True, "result": [{"name": "sw-1"}]}] and host.via == ["link"]
    assert any("WebSocket link" in line for line in logged)


def test_without_the_link_it_long_polls():
    host = LinkHost(link=False)
    try:
        _, _, errors = run_against(host, lambda: host.answers)
    finally:
        host.close()
    assert not errors and host.via == ["poll"] and host.answers[0]["result"] == [{"name": "sw-1"}]


def test_a_link_that_cannot_open_falls_back_to_the_poll():
    host = LinkHost()
    original = host.app

    def broken(environ, start_response):
        from werkzeug.wrappers import Response
        if environ["PATH_INFO"] == "/api/agent/v2/link":
            return Response("{}", status=502)(environ, start_response)     # say a proxy without upgrades
        return original(environ, start_response)

    host.server.app = broken
    try:
        a, logged, errors = run_against(host, lambda: host.answers)
    finally:
        host.close()
    assert not errors and host.via == ["poll"]
    assert any("long poll" in line for line in logged) and a.link_after > time.monotonic()


def test_a_refused_key_on_the_link_stops_the_agent():
    host = LinkHost(refuse_link=True)
    try:
        _, _, errors = run_against(host, lambda: False, seconds=5)
    finally:
        host.close()
    assert len(errors) == 1 and isinstance(errors[0], agent_mod.Refused)


def test_a_link_that_hangs_gives_up_in_time():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        agent_mod._within(0.2, lambda: time.sleep(5))
    assert time.monotonic() - started < 2
    assert agent_mod._within(1, lambda: "open") == "open"
    with pytest.raises(ValueError):
        agent_mod._within(1, lambda: int("x"))
    closed = []
    with pytest.raises(TimeoutError):
        agent_mod._within(0.1, lambda: time.sleep(0.4) or "late link", late=closed.append)
    time.sleep(0.6)
    assert closed == ["late link"]                 # opened too late: closed, not left taking calls
