"""The switches' SSH, through the agents: what the crew does on a switch from Floorbit.

The lab switches answer SSH as Cisco IOS and NX-OS (lab/switch_ssh/server.py) with a login setup.py
stored in Floorbit's inventory, so nothing here types one. A shut port does what it does at the
event: the switch logs it, its SNMP says so and the next backup has "shutdown" under it.
"""
import os
import time

import pytest
from conftest import eventually

pytestmark = pytest.mark.skipif(os.environ.get('LAB_SSH', '1') != '1',
                                reason='the switches answer no SSH here (LAB_SSH=1, docs/lab/README.md)')
SEAT = 'A-14'


def _run(fb, template, switch, **extra):
    r = fb.post('/api/commands/run', {'template': template, 'switches': [switch], **extra})
    assert r.status_code == 200, r.text
    result = r.json()['results'][0]
    assert result['ok'], result
    return result['output']


def _port(fb, switch, port):
    return _run(fb, 'port_status', switch, port=port)


def _logged(fb, switch, text):
    lines = fb.get('/api/logs', params={'q': text, 'limit': 50})['lines']
    return [x for x in lines if text in (x.get('message') or '')]


def _backup(fb, switch):
    r = fb.post('/api/configs/backup', {'switches': [switch]})
    assert r.status_code == 200, r.text
    result = r.json()['results'][0]
    assert result['ok'], result
    return result


def test_show_commands_through_the_agent(fb, lab):
    seat = lab.seat(SEAT)
    assert lab.access[0].model in _run(fb, 'version', seat.switch)
    table = _run(fb, 'all_ports', seat.switch)
    assert 'Gi1/0/14' in table and 'connected' in table
    macs = _run(fb, 'port_macs', seat.switch, port='Gi1/0/14')
    assert seat.mac.replace(':', '')[:4] in macs.replace('.', '')
    # The core runs NX-OS: the same templates work there.
    assert 'NX-OS' in _run(fb, 'version', lab.core.name)


def test_a_config_backup_without_its_secrets(fb, lab):
    sw = lab.seat(SEAT).switch
    _backup(fb, sw)
    copies = fb.get(f'/api/configs/{sw}')
    latest = copies['copies'][0] if 'copies' in copies else copies[0]
    text = fb.get(f"/api/configs/{sw}/{latest['id']}")['text']
    assert f'hostname {sw}' in text and 'interface GigabitEthernet1/0/14' in text
    # The RW community and the password hashes stay on the switch.
    assert 's3cr3t-lab-rw' not in text and '$9$lab.not.a.real.hash' not in text


def test_shut_a_seat_port_and_turn_it_on_again(fb, lab):
    seat = lab.seat(SEAT)
    up_before = fb.get(f'/api/switches/{seat.switch}/status').get('up_ports')
    r = fb.post('/api/ports/shutdown', {'seat': seat.label})
    assert r.status_code == 200, r.text
    try:
        assert 'administratively down' in _port(fb, seat.switch, 'Gi1/0/14')
        assert any(p.get('seat') == seat.label or p.get('port') in ('Gi1/0/14', seat.port)
                   for p in fb.get('/api/ports/shut')['ports'])
        # The switch tells its logging hosts, the agents bring it to Floorbit's log.
        eventually(lambda: _logged(fb, seat.switch, 'GigabitEthernet1/0/14, changed state to administratively down'),
                   what='the switch logging the shut port')
        eventually(lambda: _logged(fb, seat.switch, 'Configured from'), what='the config change in the log')
        # Its SNMP says so: LibreNMS counts one port fewer up.
        if up_before:
            eventually(lambda: fb.get(f'/api/switches/{seat.switch}/status').get('up_ports') == up_before - 1,
                       timeout=120, what='LibreNMS to see the port down')
        # The next backup has it, and says the config changed.
        result = _backup(fb, seat.switch)
        assert result.get('changed'), result
        diff = fb.get(f'/api/configs/{seat.switch}/diff')
        assert 'shutdown' in str(diff)
    finally:
        r = fb.post('/api/ports/enable', {'seat': seat.label})
    assert r.status_code == 200, r.text
    assert 'line protocol is up' in _port(fb, seat.switch, 'Gi1/0/14')
    eventually(lambda: _logged(fb, seat.switch, 'GigabitEthernet1/0/14, changed state to up'),
               what='the switch logging the port back up')


def test_bounce_a_seat_port(fb, lab):
    seat = lab.seat('A-15')
    r = fb.post('/api/ports/bounce', {'seat': seat.label})
    assert r.status_code == 200, r.text
    assert 'line protocol is up' in _port(fb, seat.switch, 'Gi1/0/15')
    eventually(lambda: _logged(fb, seat.switch, 'GigabitEthernet1/0/15, changed state to administratively down'),
               what='the bounce in the log')


def test_clear_counters_really_clears_them(fb, lab):
    """IOS asks [confirm] first; Floorbit answers it (floorbit#864)."""
    sw = lab.seat(SEAT).switch
    time.sleep(2)
    _run(fb, 'clear_counters', sw, port='Gi1/0/16', confirm=True)
    status = _port(fb, sw, 'Gi1/0/16')
    cleared = next(line for line in status.splitlines() if 'Last clearing' in line)
    assert cleared.rstrip().endswith(('00:00:00', '00:00:01', '00:00:02', '00:00:03')), cleared


def test_the_terminal(fb, lab):
    sw = lab.seat(SEAT).switch
    r = fb.post('/api/terminal', {'switch': sw, 'cols': 120, 'rows': 32})
    assert r.status_code == 201, r.text
    sid = r.json()['id']
    seen, after = '', 0
    try:
        def read(until):
            nonlocal seen, after
            out = fb.get(f'/api/terminal/{sid}/output', params={'after': after, 'wait': 5})
            seen += out['data']
            after = out['next']
            return until in seen
        eventually(lambda: read(f'{sw}#'), timeout=30, every=0, what='the prompt')
        assert fb.post(f'/api/terminal/{sid}/input', {'data': 'sh ip int br\r'}).status_code == 200
        # A long answer stops at the pager, as on the switch; space shows the next screen, q stops.
        eventually(lambda: read('--More--'), timeout=30, every=0, what='the pager in the terminal')
        fb.post(f'/api/terminal/{sid}/input', {'data': 'q'})
        fb.post(f'/api/terminal/{sid}/input', {'data': 'show clock\r'})
        eventually(lambda: read(' UTC '), timeout=30, every=0, what='the clock in the terminal')
        # A typo gets the switch's own answer.
        fb.post(f'/api/terminal/{sid}/input', {'data': 'shw clock\r'})
        eventually(lambda: read('% Invalid input'), timeout=30, every=0, what='the switch refusing a typo')
    finally:
        fb.s.delete(f'{fb.url}/api/terminal/{sid}', timeout=30)
