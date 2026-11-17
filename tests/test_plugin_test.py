"""plugin_test: the hosted Settings > Plugins > Test button, run on the agent for LAN plugins."""
import pytest

import floorbit_agent


class Spec:
    id, name = 'librenms', 'LibreNMS'


class Registry:
    def __init__(self, on=True, answer='LibreNMS answers: 42 devices'):
        self.on, self.answer, self.called = on, answer, []

    def active(self, plugin_id):
        return Spec() if self.on and plugin_id == 'librenms' else None

    def call(self, spec, method):
        self.called.append((spec.id, method))
        return self.answer


def test_the_test_runs_here():
    reg = Registry()
    assert floorbit_agent.plugin_test(reg, ['librenms']) == 'LibreNMS answers: 42 devices'
    assert reg.called == [('librenms', 'test')]


def test_a_plugin_not_on_here_says_so():
    from floorbit.plugins.base import PluginError
    with pytest.raises(PluginError, match='not on on the agent'):
        floorbit_agent.plugin_test(Registry(on=False), ['librenms'])
    with pytest.raises(PluginError):
        floorbit_agent.plugin_test(Registry(), [])
