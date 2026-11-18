"""The lab's end-to-end tests run against a running lab (lab/compose.yml or lab/run-local.sh), not
against fakes: a real Floorbit, real agents, the lab LibreNMS and the simulated switches.

Settings (env): LAB_FLOORBIT_URL, LAB_ADMIN_PASSWORD, LAB_AGENT_METRICS ("name=url,..."),
LAB_CAPTIVE (an agent's captive portal URL), LAB_FRONT_DOOR (an agent's front door),
LAB_AGENT_DOWN (the agent the failover run stopped).
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


def pytest_configure(config):
    config.addinivalue_line('markers', 'failover: run after one agent was stopped (LAB_AGENT_DOWN)')


def pytest_collection_modifyitems(config, items):
    down = os.environ.get('LAB_AGENT_DOWN')
    for item in items:
        if ('failover' in item.keywords) != bool(down):
            item.add_marker(pytest.mark.skip(reason='failover run only' if not down else 'normal run only'))


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
    want = 1 if os.environ.get('LAB_AGENT_DOWN') else 2

    def agents_up():
        return sum(a['connected'] for a in f.get('/api/agents')['agents']) >= want
    eventually(agents_up, what='the agents')
    eventually(lambda: f.get('/api/switches/access-b1/status').get('status') == 'online', what='the lab LibreNMS')
    return f


def short(port):
    return port.replace('TenGigabitEthernet', 'Te').replace('GigabitEthernet', 'Gi')
