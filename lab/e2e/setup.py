#!/usr/bin/env python3
"""Set up the lab's Floorbit once: enrol the lab's agents and draw the lab hall.

    python3 lab/e2e/setup.py --url http://floorbit:5000 --out /keys agent-a agent-b

Signs in as the admin, enrols each agent and writes its key to <out>/<name>.key (the agents read
it at start); an agent whose key file is there already is left alone, so a restart keeps its keys.
Then, if the plan has no seats yet, it adds what lab/topology.py describes: the switches and
firewalls to the inventory (with their management addresses, so syslog finds its device) and one
seat block per access switch. Standard library only.
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
from topology import LAB, SEATS_PER_BLOCK


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
    print(f'Drew the lab hall: {len(LAB.devices)} devices, {len(LAB.seats)} seats.')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('names', nargs='+')
    p.add_argument('--url', default=os.environ.get('LAB_FLOORBIT_URL', 'http://floorbit:5000'))
    p.add_argument('--out', type=pathlib.Path, default=pathlib.Path(os.environ.get('LAB_KEYS', '/keys')))
    p.add_argument('--user', default=os.environ.get('AUTH_ADMIN_USER', 'admin'))
    p.add_argument('--password', default=os.environ.get('AUTH_ADMIN_PASSWORD', ''))
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
    seed(fb)
    return 0


if __name__ == '__main__':
    sys.exit(main())
