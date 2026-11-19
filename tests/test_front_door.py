"""The attendee front door (front_door.py): the hosted Floorbit's pages from the event network, with
the attendee's own address and the agent's key on each request, and the speed test answered here."""
import http.client

import pytest

from floorbit_agent import front_door

KEY = 'fba_0123abcd_example-key-for-tests-only'


class FakeResp:
    def __init__(self, status=200, headers=None, body=b'hello'):
        self.status_code, self.headers, self.body = status, headers or {'Content-Type': 'text/html'}, body
        self.raw = type('Raw', (), {'headers': self.headers})()
        self.closed = False

    def iter_content(self, size):
        yield self.body

    def close(self):
        self.closed = True


class FakeHttp:
    def __init__(self, resp=None, fail=False):
        self.resp, self.fail, self.calls = resp or FakeResp(), fail, []

    def request(self, method, url, data=None, headers=None, **kw):
        self.calls.append({'method': method, 'url': url, 'data': data, 'headers': headers, **kw})
        if self.fail:
            raise ConnectionError('hosted side away')
        return self.resp


@pytest.fixture
def door():
    made = []

    def start(http, proxies=()):
        d = front_door.FrontDoor('https://floorbit.example.com', KEY, http=http, proxies=proxies, log=lambda *_: None)
        server = front_door.serve(d, '127.0.0.1:0')
        made.append(server)
        return d, server.server_address[1]
    yield start
    for s in made:
        s.shutdown()


def ask(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


def test_requests_go_on_with_the_attendees_address_and_the_agents_key(door):
    http = FakeHttp()
    _d, port = door(http)
    r, data = ask(port, 'GET', '/checkin?x=1', headers={
        'Host': 'checkin.lan', 'X-Forwarded-For': '203.0.113.9', 'Floorbit-Client-IP': '10.0.0.99',
        'Floorbit-Front-Door': 'fba_fake', 'Cookie': 'session=abc'})
    assert r.status == 200 and data == b'hello'
    call = http.calls[0]
    assert call['url'] == 'https://floorbit.example.com/checkin?x=1' and call['method'] == 'GET'
    h = call['headers']
    assert h['Floorbit-Front-Door'] == KEY and h['Floorbit-Client-IP'] == '127.0.0.1'
    assert h['Floorbit-Front-Door-Host'] == 'checkin.lan' and h['Cookie'] == 'session=abc'
    assert 'X-Forwarded-For' not in h and 'Host' not in h


def test_post_bodies_go_on(door):
    http = FakeHttp(FakeResp(201, {'Content-Type': 'application/json'}, b'{"ok": true}'))
    _d, port = door(http)
    r, data = ask(port, 'POST', '/api/checkin', body=b'{"seat": "A-1"}', headers={'Content-Type': 'application/json'})
    assert r.status == 201 and data == b'{"ok": true}'
    assert http.calls[0]['data'] == b'{"seat": "A-1"}'


def test_only_our_proxy_may_name_the_client(door):
    http = FakeHttp()
    _d, port = door(http, proxies=['127.0.0.1'])
    ask(port, 'GET', '/checkin', headers={'X-Forwarded-For': '10.1.2.3, 10.20.30.40'})
    assert http.calls[0]['headers']['Floorbit-Client-IP'] == '10.20.30.40'


def test_the_agents_own_api_is_not_passed_on(door):
    http = FakeHttp()
    _d, port = door(http)
    r, _ = ask(port, 'GET', '/api/agent/v1/calls')
    assert r.status == 404 and http.calls == []


def test_redirects_to_the_hosted_address_stay_on_the_front_door(door):
    http = FakeHttp(FakeResp(302, {'Location': 'https://floorbit.example.com/checkin'}, b''))
    _d, port = door(http)
    r, _ = ask(port, 'GET', '/')
    assert r.status == 302 and r.getheader('Location') == '/checkin'


def test_while_the_hosted_side_is_away_attendees_get_a_plain_page(door):
    d, port = door(FakeHttp(fail=True))
    r, data = ask(port, 'GET', '/checkin')
    assert r.status == 502 and b'nicht erreichbar' in data and b'refresh' in data
    assert d.counts['failed'] == 1


def test_the_speed_test_is_answered_here(door):
    from floorbit import speedtest
    speedtest.reset()
    http = FakeHttp()
    d, port = door(http)
    r, data = ask(port, 'GET', '/help/A-1/speed/down?bytes=300000')
    assert r.status == 200 and len(data) == 300000
    r, data = ask(port, 'POST', '/help/A-1/speed/up', body=b'x' * 100000)
    assert r.status == 200 and data == b'{"bytes": 100000}'
    r, _ = ask(port, 'GET', '/help/A-1/speed/ping')
    assert r.status == 200
    assert http.calls == [] and d.counts['speed'] == 3
    assert 'A-1' in speedtest._seat_spent   # the seat's own budget counts too, as on the hosted side
    # The result still goes to the hosted side, which keeps it with the seat.
    ask(port, 'POST', '/help/A-1/speed/result', body=b'{"down_mbps": 900}')
    assert http.calls[0]['url'].endswith('/help/A-1/speed/result')


def test_too_many_at_once_get_busy(door, monkeypatch):
    monkeypatch.setattr(front_door, 'MAX_OPEN', 0)
    http = FakeHttp()
    _d, port = door(http)
    r, _ = ask(port, 'GET', '/checkin')
    assert r.status == 503 and http.calls == []
