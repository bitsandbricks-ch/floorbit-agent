"""What the crew relies on at the event, end to end: hosted Floorbit -> agent -> LibreNMS -> switches,
and the event's syslog and flows -> agent -> hosted Floorbit."""
import os
import time

import pytest
import requests
from conftest import eventually, short

# --- the agents -----------------------------------------------------------------------------


def test_both_agents_are_connected_and_current(fb):
    body = fb.get('/api/agents')
    agents = {a['name']: a for a in body['agents']}
    assert {'Lab agent-a', 'Lab agent-b'} <= set(agents)
    for a in agents.values():
        assert a['connected'], a['name']
        assert not a['outdated'], f"{a['name']} speaks an older protocol"


def test_monitoring_goes_through_the_agent(fb):
    # The status monitor may have asked once before the agents were up, and answers "not
    # connected" until it asks again (up to a minute later): wait for a good answer.
    health = eventually(lambda: (h := fb.get('/api/monitoring/health')).get('source') == 'agent' and h['ok'] and h,
                        what='monitoring through the agent')
    assert health['ok'], health


# --- switches -------------------------------------------------------------------------------


@pytest.mark.parametrize('name', ['core-01', 'dist-01', 'access-a1', 'access-b1', 'access-c1', 'access-d1'])
def test_every_switch_is_online_with_its_ports(fb, lab, name):
    status = fb.get(f'/api/switches/{name}/status')
    assert status['status'] == 'online'
    dev = lab.device(name)
    assert status['total_ports'] == len(dev.ports)
    assert status['up_ports'] == sum(1 for p in dev.ports if p.up or p.kind == 'vlan')


def test_switch_health_numbers_come_through(fb, lab):
    status = fb.get(f'/api/switches/{lab.access[0].name}/status')
    assert status.get('temperature') == lab.access[0].temp_c


# --- seats: IP -> MAC (core ARP) -> access port (FDB), the check-in, history -----------------


def test_every_seat_resolves_to_its_access_port(fb, lab):
    """The core holds the ARP table, the access switches only their FDB, and the distribution and
    core see every MAC on their uplinks: each seat must land on its own access port."""
    results = fb.post('/api/seats/resolve').json()['seats']
    by_seat = {r.get('seat'): r for r in (results if isinstance(results, list) else
                                           [{'seat': k, **v} for k, v in results.items()])}
    wrong = []
    for seat in [s for s in lab.seats if s.online]:
        got = by_seat.get(seat.label) or {}
        where = (got.get('hostname') or got.get('switch') or got.get('device_name'), got.get('port'))
        if where != (seat.switch, short(seat.port)):
            wrong.append(f'{seat.label}: {where} (should be {seat.switch} {short(seat.port)})')
    assert not wrong, '\n'.join(wrong)


def test_port_faults_show_on_the_seat(fb, lab):
    half = fb.get(f"/api/resolve/{lab.seat('B-3').ip}")
    assert half['stats']['duplex'] == 'half'

    def crc():
        stats = fb.get(f"/api/resolve/{lab.seat('C-9').ip}")['stats']
        return stats['state'] == 'errors' and stats['error_rate'] > 0
    eventually(crc, what='the CRC errors on C-9')


def test_a_shut_port_is_disabled(fb, lab):
    status = fb.get(f"/api/switches/{lab.seat('D-12').switch}/status")
    assert status['up_ports'] < status['total_ports']


def test_a_player_checks_in_and_the_seat_learns_its_port(fb, lab):
    seat = lab.seat('A-7')
    # Through an agent's front door, as from the seat's PC: Floorbit takes the address only from there.
    door = os.environ.get('LAB_FRONT_DOOR', '').rstrip('/')
    if not door:
        pytest.skip('LAB_FRONT_DOOR is not set')
    r = requests.post(f'{door}/api/checkin', json={'seat': seat.label, 'username': seat.nick},
                      headers={'X-Forwarded-For': seat.ip}, timeout=30)
    assert r.status_code == 200, r.text
    assert r.json()['seat'] == seat.label

    def verified():
        row = next((c for c in fb.get('/api/checkins')['checkins'] if c['seat'] == seat.label), None)
        return row if row and row['wired'] == 'verified' else None
    row = eventually(verified, what='the check-in port lookup')
    assert row['port_seen'] == {'switch': seat.switch, 'port': short(seat.port)}
    assert row['mac'] == seat.mac


