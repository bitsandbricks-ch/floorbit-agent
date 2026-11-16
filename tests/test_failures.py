"""The agent when things go wrong (issue #3): the hosted side away and back, a link that drops
mid-call, a hung LibreNMS, a call storm, a full disk, a clock that steps back, stopping mid-call.

All against local fakes: no network beyond 127.0.0.1.
"""
import json
import socket
import threading
import time

import pytest

import floorbit_agent as agent_mod
from floorbit_agent import metrics as metrics_mod
from tests.test_agent import FakeHost, LinkHost, make


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_hosted_offline_then_back(monkeypatch):
    """Nothing listens at first; the agent keeps retrying with backoff and gets the call once the
    hosted Floorbit is back on the same address."""
    import requests
    from werkzeug.serving import make_server
    monkeypatch.setattr(agent_mod, 'MAX_BACKOFF', 0.2)
    port = free_port()
    http = requests.Session()
    http.trust_env = False
    logged = []
    a = agent_mod.Agent(f'http://127.0.0.1:{port}', 'fba_x_y', lambda m, args: ['ok'], http=http, log=logged.append)
    t = threading.Thread(target=a.run, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: any('cannot reach' in line for line in logged), 5)
        host = LinkHost(link=False)
        host.server.shutdown()                     # the fake picked its own port: serve it on ours
        server = make_server('127.0.0.1', port, host.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            assert wait_for(lambda: host.answers, 10)
            assert host.answers[0]['result'] == ['ok']
            assert a.metrics.get('floorbit_agent_connected') == 1
        finally:
            server.shutdown()
    finally:
        a.stop()
        t.join(5)


def test_a_link_that_drops_after_the_calls_still_answers_over_https():
    """The link brings the call and closes before the answer: the answer goes over HTTPS."""
    host = LinkHost()
    original = host.app

    def drop_after_calls(environ, start_response):
        import simple_websocket
        from werkzeug.wrappers import Response
        if environ['PATH_INFO'] == '/api/agent/v2/link':
            ws = simple_websocket.Server(environ)
            calls, host.calls = host.calls, []
            ws.send(json.dumps({'type': 'calls', 'calls': calls}))
            ws.close()
            return Response()(environ, start_response)
        return original(environ, start_response)

    host.server.app = drop_after_calls
    slow = threading.Event()

    def execute(method, args):
        slow.wait(0.5)                             # still running when the link goes
        return ['answered']

    import requests
    http = requests.Session()
    http.trust_env = False
    a = agent_mod.Agent(host.url, 'fba_x_y', execute, http=http, log=lambda *_: None)
    t = threading.Thread(target=a.run, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: host.answers, 10)
    finally:
        a.stop()
        t.join(5)
        host.close()
    assert host.answers[0]['result'] == ['answered'] and host.via[0] == 'poll'
    assert a.metrics.get('floorbit_agent_answers_total', {'way': 'https'}) >= 1


def test_a_hung_librenms_holds_up_only_its_own_kind():
    """Every port_stats call hangs; devices calls still get answered straight away."""
    release = threading.Event()
    host = FakeHost()

    def execute(method, args):
        if method == 'port_stats':
            release.wait(10)
        return [method]

    a = agent_mod.Agent('https://floor.example.com', 'fba_x_y', execute, http=host, log=lambda *_: None)
    try:
        for n in range(agent_mod.WORKERS * 3):
            a.take({'id': f'slow{n}', 'method': 'port_stats', 'args': []})
        started = time.monotonic()
        a.take({'id': 'fast', 'method': 'devices', 'args': []})
        assert wait_for(lambda: any(p[2]['answers'][0]['id'] == 'fast' for p in host.posts), 3)
        assert time.monotonic() - started < 2
    finally:
        release.set()
    assert wait_for(lambda: len(host.posts) == agent_mod.WORKERS * 3 + 1, 10)
    a.stop()


def test_a_call_storm_gets_busy_answers_instead_of_piling_up(monkeypatch):
    monkeypatch.setattr(agent_mod, 'MAX_WAITING', 5)
    release = threading.Event()
    host = FakeHost()
    a = agent_mod.Agent('https://floor.example.com', 'fba_x_y', lambda m, args: release.wait(10) and [],
                        http=host, log=lambda *_: None)
    try:
        for n in range(50):
            a.take({'id': str(n), 'method': 'arp', 'args': []})
        busy = [p for p in host.posts if not p[2]['answers'][0]['ok']]
        assert len(busy) == 45 and 'busy' in busy[0][2]['answers'][0]['error']
        assert a.metrics.get('floorbit_agent_calls_total', {'method': 'arp', 'outcome': 'busy'}) == 45
    finally:
        release.set()
    assert wait_for(lambda: len(host.posts) == 50, 10)
    assert wait_for(lambda: a._waiting['arp'] == 0, 5)       # room again once they're done
    a.stop()


def test_a_full_disk_doesnt_stop_the_heartbeat_for_good(tmp_path):
    a = make(FakeHost())
    logged = []
    a.log = logged.append
    a.heartbeat = str(tmp_path / 'no-such-folder' / 'beat')       # can't be written, like a full disk
    a.beat(now=1000)
    a.beat(now=1000 + agent_mod.BEAT_EVERY + 1)
    assert len([line for line in logged if 'cannot write' in line]) == 1   # said once
    (tmp_path / 'no-such-folder').mkdir()                                  # space again
    a.beat(now=1000 + 3 * agent_mod.BEAT_EVERY)
    assert (tmp_path / 'no-such-folder' / 'beat').is_file()
    assert any('can be written again' in line for line in logged)


def test_a_clock_that_steps_back_doesnt_read_as_unhealthy():
    a = make(FakeHost())
    a.last_contact = time.time() + 3600          # the clock was an hour ahead, then NTP fixed it
    ok, details = metrics_mod.health_of(a, 120)
    assert ok and details['last_contact_seconds'] == 0


def test_retry_timers_use_the_monotonic_clock():
    """Wall-clock jumps can't stall or rush the link retry: it is measured on time.monotonic."""
    import inspect
    source = inspect.getsource(agent_mod.Agent._try_link) + inspect.getsource(agent_mod.Agent.run)
    assert 'time.time()' not in source and 'time.monotonic()' in source


def test_stopping_mid_call_returns_quickly():
    host = LinkHost(link=False)
    host.calls = [{'id': '1', 'method': 'devices', 'args': []}]
    import requests
    http = requests.Session()
    http.trust_env = False
    running = threading.Event()

    def execute(method, args):
        running.set()
        time.sleep(30)                            # a call that would never end on its own

    a = agent_mod.Agent(host.url, 'fba_x_y', execute, http=http, log=lambda *_: None)
    t = threading.Thread(target=a.run, daemon=True)
    t.start()
    try:
        assert running.wait(10)
        started = time.monotonic()
        a.stop()
        t.join(5)
        assert not t.is_alive() and time.monotonic() - started < agent_mod.HTTP_TIMEOUT
    finally:
        host.close()


@pytest.mark.parametrize('status', [500, 502, 503])
def test_server_errors_are_retried_not_fatal(status, monkeypatch):
    monkeypatch.setattr(agent_mod, 'MAX_BACKOFF', 0.1)
    a = make(FakeHost(status=status))
    t = threading.Thread(target=a.run, daemon=True)
    t.start()
    time.sleep(0.5)
    assert t.is_alive()                           # still trying; only 401/426 stop it
    a.stop()
    t.join(5)

