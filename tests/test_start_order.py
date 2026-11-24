"""How the agent starts: reconnects spread out, and a wrong VRRP setting stops it before any plugin
binds a port."""
import pytest

import floorbit_agent
from floorbit_agent import plugin_host


def test_backoff_is_spread_by_a_fifth_either_way():
    assert floorbit_agent.jittered(10, rand=lambda: 0.0) == pytest.approx(8)
    assert floorbit_agent.jittered(10, rand=lambda: 1.0) == pytest.approx(12)
    waits = {round(floorbit_agent.jittered(30), 3) for _ in range(20)}
    assert len(waits) > 1 and all(24 <= w <= 36 for w in waits)


def test_a_wrong_vrrp_setting_stops_the_agent_before_the_plugins_start(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(plugin_host.PluginHost, 'start', lambda self: started.append(self))
    for name, value in {'FLOORBIT_URL': 'https://floorbit.example.com',
                        'FLOORBIT_AGENT_KEY': 'fba_10ca1000_example-key-for-tests-only',
                        'FLOORBIT_AGENT_KEY_FILE': '', 'FLOORBIT_AGENT_INSECURE': '0',
                        'FLOORBIT_AGENT_STATE': str(tmp_path),
                        'FLOORBIT_DATA_FILE': str(tmp_path / 'agent-data.json'),
                        'FLOORBIT_AGENT_METRICS': 'off',
                        'FLOORBIT_AGENT_VRRP_VIPS': '192.0.2.10/24',
                        'FLOORBIT_AGENT_VRRP_INTERFACE': ''}.items():
        monkeypatch.setenv(name, value)
    assert floorbit_agent.main(['run']) == 2
    assert started == []
