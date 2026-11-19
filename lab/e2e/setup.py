#!/usr/bin/env python3
"""Set up the lab's Floorbit once: enrol the lab's agents and draw the lab hall.

    python3 lab/e2e/setup.py --url http://floorbit:5000 --out /keys agent-a agent-b

Signs in as the admin, enrols each agent and writes its key to <out>/<name>.key (the agents read
it at start); an agent whose key file is there already is left alone, so a restart keeps its keys.
It sets up the agents' plugins in Floorbit, as the crew would at an event (the agents read none
of it from their own environment): the lab LibreNMS, syslog and NetFlow from the hall, the
check-in note and the front door. With --listeners-on, only that agent runs the ones that listen
(two agents on one machine can't take the same port); with --telemetry-on, only that agent
collects telemetry. If the plan has no seats yet, it adds what lab/topology.py describes: the
switches and firewalls to the inventory (with their management addresses, so syslog finds its
device), one seat block per access switch and the seat ranges each firewall serves. Last, it puts
every device in an inventory group with Telemetry on. Standard library only.
"""
import argparse
import http.cookiejar
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from topology import LAB, SEAT_VLAN, SEATS_PER_BLOCK


class Client:
    def __init__(self, url):
        self.url = url.rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(req, timeout=15) as answer:
                return answer.status, json.loads(answer.read() or b'{}')
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b'{}')

    def wait(self, seconds=120):
        until = time.time() + seconds
        while True:
            try:
                with urllib.request.urlopen(self.url + '/healthz', timeout=5) as answer:
                    if answer.status == 200:
                        return
            except OSError:
                pass
            if time.time() > until:
                raise SystemExit(f'{self.url} did not come up in {seconds}s')
            time.sleep(2)

    def login(self, user, password):
        # The sign-in form: every Floorbit version has it (the JSON route came later).
        form = urllib.parse.urlencode({'username': user, 'password': password}).encode()
        req = urllib.request.Request(self.url + '/login', data=form, method='POST')
        try:
            self.opener.open(req, timeout=15).read()
        except urllib.error.HTTPError as exc:
            # A redirect to the start page means it worked; only the page itself may not be built.
            if exc.code in (401, 429):
                raise SystemExit(f'Signing in as {user} failed: {exc.code}') from exc
        status, _ = self.call('GET', '/api/agents')
        if status != 200:
            raise SystemExit(f'Signed in as {user}, but the agents list answers {status}: is it an admin?')


def plugins(fb, args):
    """The lab's plugins, set up and turned on in Floorbit (Settings > Plugins)."""
    settings = {
        'librenms.url': args.librenms_url, 'librenms.token': args.librenms_token,
        'plugin.syslog-relay.port': args.syslog_port,
        'plugin.netflow-relay.port': args.flows_port,
        'plugin.captive-note.address': args.captive, 'plugin.captive-note.proxies': args.captive_proxies,
        # Check-in learns the seat PC's address only through the front door.
        'plugin.front-door.address': args.front_door, 'plugin.front-door.proxies': args.captive_proxies,
    }
    status, body = fb.call('PATCH', '/api/settings', settings)
    if status != 200:
        raise SystemExit(f'Setting up the plugins failed: {status} {body}')
    listeners = ['syslog-relay', 'netflow-relay', 'captive-note', 'front-door']
    for pid in ['librenms', *listeners]:
        status, body = fb.call('PUT', f'/api/settings/plugins/{pid}', {'enabled': True})
        if status != 200:
            raise SystemExit(f'Turning on {pid} failed: {status} {body}')
    if args.listeners_on or args.telemetry_on:
        _, body = fb.call('GET', '/api/agents')
        for agent in body.get('agents', []):
            off = []
            if args.listeners_on and agent['name'] != f'Lab {args.listeners_on}':
                off += listeners
            if args.telemetry_on and agent['name'] != f'Lab {args.telemetry_on}':
                off.append('telemetry')
            status, answer = fb.call('PATCH', f'/api/agents/{agent["id"]}', {'plugins_off': off})
            if status != 200:
                raise SystemExit(f'Leaving plugins off on {agent["name"]} failed: {status} {answer}')
    print('Set up the plugins: LibreNMS, syslog, NetFlow, the check-in note, the front door.')


