"""VRRP mode (issue #3): settings, the keepalived config, and the agent running keepalived as its
child. A fake keepalived stands in (the real one needs NET_ADMIN); scripts/vrrp_check.py runs the
real one between two network namespaces."""
import os
import sys
import textwrap
import time

import pytest

import floorbit_agent as agent_mod
from floorbit_agent import vrrp
from floorbit_agent.metrics import Metrics

ENV = {'FLOORBIT_AGENT_VRRP_VIPS': '192.0.2.10/24, 192.0.2.11/24', 'FLOORBIT_AGENT_VRRP_INTERFACE': 'eth0',
       'FLOORBIT_AGENT_VRRP_PREFER': '2'}


def test_off_unless_vips_are_set():
    assert vrrp.settings_from({}) is None
    assert vrrp.settings_from({'FLOORBIT_AGENT_VRRP_INTERFACE': 'eth0'}) is None


def test_settings():
    s = vrrp.settings_from({**ENV, 'FLOORBIT_AGENT_VRRP_PEERS': '192.0.2.2 192.0.2.3'})
    assert [str(v) for v in s.vips] == ['192.0.2.10/24', '192.0.2.11/24']
    assert s.interface == 'eth0' and s.router_id == 51 and s.prefer == {2}
    assert [str(p) for p in s.peers] == ['192.0.2.2', '192.0.2.3']
    one = vrrp.settings_from({**ENV, 'FLOORBIT_AGENT_VRRP_VIPS': '192.0.2.10', 'FLOORBIT_AGENT_VRRP_PREFER': ''})
    assert str(one.vips[0]) == '192.0.2.10/32'


@pytest.mark.parametrize('change, says', [
    ({'FLOORBIT_AGENT_VRRP_VIPS': '192.0.2.300/24'}, 'FLOORBIT_AGENT_VRRP_VIPS'),
    ({'FLOORBIT_AGENT_VRRP_VIPS': '192.0.2.10/24,192.0.2.10/24'}, 'twice'),
    ({'FLOORBIT_AGENT_VRRP_INTERFACE': ''}, 'INTERFACE'),
    ({'FLOORBIT_AGENT_VRRP_INTERFACE': 'eth0 }\nglobal_defs {'}, 'INTERFACE'),   # no config injection
    ({'FLOORBIT_AGENT_VRRP_ID': '255'}, 'from 1 to 254'),
    ({'FLOORBIT_AGENT_VRRP_ID': 'x'}, 'number'),
    ({'FLOORBIT_AGENT_VRRP_PREFER': '3'}, 'only 2'),
    ({'FLOORBIT_AGENT_VRRP_PEERS': 'agent-b'}, 'PEERS'),
])
def test_wrong_settings_say_what_is_wrong(change, says):
    with pytest.raises(ValueError, match=says):
        vrrp.settings_from({**ENV, **change})


def test_the_config():
    conf = vrrp.render(vrrp.settings_from({**ENV, 'FLOORBIT_AGENT_VRRP_PEERS': '192.0.2.3'}), '/x/fifo', '/x/healthy')
    assert 'notify_fifo /x/fifo' in conf and 'file /x/healthy' in conf
    assert 'script' not in conf.replace('notify_fifo', '')          # no scripts: they would need root
    first, second = conf.split('vrrp_instance ')[1:]
    assert 'virtual_router_id 51' in first and 'priority 100' in first and '192.0.2.10/24' in first
    assert 'virtual_router_id 52' in second and 'priority 150' in second and '192.0.2.11/24' in second
    assert 'state BACKUP' in first and 'state BACKUP' in second
    assert 'unicast_peer {\n    192.0.2.3' in first
    assert conf.count('{') == conf.count('}')


def test_fifo_lines():
    assert vrrp.parse_fifo_line('INSTANCE "floorbit_1" MASTER 150\n') == ('floorbit_1', 'MASTER')
    assert vrrp.parse_fifo_line('GROUP "g" MASTER 150') is None
    assert vrrp.parse_fifo_line('INSTANCE "floorbit_1" SOMETHING 1') is None


