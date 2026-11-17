#!/usr/bin/env python3
"""A small stand-in for LibreNMS's API, fed by SNMP from the lab's simulated switches.

The agent's LibreNMS plugin asks LibreNMS for devices, ports, ARP and FDB. A real LibreNMS needs a
database, a poller and minutes of discovery; for the quick lab and CI this polls the snmpsim
devices itself (net-snmp's snmpbulkwalk) and answers the part of API v0 that Floorbit uses, in
LibreNMS's shapes (MACs as 12 hex digits, rates in bytes per second, status 1/0).

It reads SNMP the way LibreNMS does on Cisco gear: ARP from ipNetToMediaTable, the FDB from
dot1qTpFdbPort or, on Catalyst IOS, from dot1dTpFdbPort per VLAN (community@vlan, VLANs from
CISCO-VTP-MIB), neighbours from LLDP and CDP. So a walk that LibreNMS couldn't read breaks here
too. The `librenms` compose profile runs the real LibreNMS against the same switches.

Settings (env): LAB_DEVICES "name=ip,name=ip" (the community is "public" unless name=ip/community),
LAB_POLL_SECONDS (30), LAB_HOSTNAMES ("name", or "ip" for devices LibreNMS knows by address),
LAB_TOKEN (the API token it expects), LAB_LISTEN (0.0.0.0:8000).
"""
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from urllib.parse import parse_qs, unquote, urlsplit

LINE = re.compile(r'^\.?([\d.]+) = (?:([\w-]+): )?(.*)$')
MAC_HEX = re.compile(r'^[0-9A-Fa-f]{2}( [0-9A-Fa-f]{2}){5}\s*$')

SUBTREES = {
    'system': '1.3.6.1.2.1.1',
    'if': '1.3.6.1.2.1.2.2.1',
    'ifx': '1.3.6.1.2.1.31.1.1.1',
    'duplex': '1.3.6.1.2.1.10.7.2.1.19',
    'addr': '1.3.6.1.2.1.4.20.1.2',
    'arp': '1.3.6.1.2.1.4.22.1.2',
    'bridge_port': '1.3.6.1.2.1.17.1.4.1.2',
    'fdb_q': '1.3.6.1.2.1.17.7.1.2.2.1.2',
    'vtp': '1.3.6.1.4.1.9.9.46.1.3.1.1.2.1',
    'lldp_loc': '1.0.8802.1.1.2.1.3.7.1.3',
    'lldp_rem': '1.0.8802.1.1.2.1.4.1.1',
    'cdp': '1.3.6.1.4.1.9.9.23.1.2.1.1',
    'cpu': '1.3.6.1.4.1.9.9.109.1.1.1.1.8',
    'mem': '1.3.6.1.4.1.9.9.48.1.1.1',
    'temp_envmon': '1.3.6.1.4.1.9.9.13.1.3.1.3',
    'temp_entity': '1.3.6.1.4.1.9.9.91.1.1.1.1.4',
    'hr_cpu': '1.3.6.1.2.1.25.3.3.1.2',
    'hr_storage': '1.3.6.1.2.1.25.2.3.1',
}


def walk(ip, community, oid, timeout=3):
    """{oid suffix: value} under oid. Strings come unquoted; hex strings as 'aa bb ..'."""
    out = subprocess.run(['snmpbulkwalk', '-v2c', '-c', community, '-On', '-Oe', '-Ot', '-OU', '-Cr25',
                          '-t', str(timeout), '-r', '1', ip, oid], capture_output=True, text=True, timeout=60)
    if out.returncode != 0 and not out.stdout:
        raise OSError(out.stderr.strip() or f'snmpbulkwalk {ip} {oid} failed')
    rows = {}
    for line in out.stdout.splitlines():
        m = LINE.match(line)
        if not m or not m.group(1).startswith(oid + '.'):
            continue
        kind, value = m.group(2) or '', m.group(3).strip()
        if not kind and value.startswith('No '):       # endOfMibView, noSuchObject
            continue
        if kind == 'STRING':
            value = value.strip('"')
        elif kind == 'Hex-STRING':
            value = value.strip()
        elif kind == 'Timeticks':
            value = value.strip('()').split(')')[0]
        rows[m.group(1)[len(oid) + 1:]] = value
    return rows


