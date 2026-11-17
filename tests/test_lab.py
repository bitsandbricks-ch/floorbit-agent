"""The test lab's own parts (lab/): the SNMP data, the replayed traffic, the lab LibreNMS and the
anonymiser. The lab's end-to-end tests are in lab/e2e and need a running lab."""
import importlib.util
import json
import pathlib
import socket
import struct
import sys
import time

import pytest

LAB_DIR = pathlib.Path(__file__).resolve().parent.parent / 'lab'
sys.path.insert(0, str(LAB_DIR))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, LAB_DIR / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


topology = _load('lab_topology', 'topology.py')
build = _load('lab_build', 'snmp/build.py')
replay = _load('lab_replay', 'replay/replay.py')
lite = _load('lab_lite', 'librenms_lite/server.py')
anonymise = _load('lab_anonymise', 'tools/anonymise.py')
LAB = topology.LAB


# --- the lab event ------------------------------------------------------------------------------

def test_the_lab_event_is_consistent():
    labels = [s.label for s in LAB.seats]
    assert len(labels) == len(set(labels)) == 96
    assert len({s.ip for s in LAB.seats}) == 96
    assert len({s.mac for s in LAB.seats}) == 96
    for s in LAB.seats:
        assert s.mac.startswith('02:'), 'locally administered MACs only: nothing real'
        assert LAB.device(s.switch).port(s.port).up == s.online
    # The 2960-X has 48 copper ports and 4 SFP uplinks.
    for sw in LAB.access:
        assert sum(1 for p in sw.ports if p.kind == 'ethernet') == 52


def test_snmp_data_is_up_to_date():
    assert build.main(['--check']) == 0, 'run python3 lab/snmp/build.py and commit lab/snmp/data'


def test_snmprec_files_are_sorted_and_well_formed():
    for path in (LAB_DIR / 'snmp' / 'data').rglob('*.snmprec'):
        oids = []
        for line in path.read_text().splitlines():
            oid, kind, _value = line.split('|', 2)
            assert kind.split(':')[0] in {'2', '4', '4x', '6', '64', '65', '66', '67', '70'}, (path, line)
            oids.append(tuple(int(x) for x in oid.split('.')))
        assert oids == sorted(oids), f'{path} is not in OID order: snmpsim needs that'


def test_only_the_core_holds_arp():
    files = build.build()
    for name, communities in files.items():
        has_arp = any('|' in line and line.startswith('1.3.6.1.2.1.4.22.1.')
                      for text in communities.values() for line in text.splitlines())
        assert has_arp == (name == 'core-01'), name


def test_catalysts_keep_the_fdb_per_vlan():
    files = build.build()
    seat = LAB.seat('B-3')
    vlan_file = files[seat.switch][f'public@{seat.vlan}']
    assert f'1.3.6.1.2.1.17.4.3.1.2.{build.mac_oid(seat.mac)}|2|3' in vlan_file
    assert '1.3.6.1.2.1.17.4.3.1.2.' not in files[seat.switch]['public']


# --- replayed traffic -------------------------------------------------------------------------

def test_netflow_packets_parse_with_floorbits_collector():
    parse = pytest.importorskip('floorbit.flows.parse')
    templates = parse.Templates()
    packets = replay.netflow_packets(1, time.time(), 0)
    records = [r for pkt in packets for r in parse.parse(pkt, '172.31.10.11', templates)]
    assert templates.missing == 0
    online = {s.ip for s in LAB.seats if s.online}
    assert records and {r['src'] for r in records} & online and {r['dst'] for r in records} & online
    assert all(r['bytes'] > 0 and r['packets'] > 0 for r in records)


def test_syslog_lines_parse_with_their_severity():
    syslog = pytest.importorskip('floorbit.syslog')
    lines = replay.syslog_round(0, time.time())
    parsed = [syslog.parse(line) for _ip, line in lines]
    # Floorbit lifts a Cisco mnemonic out of the message into `program`; older parsers left it in.
    link = next(p for p in parsed if '%LINK-3-UPDOWN' in p['program'] + p['message'])
    assert link['severity'] == 'err'
    fw = next(p for p in parsed if p['program'] == 'filterlog')
    assert fw['host'].startswith('fw-')
    senders = {ip for ip, _line in lines}
    assert senders <= {d.ip for d in LAB.devices}