FAKE = textwrap.dedent('''\
    """Stands in for keepalived: reads the config it is given, says BACKUP then MASTER for each
    instance on the FIFO, FAULT while the health file says 1, and BACKUP again when it says 0."""
    import re, sys, time, signal
    conf = open(sys.argv[sys.argv.index('--use-file') + 1]).read()
    fifo = re.search(r'notify_fifo (\\S+)', conf).group(1)
    health = re.search(r'\\n  file (\\S+)', conf).group(1)
    names = re.findall(r'vrrp_instance (\\S+)', conf)
    out = open(fifo, 'w', buffering=1)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    for state in ('BACKUP', 'MASTER'):
        for n in names:
            out.write(f'INSTANCE "{n}" {state} 150\\n')
    print('Keepalived (fake) started', flush=True)
    last = '0'
    while True:
        now = open(health).read().strip()
        if now != last:
            for n in names:
                out.write(f'INSTANCE "{n}" {"FAULT" if now == "1" else "MASTER"} 150\\n')
            last = now
        time.sleep(0.05)
    ''')


@pytest.fixture
def fake_keepalived(tmp_path):
    path = tmp_path / 'keepalived'
    path.write_text(f'#!{sys.executable}\n' + FAKE)
    path.chmod(0o755)
    return str(path)


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_the_agent_runs_keepalived_and_follows_its_state(fake_keepalived, monkeypatch):
    monkeypatch.setattr(vrrp, 'CHECK_EVERY', 0.1)
    healthy = [True]
    logged, m = [], Metrics()
    k = vrrp.Keepalived(vrrp.settings_from(ENV), m, log=logged.append, binary=fake_keepalived,
                        healthy=lambda: healthy[0])
    k.start()
    try:
        assert vrrp.wait_for_state(k, 'MASTER')
        assert k.holds() == ['192.0.2.10', '192.0.2.11']
        assert 'floorbit_agent_vrrp_master{vip="192.0.2.10"} 1' in m.render()
        assert 'floorbit_agent_vrrp_running 1' in m.render()
        healthy[0] = False                                   # the agent stops answering: give them away
        assert vrrp.wait_for_state(k, 'FAULT')
        assert any('giving the VIPs away' in line for line in logged)
        healthy[0] = True
        assert vrrp.wait_for_state(k, 'MASTER')
    finally:
        k.stop()
    assert k.proc.poll() is not None and not os.path.exists(k.dir)


def test_a_broken_health_check_counts_as_not_healthy(fake_keepalived, monkeypatch):
    monkeypatch.setattr(vrrp, 'CHECK_EVERY', 0.1)

    def broken():
        raise OSError('nothing listens')
    k = vrrp.Keepalived(vrrp.settings_from(ENV), Metrics(), log=lambda *_: None, binary=fake_keepalived,
                        healthy=broken)
    k.start()
    try:
        assert vrrp.wait_for_state(k, 'FAULT')
    finally:
        k.stop()


def test_keepalived_that_dies_is_noticed(fake_keepalived, monkeypatch):
    monkeypatch.setattr(vrrp, 'CHECK_EVERY', 0.1)
    logged, m = [], Metrics()
    k = vrrp.Keepalived(vrrp.settings_from(ENV), m, log=logged.append, binary=fake_keepalived)
    k.start()
    try:
        assert vrrp.wait_for_state(k, 'MASTER')
        k.proc.kill()
        assert wait_for(lambda: any('keepalived stopped' in line for line in logged))
        assert k.holds() == [] and 'floorbit_agent_vrrp_running 0' in m.render()
    finally:
        k.stop()


def test_start_vrrp_off_wrong_or_missing():
    a = agent_mod.Agent('https://floor.example.com', 'fba_x_y', lambda m, args: [], log=lambda *_: None)
    said = []
    assert agent_mod.start_vrrp(a, {}, out=said.append) is None and not said
    assert agent_mod.start_vrrp(a, {**ENV, 'FLOORBIT_AGENT_VRRP_INTERFACE': ''}, out=said.append) is False
    assert 'INTERFACE' in said[-1]
    assert agent_mod.start_vrrp(a, {**ENV, 'FLOORBIT_AGENT_KEEPALIVED': '/nonexistent/keepalived'},
                                out=said.append) is not None   # env of the process decides the binary
    a.stop()


def test_local_check_needs_no_hosted_floorbit():
    """The VIP stays while the uplink is away: the check asks only the agent's own /healthz."""
    from floorbit_agent import metrics as metrics_mod
    a = agent_mod.Agent('https://floor.example.com', 'fba_x_y', lambda m, args: [], log=lambda *_: None)
    server = metrics_mod.serve(a.metrics, a, '127.0.0.1:0', 120, None, log=lambda *_: None)
    try:
        port = server.server_address[1]
        assert vrrp.local_check(f'0.0.0.0:{port}')() is True     # 503 (never reached the host) is alive
    finally:
        server.shutdown()
    with pytest.raises(OSError):
        vrrp.local_check(f'127.0.0.1:{port}', timeout=0.5)()