def test_the_crew_walk_through_finds_each_seat_port(fb, lab):
    """Floorbit's seat walk-through (docs/seat-walk.md): a laptop at each seat, Locate, the seat's
    access port from the core's ARP and the access switch's MAC table, through the agent."""
    if fb.s.get(f'{fb.url}/api/seat-walk', timeout=30).status_code == 404:
        pytest.skip('this Floorbit has no seat walk-through yet (FLOORBIT_REF)')
    seats = [s for s in lab.seats if s.online and s.label.startswith('B-')][:4]

    def hello():
        r = fb.s.post(f'{fb.url}/api/seat-walk/hello', timeout=30)
        return r.json() if r.status_code == 200 else None

    def identify(seat):
        """The laptop at this seat opens the agent's hello page; the agent tells Floorbit its IP."""
        made = eventually(hello, timeout=90, what='an agent that says where laptops reach it')
        r = requests.get(made['url'], params={'back': f'{fb.url}/seat-walk'}, allow_redirects=False,
                         headers={'X-Forwarded-For': seat.ip}, timeout=30)
        assert r.status_code in (200, 303), r.text
        return made['hello']

    if fb.s.post(f'{fb.url}/api/seat-walk/hello', timeout=30).status_code == 404:
        pytest.skip('this Floorbit has no seat walk-through through the agent yet (FLOORBIT_REF)')
    # Floorbit never takes the request's own address for the laptop's: without the agent it refuses.
    r = fb.post('/api/seat-walk/locate', {'seat': seats[0].label}, headers={'X-Forwarded-For': seats[0].ip})
    assert r.status_code == 409, r.text
    previous = None
    for seat in seats:
        r = fb.post('/api/seat-walk/locate', {'seat': seat.label, 'previous': previous, 'hello': identify(seat)})
        assert r.status_code == 200, r.text
        got = r.json()
        assert got['ok'] and got['found'] == {'switch': seat.switch, 'port': short(seat.port)}, got
        assert got['ip'] == seat.ip and got['mac'] == seat.mac and got['how'] == 'monitoring'
        previous = got['found']
    block = next(b['id'] for b in fb.get('/api/seat-blocks') if b['name'] == 'B')
    kept = {s['seat']: s['port'] for s in fb.get(f'/api/seat-walk?block={block}')['seats']}
    assert all(kept[s.label]['port'] == short(s.port) and kept[s.label]['source'] == 'walk' for s in seats)
    # The last seat's port again (as an old MAC table would say) is never taken for the next seat.
    stale = fb.post('/api/seat-walk/locate', {'seat': 'B-20', 'previous': previous,
                                              'hello': identify(seats[-1])}).json()
    assert not stale['ok'] and stale['stale'] and stale['retry']
    # A typed MAC needs no agent.
    typed = fb.post('/api/seat-walk/locate', {'seat': seats[0].label, 'mac': seats[0].mac, 'mac_typed': True}).json()
    assert typed['ok'] and typed['found'] == {'switch': seats[0].switch, 'port': short(seats[0].port)}, typed
    for seat in seats:
        assert fb.s.delete(f'{fb.url}/api/seat-walk/{seat.label}', timeout=30).status_code == 200


def test_ip_and_mac_history_and_search(fb, lab):
    seat = lab.seat('A-7')
    eventually(lambda: any(r['seat'] == seat.label and r['mac'] == seat.mac
                           for r in fb.get('/api/checkins/history')['rows']), what='the check-in history')
    for query in (seat.mac, seat.mac.replace(':', ''), seat.ip):
        found = fb.get('/api/seats/find', params={'q': query})['results']
        assert any(f['seat'] == seat.label and f.get('port') == short(seat.port) for f in found), (query, found)
    found = fb.get('/api/seats/find', params={'q': seat.nick})['results']
    assert any(f['seat'] == seat.label for f in found), (seat.nick, found)