def test_each_device_sends_only_its_own_lines(monkeypatch):
    sent = []
    monkeypatch.setenv('LAB_SYSLOG_TARGETS', '127.0.0.1:9')
    monkeypatch.setattr(replay, 'send_syslog', lambda lines, dests: sent.extend(lines))
    for rnd in range(6):
        replay.one_round(rnd, time.time(), device='core-01')
    assert sent and {ip for ip, _ in sent} == {LAB.core.ip}


def test_lancache_lines_have_the_formats_floorbit_reads(tmp_path):
    access, dns = replay.lancache_lines(3, time.time())
    replay.write_lancache(tmp_path, access, dns)
    assert (tmp_path / 'logs' / 'access.log').read_text().count('\n') == len(access)
    # lancache.net's access log and bind's query log, as the LAN cache plugin reads them.
    assert all(line.startswith('[') and '" 200 ' in line and line.endswith('"-"') for line in access)
    assert all(' queries: info: client @' in line and ' query: ' in line for line in dns)


def test_opnsense_push_is_line_protocol():
    for line in replay.opnsense_lines(2, 1_800_000_000):
        measurement_tags, fields, stamp = line.split(' ')
        assert ',host=fw-0' in measurement_tags and '=' in fields and stamp.isdigit()


# --- the lab LibreNMS ---------------------------------------------------------------------------

def test_mac_forms():
    assert lite.mac('02 1A B0 0B 00 03') == '021ab00b0003'
    assert lite.mac('2.26.176.11.0.3') == '021ab00b0003'
    assert lite.mac('02:1a:b0:0b:00:03') == '021ab00b0003'


def _device():
    d = lite.Device(1, 'access-b1', '192.0.2.21', 'public')
    d.status = 1
    d.row = {'device_id': 1, 'hostname': 'access-b1', 'display': 'access-b1', 'sysName': 'access-b1', 'status': 1}
    d.ports = {10103: {'port_id': 1003, 'ifIndex': 10103, 'ifName': 'Gi1/0/3', 'ifAlias': 'seat B-3',
                       'ifDescr': 'GigabitEthernet1/0/3', 'ifOperStatus': 'up'}}
    d.fdb = [{'port_id': 1003, 'device_id': 1, 'vlan_id': 102, 'mac_address': '021ab00b0003'}]
    d.arp = [{'port_id': 1003, 'device_id': 1, 'mac_address': '021ab00b0003', 'ipv4_address': '10.10.102.13',
              'context_name': ''}]
    return d


def test_lite_answers_like_librenms():
    app = lite.Lite([], 30)
    app.devices = [_device()]
    status, body = lite.route(app, 'GET', '/api/v0/devices', {'type': ['display'], 'query': ['access']})
    assert status == 200 and body['devices'][0]['display'] == 'access-b1'
    _, body = lite.route(app, 'GET', '/api/v0/resources/fdb/02:1a:b0:0b:00:03/detail', {})
    assert body['ports_fdb'][0]['ifName'] == 'Gi1/0/3' and body['ports_fdb'][0]['ifAlias'] == 'seat B-3'
    _, body = lite.route(app, 'GET', '/api/v0/resources/ip/arp/10.10.102.0%2F24', {})
    assert [r['ipv4_address'] for r in body['arp']] == ['10.10.102.13']
    _, body = lite.route(app, 'GET', '/api/v0/devices/access-b1/ports', {'columns': ['ifName,ifOperStatus']})
    assert body['ports'] == [{'ifName': 'Gi1/0/3', 'ifOperStatus': 'up'}]
    assert lite.route(app, 'POST', '/api/v0/devices', {})[0] == 405
    assert lite.route(app, 'GET', '/api/v0/devices/nope', {})[0] == 404


