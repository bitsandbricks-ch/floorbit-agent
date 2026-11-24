"""The high availability drill (Floorbit issue #1405): two agents in one group, and what happens,
and how fast, when one of them goes away. Runs on the machine that runs the lab (it stops and
starts the lab's containers), against a running lab:

    docker compose -f lab/compose.yml up -d --wait
    python3 lab/ha_drill.py --report ha-drill.md      # needs requests; about 6 minutes

Each step does one thing to the lab and times what the crew would notice, from the moment it was
done: the other agent is primary on the Local agents page, a seat lookup answers through it, new
syslog arrives, the other agent's front door answers. A step fails when a time is over its budget
or the group ever has two primaries. The report is a Markdown table (and --json for the numbers).

Steps: the primary is killed (the machine dies), it comes back (it must not take the lease back),
the standby is stopped (nothing may move), the hosted Floorbit is away for a minute (nobody may
change role), and the primary is stopped cleanly.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from topology import LAB

URL = os.environ.get('LAB_FLOORBIT_URL', 'http://172.31.10.2:5000').rstrip('/')
PASSWORD = os.environ.get('LAB_ADMIN_PASSWORD', 'lab-admin-password')
COMPOSE = ['docker', 'compose', '-f', str(pathlib.Path(__file__).resolve().parent / 'compose.yml')]
# The lab's agents: container name -> front door. Their names in Floorbit are "Lab <container>".
AGENTS = {'agent-a': 'http://172.31.10.31:8080', 'agent-b': 'http://172.31.10.32:8080'}
SEAT = 'D-20'
# Seconds the crew may wait, per thing it notices. A killed agent's link closes when its TCP
# connection does, which Floorbit's WebSocket notices within its ping (20 s) at worst.
BUDGET = {'primary': 30, 'lookup': 45, 'syslog': 60, 'front_door': 5}
EVERY = 0.5


class Floorbit:
    def __init__(self):
        self.s = requests.Session()

    def login(self):
        r = self.s.post(f'{URL}/login', data={'username': 'admin', 'password': PASSWORD},
                        allow_redirects=False, timeout=15)
        if r.status_code not in (302, 303):
            raise SystemExit(f'sign-in to {URL} failed: {r.status_code}')

    def get(self, path, **kw):
        r = self.s.get(f'{URL}{path}', timeout=10, **kw)
        r.raise_for_status()
        return r.json()


def compose(*args):
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True)


def group(fb):
    """(the group's primary as a container name or None, {container: role}, connected count)."""
    body = fb.get('/api/agents')
    by_id = {a['id']: a['name'].removeprefix('Lab ') for a in body['agents']}
    groups = body.get('groups') or []
    g = next((g for g in groups if len(g['agents']) >= 2), None)
    if g is None:
        return None, {}, 0
    roles = {by_id.get(a['id'], a['id']): a['role'] for a in g['agents']}
    primary = by_id.get(g['primary']) if g.get('primary') else None
    return primary, roles, sum(a['connected'] for a in g['agents'])


class Watch:
    """Samples the group in the background: every primary seen, and whether two ever were at once."""

    def __init__(self, fb):
        self.fb, self.seen, self.double = fb, [], []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(5)

    def _run(self):
        while not self._stop.wait(EVERY):
            try:
                _primary, roles, _n = group(self.fb)
            except requests.RequestException:
                continue
            primaries = sorted(n for n, r in roles.items() if r == 'primary')
            self.seen.append((time.monotonic(), primaries))
            if len(primaries) > 1:
                self.double.append(primaries)


def until(check, timeout):
    """Seconds until check() is truthy, or None after ``timeout``."""
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        try:
            if check():
                return round(time.monotonic() - began, 1)
        except (requests.RequestException, KeyError, ValueError):
            pass
        time.sleep(EVERY)
    return None


def lookup_ok(fb):
    seat = LAB.seat(SEAT)
    found = fb.get(f'/api/resolve/{seat.ip}')
    return found.get('hostname') == seat.switch


def last_syslog(fb):
    return fb.get('/api/logs', params={'limit': 1})['status']['last_id']


def front_door_ok(url):
    return requests.get(f'{url}/.well-known/captive-portal', timeout=3).ok


def settle(fb, timeout=180):
    """Both agents connected and one primary: the lab's normal state."""
    def ok():
        primary, _roles, n = group(fb)
        return n == 2 and primary
    if until(ok, timeout) is None:
        raise SystemExit('the lab did not settle: two connected agents and one primary')
    return group(fb)[0]


def step_kill_primary(fb, primary, other):
    since = last_syslog(fb)
    with Watch(fb) as w:
        t0 = time.monotonic()
        compose('kill', primary)
        times = {
            'primary': until(lambda: group(fb)[0] == other, BUDGET['primary']),
            'lookup': until(lambda: lookup_ok(fb), BUDGET['lookup']),
            'syslog': until(lambda: last_syslog(fb) > since, BUDGET['syslog']),
            'front_door': until(lambda: front_door_ok(AGENTS[other]), BUDGET['front_door']),
        }
        total = round(time.monotonic() - t0, 1)
    return times, w.double, f'{total} s'


def step_back(fb, primary, other):
    """The old primary comes back: the lease must stay where it is (sticky)."""
    with Watch(fb) as w:
        compose('start', primary)
        back = until(lambda: group(fb)[2] == 2, 120)
        time.sleep(60)
    moved = [p for _t, p in w.seen if p and p != [other]]
    return {'reconnected': back}, w.double, 'lease stayed' if not moved else f'lease moved: {moved[-1]}'


def step_stop_standby(fb, primary, standby):
    with Watch(fb) as w:
        compose('stop', standby)
        time.sleep(30)
        compose('start', standby)
        back = until(lambda: group(fb)[2] == 2, 120)
    moved = [p for _t, p in w.seen if p and p != [primary]]
    return {'reconnected': back}, w.double, 'primary stayed' if not moved else f'primary moved: {moved[-1]}'


def step_hosted_away(fb, primary):
    compose('stop', 'floorbit')
    time.sleep(60)
    compose('start', 'floorbit')
    if until(lambda: requests.get(f'{URL}/healthz', timeout=3).ok, 120) is None:
        raise SystemExit('Floorbit did not come back')
    fb.login()
    with Watch(fb) as w:
        back = until(lambda: group(fb)[2] == 2, 120)
        time.sleep(30)
    moved = [p for _t, p in w.seen if p and p != [primary]]
    return {'reconnected': back}, w.double, 'roles kept' if not moved else f'primary moved: {moved[-1]}'


def step_stop_primary(fb, primary, other):
    with Watch(fb) as w:
        compose('stop', primary)
        times = {'primary': until(lambda: group(fb)[0] == other, BUDGET['primary']),
                 'lookup': until(lambda: lookup_ok(fb), BUDGET['lookup'])}
    compose('start', primary)
    return times, w.double, ''


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--report', help='write the Markdown table here')
    p.add_argument('--json', help='write the numbers here')
    args = p.parse_args(argv)
    fb = Floorbit()
    fb.login()
    rows, failed = [], False

    def run(name, fn, *a):
        nonlocal failed
        times, double, note = fn(fb, *a)
        late = [k for k, v in times.items() if k in BUDGET and v is None]
        ok = not late and not double and 'moved' not in note
        failed = failed or not ok
        rows.append({'step': name, 'ok': ok, 'times': times, 'two_primaries': double[:3], 'note': note})
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {json.dumps(times)} {note}"
              + (f' two primaries: {double[0]}' if double else ''), flush=True)

    primary = settle(fb)
    other = next(n for n in AGENTS if n != primary)
    print(f'group primary: {primary}, standby: {other}', flush=True)
    run(f'{primary} is killed', step_kill_primary, primary, other)
    run(f'{primary} comes back', step_back, primary, other)
    primary, other = other, primary
    run(f'standby {other} stops for 30 s', step_stop_standby, primary, other)
    run('the hosted Floorbit is away for 60 s', step_hosted_away, primary)
    settle(fb)
    run(f'{primary} is stopped cleanly', step_stop_primary, primary, other)

    if args.report:
        lines = ['| Step | | Primary moved | Lookup answers | New syslog | Other front door | Note |',
                 '| --- | --- | --- | --- | --- | --- | --- |']
        for r in rows:
            t = r['times']

            def cell(k, t=t):
                return '' if k not in t else ('late' if t[k] is None else f'{t[k]} s')
            note = r['note'] + (f"; two primaries: {r['two_primaries'][0]}" if r['two_primaries'] else '')
            lines.append(f"| {r['step']} | {'OK' if r['ok'] else 'FAIL'} | {cell('primary')} | {cell('lookup')} "
                         f"| {cell('syslog')} | {cell('front_door')} | {note} |")
        budgets = ', '.join(f'{k} {v} s' for k, v in BUDGET.items())
        pathlib.Path(args.report).write_text('\n'.join(lines) + f'\n\nBudgets: {budgets}.\n')
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(rows, indent=2))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