# --- the event's chatter --------------------------------------------------------------------


def _logs(fb, **params):
    return fb.get('/api/logs', params={'limit': 500, **params})


def test_syslog_from_every_switch_kind_arrives_once(fb, lab):
    """Every simulated switch sends to both agents (two logging hosts): the hosted side keeps one."""
    def arrived():
        body = _logs(fb)
        lines = body['lines']
        text = ' '.join(line.get('message', '') for line in lines)
        kinds = ['%LINK-3-UPDOWN', '%ETHPORT-5-SPEED', 'filterlog' if 'filterlog' in str(lines) else None]
        return body if all(k is None or k in text or k in str(lines) for k in kinds) and lines else None
    body = eventually(arrived, timeout=120, what='syslog from the switches')
    assert body['status']['counts']['duplicates'] > 0, 'both agents should have sent every line'
    seen = [(line.get('ts'), line.get('message')) for line in body['lines']]
    assert len(seen) == len(set(seen)), 'a line was kept twice'


def test_netflow_summary_counts_the_seats(fb, lab):
    def summary():
        body = fb.get('/api/flows')
        return body if body.get('seats') and body.get('services') else None
    body = eventually(summary, timeout=120, what='the NetFlow summary')
    exporters = {e['address'] for e in body['exporters']}
    assert exporters, 'no exporter seen'
    assert body['agent'], 'the summary should come from an agent'


def test_captive_portal_note_until_checked_in(fb, lab):
    url = os.environ.get('LAB_CAPTIVE')
    if not url:
        pytest.skip('LAB_CAPTIVE is not set')
    done, waiting = lab.seat('A-7'), lab.seat('A-8')

    def ask(seat):
        r = requests.get(f'{url}/.well-known/captive-portal', headers={'X-Forwarded-For': seat.ip}, timeout=10)
        r.raise_for_status()
        return r.json()
    eventually(lambda: ask(done)['captive'] is False, timeout=150, what='the captive note for a checked-in seat')
    assert ask(waiting)['captive'] is True


def test_agent_metrics_count_what_came_in(lab):
    spec = os.environ.get('LAB_AGENT_METRICS', '')
    for item in [s for s in spec.split(',') if s]:
        name, _, url = item.partition('=')
        text = requests.get(f'{url}/metrics', timeout=10).text
        received = [line for line in text.splitlines()
                    if line.startswith('floorbit_agent_plugin_received{') and 'plugin="syslog-relay"' in line]
        assert received and float(received[0].split()[-1]) > 0, f'{name} received no syslog'


# --- features still on their way: these start running once Floorbit has them -----------------


def test_lan_cache_numbers(fb):
    r = fb.s.get(f'{fb.url}/api/lancache', timeout=30)
    if r.status_code == 404:
        pytest.skip('This Floorbit has no LAN cache plugin yet (floorbit#660)')
    r.raise_for_status()


def _series(fb, device, metric, port=None):
    params = {'device': device, 'metric': metric, 'hours': 1, **({'port': port} if port else {})}
    return fb.get('/api/telemetry/series', params=params)['points']


def test_every_device_reports_telemetry(fb, lab):
    """Monitoring > Telemetry has every lab device; the agent's collector hears from each."""
    def all_up():
        items = fb.get('/api/plugins/telemetry/widgets/devices')['items']
        return items if {i['label'] for i in items if i['state'] == 'up'} >= {d.name for d in lab.devices} else None
    eventually(all_up, timeout=180, what='telemetry from every device')