def telemetry(fb, args):
    """Telemetry for every lab device, as the crew would set it up in the inventory: a group with the
    Telemetry feature on and the SNMP community, each device in it with what its kind needs (MAC
    tables per VLAN on the Catalysts, model-driven telemetry by node name on the core, the
    firewalls' own Telegraf push). Floorbit collects what the inventory says (docs/telemetry.md)."""
    where = dict(item.split('=', 1) for item in args.snmp_devices.split(',') if '=' in item)
    _, groups = fb.call('GET', '/api/inventory/groups')
    group = next((g for g in (groups or {}).get('groups', []) if g['name'] == 'Lab telemetry'), None)
    if group is None:
        status, group = fb.call('POST', '/api/inventory/groups',
                                {'name': 'Lab telemetry', 'features': {'telemetry': True}})
        if status not in (200, 201):
            raise SystemExit(f'Adding the telemetry group failed: {status} {group}')
        group = group.get('group', group)
    status, answer = fb.call('PUT', '/api/inventory/credentials', {
        'owner_type': 'group', 'owner_id': group['id'], 'kind': 'snmp_v2c', 'secret': 'public'})
    if status != 200:
        raise SystemExit(f'Setting the lab SNMP community failed: {status} {answer}')
    _, inv = fb.call('GET', '/api/inventory')
    ids = {d['name']: d['id'] for d in inv.get('devices', [])}
    for dev in LAB.devices:
        host, _, port = where.get(dev.name, dev.ip).partition(':')
        props = {'telemetry_address': host, 'telemetry_port_snmp': port}
        if dev.role == 'firewall':
            platform, props['telemetry_drivers'] = 'OPNsense', 'snmp push'
        elif dev.role == 'core':
            platform = 'NX-OS'
            props.update(telemetry_drivers='snmp traps mdt', telemetry_mdt_node=dev.name)
        else:
            platform = 'IOS'
            vlans = sorted(SEAT_VLAN.values()) if dev.role == 'distribution' else sorted(
                {s.vlan for s in LAB.seats if s.switch == dev.name})
            props.update(telemetry_drivers='snmp traps', telemetry_fdb='cisco_vlans',
                         telemetry_vlans=' '.join(map(str, vlans)))
        body = {'group_id': group['id'], 'platform': platform,
                'properties': {k: v for k, v in props.items() if v}}
        status, answer = fb.call('PATCH', f'/api/inventory/{ids[dev.name]}', body)
        if status != 200:
            raise SystemExit(f'Setting up telemetry for {dev.name} failed: {status} {answer}')
    status, answer = fb.call('PUT', '/api/settings/plugins/telemetry', {'enabled': True})
    if status != 200:
        raise SystemExit(f'Turning on telemetry failed: {status} {answer}')
    print(f'Set up telemetry for {len(LAB.devices)} devices.')


