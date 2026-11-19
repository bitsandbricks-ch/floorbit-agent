"""/metrics and /healthz (issue #3): counts and timings, nothing personal."""
import json
import time
import urllib.error
import urllib.request

import pytest

import floorbit_agent as agent_mod
from floorbit_agent import metrics as metrics_mod
from tests.test_agent import FakeHost, make


def sample(text, name, **labels):
    """The value of one sample in the Prometheus text, or None."""
    want = metrics_mod._labels(labels) if labels else ''
    for line in text.splitlines():
        if line.startswith(name + want + ' '):
            return float(line.rsplit(' ', 1)[1])
    return None


def test_the_text_format():
    m = metrics_mod.Metrics()
    m.inc('floorbit_agent_calls_total', {'method': 'arp', 'outcome': 'ok'}, 3)
    m.observe('floorbit_agent_call_seconds', 0.2, {'method': 'arp'})
    m.observe('floorbit_agent_call_seconds', 7, {'method': 'arp'})
    text = m.render()
    assert '# TYPE floorbit_agent_calls_total counter' in text
    assert sample(text, 'floorbit_agent_calls_total', method='arp', outcome='ok') == 3
    assert sample(text, 'floorbit_agent_call_seconds_bucket', method='arp', le='0.25') == 1
    assert sample(text, 'floorbit_agent_call_seconds_bucket', method='arp', le='10') == 2
    assert sample(text, 'floorbit_agent_call_seconds_bucket', method='arp', le='+Inf') == 2
    assert sample(text, 'floorbit_agent_call_seconds_count', method='arp') == 2
    assert sample(text, 'floorbit_agent_up_seconds') >= 0


def test_label_values_are_escaped():
    m = metrics_mod.Metrics()
    m.set('floorbit_agent_info', 1, {'version': 'a"b\\c\nd'})
    assert 'version="a\\"b\\\\c d"' in m.render()


def test_calls_are_counted_by_method_and_outcome_and_unknown_methods_fold():
    def execute(method, args):
        if method == 'devices':
            raise RuntimeError('LibreNMS down')
        return []
    a = agent_mod.Agent('https://floor.example.com', 'fba_x_y', execute, http=FakeHost(), log=lambda *_: None)
    a._run({'id': '1', 'method': 'arp', 'args': []})
    a._run({'id': '2', 'method': 'devices', 'args': []})
    a._run({'id': '3', 'method': 'evil{label="x"}', 'args': []})
    text = a.metrics.render()
    assert sample(text, 'floorbit_agent_calls_total', method='arp', outcome='ok') == 1
    assert sample(text, 'floorbit_agent_calls_total', method='devices', outcome='error') == 1
    assert sample(text, 'floorbit_agent_calls_total', method='other', outcome='ok') == 1
    assert 'evil' not in text
    assert sample(text, 'floorbit_agent_answers_total', way='https') == 3
    assert sample(text, 'floorbit_agent_calls_running') == 0


def test_hosted_round_trips_and_failures_are_counted():
    a = make(FakeHost())
    a.hello()
    assert a.last_contact > 0
    bad = make(FakeHost(status=500))
    with pytest.raises(RuntimeError):
        bad.hello()
    assert sample(a.metrics.render(), 'floorbit_agent_hosted_request_seconds_count', kind='hello') == 1
    assert sample(bad.metrics.render(), 'floorbit_agent_hosted_errors_total', kind='hello') == 1


def test_a_broken_collector_does_not_hide_the_rest():
    m = metrics_mod.Metrics()
    m.collect(lambda _m: 1 / 0)
    m.inc('floorbit_agent_reconnects_total')
    text = m.render()
    assert sample(text, 'floorbit_agent_reconnects_total') == 1
    assert sample(text, 'floorbit_agent_collector_errors_total') == 1


