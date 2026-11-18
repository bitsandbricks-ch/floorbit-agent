"""Plugin settings come from the hosted Floorbit only (Floorbit's docs/agent.md, "Settings come
from Floorbit").

The agent used to read them from its own environment too: ``LIBRENMS_URL``, ``NETBOX_URL``,
``FLOORBIT_PLUGIN_<ID>_<KEY>``, ``FLOORBIT_AGENT_PLUGINS`` and its relays
(``FLOORBIT_AGENT_SYSLOG_PORT``, ...). Now it ignores them, and so an event keeps running after the
update, it sends what they set once (``POST /api/agent/v1/env-settings``). The hosted Floorbit takes
it over for each plugin nobody set up there yet and says which; the agent remembers what it sent
(a hash, never the values) and sends again only when the environment changes.
"""
import hashlib
import json
import os

# The relays the agent ran itself before they were plugins: variable -> the plugin's setting.
OLD_RELAYS = {
    'FLOORBIT_AGENT_SYSLOG_PORT': 'plugin.syslog-relay.port',
    'FLOORBIT_AGENT_SYSLOG_BIND': 'plugin.syslog-relay.bind',
    'FLOORBIT_AGENT_FLOWS_PORT': 'plugin.netflow-relay.port',
    'FLOORBIT_AGENT_FLOWS_BIND': 'plugin.netflow-relay.bind',
    'FLOORBIT_AGENT_CAPTIVE': 'plugin.captive-note.address',
    'FLOORBIT_AGENT_CAPTIVE_PORTAL_URL': 'plugin.captive-note.portal_url',
    'FLOORBIT_AGENT_CAPTIVE_PROXIES': 'plugin.captive-note.proxies',
    'FLOORBIT_AGENT_FRONT_DOOR': 'plugin.front-door.address',
    'FLOORBIT_AGENT_FRONT_DOOR_PROXIES': 'plugin.front-door.proxies',
}
TURNED_ON = 'FLOORBIT_AGENT_PLUGINS'
FILE = 'env-settings-sent.json'


def plugin_keys(registry):
    """Every settings key a plugin in this image has: its own and a built-in's core section."""
    keys = set()
    for spec in registry.specs.values():
        keys.update(registry._core_fields(spec))
        keys.update(f"plugin.{spec.id}.{f['key']}" for f in spec.items('settings'))
    return keys


def collect(registry, env=None):
    """``(settings, plugins, names)``: what the environment set for plugins, as the hosted side's
    keys, the plugins it turned on, and the variable names (for the log; never the values)."""
    from floorbit.settings import FIELDS_BY_KEY
    env = os.environ if env is None else env
    settings, names = {}, []
    for key in sorted(plugin_keys(registry)):
        name = (FIELDS_BY_KEY.get(key) or {}).get('env')
        if name and env.get(name, '') != '':
            settings[key] = env[name]
            names.append(name)
    for name, key in OLD_RELAYS.items():
        value = env.get(name, '').strip()
        if value and value.lower() != 'off':
            settings[key] = int(value) if value.isdigit() else value
            names.append(name)
    plugins = sorted({p.strip() for p in env.get(TURNED_ON, '').split(',') if p.strip()})
    if plugins:
        names.append(TURNED_ON)
    return settings, plugins, sorted(names)


def ignore(registry):
    """From now on only the hosted Floorbit sets the plugins (SettingsStore.ignore_env)."""
    registry.settings_store.ignore_env(plugin_keys(registry))


class Sender:
    """Sends what the environment set once, after the agent got through to the hosted Floorbit."""

    def __init__(self, registry, state_root, env=None, log=print):
        self.settings, self.plugins, self.names = collect(registry, env)
        self.path = os.path.join(state_root, FILE)
        self.log = log
        self.done = not self.names or self._sent() == self._digest()
        self._told = False

    def _digest(self):
        raw = json.dumps([self.settings, self.plugins], sort_keys=True, default=str).encode()
        return hashlib.sha256(raw).hexdigest()

    def _sent(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                return json.load(f).get('sha256')
        except (OSError, ValueError, AttributeError):
            return None

    def send(self, agent):
        """Once; again after a failure, at the next connect. Never raises."""
        if self.done:
            return
        try:
            resp = agent._post('/api/agent/v1/env-settings', {'settings': self.settings, 'plugins': self.plugins})
            if resp.status_code == 404:
                raise RuntimeError('the hosted Floorbit is older than this agent: update Floorbit first')
            body = agent._check(resp) or {}
        except Exception as exc:  # noqa: BLE001  # the next connect tries again
            if not self._told:
                self.log(f'[agent] could not hand {", ".join(self.names)} to the hosted Floorbit: {exc}. '
                         'These variables are no longer read; set the plugins up in Settings > Plugins there.')
                self._told = True
            return
        self.done = True
        try:
            with open(self.path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump({'sha256': self._digest()}, f)
            os.replace(self.path + '.tmp', self.path)
        except OSError as exc:
            self.log(f'[agent] cannot note that the environment was handed over ({exc}): '
                     'it is sent again at the next start')
        taken = body.get('imported') or []
        kept = body.get('skipped') or {}
        if taken:
            self.log(f'[agent] the hosted Floorbit took over {", ".join(taken)} from this agent\'s environment')
        for pid, why in sorted(kept.items()):
            self.log(f'[agent] {pid} from this agent\'s environment was not taken over: {why}')
        self.log(f'[agent] {", ".join(self.names)} are no longer read: remove them from the agent\'s '
                 'environment (Settings > Plugins on the hosted Floorbit decides)')
