"""The agent hands what its environment set for plugins to the hosted Floorbit once, and from then
on reads none of it (floorbit_agent/env_settings.py)."""
import pytest

from floorbit_agent import env_settings
from tests.test_agent import FakeHost, Resp, make


class Spec:
    def __init__(self, pid, keys=(), core=None):
        self.id, self.keys, self.manifest = pid, keys, {'core_settings': core} if core else {}

    def items(self, kind):
        return [{'key': k} for k in self.keys] if kind == 'settings' else []


class Registry:
    def __init__(self, store=None):
        self.settings_store = store
        self.specs = {'librenms': Spec('librenms', core='librenms'), 'pihole': Spec('pihole', ['url', 'token'])}

    def _core_fields(self, spec):
        return ['librenms.url', 'librenms.token'] if spec.manifest.get('core_settings') == 'librenms' else []


@pytest.fixture(autouse=True)
def pihole_fields(monkeypatch):
    from floorbit.settings import FIELDS_BY_KEY
    monkeypatch.setitem(FIELDS_BY_KEY, 'plugin.pihole.url',
                        {'key': 'plugin.pihole.url', 'env': 'FLOORBIT_PLUGIN_PIHOLE_URL'})
    monkeypatch.setitem(FIELDS_BY_KEY, 'plugin.pihole.token',
                        {'key': 'plugin.pihole.token', 'env': 'FLOORBIT_PLUGIN_PIHOLE_TOKEN'})


ENV = {
    'LIBRENMS_URL': 'https://librenms.example.com',
    'FLOORBIT_PLUGIN_PIHOLE_TOKEN': 'example-token',
    'FLOORBIT_AGENT_SYSLOG_PORT': '514',
    'FLOORBIT_AGENT_CAPTIVE': 'off',
    'FLOORBIT_AGENT_FRONT_DOOR': '0.0.0.0:8443',
    'FLOORBIT_AGENT_PLUGINS': 'http-check, pihole,',
    'FLOORBIT_AGENT_KEY': 'fba_x_y',                 # the agent's own: never handed over
}


def test_it_collects_plugin_settings_as_the_hosted_sides_keys():
    settings, plugins, names = env_settings.collect(Registry(), ENV)
    assert settings == {'librenms.url': 'https://librenms.example.com', 'plugin.pihole.token': 'example-token',
                        'plugin.syslog-relay.port': 514, 'plugin.front-door.address': '0.0.0.0:8443'}
    assert plugins == ['http-check', 'pihole']
    assert 'FLOORBIT_AGENT_KEY' not in names and 'FLOORBIT_AGENT_CAPTIVE' not in names
    assert names == sorted(names) and 'FLOORBIT_AGENT_PLUGINS' in names


class Hosted(FakeHost):
    def __init__(self, status=200, body=None):
        super().__init__()
        self.answer = Resp(status, body if body is not None else
                           {'imported': ['pihole'], 'skipped': {'librenms': 'already set up here'}})

    def post(self, url, data=None, headers=None, timeout=None):
        super().post(url, data, headers, timeout)
        return self.answer


def test_it_is_sent_once_and_again_only_when_the_environment_changes(tmp_path):
    host, said = Hosted(), []
    sender = env_settings.Sender(Registry(), str(tmp_path), ENV, log=said.append)
    a = make(host)
    sender.send(a)
    url, _headers, body = host.posts[-1]
    assert url.endswith('/api/agent/v1/env-settings') and body['plugins'] == ['http-check', 'pihole']
    assert any('took over pihole' in line for line in said)
    assert any('librenms' in line and 'already set up here' in line for line in said)
    assert 'example-token' not in (tmp_path / env_settings.FILE).read_text()     # a hash, never the values
    sender.send(a)
    assert len(host.posts) == 1
    assert env_settings.Sender(Registry(), str(tmp_path), ENV).done           # after a restart too
    changed = env_settings.Sender(Registry(), str(tmp_path), {**ENV, 'FLOORBIT_AGENT_SYSLOG_PORT': '5514'})
    assert not changed.done


def test_nothing_set_sends_nothing(tmp_path):
    host = Hosted()
    sender = env_settings.Sender(Registry(), str(tmp_path), {'FLOORBIT_AGENT_KEY': 'fba_x_y'})
    sender.send(make(host))
    assert sender.done and host.posts == []


@pytest.mark.parametrize('status, why', [(404, 'update Floorbit first'), (500, 'HTTP 500')])
def test_a_failure_says_so_once_and_tries_again_at_the_next_connect(tmp_path, status, why):
    host, said = Hosted(status, {'error': 'no'}), []
    sender = env_settings.Sender(Registry(), str(tmp_path), ENV, log=said.append)
    a = make(host)
    sender.send(a)
    sender.send(a)
    assert len(host.posts) == 2 and not sender.done
    assert len(said) == 1 and 'no longer read' in said[0]
    assert not (tmp_path / env_settings.FILE).exists()
    host.answer = Resp(200, {'imported': [], 'skipped': {}})
    sender.send(a)
    assert sender.done


def test_after_the_handover_the_environment_no_longer_counts(tmp_path):
    from floorbit.settings import SettingsStore
    store = SettingsStore(str(tmp_path / 'data.json'), environ=ENV)
    assert store.get('librenms.url') == 'https://librenms.example.com'
    env_settings.ignore(Registry(store))
    assert store.get('librenms.url') == ''


def test_the_agent_tells_its_listeners_when_it_got_through():
    a = make(FakeHost())
    seen = []
    a.on_connect.append(seen.append)
    a.on_connect.append(lambda _agent: 1 / 0)       # one that fails doesn't stop the link
    a.connected()
    assert seen == [a]
