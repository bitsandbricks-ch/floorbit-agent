"""The Captive Portal API (RFC 8908) the agent answers on the event network (Floorbit issue #473)."""
import json
import urllib.request

import pytest

import floorbit_agent as agent_mod
from floorbit_agent import captive
from tests.test_agent import FakeHost, Resp, make

PORTAL = "https://floor.example.com/checkin"


def quiet(*_):
    pass


def test_only_a_known_client_that_did_not_check_in_sees_the_note():
    answers = [{"done": ["192.0.2.10", "not an ip"], "portal_url": PORTAL}]
    api = captive.Captive(lambda: answers[-1], log=quiet)
    assert api.answer("192.0.2.20") == {"captive": False}        # nothing asked yet: say nothing
    assert api.refresh(now=1) == captive.REFRESH
    assert api.answer("192.0.2.10") == {"captive": False}
    assert api.answer("192.0.2.20") == {"captive": True, "user-portal-url": PORTAL}
    assert api.answer("garbage") == {"captive": False}

    def away():
        raise OSError("hosted is away")
    api.ask = away
    assert api.refresh() == captive.RETRY
    assert api.answer("192.0.2.20")["captive"] is True               # the last answer stays

    def off():
        raise RuntimeError("Check-in is turned off on this Floorbit.")
    api.ask = off
    api.refresh()
    assert api.answer("192.0.2.20") == {"captive": False}


def test_the_portal_url_can_be_set_here_and_the_proxy_is_trusted():
    api = captive.Captive(lambda: {"done": [], "portal_url": PORTAL}, portal_url="https://checkin.example.net/",
                          proxies=["198.51.100.1"], log=quiet)
    api.refresh()
    assert api.answer("192.0.2.5")["user-portal-url"] == "https://checkin.example.net/"
    assert api.client_ip("198.51.100.1", "203.0.113.9, 192.0.2.5") == "192.0.2.5"
    assert api.client_ip("192.0.2.66", "192.0.2.5") == "192.0.2.66"     # not our proxy: its word is ignored
    assert api.client_ip("198.51.100.1", "") == "198.51.100.1"


def test_it_answers_over_http_as_captive_json():
    api = captive.Captive(lambda: {"done": [], "portal_url": PORTAL}, log=quiet)
    api.refresh()
    server = captive.serve(api, "127.0.0.1:0", log=quiet)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{captive.PATH}", timeout=5) as resp:
            assert resp.headers["Content-Type"] == "application/captive+json"
            assert "no-store" in resp.headers["Cache-Control"]
            assert json.loads(resp.read()) == {"captive": True, "user-portal-url": PORTAL}
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=5)
    finally:
        api.stop()
        server.shutdown()


def test_the_agent_asks_the_hosted_side_and_starts_only_when_set():
    class Host(FakeHost):
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/api/agent/v1/captive"):
                return Resp(self.status, {"done": ["192.0.2.10"], "portal_url": PORTAL}
                            if self.status == 200 else {"error": "Check-in is turned off on this Floorbit."})
            return super().get(url, params, headers, timeout)

    assert make(Host()).captive_state() == {"done": ["192.0.2.10"], "portal_url": PORTAL}
    with pytest.raises(RuntimeError, match="turned off"):
        make(Host(status=409)).captive_state()
    a = make(Host())
    assert agent_mod.start_captive(a, {}) is None
    assert agent_mod.start_captive(a, {"FLOORBIT_AGENT_CAPTIVE": "off"}) is None
    assert agent_mod.start_captive(a, {"FLOORBIT_AGENT_CAPTIVE": "nope"}, out=quiet) is False
    served = agent_mod.start_captive(a, {"FLOORBIT_AGENT_CAPTIVE": "127.0.0.1:0"}, out=quiet)
    served.stop()


CODE = "abcdefghijklmnop1234"


