"""Which monitoring calls the agent answers, and that it hands them to its own plugin."""
import flask
import pytest
from floorbit import sources
from floorbit.plugins.base import PluginError

import floorbit_agent


class Registry:
    def __init__(self):
        self.calls = []

    def monitoring_source(self):
        return 'librenms'

    def call(self, spec, method, *args):
        self.calls.append((spec, method, args))
        return [{'port': 'Gi1/0/7', 'mac': '02:00:00:00:00:07', 'vlan': 3}]


def test_the_mac_table_call_goes_to_the_plugin(monkeypatch):
    registry = Registry()
    monkeypatch.setattr(sources, 'registry', registry)
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    assert execute('port_clients', ['sw-a-01'])[0]['port'] == 'Gi1/0/7'
    assert registry.calls == [('librenms', 'port_clients', ('sw-a-01',))]


def test_a_call_it_does_not_know_is_refused(monkeypatch):
    monkeypatch.setattr(sources, 'registry', Registry())
    with pytest.raises(PluginError):
        floorbit_agent.local_executor(flask.Flask('test'))('delete_everything', [])


def test_lldp_neighbours_go_to_the_plugin(monkeypatch):
    registry = Registry()
    monkeypatch.setattr(sources, 'registry', registry)
    floorbit_agent.local_executor(flask.Flask('test'))('neighbours', ['sw-a-01'])
    assert registry.calls == [('librenms', 'neighbours', ('sw-a-01',))]


def test_device_facts_go_to_the_monitoring_plugins(monkeypatch):
    class Spec:
        id = name = 'librenms'

        def plugin_class(self):
            class P:
                def configured(self):
                    return True

                def device_facts(self, hostname):
                    return {}
            return P

    spec = Spec()

    class Facts(Registry):
        def monitoring_source(self):
            return spec

        def monitoring_sources(self):
            return [spec]

        def call(self, spec, method, *args):
            self.calls.append((spec.id, method, args))
            return {'vlans': {'10': 'seats'}}

    registry = Facts()
    monkeypatch.setattr(sources, 'registry', registry)
    got = floorbit_agent.local_executor(flask.Flask('test'))('device_facts', ['sw-a-01'])
    assert got == {'vlans': {'10': 'seats'}}
    assert registry.calls == [('librenms', 'device_facts', ('sw-a-01',))]


def test_firewall_ranges_come_from_the_agents_netbox(monkeypatch):
    from floorbit import firewalls

    class Feeding(Registry):
        def call(self, spec, method, *args):
            self.calls.append((spec.name, method, args))
            return [{'cidr': '192.0.2.0/27', 'name': 'Block A', 'firewalls': ['fw-01', 'fw-02']}]

    class Spec:
        id, name = 'netbox', 'NetBox'

    registry = Feeding()
    monkeypatch.setattr(sources, 'registry', registry)
    monkeypatch.setattr(firewalls, 'import_sources', lambda reg: [Spec()])
    feeds = floorbit_agent.local_executor(flask.Flask('test'))('firewall_feeds', [])
    assert feeds == [{'plugin': 'netbox', 'name': 'NetBox',
                      'items': [{'cidr': '192.0.2.0/27', 'name': 'Block A', 'firewalls': ['fw-01', 'fw-02']}]}]
    assert registry.calls == [('NetBox', 'firewall_ranges', ())]