def seed(fb):
    """The lab hall: inventory, switches placed, a seat block per access switch. Only on an empty plan."""
    _, blocks = fb.call('GET', '/api/seat-blocks')
    if (blocks.get('blocks') if isinstance(blocks, dict) else blocks):
        print('The lab hall is there already.')
        return
    _, inv = fb.call('GET', '/api/inventory')
    known = {d['name'] for d in inv.get('devices', [])}
    # The core and distribution at the top, a block per access switch below with its switch beside it.
    spots = {LAB.core.name: (50, 8), LAB.dist.name: (50, 18),
             **{fw.name: (30 + 40 * i, 8) for i, fw in enumerate(LAB.firewalls)}}
    for i, sw in enumerate(LAB.access):
        spots[sw.name] = (12, 32 + i * 14)
    for dev in LAB.devices:
        if dev.name in known:
            continue
        x, y = spots[dev.name]
        status, body = fb.call('POST', '/api/inventory', {
            'name': dev.name, 'role': 'firewall' if dev.role == 'firewall' else 'switch', 'model': dev.model,
            'serial': dev.serial, 'mgmt_ip': dev.ip, 'x': x, 'y': y, 'notes': f'Lab {dev.role}'})
        if status != 201:
            raise SystemExit(f'Adding {dev.name} failed: {status} {body}')
    for i, block in enumerate('ABCD'):
        status, body = fb.call('POST', '/api/seat-blocks', {
            'name': block, 'x': 20, 'y': 30 + i * 14, 'cols': SEATS_PER_BLOCK, 'rows': 1})
        if status != 201:
            raise SystemExit(f'Adding block {block} failed: {status} {body}')
    # The core routes blocks A and B to fw-01, C and D to fw-02, each with the other as its backup.
    first, second = (fw.name for fw in LAB.firewalls)
    for i, (block, vlan) in enumerate(SEAT_VLAN.items()):
        line = [first, second] if i < len(SEAT_VLAN) // 2 else [second, first]
        status, body = fb.call('POST', '/api/firewalls/ranges', {
            'cidr': f'10.10.{vlan}.0/24', 'name': f'Block {block}', 'firewalls': line})
        if status not in (200, 201):
            raise SystemExit(f'Adding the range of block {block} failed: {status} {body}')
    print(f'Drew the lab hall: {len(LAB.devices)} devices, {len(LAB.seats)} seats, {len(SEAT_VLAN)} firewall ranges.')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('names', nargs='+')
    p.add_argument('--url', default=os.environ.get('LAB_FLOORBIT_URL', 'http://floorbit:5000'))
    p.add_argument('--out', type=pathlib.Path, default=pathlib.Path(os.environ.get('LAB_KEYS', '/keys')))
    p.add_argument('--user', default=os.environ.get('AUTH_ADMIN_USER', 'admin'))
    p.add_argument('--password', default=os.environ.get('AUTH_ADMIN_PASSWORD', ''))
    p.add_argument('--librenms-url', default='http://172.31.10.5:8000')
    p.add_argument('--librenms-token', default='lab-librenms-token')
    p.add_argument('--syslog-port', type=int, default=5514)
    p.add_argument('--flows-port', type=int, default=2055)
    p.add_argument('--captive', default='0.0.0.0:8908')
    p.add_argument('--front-door', default='0.0.0.0:8443')
    # The tests ask the note and the front door as a reverse proxy would, naming the player in
    # X-Forwarded-For.
    p.add_argument('--captive-proxies', default='172.31.10.60')
    p.add_argument('--listeners-on', default='', help='the one agent that runs the listening plugins')
    # Without VRRP every agent counts as the collector (Floorbit's docs/telemetry.md): one collects.
    p.add_argument('--telemetry-on', default='', help='the one agent that collects telemetry')
    p.add_argument('--snmp-devices', default='',
                   help='where Telegraf polls each device, "name=host:port,..." (default: its lab address, port 161)')
    args = p.parse_args(argv)
    fb = Client(args.url)
    fb.wait()
    args.out.mkdir(parents=True, exist_ok=True)
    fb.login(args.user, args.password)
    for name in [n for n in args.names if not (args.out / f'{n}.key').exists()]:
        status, body = fb.call('POST', '/api/agents', {'name': f'Lab {name}'})
        if status != 201:
            raise SystemExit(f'Enrolling {name} failed: {status} {body}')
        path = args.out / f'{name}.key'
        path.write_text(body['key'])
        os.chmod(path, 0o644)        # the agent runs as another user; lab keys only
        print(f'Enrolled {name} as {body["id"]}')
    plugins(fb, args)
    seed(fb)
    telemetry(fb, args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