def test_the_seat_walk_hello_sends_the_laptop_back_only_to_its_floorbit():
    said = []
    api = captive.Captive(lambda: {"done": []}, log=quiet, say_hello=lambda code, ip: said.append((code, ip)),
                          floorbit_url="https://floor.example.com/")
    back = "https://floor.example.com/seat-walk?hello=" + CODE
    assert api.hello(CODE, "192.0.2.50", back) == (303, back, "")
    assert said == [(CODE, "192.0.2.50")]
    # Another host, or no back at all: a short page, never a redirect somewhere else.
    for other in ("https://evil.example.net/seat-walk", "http://floor.example.com/seat-walk", ""):
        status, location, text = api.hello(CODE, "192.0.2.50", other)
        assert (status, location) == (200, None) and "192.0.2.50" in text

    def refused(code, ip):
        raise RuntimeError("That walk-through code is unknown, used or too old.")
    api.say_hello = refused
    status, location, text = api.hello(CODE, "192.0.2.50", back)
    assert status == 502 and location is None and "too old" in text
    assert captive.Captive(lambda: {}, log=quiet).hello(CODE, "192.0.2.50", back)[0] == 404


def test_the_hello_over_http_redirects_and_escapes_its_page():
    said = []
    api = captive.Captive(lambda: {"done": []}, log=quiet, proxies=["127.0.0.1"],
                          say_hello=lambda code, ip: said.append((code, ip)), floorbit_url="https://floor.example.com")
    server = captive.serve(api, "127.0.0.1:0", log=quiet)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}/floorbit/hello/{CODE}"
        req = urllib.request.Request(base + "?back=https%3A%2F%2Ffloor.example.com%2Fseat-walk",
                                     headers={"X-Forwarded-For": "192.0.2.50"})
        with pytest.raises(urllib.error.HTTPError) as moved:
            opener.open(req, timeout=5)
        assert moved.value.code == 303 and moved.value.headers["Location"] == "https://floor.example.com/seat-walk"
        assert said == [(CODE, "192.0.2.50")]     # the laptop's own IP, through the trusted proxy
        with urllib.request.urlopen(base + "?back=<script>", timeout=5) as resp:
            assert "text/html" in resp.headers["Content-Type"] and b"<script>" not in resp.read()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/floorbit/hello/short", timeout=5)
    finally:
        api.stop()
        server.shutdown()


def test_the_agent_reports_the_hello_and_where_laptops_reach_it(monkeypatch):
    host = FakeHost()
    a = make(host)
    a.seat_walk_hello(CODE, "192.0.2.50")
    assert host.posts[-1][0].endswith("/api/agent/v1/seat-walk")
    assert host.posts[-1][2] == {"token": CODE, "ip": "192.0.2.50"}
    with pytest.raises(RuntimeError, match="too old"):
        make(type("H", (FakeHost,), {"post": lambda self, *a, **k: Resp(404, {"error": "too old"})})()
             ).seat_walk_hello(CODE, "192.0.2.50")
    monkeypatch.setenv("FLOORBIT_AGENT_LAN_URL", "http://agent.lan.example.com:8908/")
    assert make(FakeHost()).lan_url == "http://agent.lan.example.com:8908"
    assert make(FakeHost()).report()["lan_url"] == "http://agent.lan.example.com:8908"


def test_metrics_count_answers_and_say_how_old_the_list_is_but_never_an_ip():
    from floorbit_agent import metrics as metrics_mod
    from tests.test_metrics import sample
    said = []
    api = captive.Captive(lambda: {"done": ["192.0.2.10"], "portal_url": PORTAL}, log=said.append)
    api.refresh()
    assert said == ["[captive] answering: 1 clients checked in"]
    api.answer("192.0.2.10")
    api.answer("192.0.2.20")
    api.answer("192.0.2.21")
    m = metrics_mod.Metrics()
    m.collect(metrics_mod.relay_collector(captive=api))
    text = m.render()
    assert sample(text, "floorbit_agent_captive_answers_total", answer="captive") == 2
    assert sample(text, "floorbit_agent_captive_answers_total", answer="done") == 1
    assert sample(text, "floorbit_agent_captive_checked_in") == 1
    assert sample(text, "floorbit_agent_captive_failing") == 0
    assert sample(text, "floorbit_agent_captive_list_age_seconds") < 5
    assert "192.0.2." not in text

    def away():
        raise OSError("hosted is away")
    api.ask = away
    api.refresh()
    assert sample(m.render(), "floorbit_agent_captive_failing") == 1
