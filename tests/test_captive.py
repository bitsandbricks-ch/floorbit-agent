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
