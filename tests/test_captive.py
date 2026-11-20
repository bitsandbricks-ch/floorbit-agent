"""The Captive Portal API (RFC 8908) the agent answers on the event network (Floorbit issue #473)."""
import json
import urllib.request

import pytest

from floorbit_agent import captive, front_door
from tests.test_agent import FakeHost, Resp, make

PORTAL = "https://floor.example.com/checkin"


def quiet(*_):
    pass


def serve(api):
    """The note and the hello are answered on the front door's port (one port for attendees)."""
    door = front_door.FrontDoor("https://floor.example.com", "fba_0123abcd_example-key-for-tests-only",
                                http=type("Away", (), {"request": lambda *a, **k: (_ for _ in ()).throw(OSError())})(),
                                log=quiet)
    return front_door.serve(door, "127.0.0.1:0", captive=api, log=quiet)


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
    server = serve(api)
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


CODE = "abcdefghijklmnop1234"


def test_the_seat_walk_hello_sends_the_laptop_back_only_to_its_floorbit():
    said = []
    api = captive.Captive(lambda: {"done": []}, log=quiet, say_hello=lambda code, ip: said.append((code, ip)),
                          floorbit_url="https://floor.example.com/")
    back = "https://floor.example.com/seat-walk?hello=" + CODE
    assert api.hello(CODE, "192.0.2.50", back) == (303, back, "")
    assert said == [(CODE, "192.0.2.50")]
    # Another host, or no back at all: a short page, never a redirect somewhere else.
    for other in ("https://evil.example.net/seat-walk", "http://floor.example.com/seat-walk", "",
                  # urlsplit drops line breaks before it compares: the header must not get them
                  "https://floor.example.com/seat-walk\r\nSet-Cookie: x=1",
                  "https://floor.example.com/\r\n\r\n<script>alert(1)</script>"):
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
    server = serve(api)

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
    # Nobody sets where laptops reach it: the front door learns it from the hall's requests.
    monkeypatch.setattr(front_door, "_seen", {"url": "", "at": 0.0})
    assert "lan_url" not in make(FakeHost()).report()
    front_door._saw("203.0.113.5", False, "203.0.113.9")        # not from the hall: ignored
    front_door._saw("checkin.lan", False, "10.10.1.20")
    assert make(FakeHost()).report()["lan_url"] == "http://checkin.lan"
    front_door._saw("evil.example/<x>", True, "10.10.1.20")      # not a host name: ignored
    assert make(FakeHost()).lan_url == "http://checkin.lan"


def test_it_counts_answers_and_says_when_the_list_is_failing():
    said = []
    api = captive.Captive(lambda: {"done": ["192.0.2.10"], "portal_url": PORTAL}, log=said.append)
    api.refresh()
    assert said == ["[captive] answering: 1 clients checked in"]
    api.answer("192.0.2.10")
    api.answer("192.0.2.20")
    api.answer("192.0.2.21")
    assert dict(api.asked) == {"captive": 2, "done": 1} and len(api.done) == 1 and not api.error

    def away():
        raise OSError("hosted is away")
    api.ask = away
    api.refresh()
    assert api.error


def test_a_laptop_cannot_make_the_agent_flood_the_hosted_side_with_hellos():
    said = []
    api = captive.Captive(lambda: {"done": []}, log=quiet, say_hello=lambda code, ip: said.append(ip),
                          floorbit_url="https://floor.example.com")
    codes = [api.hello(CODE, "192.0.2.50", "", now=100.0 + n)[0] for n in range(25)]
    assert codes == [200] * captive.HELLO_TRIES + [429] * (25 - captive.HELLO_TRIES)
    assert len(said) == captive.HELLO_TRIES             # the refused ones never reached Floorbit
    assert api.hello(CODE, "192.0.2.51", "", now=125.0)[0] == 200    # another laptop is not held up
    assert api.hello(CODE, "192.0.2.50", "", now=100.0 + captive.HELLO_WINDOW + 30)[0] == 200


def test_many_addresses_do_not_grow_the_hello_memory_without_end(monkeypatch):
    monkeypatch.setattr(captive, "HELLO_ADDRESSES", 50)
    api = captive.Captive(lambda: {"done": []}, log=quiet, say_hello=lambda code, ip: None)
    for n in range(500):
        api.hello(CODE, f"10.0.{n // 250}.{n % 250}", "", now=1.0)
    assert len(api._hellos) <= 50