def test_health_follows_the_last_contact():
    a = make(FakeHost())
    ok, details = metrics_mod.health_of(a, 120)
    assert not ok and 'not reached' in details['reason']
    a.last_contact = time.time() - 30
    assert metrics_mod.health_of(a, 120)[0]
    a.last_contact = time.time() - 300
    ok, details = metrics_mod.health_of(a, 120)
    assert not ok and details['last_contact_seconds'] >= 300


def _get(url, token=None):
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


@pytest.fixture
def served():
    started = []

    def start(agent, token=None):
        server = metrics_mod.serve(agent.metrics, agent, '127.0.0.1:0', 120, token, log=lambda *_: None)
        started.append(server)
        return f'http://127.0.0.1:{server.server_address[1]}'
    yield start
    for server in started:
        server.shutdown()


def test_the_endpoint(served):
    a = make(FakeHost())
    base = served(a)
    status, body = _get(base + '/healthz')
    assert status == 503 and json.loads(body)['ok'] is False
    a.hello()
    status, body = _get(base + '/healthz')
    assert status == 200 and json.loads(body)['link'] == 'poll'
    status, body = _get(base + '/metrics')
    assert status == 200 and 'floorbit_agent_hosted_request_seconds_count{kind="hello"} 1' in body
    assert 'fba_x_y' not in body
    assert _get(base + '/nothing')[0] == 404


def test_the_endpoint_with_a_token(served):
    base = served(make(FakeHost()), token='scrape-me')
    assert _get(base + '/metrics')[0] == 401
    assert _get(base + '/metrics', 'wrong')[0] == 401
    assert _get(base + '/metrics', 'scrape-me')[0] == 200
    assert _get(base + '/healthz')[0] == 503      # no token needed: up or not, nothing else


@pytest.mark.parametrize('address', ['off', '', 'no-port-here'])
def test_off_or_wrong_addresses_serve_nothing(address):
    assert metrics_mod.serve(metrics_mod.Metrics(), make(FakeHost()), address, 120, log=lambda *_: None) is None


class _Plugins:
    """What PluginHost.report() gives: one plugin warning about a device, with a secret in its text."""
    def report(self):
        return {'telemetry': {'status': 'warning', 'running': True, 'hint': 'device_problems',
                              'message': '1 of 2 devices fail. sw-a-01 (snmp): <b>no answer</b> community=public',
                              'metrics': {'points': 6720, 'sent': 69, 'devices_heard': 1}, 'auth_blocked': ['x'],
                              'errors': [{'at': 1, 'error': 'token: fba_abc_secret refused'}]},
                'syslog': {'status': 'error', 'running': False, 'message': 'cannot listen', 'metrics': {},
                           'errors': []}}


def test_the_status_page_is_open_read_only_and_gives_nothing_away(served):
    a = make(FakeHost())
    a.plugins = _Plugins()
    base = served(a, token='scrape-me')
    a.hello()
    req = urllib.request.Request(base + '/')
    with urllib.request.urlopen(req, timeout=5) as resp:
        page, headers = resp.read().decode(), resp.headers
    # No token needed, unlike /metrics; and nothing it could run or be framed in.
    assert "default-src 'none'" in headers['Content-Security-Policy'] and headers['X-Frame-Options'] == 'DENY'
    assert '<script' not in page and '<form' not in page
    assert 'Connected to Floorbit' in page and '6,720 points received' in page and '69 sends to Floorbit' in page
    assert 'sw-a-01 (snmp)' in page and 'not running' in page
    # Escaped, and secrets masked even inside a plugin's own message.
    assert '<b>no answer</b>' not in page and '&lt;b&gt;no answer&lt;/b&gt;' in page
    assert 'public' not in page and 'fba_abc_secret' not in page and 'fba_x_y' not in page
    status, body = _get(base + '/status.json')
    data = json.loads(body)
    assert status == 200 and data['plugins']['telemetry']['metrics']['points'] == 6720
    assert 'auth_blocked' not in data['plugins']['telemetry'] and 'fba_' not in body
    # Read only: nothing but GET.
    post = urllib.request.Request(base + '/', data=b'x', method='POST')
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(post, timeout=5)
    assert err.value.code == 501
