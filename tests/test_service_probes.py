"""DNS and TCP probes for the services view (bitsandbricks-ch/floorbit#471): run on the event network,
never towards this machine, and a TCP probe only to the few ports every server answers."""
import sys
import types

import flask
import pytest
from floorbit.plugins.base import PluginError

import floorbit_agent


class ProbeError(RuntimeError):
    pass


@pytest.fixture
def probes(monkeypatch):
    """Floorbit's floorbit/probes.py, faked: the calls it got, and what it answers."""
    fake = types.SimpleNamespace(ProbeError=ProbeError, PROBE_PORTS=(53, 80, 443, 853), MAX_COUNT=10, calls=[])

    def dns(server, name):
        fake.calls.append(('dns', server, name))
        if server == '192.0.2.1':
            raise ProbeError('The resolver did not answer.')
        return {'ms': 3.1, 'rcode': 'NOERROR', 'answers': 2}

    def tcp(host, port, count):
        fake.calls.append(('tcp', host, port, count))
        return {'sent': count, 'lost': 0, 'avg_ms': 9.5}
    fake.dns, fake.tcp = dns, tcp
    import floorbit
    monkeypatch.setitem(sys.modules, 'floorbit.probes', fake)
    monkeypatch.setattr(floorbit, 'probes', fake, raising=False)
    monkeypatch.delenv('FLOORBIT_AGENT_PROBE_NETS', raising=False)
    return fake


def test_probes_run_here_towards_public_and_private_addresses(probes):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    assert execute('dns_probe', ['198.51.100.53', 'example.com'])['rcode'] == 'NOERROR'
    assert execute('tcp_probe', ['10.0.0.1', 443, 50])['avg_ms'] == 9.5
    assert probes.calls == [('dns', '198.51.100.53', 'example.com'), ('tcp', '10.0.0.1', 443, 10)]
    with pytest.raises(PluginError, match='did not answer'):
        execute('dns_probe', ['192.0.2.1', 'example.com'])


def test_never_this_machine_nor_other_ports(probes, monkeypatch):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    for host in ('127.0.0.1', 'localhost', '169.254.1.1', '::1', '::ffff:127.0.0.1', '::ffff:169.254.169.254',
                 '64:ff9b::a9fe:a9fe', '64:ff9b::7f00:1', '2002:a9fe:a9fe::1'):
        with pytest.raises(PluginError, match='may probe'):
            execute('tcp_probe', [host, 443, 5])
    with pytest.raises(PluginError, match='only probes ports'):
        execute('tcp_probe', ['10.0.0.1', 22, 5])
    with pytest.raises(PluginError, match='asked for wrongly'):
        execute('tcp_probe', ['10.0.0.1'])
    assert probes.calls == []
    monkeypatch.setenv('FLOORBIT_AGENT_PROBE_NETS', '203.0.113.0/24')
    assert execute('tcp_probe', ['203.0.113.9', 53, 3])['sent'] == 3
    with pytest.raises(PluginError, match='may probe'):
        execute('tcp_probe', ['10.0.0.1', 443, 5])


def test_own_checks_run_here_through_the_same_address_rule(probes, monkeypatch):
    """A crew's own check (bitsandbricks-ch/floorbit#1338): any port, but the address goes through
    probe_target like every probe, and only what floorbit/probes.py returns comes back."""
    seen = []

    def check(spec, resolve):
        address = resolve(spec.get('host') or 'www.example.com')
        seen.append((spec['kind'], address))
        return {'kind': spec['kind'], 'sent': 3, 'lost': 0, 'ms': 1.5}
    probes.check = check
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    assert execute('check_probe', [{'kind': 'tcp', 'host': '10.0.0.1', 'port': 8080}])['ms'] == 1.5
    assert seen == [('tcp', '10.0.0.1')]
    with pytest.raises(PluginError, match='may probe'):
        execute('check_probe', [{'kind': 'ping', 'host': '127.0.0.1'}])
    with pytest.raises(PluginError, match='asked for wrongly'):
        execute('check_probe', ['10.0.0.1'])
    monkeypatch.setenv('FLOORBIT_AGENT_PROBE_NETS', '203.0.113.0/24')
    with pytest.raises(PluginError, match='may probe'):
        execute('check_probe', [{'kind': 'tcp', 'host': '10.0.0.1', 'port': 8080}])
