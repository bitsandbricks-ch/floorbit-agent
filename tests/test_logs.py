"""The agent's log: time, level and part on every line, a level to turn it up, a summary."""
import io
import logging
import re

import pytest

from floorbit_agent import logs


@pytest.fixture
def captured():
    stream = io.StringIO()
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    yield stream
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in before[0]:
        root.addHandler(h)
    root.setLevel(before[1])


def test_every_line_has_time_level_and_part(captured):
    assert logs.setup({}, captured) == 'info'
    logs.say('[agent] connected to https://floor.example.com')
    logs.say('[telemetry] cannot start Telegraf (/usr/bin/telegraf): no such file')
    logs.say(logs.debug('[agent] call arp answered in 12 ms'))
    lines = captured.getvalue().splitlines()
    assert len(lines) == 2                     # debug is off at info
    assert re.match(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} INFO  \[agent\] connected', lines[0])
    assert ' WARNING [telemetry] cannot start Telegraf' in lines[1]


def test_the_level_can_go_up_to_debug(captured):
    assert logs.setup({'FLOORBIT_AGENT_LOG_LEVEL': 'DEBUG'}, captured) == 'debug'
    assert logs.enabled('debug')
    logs.say(logs.debug('[agent] call arp answered in 12 ms'))
    assert ' DEBUG [agent] call arp answered' in captured.getvalue()
    assert logs.setup({'FLOORBIT_AGENT_LOG_LEVEL': 'loud'}, captured) == 'info'   # unknown: info
    assert logs.setup({'LOG_LEVEL': 'warning'}, captured) == 'warning'


def test_a_line_keeps_its_level_and_stays_a_string():
    line = logs.warning('[syslog] queue full')
    assert line == '[syslog] queue full' and logs.level_of(line) == logging.WARNING
    assert logs.level_of('[agent] plugin syslog started') == logging.INFO
    assert logs.level_of('[agent] lost the connection to x') == logging.WARNING


def _data(plugins, ok=True):
    return {'health': {'ok': ok, 'link': 'websocket', 'reason': 'no answer for 300 s'},
            'agent': {'up_seconds': 7200, 'reconnects': 2}, 'plugins': plugins}


def test_the_summary_line():
    line = logs.summary(_data({'telemetry': {'status': 'warning', 'running': True,
                                             'metrics': {'points': 6720, 'sent': 69, 'bad_lines': 0}},
                               'syslog': {'status': 'error', 'running': False, 'metrics': {}}}))
    assert line == ('[agent] connected over WebSocket; up 2 h; 2 reconnects | syslog not running; '
                    'telemetry warning (6,720 points, 69 sent)')
    assert 'NOT connected: no answer for 300 s' in logs.summary(_data({}, ok=False))


def test_a_plugin_that_changes_state_says_so_once():
    said = []
    plugins = {'telemetry': {'status': 'ok', 'running': True, 'message': 'all heard'}}
    r = logs.Reporter(lambda: _data(plugins), log=said.append)
    r.tick()
    assert said == []                           # fine from the start: nothing to say
    plugins['telemetry'] = {'status': 'warning', 'running': True, 'message': '1 of 2 devices fail'}
    r.tick()
    r.tick()
    assert said == ['[telemetry] now warning: 1 of 2 devices fail']
    assert logs.level_of(said[0]) == logging.WARNING
    plugins['telemetry'] = {'status': 'ok', 'running': True, 'message': 'all heard'}
    r.tick()
    assert said[-1] == '[telemetry] now ok: all heard' and logs.level_of(said[-1]) == logging.INFO


@pytest.mark.parametrize('value, every', [(None, 300), ('0', 0), ('60', 60), ('x', 300)])
def test_summary_interval(value, every):
    assert logs.summary_every({'FLOORBIT_AGENT_LOG_SUMMARY': value} if value is not None else {}) == every
