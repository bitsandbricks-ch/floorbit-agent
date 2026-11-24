"""Bad days: what the crew sees when part of the event goes away, and that it comes back.

Each drill runs on its own (LAB_DRILL, see conftest.py), after the lab stopped something:
CI's lab job and docs/lab/README.md say which part and in what order.
"""
import os

import pytest
import requests
from conftest import eventually, short

DOWN = [d for d in os.environ.get('LAB_DOWN', 'fw-01,access-c1').split(',') if d]


def _firewalls(fb):
    return fb.get('/api/firewalls')


def _ranges(fb):
    return {r['cidr']: r for r in _firewalls(fb)['ranges']}


def _parts(body):
    """Every range's parts: each has its own firewall, the one it takes now and its state."""
    return [p for r in body['ranges'] for p in r['parts']]


def _status(fb, switch):
    return fb.get(f'/api/switches/{switch}/status').get('status')


def _resolves(fb, seat):
    found = fb.get(f'/api/resolve/{seat.ip}')
    return (found.get('hostname'), found.get('port')) == (seat.switch, short(seat.port))


def _metric(url, name, plugin):
    for line in requests.get(f'{url}/metrics', timeout=10).text.splitlines():
        if line.startswith(f'{name}{{') and f'plugin="{plugin}"' in line:
            return float(line.split()[-1])
    return None


def _agent_metrics():
    spec = os.environ.get('LAB_AGENT_METRICS', '')
    return dict(item.partition('=')[::2] for item in spec.split(',') if '=' in item)


# --- a firewall and an access switch die (LAB_DOWN) -------------------------------------------


@pytest.mark.drill('devices-down')
def test_a_dead_firewalls_ranges_move_to_its_backup(fb, lab):
    fw = next(f for f in lab.firewalls if f.name in DOWN)
    backup = next(f for f in lab.firewalls if f.name != fw.name)

    def moved():
        body = _firewalls(fb)
        card = {c['name']: c for c in body['firewalls']}[fw.name]
        theirs = [p for p in _parts(body) if p['firewall'] == fw.name]
        return body if card['status'] == 'offline' and theirs and all(
            p['now'] == backup.name and p['state'] == 'failover' for p in theirs) else None
    body = eventually(moved, timeout=240, what=f'the ranges of {fw.name} to move to {backup.name}')
    assert body['counts']['down'] == 1 and body['counts']['without'] == 0
    assert all(p['state'] == 'primary' for p in _parts(body) if p['firewall'] == backup.name)
    # The plan's firewall layer marks the seats behind it, by seat, without their IPs.
    seat = next(s for s in lab.seats if s.ip.startswith(f'10.10.{s.vlan}.') and
                _ranges(fb)[f'10.10.{s.vlan}.0/24']['firewalls'] == [fw.name])
    routes = fb.get('/api/firewalls/seats')
    assert routes['seats'][seat.label] == {'now': backup.name, 'intended': fw.name, 'state': 'failover'}
    assert seat.ip not in str(routes)


@pytest.mark.drill('devices-down')
def test_a_dead_switch_is_offline_and_a_problem(fb, lab):
    sw = next(d for d in lab.access if d.name in DOWN)
    eventually(lambda: _status(fb, sw.name) == 'offline', timeout=240, what=f'{sw.name} to show offline')

    def listed():
        body = fb.get('/api/problems')
        return [p for p in body.get('problems') or [] if sw.name in f"{p.get('title')} {p.get('where')}"]
    eventually(listed, timeout=120, what=f'{sw.name} on the problems page')


@pytest.mark.drill('devices-down')
def test_the_rest_of_the_hall_carries_on(fb, lab):
    for sw in [d for d in lab.switches if d.name not in DOWN]:
        assert _status(fb, sw.name) == 'online', f'{sw.name} went down with the others'
    seat = next(s for s in lab.seats if s.online and s.switch not in DOWN)
    eventually(lambda: _resolves(fb, seat), what=f'seat {seat.label} on a switch that is up')


# --- they come back --------------------------------------------------------------------------


@pytest.mark.drill('recovered')
def test_the_firewall_takes_its_ranges_back(fb, lab):
    def back():
        body = _firewalls(fb)
        return body if all(r['state'] == 'primary' for r in body['ranges']) and body['counts']['down'] == 0 else None
    eventually(back, timeout=240, what='every range back on its own firewall')
    assert fb.get('/api/firewalls/seats')['seats'] == {}


@pytest.mark.drill('recovered')
def test_the_switch_is_back_and_its_seats_resolve(fb, lab):
    sw = next(d for d in lab.access if d.name in DOWN)
    eventually(lambda: _status(fb, sw.name) == 'online', timeout=240, what=f'{sw.name} back online')
    seat = next(s for s in lab.seats if s.online and s.switch == sw.name)
    eventually(lambda: _resolves(fb, seat), timeout=120, what=f'seat {seat.label} on {sw.name} again')


