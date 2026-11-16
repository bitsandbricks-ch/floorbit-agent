"""Game server queries and RCON (bitsandbricks-ch/floorbit#472): run on the event network, and only
towards private addresses, so a hosted Floorbit can't point the agent at the internet."""
import sys
import types

import flask
import pytest
from floorbit.plugins.base import PluginError

import floorbit_agent


class QueryError(RuntimeError):
    pass


@pytest.fixture
def game_query(monkeypatch):
    """Floorbit's floorbit/game_query.py, faked: the calls it got, and what it answers."""
    fake = types.SimpleNamespace(QueryError=QueryError, calls=[])

    def query(protocol, host, port):
        fake.calls.append(('query', protocol, host, port))
        if port == 1:
            raise QueryError('No answer to the server query.')
        return {'name': 'Hall A', 'players': 7, 'max_players': 10, 'ping_ms': 1.5}

    def rcon(host, port, password, command):
        fake.calls.append(('rcon', host, port, password, command))
        return f'ran {command}'
    fake.query, fake.rcon = query, rcon
    import floorbit
    monkeypatch.setitem(sys.modules, 'floorbit.game_query', fake)
    monkeypatch.setattr(floorbit, 'game_query', fake, raising=False)
    monkeypatch.delenv('FLOORBIT_AGENT_GAME_NETS', raising=False)
    return fake


def test_a_query_and_an_rcon_command_run_here(game_query):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    assert execute('game_query', ['a2s', '192.168.10.5', 27015])['players'] == 7
    assert execute('game_rcon', ['10.0.0.9', '27015', 'example-rcon', 'status']) == 'ran status'
    assert game_query.calls == [('query', 'a2s', '192.168.10.5', 27015),
                                ('rcon', '10.0.0.9', 27015, 'example-rcon', 'status')]
    with pytest.raises(PluginError, match='No answer'):
        execute('game_query', ['a2s', '192.168.10.5', 1])


def test_only_private_addresses_unless_told_otherwise(game_query, monkeypatch):
    execute = floorbit_agent.local_executor(flask.Flask('test'))
    for host in ('8.8.8.8', '127.0.0.1', 'localhost'):
        with pytest.raises(PluginError, match='not on a network'):
            execute('game_query', ['tcp', host, 80])
    assert game_query.calls == []
    monkeypatch.setenv('FLOORBIT_AGENT_GAME_NETS', '203.0.113.0/24')
    assert execute('game_query', ['a2s', '203.0.113.20', 27015])['name'] == 'Hall A'
    with pytest.raises(PluginError, match='not on a network'):
        execute('game_query', ['a2s', '192.168.10.5', 27015])     # the list replaces the default