@pytest.mark.skipif(os.environ.get('LAB_TELEGRAF') == '0', reason='no Telegraf in this lab (run-local without one)')
def test_telegraf_polls_the_switches_through_the_agent(fb, lab):
    """Telegraf polls the simulated Catalysts and Nexus over SNMP for the agent; the per-port numbers
    reach Floorbit's history: traffic on a seat port, the shut port down."""
    busy, shut = lab.seat('B-3'), lab.seat('D-12')
    eventually(lambda: any(v > 0 for _t, v in _series(fb, busy.switch, 'in_bps', short(busy.port))),
               timeout=240, what=f'traffic on {busy.label} from Telegraf')
    eventually(lambda: _series(fb, shut.switch, 'up', short(shut.port))[-1][1] == 0,
               timeout=120, what=f'{shut.label} down from Telegraf')
    assert _series(fb, lab.dist.name, 'cpu'), 'the 4500 reports its CPU'


@pytest.mark.skipif(os.environ.get('LAB_TELEGRAF') == '0', reason='no Telegraf in this lab (run-local without one)')
def test_telegraf_mac_tables_feed_the_ip_and_mac_history(fb, lab):
    """Per-VLAN MAC tables (community@vlan) on the access switches and the core's ARP, polled by
    Telegraf: each seat's machine is on its own access port, never an uplink."""
    seat = lab.seat('A-7')

    def found():
        rows = fb.get('/api/endpoints', params={'q': seat.mac})['rows']
        return [r for r in rows if r['mac'] == seat.mac] or None
    rows = eventually(found, timeout=240, what=f'{seat.label} in the IP and MAC history')
    assert {(r['device'], r['port']) for r in rows} == {(seat.switch, short(seat.port))}
    assert rows[0]['ip'] == seat.ip


@pytest.mark.skipif(os.environ.get('LAB_TELEGRAF') == '0', reason='no Telegraf in this lab (run-local without one)')
def test_telegraf_takes_the_switches_traps(fb, lab):
    """The switches' linkDown/linkUp traps get through the agent's Telegraf: its trap listener needs
    net-snmp's snmptranslate, and an image without it drops every trap with "Error resolving OID"
    (the telemetry plugin's health then names the traps listener)."""
    def trap_trouble():
        out = []
        for a in fb.get('/api/agents')['agents']:
            health = (((a.get('report') or {}).get('plugins') or {}).get('telemetry')) or {}
            text = f"{health.get('message') or ''} {health.get('hint') or ''}"
            if '(traps)' in text or 'snmptranslate' in text:
                out.append(f"{a['name']}: {text.strip()}")
        return out
    # The senders send traps every round: watch over a few rounds, so a dropped one shows up.
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        trouble = trap_trouble()
        assert not trouble, f'traps are dropped: {trouble}'
        time.sleep(5)


def test_a_firewalls_own_telegraf_lands_on_the_firewall(fb, lab):
    """OPNsense's os-telegraf pushes with its full host name (fw-01.lab.example): the numbers belong
    to fw-01 as set up, and no second device appears."""
    fw = lab.firewalls[0]
    eventually(lambda: _series(fb, fw.name, 'states'), timeout=180, what=f'pf states from {fw.name}')
    assert not _series(fb, f'{fw.name}.lab.example', 'up')


# --- failover: run with LAB_AGENT_DOWN=agent-a after stopping it ------------------------------


@pytest.mark.failover
def test_the_other_agent_takes_over(fb, lab):
    down = os.environ['LAB_AGENT_DOWN']
    agents = {a['name']: a for a in fb.get('/api/agents')['agents']}
    eventually(lambda: not {a['name']: a for a in fb.get('/api/agents')['agents']}[f'Lab {down}']['connected'],
               what=f'{down} to show as gone')
    assert any(a['connected'] for n, a in agents.items() if n != f'Lab {down}')
    seat = lab.seat('D-20')
    found = eventually(lambda: fb.get(f'/api/resolve/{seat.ip}'), what='a lookup through the other agent')
    assert (found['hostname'], found['port']) == (seat.switch, short(seat.port))
    assert fb.get('/api/monitoring/health')['ok']


@pytest.mark.failover
def test_syslog_keeps_coming_through_the_other_agent(fb):
    last = _logs(fb)['status']['last_id']
    eventually(lambda: _logs(fb)['status']['last_id'] > last, timeout=60, what='new syslog after the failover')
