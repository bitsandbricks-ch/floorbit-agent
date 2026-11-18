"""Switch SSH for the hosted Floorbit (bitsandbricks-ch/floorbit, floorbit/switch_link.py): port
actions, commands and the terminal run here, only towards private addresses, and the login is
dropped after the call."""
import time

import flask
import pytest
from floorbit import sources
from floorbit.plugins.base import PluginError

import floorbit_agent
from floorbit_agent import switches


class Spec:
    def __init__(self, pid):
        self.id = pid


class FakeShell:
    def __init__(self):
        self.sent, self.queue, self.closed, self.size = [], ['sw-a-01#'], False, None

    def recv(self):
        if self.closed:
            return None
        return self.queue.pop(0) if self.queue else ''

    def send(self, data):
        self.sent.append(data)
        self.queue.append(f'{data.strip()}\r\nok\r\nsw-a-01#')

    def resize(self, cols, rows):
        self.size = (cols, rows)

    def close(self):
        self.closed = True


class Registry:
    def __init__(self, plugins=('ssh-ports',)):
        self.specs = [Spec(p) for p in plugins]
        self.calls, self.logins, self.shell = [], [], FakeShell()

    def feeders(self, area):
        return self.specs if area == 'ports' else []

    def call(self, spec, method, target):
        self.calls.append((spec.id, method, target['host']))
        self.logins.append(target['login'])
        if target.get('pinned') is None:
            target['pinned_new'] = 'ssh-ed25519 SHA256:example'
        if method == 'open_shell':
            return self.shell
        return {'message': f"{method} on {target['switch']}"}


@pytest.fixture
def registry(monkeypatch):
    reg = Registry()
    monkeypatch.setattr(sources, 'registry', reg)
    monkeypatch.delenv('FLOORBIT_AGENT_SWITCH_NETS', raising=False)
    yield reg
    switches.shells.close_all()


def target(host='10.0.0.2', pinned=None):
    return {'switch': 'sw-a-01', 'host': host, 'port': 'Gi1/0/7', 'pinned': pinned,
            'login': {'username': 'tech', 'password': 'example-password'}}


def test_a_port_action_runs_here_and_forgets_the_login(registry):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    answer = execute('switch_call', ['ssh-ports', 'bounce_port', target()])
    assert answer == {'result': {'message': 'bounce_port on sw-a-01'}, 'pinned': 'ssh-ed25519 SHA256:example'}
    assert registry.calls == [('ssh-ports', 'bounce_port', '10.0.0.2')]
    assert registry.logins == [{}]            # cleared once the call was done


def test_a_known_key_is_not_sent_back(registry):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    assert execute('switch_call', ['ssh-ports', 'set_port', target(pinned='ssh-rsa SHA256:x')])['pinned'] is None


def test_only_private_addresses_and_known_methods(registry, monkeypatch):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    for host in ('8.8.8.8', '127.0.0.1', '169.254.169.254', '::ffff:127.0.0.1'):
        with pytest.raises(PluginError, match='not on a network'):
            execute('switch_call', ['ssh-ports', 'bounce_port', target(host)])
    with pytest.raises(PluginError, match='not a switch call'):
        execute('switch_call', ['ssh-ports', 'open_shell', target()])
    with pytest.raises(PluginError, match='not on on the agent'):
        execute('switch_call', ['demo', 'bounce_port', target()])
    with pytest.raises(PluginError, match='wrongly'):
        execute('switch_call', ['ssh-ports', 'bounce_port', {'host': '10.0.0.2'}])
    assert registry.calls == []
    monkeypatch.setenv('FLOORBIT_AGENT_SWITCH_NETS', '203.0.113.0/24')
    execute('switch_call', ['ssh-ports', 'bounce_port', target('203.0.113.4')])
    assert registry.calls == [('ssh-ports', 'bounce_port', '203.0.113.4')]


def test_a_terminal_is_held_here_and_read_with_long_polls(registry):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    opened = execute('shell_open', ['ssh-ports', target()])
    sid = opened['id']
    first = execute('shell_io', [sid, '', None, 0, 2])
    assert first['data'] == 'sw-a-01#' and not first['closed']
    execute('shell_io', [sid, 'show clock\n', [100, 30], first['next'], 0])
    assert registry.shell.sent == ['show clock\n'] and registry.shell.size == (100, 30)
    later = execute('shell_io', [sid, '', None, first['next'], 2])
    assert 'ok' in later['data']
    assert execute('shell_close', [sid]) is True
    assert registry.shell.closed
    with pytest.raises(PluginError, match='closed'):
        execute('shell_io', [sid, '', None, 0, 0])


def test_shells_nobody_reads_are_closed(registry, monkeypatch):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    sid = execute('shell_open', ['ssh-ports', target()])['id']
    switches.shells.prune(now=time.monotonic() + switches.SHELL_IDLE + 1)
    assert registry.shell.closed
    with pytest.raises(PluginError, match='closed'):
        execute('shell_io', [sid, '', None, 0, 0])


def test_at_most_so_many_shells(registry, monkeypatch):
    monkeypatch.setattr(switches, 'MAX_SHELLS', 1)
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    execute('shell_open', ['ssh-ports', target()])
    with pytest.raises(PluginError, match='terminals open'):
        execute('shell_open', ['ssh-ports', target()])