def column(rows, col):
    """The rows of one table column: {index: value}."""
    prefix = f'{col}.'
    return {k[len(prefix):]: v for k, v in rows.items() if k.startswith(prefix)}


def mac(value):
    """A MAC from an SNMP value or OID index, as LibreNMS stores it: 12 lowercase hex digits."""
    value = value.strip().strip('"')
    if MAC_HEX.match(value):
        return value.replace(' ', '').lower()
    parts = value.split('.')
    if len(parts) == 6:
        return ''.join(f'{int(p):02x}' for p in parts)
    return re.sub(r'[^0-9a-f]', '', value.lower())


STATUS = {'1': 'up', '2': 'down', '3': 'testing', '5': 'dormant', '6': 'notPresent', '7': 'lowerLayerDown'}
DUPLEX = {'2': 'halfDuplex', '3': 'fullDuplex'}


def num(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Device:
    def __init__(self, device_id, name, ip, community):
        self.device_id, self.name, self.ip, self.community = device_id, name, ip, community
        # LibreNMS's hostname is whatever the device was added as: a name or an IP (LAB_HOSTNAMES).
        self.hostname = ip.split(':')[0] if os.environ.get('LAB_HOSTNAMES') == 'ip' else name
        self.status = 0
        self.row = {}
        self.ports = {}            # ifIndex -> port row
        self.arp = []
        self.fdb = []
        self.links = []
        self.cpu = None
        self.mem = None
        self.temps = []
        self.last = {}             # ifIndex -> (time, counters) for rates
        self.error = None

    def poll(self):
        now = time.time()
        try:
            system = walk(self.ip, self.community, SUBTREES['system'])
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.status, self.error = 0, str(exc)
            return
        self.status, self.error = (1, None) if system else (0, 'no answer')
        if not system:
            return
        get = lambda o: system.get(o, '')  # noqa: E731
        descr = get('1.0')
        self.row = {
            'device_id': self.device_id, 'hostname': self.hostname, 'sysName': get('5.0'), 'display': self.name,
            'ip': self.ip, 'status': 1, 'status_reason': '', 'sysDescr': descr, 'sysObjectID': get('2.0'),
            'sysContact': get('4.0'), 'location': get('6.0'),
            'os': 'nxos' if 'NX-OS' in descr else 'iosxe' if 'IOS-XE' in descr else 'ios' if 'IOS' in descr
            else 'opnsense' if 'OPNsense' in descr else 'generic',
            'version': (re.search(r'Version ([^,\s]+)', descr) or [None, ''])[1],
            'uptime': int(num(get('3.0')) // 100), 'last_polled': time.strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'firewall' if 'OPNsense' in descr else 'network', 'disabled': 0, 'ignore': 0,
        }
        ifs = walk(self.ip, self.community, SUBTREES['if'])
        ifx = walk(self.ip, self.community, SUBTREES['ifx'])
        duplex = walk(self.ip, self.community, SUBTREES['duplex'])
        ports = {}
        for n, (idx, name) in enumerate(sorted(column(ifx, '1').items(), key=lambda r: int(r[0])), start=1):
            i = int(idx)
            pid = self.device_id * 1000 + n      # stays the same across polls, as LibreNMS's do
            counters = {'in': num(ifx.get(f'6.{idx}')), 'out': num(ifx.get(f'10.{idx}')),
                        'in_err': num(ifs.get(f'14.{idx}')), 'out_err': num(ifs.get(f'20.{idx}'))}
            rates = {}
            if i in self.last:
                then, old = self.last[i]
                dt = max(now - then, 1)
                rates = {k: max(counters[k] - old[k], 0) / dt for k in counters}
            self.last[i] = (now, counters)
            ports[i] = {
                'port_id': pid, 'device_id': self.device_id, 'ifIndex': i, 'ifName': name,
                'ifDescr': ifs.get(f'2.{idx}', name), 'ifAlias': ifx.get(f'18.{idx}', ''),
                'ifType': 'ethernetCsmacd' if ifs.get(f'3.{idx}') == '6' else 'propVirtual',
                'ifAdminStatus': STATUS.get(ifs.get(f'7.{idx}'), ''),
                'ifOperStatus': STATUS.get(ifs.get(f'8.{idx}'), ''),
                'ifSpeed': int(num(ifx.get(f'15.{idx}')) * 1_000_000), 'ifHighSpeed': int(num(ifx.get(f'15.{idx}'))),
                'ifDuplex': DUPLEX.get(duplex.get(idx), 'unknown'), 'ifPhysAddress': mac(ifs.get(f'6.{idx}', '')),
                'ifInOctets': int(counters['in']), 'ifOutOctets': int(counters['out']),
                'ifInErrors': int(counters['in_err']), 'ifOutErrors': int(counters['out_err']),
                'ifInOctets_rate': round(rates.get('in', 0)), 'ifOutOctets_rate': round(rates.get('out', 0)),
                'ifInErrors_rate': round(rates.get('in_err', 0), 3),
                'ifOutErrors_rate': round(rates.get('out_err', 0), 3),
                'ifLastChange': int(num(ifs.get(f'9.{idx}'))) // 100, 'poll_time': int(now),
                'deleted': 0, 'disabled': 0, 'ignore': 0,
            }
        self.ports = ports
        by_index = {i: p['port_id'] for i, p in ports.items()}

        # ARP: ipNetToMediaPhysAddress.<ifIndex>.<ip>
        self.arp = []
        for idx, value in walk(self.ip, self.community, SUBTREES['arp']).items():
            parts = idx.split('.')
            if len(parts) != 5:
                continue
            self.arp.append({'port_id': by_index.get(int(parts[0])), 'device_id': self.device_id,
                             'mac_address': mac(value), 'ipv4_address': '.'.join(parts[1:]), 'context_name': ''})

        # FDB: Q-BRIDGE when the switch has it, else the bridge table of each VLAN (Cisco IOS).
        bridge = {k: int(v) for k, v in walk(self.ip, self.community, SUBTREES['bridge_port']).items()}
        fdb = []
        for idx, bport in walk(self.ip, self.community, SUBTREES['fdb_q']).items():
            parts = idx.split('.')
            fdb.append((int(parts[0]), mac('.'.join(parts[1:])), bridge.get(bport)))
        if not fdb:
            vlans = [int(k) for k in walk(self.ip, self.community, SUBTREES['vtp'])]
            for vlan in vlans:
                try:
                    per_vlan = walk(self.ip, f'{self.community}@{vlan}', '1.3.6.1.2.1.17.4.3.1.2', timeout=2)
                    vbridge = walk(self.ip, f'{self.community}@{vlan}', SUBTREES['bridge_port'], timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                vbridge = {k: int(v) for k, v in vbridge.items()}
                fdb += [(vlan, mac(idx), vbridge.get(bport)) for idx, bport in per_vlan.items()]
        self.fdb = [{'port_id': by_index.get(if_index), 'device_id': self.device_id, 'vlan_id': vlan,
                     'mac_address': m} for vlan, m, if_index in fdb if if_index in by_index]

        # Neighbours: LLDP (lldpRemTable index timeMark.localPort.n), then CDP (ifIndex.n).
        loc = walk(self.ip, self.community, SUBTREES['lldp_loc'])
        rem = walk(self.ip, self.community, SUBTREES['lldp_rem'])
        by_name = {p['ifName']: p['port_id'] for p in ports.values()}
        links = {}
        for idx, sysname in column(rem, '9').items():
            local = loc.get(idx.split('.')[1])
            if local in by_name:
                links[(by_name[local], sysname)] = {
                    'local_port_id': by_name[local], 'remote_hostname': sysname, 'protocol': 'lldp',
                    'remote_port': rem.get(f'7.{idx}', ''), 'local_device_id': self.device_id}
        cdp = walk(self.ip, self.community, SUBTREES['cdp'])
        for idx, remote in column(cdp, '6').items():
            pid = by_index.get(int(idx.split('.')[0]))
            if pid and (pid, remote) not in links:
                links[(pid, remote)] = {'local_port_id': pid, 'remote_hostname': remote, 'protocol': 'cdp',
                                        'remote_port': cdp.get(f'7.{idx}', ''), 'local_device_id': self.device_id}
        self.links = list(links.values())

        cpu = walk(self.ip, self.community, SUBTREES['cpu']) or walk(self.ip, self.community, SUBTREES['hr_cpu'])
        self.cpu = round(sum(num(v) for v in cpu.values()) / len(cpu)) if cpu else None
        mem = walk(self.ip, self.community, SUBTREES['mem'])
        used, free = num(mem.get('5.1')), num(mem.get('6.1'))
        if not mem:
            hr = walk(self.ip, self.community, SUBTREES['hr_storage'])
            used, size = num(hr.get('6.1')), num(hr.get('5.1'))
            free = size - used
        self.mem = round(used * 100 / (used + free)) if used + free else None
        temps = walk(self.ip, self.community, SUBTREES['temp_envmon']) or \
            walk(self.ip, self.community, SUBTREES['temp_entity'])
        self.temps = [num(v) for v in temps.values()]


class Lite:
    def __init__(self, devices, interval):
        self.devices = [Device(n, name, ip, community) for n, (name, ip, community) in enumerate(devices, start=1)]
        self.interval = interval
        self.lock = threading.Lock()
        self.polls = 0

    def _poll(self, dev):
        try:
            dev.poll()
        except Exception as exc:  # noqa: BLE001 - one odd device must not stop the others
            dev.status, dev.error = 0, f'{type(exc).__name__}: {exc}'
            print(f'{dev.name}: {dev.error}', flush=True)

    def poll_once(self):
        # Side by side, as LibreNMS's poller does: one slow switch doesn't hold up the rest.
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(self._poll, self.devices))
        with self.lock:
            self.polls += 1

    def loop(self):
        # Two polls close together first, so rates are there within seconds of starting.
        self.poll_once()
        time.sleep(3)
        while True:
            started = time.time()
            self.poll_once()
            time.sleep(max(1.0, self.interval - (time.time() - started)))

    def find(self, key):
        key = unquote(key)
        for dev in self.devices:
            if key in (str(dev.device_id), dev.ip, dev.hostname, dev.name, dev.row.get('sysName')):
                return dev
        return None

    def device_row(self, dev):
        return {**(dev.row or {'device_id': dev.device_id, 'hostname': dev.hostname, 'display': dev.name,
                               'sysName': dev.name}), 'status': dev.status}


def route(lite, method, path, query):
    """(status, body) for one API request."""
    parts = [p for p in path.split('/') if p][2:]          # after /api/v0
    if method != 'GET':
        return 405, {'status': 'error', 'message': 'The lab LibreNMS only reads.'}
    if parts == ['system']:
        return 200, {'status': 'ok', 'system': [{'local_ver': 'lab', 'db_schema': 'lab'}]}
    if parts == ['devices']:
        rows = [lite.device_row(d) for d in lite.devices]
        q = (query.get('query') or [''])[0].lower()
        kind = (query.get('type') or ['all'])[0]
        if q and kind in ('display', 'hostname', 'sysName'):
            field = {'display': 'display', 'hostname': 'hostname', 'sysName': 'sysName'}[kind]
            rows = [r for r in rows if q in str(r.get(field, '')).lower()]
        return 200, {'status': 'ok', 'devices': rows, 'count': len(rows)}
    if parts[:1] == ['devices'] and len(parts) >= 2:
        dev = lite.find(parts[1])
        if dev is None:
            return 404, {'status': 'error', 'message': f'Device {parts[1]} does not exist'}
        sub = parts[2] if len(parts) > 2 else ''
        if not sub:
            return 200, {'status': 'ok', 'devices': [lite.device_row(dev)]}
        if sub == 'ports':
            cols = [c for c in (query.get('columns') or [''])[0].split(',') if c] or ['ifName']
            rows = [{c: p.get(c) for c in cols} for p in dev.ports.values()]
            return 200, {'status': 'ok', 'ports': rows}
        if sub == 'arp':
            return 200, {'status': 'ok', 'arp': dev.arp, 'count': len(dev.arp)}
        if sub == 'fdb':
            return 200, {'status': 'ok', 'ports_fdb': dev.fdb, 'count': len(dev.fdb)}
        if sub == 'links':
            return 200, {'status': 'ok', 'links': dev.links, 'count': len(dev.links)}
        if sub in ('sensors', 'health'):
            sensors = [{'sensor_class': 'temperature', 'sensor_current': t, 'sensor_descr': f'Temperature {n}'}
                       for n, t in enumerate(dev.temps, start=1)]
            return 200, {'status': 'ok', 'sensors': sensors, 'graphs': sensors}
        return 404, {'status': 'error', 'message': f'The lab LibreNMS has no {sub} for devices.'}
    if parts[:3] == ['resources', 'ip', 'arp'] and len(parts) == 4:
        q = unquote(parts[3])
        rows = [r for d in lite.devices for r in d.arp]
        try:
            net = ip_network(q, strict=False)
            rows = [r for r in rows if ip_address(r['ipv4_address']) in net]
        except ValueError:
            rows = [r for r in rows if r['mac_address'] == mac(q)]
        return 200, {'status': 'ok', 'arp': rows, 'count': len(rows)}
    if parts[:2] == ['resources', 'fdb'] and len(parts) >= 3:
        want = mac(unquote(parts[2]))
        detail = len(parts) == 4 and parts[3] == 'detail'
        out = []
        for d in lite.devices:
            for r in d.fdb:
                if r['mac_address'] != want:
                    continue
                port = next((p for p in d.ports.values() if p['port_id'] == r['port_id']), {})
                row = dict(r)
                if detail:
                    row.update({'hostname': d.hostname, 'sysName': d.row.get('sysName', d.name),
                                'ifName': port.get('ifName'), 'ifAlias': port.get('ifAlias'),
                                'ifDescr': port.get('ifDescr'), 'ifIndex': port.get('ifIndex'),
                                'vlan': r['vlan_id'], 'last_seen': 'just now'})
                out.append(row)
        return 200, {'status': 'ok', 'ports_fdb': out, 'count': len(out)}
    return 404, {'status': 'error', 'message': 'The lab LibreNMS does not answer this.'}


def serve(lite, listen, token):
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _handle(self, method):
            url = urlsplit(self.path)
            if url.path == '/healthz':
                return self._answer(200 if lite.polls else 503, {'polls': lite.polls,
                                    'devices': {d.name: d.status for d in lite.devices}})
            if not url.path.startswith('/api/v0'):
                return self._answer(404, {'status': 'error'})
            if token and self.headers.get('X-Auth-Token') != token:
                return self._answer(401, {'message': 'Unauthenticated.'})
            status, body = route(lite, method, url.path, parse_qs(url.query))
            return self._answer(status, body)

        def do_GET(self):
            self._handle('GET')

        def do_POST(self):
            self._handle('POST')

        def log_message(self, *args):
            pass

    host, port = listen.rsplit(':', 1)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


def parse_devices(spec):
    out = []
    for item in [s.strip() for s in spec.split(',') if s.strip()]:
        name, _, rest = item.partition('=')
        ip, _, community = rest.partition('/')
        out.append((name, ip, community or 'public'))
    return out


def main():
    lite = Lite(parse_devices(os.environ.get('LAB_DEVICES', '')), float(os.environ.get('LAB_POLL_SECONDS', '30')))
    threading.Thread(target=lite.loop, daemon=True).start()
    print(f'lab LibreNMS: {len(lite.devices)} devices, polling every {lite.interval:g}s', flush=True)
    serve(lite, os.environ.get('LAB_LISTEN', '0.0.0.0:8000'), os.environ.get('LAB_TOKEN', ''))


if __name__ == '__main__':
    main()
