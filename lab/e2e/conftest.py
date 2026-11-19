"""The lab's end-to-end tests run against a running lab (lab/compose.yml or lab/run-local.sh), not
against fakes: a real Floorbit, real agents, the lab LibreNMS and the simulated switches.

Settings (env): LAB_FLOORBIT_URL, LAB_ADMIN_PASSWORD, LAB_AGENT_METRICS ("name=url,..."),
LAB_FRONT_DOOR (an agent's front door: check-in, the note on phones, the seat walk),
LAB_AGENT_DOWN (the agent the failover run stopped).

LAB_DRILL names the bad day the lab is in, and only that drill's tests run (test_drills.py, and
the failover's here): devices-down (LAB_DOWN, fw-01 and access-c1 stopped), recovered (started
again), librenms-down, hosted-down (Floorbit stopped) and hosted-back (LAB_HOSTED_AWAY, "from-to"
in epoch seconds). Without it, the normal run.
"""
import os
import pathlib
import sys
import time

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from topology import LAB

URL = os.environ.get('LAB_FLOORBIT_URL', 'http://127.0.0.1:5000').rstrip('/')


def drill():
    """The bad day the lab is in ('' on a normal day)."""
    return os.environ.get('LAB_DRILL') or ('agent-down' if os.environ.get('LAB_AGENT_DOWN') else '')


def pytest_configure(config):
    config.addinivalue_line('markers', 'failover: run after one agent was stopped (LAB_AGENT_DOWN)')
    config.addinivalue_line('markers', 'drill(name): run only in that drill (LAB_DRILL)')


def pytest_collection_modifyitems(config, items):
    now = drill()
    for item in items:
        mark = item.get_closest_marker('drill')
        wants = mark.args[0] if mark else 'agent-down' if 'failover' in item.keywords else ''
        if wants != now:
            item.add_marker(pytest.mark.skip(reason=f'{wants or "normal"} run only'))


def eventually(check, timeout=90, every=3, what='the lab'):
    """check() until it returns something truthy without raising; then that value."""
    until = time.time() + timeout
    last = None
    while True:
        try:
            value = check()
            if value:
                return value
            last = f'{check.__name__} returned {value!r}'
        except (AssertionError, requests.RequestException, KeyError, ValueError) as exc:
            last = f'{type(exc).__name__}: {exc}'
        if time.time() > until:
            raise AssertionError(f'{what} did not settle in {timeout}s: {last}')
        time.sleep(every)


class Floorbit:
    def __init__(self, url):
        self.url = url
        self.s = requests.Session()

    def login(self):
        password = os.environ.get('LAB_ADMIN_PASSWORD', 'lab-admin-password')
        r = self.s.post(f'{self.url}/login', data={'username': 'admin', 'password': password},
                        allow_redirects=False, timeout=15)
        assert r.status_code in (302, 303), f'sign-in failed: {r.status_code}'

    def get(self, path, **kw):
        r = self.s.get(f'{self.url}{path}', timeout=60, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None, **kw):
        return self.s.post(f'{self.url}{path}', json=body, timeout=60, **kw)

    def patch(self, path, body, **kw):
        r = self.s.patch(f'{self.url}{path}', json=body, timeout=60, headers={'Origin': self.url}, **kw)
        r.raise_for_status()
        return r.json()


@pytest.fixture(scope='session')
def lab():
    return LAB


@pytest.fixture(scope='session')
def fb():
    f = Floorbit(URL)
    eventually(lambda: requests.get(f'{URL}/healthz', timeout=5).ok, what='Floorbit')
    f.login()
    # Both agents connected (or, in the failover run, the one still up) and the lab LibreNMS
    # polled twice, so port rates are there.
    want = 1 if drill() == 'agent-down' else 2

    def agents_up():
        return sum(a['connected'] for a in f.get('/api/agents')['agents']) >= want
    eventually(agents_up, timeout=180, what='the agents')
    if drill() != 'librenms-down':
        eventually(lambda: f.get('/api/switches/access-b1/status').get('status') == 'online', what='the lab LibreNMS')
    return f


def short(port):
    return port.replace('TenGigabitEthernet', 'Te').replace('GigabitEthernet', 'Gi')