def test_lite_reads_snmp_through_the_walk_parser(monkeypatch):
    out = ('.1.3.6.1.2.1.31.1.1.1.1.10101 = STRING: "Gi1/0/1"\n'
           '.1.3.6.1.2.1.4.22.1.2.151060582.10.10.102.13 = Hex-STRING: 02 1A B0 0B 00 03 \n'
           '.1.3.6.1.2.1.31.1.1.1.1.10101 = No more variables left in this MIB View\n')

    class Done:
        returncode, stdout, stderr = 0, out, ''
    monkeypatch.setattr(lite.subprocess, 'run', lambda *a, **k: Done())
    rows = lite.walk('192.0.2.1', 'public', '1.3.6.1.2.1.31.1.1.1.1')
    assert rows == {'10101': 'Gi1/0/1'}
    arp = lite.walk('192.0.2.1', 'public', '1.3.6.1.2.1.4.22.1.2')
    assert arp == {'151060582.10.10.102.13': '02 1A B0 0B 00 03'}


# --- the anonymiser ---------------------------------------------------------------------------

def test_anonymiser_keeps_things_lining_up():
    a = anonymise.Anonymiser(names={'Realevent': 'example'})
    walk = ('.1.3.6.1.2.1.4.22.1.2.151060582.10.20.5.17 = Hex-STRING: 3C 52 82 1A 2B 3C \n'
            '.1.3.6.1.2.1.17.7.1.2.2.1.2.20.60.82.130.26.43.60 = INTEGER: 12\n'
            '.1.3.6.1.2.1.1.5.0 = STRING: core1.realevent.ch\n'
            '.1.3.6.1.2.1.1.6.0 = STRING: Hall 3, Somewhere\n'
            '.1.3.6.1.2.1.47.1.1.1.1.11.22 = STRING: "FOC1234X5YZ"\n'
            '.1.3.6.1.2.1.4.20.1.3.10.20.5.1 = IpAddress: 255.255.255.0\n')
    out = a.file(walk, walk=True)
    cli = a.file('Internet  10.20.5.17  0  3c52.821a.2b3c  ARPA  Vlan20\nuplink 85.195.1.2\n', walk=False)
    new_ip = a.ip('10.20.5.17')
    new_mac = ':'.join(f'{x:02x}' for x in a.mac([0x3c, 0x52, 0x82, 0x1a, 0x2b, 0x3c]))
    assert new_ip != '10.20.5.17' and new_ip.startswith('10.')
    assert f'.151060582.{new_ip} =' in out and new_ip in cli
    assert new_mac.startswith('3c:52:82:') and new_mac.replace(':', '')[:4] in cli.replace('.', '')
    assert '.'.join(str(int(x, 16)) for x in new_mac.split(':')) in out, 'the FDB index follows the MAC'
    assert 'realevent' not in out.lower() and 'Somewhere' not in out and 'FOC1234X5YZ' not in out
    assert '255.255.255.0' in out, 'masks stay'
    assert a.ip('10.20.5.1').endswith('.1'), 'the gateway stays .1'
    assert a.ip('10.20.5.99').rsplit('.', 1)[0] == new_ip.rsplit('.', 1)[0], 'a subnet stays a subnet'
    assert any(cli.find(net) >= 0 for net in ('192.0.2.', '198.51.100.', '203.0.113.')), 'public -> documentation'


def test_anonymiser_mapping_is_kept_between_runs(tmp_path):
    raw = tmp_path / 'show-arp.txt'
    raw.write_text('Internet  10.20.5.17  0  3c52.821a.2b3c  ARPA  Vlan20\n')
    mapping = tmp_path / 'map.json'
    anonymise.main([str(raw), '--out', str(tmp_path / 'one'), '--map', str(mapping)])
    anonymise.main([str(raw), '--out', str(tmp_path / 'two'), '--map', str(mapping)])
    assert (tmp_path / 'one' / raw.name).read_text() == (tmp_path / 'two' / raw.name).read_text()
    assert oct(mapping.stat().st_mode)[-3:] == '600'
    assert json.loads(mapping.read_text())['ips']


def test_netflow_header_counts_its_records():
    pkt = replay.netflow_packets(0, 1_800_000_000, 7)[0]
    version, count, _uptime, secs, seq, _source = struct.unpack_from('!HHIIII', pkt)
    assert (version, secs, seq) == (9, 1_800_000_000, 7)
    assert count >= 2
    assert socket.inet_aton(LAB.seats[0].ip) in pkt or count > 1