# --- LibreNMS goes away ------------------------------------------------------------------------


@pytest.mark.drill('librenms-down')
def test_floorbit_says_monitoring_is_away(fb):
    body = eventually(lambda: (lambda h: h if not h['ok'] else None)(fb.get('/api/monitoring/health')),
                      timeout=240, what='the monitoring health to say LibreNMS is away')
    assert body['error'], 'it should say why'


@pytest.mark.drill('librenms-down')
def test_a_blind_monitoring_source_moves_no_range(fb):
    """Not hearing about a firewall is not the firewall being down: the core still routes to it."""
    eventually(lambda: not fb.get('/api/monitoring/health')['ok'], timeout=240, what='LibreNMS to count as away')
    body = _firewalls(fb)
    assert not [r for r in body['ranges'] if r['state'] in ('failover', 'down')], body['ranges']


@pytest.mark.drill('librenms-down')
def test_the_pages_still_answer_and_telemetry_goes_on(fb, lab):
    for path in ('/api/switches/access-b1/status', '/api/problems', '/api/firewalls', '/api/plan'):
        r = fb.s.get(f'{fb.url}{path}', timeout=60)
        assert r.status_code < 500, f'{path} answered {r.status_code}'
    if os.environ.get('LAB_TELEGRAF') == '0':
        return
    # Telegraf polls the switches itself: the numbers keep coming without LibreNMS.
    seat = lab.seat('B-3')
    eventually(lambda: fb.get('/api/telemetry/series', params={
        'device': seat.switch, 'port': short(seat.port), 'metric': 'in_bps', 'hours': 1})['points'],
        what='telemetry without LibreNMS')


# --- the hosted Floorbit is away (stopped), then back -------------------------------------------


@pytest.mark.drill('hosted-down')
def test_the_check_in_note_keeps_answering(lab):
    url = os.environ.get('LAB_FRONT_DOOR')
    if not url:
        pytest.skip('LAB_FRONT_DOOR is not set')

    def ask(seat):
        r = requests.get(f'{url}/.well-known/captive-portal', headers={'X-Forwarded-For': seat.ip}, timeout=10)
        r.raise_for_status()
        return r.json()['captive']
    # A-7 checked in on the normal run: the agent keeps the last list.
    assert ask(lab.seat('A-7')) is False
    assert ask(lab.seat('A-8')) is True


@pytest.mark.drill('hosted-down')
def test_the_front_door_says_floorbit_is_away():
    url = os.environ.get('LAB_FRONT_DOOR')
    if not url:
        pytest.skip('LAB_FRONT_DOOR is not set')
    r = requests.get(f'{url}/checkin', headers={'X-Forwarded-For': '10.10.101.18'}, timeout=30)
    assert r.status_code == 502 and 'text/html' in r.headers['Content-Type']


@pytest.mark.drill('hosted-down')
def test_the_agents_keep_the_syslog_for_later():
    metrics = _agent_metrics()
    if not metrics:
        pytest.skip('LAB_AGENT_METRICS is not set')
    for name, url in metrics.items():
        eventually(lambda url=url: _metric_plain(url, 'floorbit_agent_connected') == 0,
                   timeout=120, what=f'{name} to notice Floorbit is away')
    name, url = next(iter(metrics.items()))
    before = _metric(url, 'floorbit_agent_plugin_backlog', 'syslog-relay') or 0
    eventually(lambda: (_metric(url, 'floorbit_agent_plugin_backlog', 'syslog-relay') or 0) > before,
               timeout=60, what=f'{name} to hold syslog while Floorbit is away')


def _metric_plain(url, name):
    for line in requests.get(f'{url}/metrics', timeout=10).text.splitlines():
        if line.startswith(f'{name} '):
            return float(line.split()[-1])
    return None


@pytest.mark.drill('hosted-back')
def test_syslog_from_while_it_was_away_arrives(fb):
    """The agents send what they held: lines from the outage, with the time they were sent."""
    start, _, end = os.environ['LAB_HOSTED_AWAY'].partition('-')
    start, end = float(start), float(end)

    def caught_up():
        lines = fb.get('/api/logs', params={'limit': 1000})['lines']
        return [x for x in lines if start + 5 < (x.get('ts') or 0) < end - 5]
    eventually(caught_up, timeout=180, what='the syslog held during the outage')


@pytest.mark.drill('hosted-back')
def test_the_agents_spool_drains(lab):
    for name, url in _agent_metrics().items():
        eventually(lambda url=url: (_metric(url, 'floorbit_agent_plugin_backlog', 'syslog-relay') or 0) <= 2,
                   timeout=180, what=f"{name}'s held syslog to go out")
