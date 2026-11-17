"""The local agent for a hosted Floorbit (issue #2). See Floorbit's docs/agent.md.

It runs on the event network, in the same image as Floorbit (``python -m floorbit_agent``), with the
event's own monitoring plugin set up the usual way (``LIBRENMS_URL`` and ``LIBRENMS_TOKEN``, or any
other monitoring plugin). It connects out to the hosted Floorbit (``FLOORBIT_URL``) with its key
(``FLOORBIT_AGENT_KEY``), takes calls, runs each one against the local plugin and sends the answer
back. Calls come over a WebSocket link (protocol 2, ``wss://``) when the hosted Floorbit offers
one, and over an HTTPS long poll when it doesn't or the link can't be opened. The hosted side is
floorbit/agent_link.py in bitsandbricks-ch/floorbit.

Nothing listens here unless asked: the agent makes outgoing HTTPS requests, and only with
``FLOORBIT_AGENT_SYSLOG_PORT`` or ``FLOORBIT_AGENT_FLOWS_PORT`` does it take syslog or flows from
the event network (syslog_relay.py, flows_relay.py), and only with ``FLOORBIT_AGENT_CAPTIVE`` does it
answer the Captive Portal API (captive.py).
When the hosted Floorbit can't be reached it waits and tries again, a little longer each time (up to ``MAX_BACKOFF``).
"""
import contextlib
import gzip
import io
import json
import os
import pathlib
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

KEY_HEADER = 'Floorbit-Agent-Key'
VERSION_HEADER = 'Floorbit-Agent-Version'
PROTOCOL_HEADER = 'Floorbit-Agent-Protocol'
PROTOCOL = 2              # the newest agent protocol this agent speaks; the hosted side says which it does
LINK_IDLE = 60            # the hosted side sends something at least every 20 s: quiet this long, it's gone
LINK_RETRY = 300          # after the link failed, long-poll this long before trying it again
LINK_STEADY = 30          # a link that drops sooner than this counts as failed
LINK_CONNECT = 15         # seconds to open the link; simple-websocket has no connect timeout of its own
POLL_WAIT = 25            # the hosted side holds a poll at most this long
HTTP_TIMEOUT = 40         # a bit more than a poll
MAX_BACKOFF = 60
GZIP_FROM = 2048          # bytes: smaller bodies aren't worth packing
BEAT_EVERY = 10           # seconds between heartbeat writes
HEALTHY_FOR = 120         # `health`: the last poll that got through is at most this old
WORKERS = 3               # calls of one kind run side by side, each kind in its own pool: a hung
                          # LibreNMS lookup holds up only more of the same, never the rest
REPORT_EVERY = 30         # seconds between reports to the hosted side (its Local agents page)
MAX_WAITING = 200         # calls of one kind waiting: more get a "busy" answer instead of piling up
METHODS = ('configured', 'devices', 'device_details', 'resolve_ip', 'port_stats', 'device_ports', 'arp',
           'port_clients', 'inventory_feeds', 'cable_feeds', 'firewall_feeds', 'health_feeds', 'dns_feeds',
           'wifi_feeds', 'game_query', 'game_rcon', 'neighbours', 'dns_probe', 'tcp_probe', 'plugin', 'plugins_reload',
           'switch_call', 'shell_open', 'shell_io', 'shell_close')
# Pools bigger than WORKERS: a switch command runs on many switches at once, and each open terminal
# keeps one shell_io read waiting on the switch.
POOL_SIZES = {'switch_call': 8, 'shell_open': 4, 'shell_io': 24}


def _within(seconds, open_it, late=None):
    """open_it(), or TimeoutError after ``seconds``: a firewall that drops the packets would
    otherwise hold the agent for the system's TCP timeout, minutes, before it falls back.
    What opens after the timeout goes to ``late`` (to be closed): a link nobody reads would take
    the hosted side's calls and leave them unanswered."""
    done, out, lock = threading.Event(), {}, threading.Lock()

    def run():
        try:
            value = open_it()
        except BaseException as exc:  # noqa: BLE001  # handed to the caller below
            out['error'] = exc
        else:
            with lock:
                if out.get('gave_up'):
                    if late is not None:
                        late(value)
                    return
                out['value'] = value
        done.set()

    threading.Thread(target=run, name='agent-link-open', daemon=True).start()
    if not done.wait(seconds):
        with lock:
            if 'value' not in out and 'error' not in out:
                out['gave_up'] = True
                raise TimeoutError(f'the link did not open within {seconds} s')
    if 'error' in out:
        raise out['error']
    return out['value']


class Refused(RuntimeError):
    """The hosted Floorbit refused the key: retrying won't help."""


def _unmapped(ip):
    """::ffff:127.0.0.1 reaches 127.0.0.1, but not every Python release calls it loopback (or
    ::ffff:169.254.0.1 link-local): check the IPv4 address it stands for."""
    return getattr(ip, 'ipv4_mapped', None) or ip


def _inside(ip):
    """The IPv4 address that NAT64 (64:ff9b::/96, 64:ff9b:1::/48) or 6to4 (2002::/16) carries in ``ip``,
    else None. Python calls 64:ff9b:1::a9fe:a9fe private, yet on a NAT64 network it is 169.254.169.254."""
    import ipaddress
    if ip.version != 6:
        return None
    if ip.sixtofour:
        return ip.sixtofour
    if ip in ipaddress.ip_network('64:ff9b::/96') or ip in ipaddress.ip_network('64:ff9b:1::/48'):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _never(ip):
    """This machine, link-local, unspecified or multicast, also when carried inside IPv6."""
    return any(i is not None and (i.is_loopback or i.is_link_local or i.is_unspecified or i.is_multicast)
               for i in (ip, _inside(ip)))


def game_target(host, env=None):
    """The address to ask a game server at, if it may be asked. The hosted side names the host, so
    the agent only reaches private addresses (or FLOORBIT_AGENT_GAME_NETS): a hosted Floorbit that
    was taken over can't use the agent to reach the internet or this machine's own ports."""
    return _private_target(host, 'FLOORBIT_AGENT_GAME_NETS', 'ask game servers on', env)


def switch_target(host, env=None):
    """The address to open a switch login to (switches.py): private addresses only, or
    FLOORBIT_AGENT_SWITCH_NETS, for the same reason as game servers."""
    return _private_target(host, 'FLOORBIT_AGENT_SWITCH_NETS', 'log in to switches on', env)


def _private_target(host, nets_var, what, env=None):
    import ipaddress
    import socket

    from floorbit.plugins.base import PluginError
    env = os.environ if env is None else env
    nets = [ipaddress.ip_network(n.strip(), strict=False)
            for n in (env.get(nets_var) or '').split(',') if n.strip()]
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(str(host), None, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError):
        raise PluginError(f'The agent cannot resolve {host}.') from None
    for addr in sorted(addrs):
        ip = _unmapped(ipaddress.ip_address(addr.split('%')[0]))
        if _never(ip):
            continue
        inside = _inside(ip)
        if any(ip in net for net in nets) if nets else (ip.is_private and (inside is None or inside.is_private)):
            return str(ip)
    raise PluginError(f'{host} is not on a network the agent may {what} '
                      f'(private addresses, or {nets_var}).')


def game_call(method, args, env=None):
    """A game server query or RCON command (floorbit/game_query.py), from the event network."""
    from floorbit import game_query
    from floorbit.plugins.base import PluginError
    if method == 'game_query':
        protocol, host, port = args
        target = game_target(host, env)
        try:
            return game_query.query(protocol, target, int(port))
        except game_query.QueryError as exc:
            raise PluginError(str(exc)) from None
    host, port, password, command = args
    target = game_target(host, env)
    try:
        return game_query.rcon(target, int(port), password, command)
    except game_query.QueryError as exc:
        raise PluginError(str(exc)) from None


def probe_target(host, env=None):
    """The address to probe, if it may be probed. Unlike game servers, internet lines and resolvers
    are often public, so any address goes except this machine's own and link-local ones (or only
    FLOORBIT_AGENT_PROBE_NETS when set). A probe only connects to 53, 80, 443 or 853 and sends
    nothing, or sends one DNS query, so it can't be used to talk to a service."""
    import ipaddress
    import socket

    from floorbit.plugins.base import PluginError
    env = os.environ if env is None else env
    nets = [ipaddress.ip_network(n.strip(), strict=False)
            for n in (env.get('FLOORBIT_AGENT_PROBE_NETS') or '').split(',') if n.strip()]
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(str(host), None, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError):
        raise PluginError(f'The agent cannot resolve {host}.') from None
    for addr in sorted(addrs):
        ip = _unmapped(ipaddress.ip_address(addr.split('%')[0]))
        if _never(ip):
            continue
        if not nets or any(ip in net for net in nets):
            return str(ip)
    raise PluginError(f'{host} is not an address the agent may probe.')


def probe_call(method, args, env=None):
    """A DNS or TCP probe for the services view (floorbit/probes.py), from the event network."""
    from floorbit import probes
    from floorbit.plugins.base import PluginError
    try:
        if method == 'dns_probe':
            server, name = args
            return probes.dns(probe_target(server, env), str(name))
        host, port, count = args
        if int(port) not in probes.PROBE_PORTS:
            raise PluginError(f'The agent only probes ports {", ".join(map(str, probes.PROBE_PORTS))}.')
        return probes.tcp(probe_target(host, env), int(port), min(int(count), probes.MAX_COUNT))
    except probes.ProbeError as exc:
        raise PluginError(str(exc)) from None
    except (TypeError, ValueError):
        raise PluginError('The probe was asked for wrongly.') from None


def local_executor(app, plugins_ref=None):
    """Run a call against this machine's monitoring plugin, as the hosted Floorbit would locally.
    ``plugins_ref["host"]``: the PluginHost, for the ``plugin`` calls."""
    plugins_ref = {} if plugins_ref is None else plugins_ref

    def execute(method, args):
        from floorbit import sources
        from floorbit.plugins.base import PluginError

        from . import switches
        if method not in METHODS:
            raise PluginError(f'{method} is not something the agent answers')
        if method in ('game_query', 'game_rcon'):
            return game_call(method, args)
        if method in ('dns_probe', 'tcp_probe'):
            return probe_call(method, args)
        if method == 'shell_io':          # no app context: it only reads and writes an open shell
            return switches.shells.io(args)
        if method == 'shell_close':
            return switches.shells.close(args)
        if method in ('plugin', 'plugins_reload'):
            host = plugins_ref.get('host')
            if host is None:
                raise PluginError('This agent runs no plugins yet.')
            return host.call(args) if method == 'plugin' else host.reload()
        with app.app_context():
            registry = sources.registry
            if method == 'inventory_feeds':
                return inventory_feeds(registry)
            if method == 'cable_feeds':
                return cable_feeds(registry)
            if method == 'firewall_feeds':
                return firewall_feeds(registry)
            if method == 'wifi_feeds':
                return wifi_feeds(registry)
            if method == 'dns_feeds':
                return dns_feeds(registry, *args)
            if method == 'switch_call':
                return switches.call(registry, args)
            if method == 'shell_open':
                return switches.shells.open(registry, args)
            if method == 'health_feeds':
                return _feeds(registry, registry.feeders('health') if registry is not None else [], 'health_checks')
            spec = registry.monitoring_source() if registry is not None else None
            if spec is None:
                raise PluginError('No monitoring plugin is set up on the agent (LIBRENMS_URL and LIBRENMS_TOKEN?)')
            return registry.call(spec, method, *args)
    return execute


def inventory_feeds(registry):
    """Every inventory plugin that's on here (NetBox), each with its devices or its error."""
    return _feeds(registry, registry.feeders('inventory') if registry is not None else [], 'inventory')


_DNS_KEY = os.urandom(32)   # lives in memory only: client hashes can't be turned back into addresses


def dns_feeds(registry, since=0):
    """The lookups of every DNS plugin here since ``since``, made safe to send first: the client is
    a keyed hash (its address never leaves the event network) and the name is cut to the site, or
    left out for local names (a machine's name says whose it is). See docs/dns.md."""
    import hashlib
    import hmac

    from floorbit import dns
    from floorbit.plugins.base import PluginError
    out = []
    for spec in registry.feeders('dns') if registry is not None else []:
        try:
            raw = registry.call(spec, 'dns_queries', float(since or 0))
        except PluginError as exc:
            out.append({'plugin': spec.id, 'name': spec.name, 'error': str(exc)[:300]})
            continue
        items = []
        for e in (raw if isinstance(raw, list) else [])[:dns.MAX_PER_POLL]:
            if not isinstance(e, dict):
                continue
            client = e.get('client')
            items.append({'ts': e.get('ts'), 'domain': dns.site(e.get('domain')), 'blocked': bool(e.get('blocked')),
                          'client': hmac.new(_DNS_KEY, str(client).encode(), hashlib.sha256).hexdigest()[:24]
                          if client else None})
        out.append({'plugin': spec.id, 'name': spec.name, 'items': items})
    return out


def wifi_feeds(registry):
    """Every Wi-Fi plugin's access points, cleaned here already: counts only, so client names and
    MACs a controller sends never leave the event network (docs/wifi.md)."""
    from floorbit import wifi
    out = []
    for feed in _feeds(registry, registry.feeders('wifi') if registry is not None else [], 'wifi_aps'):
        if 'items' in feed:
            feed['items'] = [ap for ap in (wifi.clean_ap(raw) for raw in feed['items'] or []) if ap][:wifi.MAX_APS]
        out.append(feed)
    return out


def cable_feeds(registry):
    """Every plugin that's on here and knows cables (NetBox), each with its cables or its error."""
    from floorbit import cables
    return _feeds(registry, cables.sources(registry) if registry is not None else [], 'cables')


def firewall_feeds(registry):
    """Every plugin here that lists firewall ranges (NetBox prefixes), each with its ranges or its error."""
    from floorbit import firewalls
    return _feeds(registry, firewalls.import_sources(registry) if registry is not None else [], 'firewall_ranges')


def _feeds(registry, specs, method):
    from floorbit.plugins.base import PluginError
    out = []
    for spec in specs:
        try:
            out.append({'plugin': spec.id, 'name': spec.name, 'items': registry.call(spec, method) or []})
        except PluginError as exc:
            out.append({'plugin': spec.id, 'name': spec.name, 'error': str(exc)[:300]})
    return out


class Agent:
    def __init__(self, url, key, execute, http=None, version='0', log=print):
        import requests
        self.url = url.rstrip('/')
        self.lan_url = os.environ.get('FLOORBIT_AGENT_LAN_URL', '').strip().rstrip('/')
        self.key = key
        self.execute = execute
        self.http = http or requests.Session()
        self.version = version
        self.log = log
        self._pools = {}          # method -> its own ThreadPoolExecutor
        self._waiting = {}        # method -> calls taken but not answered yet
        self._stop = threading.Event()
        self.answered = 0
        self.poll_wait = POLL_WAIT
        self.gzip = False         # once hello says the hosted Floorbit takes gzipped bodies
        self.protocol = 1         # what the headers say: 1 until hello shows the hosted side knows more
        self.link_path = None     # the WebSocket link, once hello offers one
        self.link_after = 0       # don't try the link again before this (time.monotonic())
        self.link_up = False
        self.link_opened = False
        self._ws = None
        self._ws_lock = threading.Lock()
        self.heartbeat = None     # a file touched after each poll that got through, for `health`
        self._beat_at = 0
        self._beat_failing = False
        self.last_contact = 0     # time.time() the hosted Floorbit last answered (/healthz)
        self._report_at = None    # time.monotonic() of the last report; None: send one soon
        self._report_https = True  # False once the hosted side says it has no report endpoint
        from .metrics import Metrics
        self.metrics = Metrics()
        self._running = 0
        self._running_lock = threading.Lock()
        self.metrics.collect(self._gauges)
        self.plugins = None       # plugin_host.PluginHost, once main() starts it

    def _gauges(self, m):
        m.set('floorbit_agent_info', 1, {'version': self.version, 'protocol': self.protocol})
        m.set('floorbit_agent_link_up', int(self.link_up))
        m.set('floorbit_agent_calls_running', self._running)
        if self.last_contact:
            m.set('floorbit_agent_last_contact_age_seconds', round(time.time() - self.last_contact, 1))

    def _headers(self, json_body=False):
        h = {KEY_HEADER: self.key, VERSION_HEADER: self.version, PROTOCOL_HEADER: str(self.protocol)}
        if json_body:
            h['Content-Type'] = 'application/json'
        return h

    def _check(self, resp):
        if resp.status_code == 401:
            raise Refused('The hosted Floorbit refused the agent key: it is wrong or was revoked. '
                          'Enrol the agent again.')
        if resp.status_code == 426:
            # Retrying won't help: one of the two has to be updated.
            raise Refused((resp.json() or {}).get('error') or 'The hosted Floorbit speaks another agent protocol.')
        resp.raise_for_status()
        return resp.json()

    def _hosted(self, kind, request):
        """One request to the hosted Floorbit, timed and counted for /metrics."""
        try:
            with self.metrics.timed('floorbit_agent_hosted_request_seconds', {'kind': kind}):
                body = self._check(request())
        except Exception:
            self.metrics.inc('floorbit_agent_hosted_errors_total', {'kind': kind})
            raise
        self.last_contact = time.time()
        return body

    def hello(self):
        body = self._hosted('hello', lambda: self.http.get(f'{self.url}/api/agent/v1/hello',
                                                          headers=self._headers(), timeout=HTTP_TIMEOUT))
        self.gzip = 'gzip' in (body.get('encodings') or [])
        # A hosted Floorbit of protocol 1 says nothing of "protocols": stay on 1 and the long poll.
        known = {p for p in body.get('protocols') or [body.get('protocol') or 1] if isinstance(p, int)}
        self.protocol = max(known & set(range(1, PROTOCOL + 1)), default=1)
        link = body.get('link')
        self.link_path = link if self.protocol >= 2 and isinstance(link, str) and link.startswith('/') else None
        return body

    def link(self):
        """Take calls over the WebSocket link until it closes. Raises Refused for a bad key, and
        anything else when it can't be opened or drops (the caller falls back to the long poll)."""
        import simple_websocket
        url = 'ws' + self.url[len('http'):] + self.link_path     # https:// -> wss://
        try:
            ws = _within(LINK_CONNECT, lambda: simple_websocket.Client.connect(
                url, headers=self._headers(), ping_interval=25, max_message_size=32 * 1024 * 1024),
                late=lambda late_ws: late_ws.close())
        except simple_websocket.ConnectionError as exc:
            if getattr(exc, 'status_code', None) in (401, 426):
                raise Refused('The hosted Floorbit refused the agent key: it is wrong or was revoked. '
                              'Enrol the agent again.') from exc
            raise
        self._ws, self.link_up, self.link_opened = ws, True, True
        self.log('[agent] live over the WebSocket link')
        try:
            while not self._stop.is_set():
                raw = ws.receive(timeout=LINK_IDLE)
                if raw is None:
                    raise TimeoutError(f'the link was quiet for {LINK_IDLE} s')
                self.beat()
                self.last_contact = time.time()
                message = json.loads(raw)
                if message.get('type') == 'calls':
                    for call in message.get('calls') or []:
                        self.take(call)
                elif message.get('type') == 'ping':
                    self._link_send({'type': 'ping'})
                if self._report_due():
                    self._link_send({'type': 'report', 'report': self.report()})
        finally:
            self._ws, self.link_up = None, False
            with contextlib.suppress(Exception):
                ws.close()

    def _link_send(self, message):
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._ws_lock:       # answers come from several worker threads
                ws.send(json.dumps(message, default=str))
            return True
        except Exception:  # noqa: BLE001  # the link dropped: the caller sends it over HTTPS
            return False

    def report(self):
        """The agent's own numbers for the hosted Local agents page: a summary of /metrics."""
        m = self.metrics
        m.refresh()
        return {
            'up_seconds': round(time.time() - m.started),
            'reconnects': m.total('floorbit_agent_reconnects_total'),
            'link_drops': m.total('floorbit_agent_link_drops_total'),
            'calls_busy': m.total('floorbit_agent_calls_total', outcome='busy'),
            'answers_failed': m.total('floorbit_agent_answers_total', way='failed'),
            'syslog_received': m.get('floorbit_agent_syslog_received_total'),
            'syslog_sent': m.get('floorbit_agent_syslog_sent_total'),
            'syslog_dropped': m.get('floorbit_agent_syslog_dropped_total'),
            'syslog_backlog': m.get('floorbit_agent_syslog_backlog'),
            'flows_packets': m.get('floorbit_agent_flows_packets_total'),
            'front_door_passed': m.total('floorbit_agent_front_door_requests_total', outcome='passed'),
            'front_door_failed': m.total('floorbit_agent_front_door_requests_total', outcome='failed'),
            # Where laptops in the hall reach this agent, for Floorbit's seat walk-through.
            **({'lan_url': self.lan_url} if self.lan_url else {}),
            **({'plugins': self.plugins.report()} if self.plugins is not None else {}),
            **({'updates': self.plugins.updater.report()}
               if getattr(self.plugins, 'updater', None) is not None else {}),
            **({'vrrp': {'primary': self.plugins.primary(), 'holds': self.plugins.vrrp.holds()}}
               if getattr(self.plugins, 'vrrp', None) else {}),
        }

    def _report_due(self, now=None):
        now = time.monotonic() if now is None else now
        if self._report_at is not None and now - self._report_at < REPORT_EVERY:
            return False
        self._report_at = now
        return True

    def report_over_https(self):
        """On the long poll the report is a POST of its own. A hosted Floorbit from before reports
        answers 404: stop asking it. Never fatal: the report is only for show."""
        if not self._report_https or not self._report_due():
            return
        try:
            resp = self._post('/api/agent/v1/report', {'report': self.report()})
            if resp.status_code == 404:
                self._report_https = False
        except Exception:  # noqa: BLE001  # the next poll says whether the hosted side is away
            pass

    def _post(self, path, payload):
        """POST JSON to the hosted Floorbit, gzipped when it takes that and the body is worth it."""
        # default=str: a plugin answer may hold dates or other things JSON doesn't know.
        data = json.dumps(payload, default=str).encode()
        headers = self._headers(True)
        if self.gzip and len(data) > GZIP_FROM:
            data = gzip.compress(data, compresslevel=6)
            headers['Content-Encoding'] = 'gzip'
        return self.http.post(f'{self.url}{path}', data=data, headers=headers, timeout=HTTP_TIMEOUT)

    def poll(self, wait=POLL_WAIT):
        """One long poll; runs the calls it brings. Returns how many it got."""
        body = self._check(self.http.get(f'{self.url}/api/agent/v1/calls', params={'wait': wait},
                                         headers=self._headers(), timeout=HTTP_TIMEOUT))
        self.last_contact = time.time()   # a long poll's round trip is its wait: not timed
        calls = body.get('calls') or []
        for call in calls:
            self.take(call)
        return len(calls)

    def take(self, call):
        """Queue one call in its method's pool, or answer "busy" when too many of that kind wait."""
        method = call.get('method') if call.get('method') in METHODS else 'other'
        with self._running_lock:
            if self._waiting.get(method, 0) >= MAX_WAITING:
                busy = True
            else:
                busy = False
                self._waiting[method] = self._waiting.get(method, 0) + 1
                pool = self._pools.get(method)
                if pool is None:
                    pool = self._pools[method] = ThreadPoolExecutor(POOL_SIZES.get(method, WORKERS),
                                                                    thread_name_prefix=f'agent-{method}')
        if busy:
            self.metrics.inc('floorbit_agent_calls_total', {'method': method, 'outcome': 'busy'})
            self._answer(call, {'id': call.get('id'), 'ok': False,
                                'error': f'The agent is busy: {MAX_WAITING} {method} calls are still waiting.'})
            return
        pool.submit(self._run, call)

    def _run(self, call):
        # Only known method names become a label: the hosted side can't blow up /metrics.
        method = call.get('method') if call.get('method') in METHODS else 'other'
        try:
            self._run_one(call, method)
        finally:
            with self._running_lock:
                self._waiting[method] = max(0, self._waiting.get(method, 0) - 1)

    def _run_one(self, call, method):
        with self._running_lock:
            self._running += 1
        try:
            with self.metrics.timed('floorbit_agent_call_seconds', {'method': method}):
                result = self.execute(call['method'], call.get('args') or [])
            answer = {'id': call['id'], 'ok': True, 'result': result}
        except Exception as exc:  # noqa: BLE001  # the hosted side gets the message, the agent goes on
            answer = {'id': call['id'], 'ok': False, 'error': str(exc)[:300] or type(exc).__name__}
        finally:
            with self._running_lock:
                self._running -= 1
        self.metrics.inc('floorbit_agent_calls_total', {'method': method, 'outcome': 'ok' if answer['ok'] else 'error'})
        self._answer(call, answer)

    def _answer(self, call, answer):
        try:
            if self._link_send({'type': 'answers', 'answers': [answer]}):
                way = 'link'
            else:
                self._hosted('answers', lambda: self._post('/api/agent/v1/answers', {'answers': [answer]}))
                way = 'https'
            self.answered += 1
            self.metrics.inc('floorbit_agent_answers_total', {'way': way})
        except Exception as exc:  # noqa: BLE001  # the call times out on the hosted side; nothing to keep
            self.metrics.inc('floorbit_agent_answers_total', {'way': 'failed'})
            self.log(f'[agent] could not send the answer to {call["method"]}: {exc}')

    def send_syslog(self, lines, dropped=0):
        """One batch for the hosted syslog (syslog_relay.Relay calls this)."""
        from .syslog_relay import TurnedOff
        resp = self._post('/api/agent/v1/syslog', {'lines': lines, 'dropped': dropped})
        if resp.status_code == 409:
            raise TurnedOff((resp.json() or {}).get('error') or 'Syslog is turned off on the hosted Floorbit.')
        return self._check(resp)

    def _said(self, resp):
        """409: the hosted side has the feature off, and says so."""
        if resp.status_code == 409:
            raise RuntimeError((resp.json() or {}).get('error') or 'turned off on the hosted Floorbit')
        return self._check(resp)

    def flow_seats(self):
        return self._said(self.http.get(f'{self.url}/api/agent/v1/seats', headers=self._headers(),
                                        timeout=HTTP_TIMEOUT))

    def captive_state(self):
        """Which client IPs are done, for the Captive Portal API (captive.py). RuntimeError: check-in is off."""
        return self._said(self.http.get(f'{self.url}/api/agent/v1/captive', headers=self._headers(),
                                        timeout=HTTP_TIMEOUT))

    def seat_walk_hello(self, token, ip):
        """The crew laptop with this walk-through code is at ``ip`` (captive.py's hello)."""
        resp = self._post('/api/agent/v1/seat-walk', {'token': token, 'ip': ip})
        if resp.status_code == 404:
            raise RuntimeError((resp.json() or {}).get('error') or 'this Floorbit has no seat walk-through')
        return self._check(resp)

    def send_flows(self, snapshot):
        return self._said(self._post('/api/agent/v1/flows', {'snapshot': snapshot}))

    def beat(self, now=None):
        """The hosted Floorbit answered: say so in the heartbeat file (at most every few seconds)."""
        now = time.time() if now is None else now
        if not self.heartbeat or now - self._beat_at < BEAT_EVERY:
            return
        try:
            with open(self.heartbeat, 'w', encoding='utf-8') as f:
                f.write(f'{now:.0f}\n')
            self._beat_at = now
            if self._beat_failing:
                self.log(f'[agent] the heartbeat file {self.heartbeat} can be written again')
            self._beat_failing = False
        except OSError as exc:
            # A full disk can pass: keep trying, say it once.
            if not self._beat_failing:
                self.log(f'[agent] cannot write the heartbeat file {self.heartbeat}: {exc}')
            self._beat_failing = True
            self._beat_at = now      # and try again in BEAT_EVERY, not on every message

    def stop(self):
        self._stop.set()
        from .switches import shells
        shells.close_all()          # an SSH session must not outlive the agent
        for pool in list(self._pools.values()):
            pool.shutdown(wait=False, cancel_futures=True)
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()

    def _try_link(self):
        """True when the link was up and then dropped; False when it couldn't be opened, and the
        long poll takes over for LINK_RETRY seconds."""
        self.link_opened = False
        started = time.monotonic()
        try:
            self.link()
            return True
        except Refused:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._stop.is_set():
                return True
            if self.link_opened:
                self.metrics.inc('floorbit_agent_link_drops_total')
                self.log(f'[agent] the WebSocket link dropped ({exc}); connecting again')
                if time.monotonic() - started < LINK_STEADY:
                    # Something on the way cuts it right after the upgrade: don't spin, poll a while.
                    self.link_after = time.monotonic() + LINK_RETRY
                return True
            self.log(f'[agent] no WebSocket link ({exc}); using the HTTPS long poll, '
                     f'trying the link again in {LINK_RETRY // 60} minutes')
            self.link_after = time.monotonic() + LINK_RETRY
            return False

    def run(self):
        """Poll until stopped. Raises Refused when the key is no good."""
        backoff, connected = 1, False
        while not self._stop.is_set():
            try:
                if not connected:
                    hello = self.hello()
                    self.log(f'[agent] connected to {self.url} as "{hello["name"]}" '
                             f'(Floorbit {hello.get("server_version")})')
                    connected, backoff = True, 1
                    self.metrics.set('floorbit_agent_connected', 1)
                if self.link_path and time.monotonic() >= self.link_after and self._try_link():
                    connected = False     # it dropped: say hello again, then link or poll
                    continue
                self.poll(self.poll_wait)
                self.beat()
                self.report_over_https()
            except Refused:
                raise
            except Exception as exc:  # noqa: BLE001  # network trouble: wait and try again
                if connected:
                    self.metrics.inc('floorbit_agent_reconnects_total')
                    self.log(f'[agent] lost the connection to {self.url}: {exc}')
                else:
                    self.log(f'[agent] cannot reach {self.url} ({exc}); trying again in {backoff} s')
                self.metrics.set('floorbit_agent_connected', 0)
                connected = False
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)


def turn_on(app, names, out=print):
    """FLOORBIT_AGENT_PLUGINS=http-check,pihole: plugins the agent turns on at start, whatever the
    hosted Floorbit says. Otherwise the hosted Floorbit's plugin settings decide (LocalPlugins in
    plugin_host.py); FLOORBIT_PLUGIN_<ID>_<KEY> variables still win over its settings."""
    from floorbit import sources
    from floorbit.plugins.base import PluginError
    registry = sources.registry
    for name in [n.strip() for n in str(names or '').split(',') if n.strip()]:
        try:
            with app.app_context():
                registry.set_enabled(name, True)
        except (KeyError, PluginError) as exc:   # an unknown id, or a plugin with an error
            out(f'[agent] cannot turn on the plugin "{name}": {exc}')


def check(agent, execute, out=print):
    """``python -m floorbit_agent check``: try each part once and say what works. True when all do.

    For setting up at the event: it runs in a second and names the part that's wrong, instead of
    an agent that keeps retrying in the background."""
    ok = True

    def line(good, text):
        nonlocal ok
        ok = ok and good
        out(f"{'OK  ' if good else 'FAIL'} {text}")

    try:
        hello = agent.hello()
        line(True, f'{agent.url} accepts the key: this is "{hello["name"]}" (Floorbit {hello.get("server_version")})')
    except Refused:
        line(False, f'{agent.url} refused the key: it is wrong or was revoked. Enrol the agent again.')
    except Exception as exc:  # noqa: BLE001
        line(False, f'cannot reach {agent.url}: {exc}')
    try:
        devices = execute('devices', [])
        line(True, f'the monitoring plugin answers: {len(devices or [])} devices')
    except Exception as exc:  # noqa: BLE001
        hint = '' if os.environ.get('LIBRENMS_URL') else ' (LIBRENMS_URL and LIBRENMS_TOKEN are not set)'
        line(False, f'the monitoring plugin: {exc}{hint}')
    try:
        feeds = execute('inventory_feeds', [])
    except Exception as exc:  # noqa: BLE001
        feeds = []
        line(False, f'the inventory plugins: {exc}')
    if not feeds:
        out('-    no inventory plugin is on (NETBOX_URL and NETBOX_TOKEN): the hosted inventory is kept by hand')
    for feed in feeds:
        if 'error' in feed:
            # The plugin's own message points at the settings page, which the agent doesn't have.
            hint = ' (NETBOX_URL and NETBOX_TOKEN are not set)' if (
                feed['plugin'] == 'netbox' and not os.environ.get('NETBOX_URL')) else ''
            line(False, f"{feed['name']}: {feed['error']}{hint}")
        else:
            line(True, f"{feed['name']} answers: {len(feed['items'])} devices")
    try:
        for feed in execute('health_feeds', []):
            if 'error' in feed:
                line(False, f"{feed['name']}: {feed['error']}")
            else:
                line(True, f"{feed['name']} answers: {len(feed['items'])} health checks")
    except Exception as exc:  # noqa: BLE001
        line(False, f'the health plugins: {exc}')
    if os.environ.get('FLOORBIT_AGENT_SYSLOG_PORT'):
        try:
            agent.send_syslog([])
            line(True, f'the hosted Floorbit takes syslog (the agent will listen on UDP '
                       f'{os.environ["FLOORBIT_AGENT_SYSLOG_PORT"]})')
        except Exception as exc:  # noqa: BLE001
            line(False, f'syslog: {exc}')
    if os.environ.get('FLOORBIT_AGENT_FLOWS_PORT'):
        try:
            seats = agent.flow_seats().get('seat_of') or {}
            line(True, f'the hosted Floorbit takes flows and knows {len(seats)} seat addresses (the agent will '
                       f'listen on UDP {os.environ["FLOORBIT_AGENT_FLOWS_PORT"]})')
        except Exception as exc:  # noqa: BLE001
            line(False, f'flows: {exc}')
    out('All good: start the agent without "check".' if ok else 'Fix what failed, then run the check again.')
    return ok


def start_syslog(agent, port, out=print):
    """The syslog relay, when FLOORBIT_AGENT_SYSLOG_PORT is set: the Relay, None when it isn't set,
    False when it can't start (a wrong port number, or one in use)."""
    from .syslog_relay import Relay
    port = str(port or '').strip()
    if not port:
        return None
    try:
        relay = Relay(agent.send_syslog, int(port), os.environ.get('FLOORBIT_AGENT_SYSLOG_BIND') or '0.0.0.0', log=out,
                      spool=open_buffer('syslog', os.environ, out))
        relay.start()
    except (ValueError, OSError, OverflowError) as exc:
        out(f'[agent] cannot listen for syslog on "{port}": {exc}')
        return False
    return relay


def open_buffer(kind, env, out=print):
    """The disk buffer for one kind of data (spool.py), in the state volume next to the agent's
    settings: FLOORBIT_AGENT_BUFFER_DIR, _MAX_MB (2048) and _MAX_HOURS (24); _MAX_MB=0 turns it
    off. None when it is off or its folder can't be made (the relay then keeps lines in memory)."""
    from .spool import Spool
    try:
        max_mb = float(env.get('FLOORBIT_AGENT_BUFFER_MAX_MB') or 2048)
        max_hours = float(env.get('FLOORBIT_AGENT_BUFFER_MAX_HOURS') or 24)
    except ValueError:
        out('[agent] FLOORBIT_AGENT_BUFFER_MAX_MB and _MAX_HOURS must be numbers; no disk buffer')
        return None
    if max_mb <= 0:
        return None
    base = env.get('FLOORBIT_AGENT_BUFFER_DIR') or os.path.join(
        os.path.dirname(os.path.abspath(env.get('FLOORBIT_DATA_FILE') or 'agent-data.json')), 'buffer')
    try:
        spool = Spool(os.path.join(base, kind), max_bytes=int(max_mb * 1024 ** 2), max_age=max_hours * 3600, log=out)
    except OSError as exc:
        out(f'[agent] no disk buffer for {kind} in {base}: {exc}')
        return None
    if len(spool):
        out(f'[agent] {len(spool)} {kind} records in the disk buffer from before: sending them once the '
            'hosted Floorbit takes them')
    return spool


def start_flows(agent, port, out=print):
    """The flow collector, when FLOORBIT_AGENT_FLOWS_PORT is set; as start_syslog."""
    from .flows_relay import FlowsRelay
    port = str(port or '').strip()
    if not port:
        return None
    try:
        relay = FlowsRelay(agent, int(port), os.environ.get('FLOORBIT_AGENT_FLOWS_BIND') or '0.0.0.0', log=out)
        relay.start()
    except (ValueError, OSError, OverflowError) as exc:
        out(f'[agent] cannot listen for flows on "{port}": {exc}')
        return False
    return relay


def start_captive(agent, env, out=print):
    """The Captive Portal API, when FLOORBIT_AGENT_CAPTIVE is set; as start_syslog."""
    from . import captive
    address = env.get('FLOORBIT_AGENT_CAPTIVE', '').strip()
    if not address or address.lower() == 'off':
        return None
    api = captive.Captive(agent.captive_state, env.get('FLOORBIT_AGENT_CAPTIVE_PORTAL_URL', ''),
                          [p for p in env.get('FLOORBIT_AGENT_CAPTIVE_PROXIES', '').split(',') if p.strip()], log=out,
                          say_hello=agent.seat_walk_hello, floorbit_url=agent.url)
    try:
        server = captive.serve(api, address, log=out)
    except (ValueError, OSError, OverflowError) as exc:
        out(f'[captive] cannot answer on "{address}" (host:port): {exc}')
        return False
    return _Served(server, api)


def start_front_door(agent, env, out=print):
    """The attendee front door (front_door.py), when FLOORBIT_AGENT_FRONT_DOOR is set; as start_syslog."""
    from . import front_door
    address = env.get('FLOORBIT_AGENT_FRONT_DOOR', '').strip()
    if not address or address.lower() == 'off':
        return None
    door = front_door.FrontDoor(agent.url, agent.key, proxies=[
        p for p in env.get('FLOORBIT_AGENT_FRONT_DOOR_PROXIES', '').split(',') if p.strip()], log=out)
    try:
        context = front_door.tls_context(env)
        server = front_door.serve(door, address, context)
    except (ValueError, OSError, OverflowError) as exc:
        out(f'[front door] cannot answer on "{address}" (host:port, and the TLS certificate if set): {exc}')
        return False

    def numbers(m):
        for outcome, n in dict(door.counts).items():
            m.set('floorbit_agent_front_door_requests_total', n, {'outcome': outcome})
        m.set('floorbit_agent_front_door_open', door.open)
    agent.metrics.collect(numbers)
    out(f'[front door] serving the hosted Floorbit\'s pages on {"https" if context else "http"}://{address}')
    return _Server(server)


class _Server:
    def __init__(self, server):
        self.server = server

    def stop(self):
        self.server.shutdown()


class _Served:
    """A server and its refresh, stopped together."""

    def __init__(self, server, api):
        self.server, self.api = server, api

    def stop(self):
        self.api.stop()
        self.server.shutdown()


def start_vrrp(agent, env, out=print):
    """keepalived for the VIPs in FLOORBIT_AGENT_VRRP_VIPS (vrrp.py). None when VRRP is off, False
    when it is set up wrong or can't start (the agent then stops: a VIP nobody holds is worse
    than an agent that says why)."""
    from . import vrrp
    try:
        settings = vrrp.settings_from(env)
    except ValueError as exc:
        out(f'[vrrp] {exc}')
        return False
    if settings is None:
        return None
    address = env.get('FLOORBIT_AGENT_METRICS', '0.0.0.0:9520').strip()
    check = vrrp.local_check(address) if address.lower() not in ('', 'off') else None
    keepalived = vrrp.Keepalived(settings, agent.metrics, log=out, healthy=check)
    try:
        keepalived.start()
    except (RuntimeError, OSError) as exc:
        out(f'[vrrp] {exc}')
        keepalived.stop()
        return False
    return keepalived


def start_plugins(agent, app):
    """The plugin host: which of this image's plugins run comes from the hosted Floorbit. For
    ``check`` it asks once and starts them, so their checks can run."""
    from floorbit import sources

    from .plugin_host import LocalPlugins, PluginHost, agent_specs
    root = state_root()
    local = LocalPlugins(app, sources.registry, root, pinned=os.environ.get('FLOORBIT_AGENT_PLUGINS', '').split(','))
    host = PluginHost(agent, agent_specs(sources.registry), root, local=local)
    from .bundles import Updater
    host.updater = Updater(agent, sources.registry, root, floorbit_version(app), log=print)
    host.updater.restore()
    host.specs = agent_specs(sources.registry)      # with the bundles installed before
    agent.plugins = host
    return host


def state_root():
    return os.environ.get('FLOORBIT_AGENT_STATE') or os.path.dirname(os.path.abspath(
        os.environ.get('FLOORBIT_DATA_FILE', 'agent-data.json')))


def floorbit_version(app):
    """The Floorbit this image is built on: the version of the plugins it came with."""
    return str(app.config.get('FLOORBIT_VERSION') or '0')


def _stop_on_term(_signum, _frame):
    raise KeyboardInterrupt


def agent_version():
    """This agent's own version: VERSION next to the package (the image) or at the repo's root."""
    here = pathlib.Path(__file__).resolve().parent
    for path in (here / 'VERSION', here.parent / 'VERSION'):
        if path.is_file():
            return path.read_text().strip()
    return '0'


def heartbeat_file():
    return os.environ.get('FLOORBIT_AGENT_HEARTBEAT') or os.path.join(
        os.path.dirname(os.path.abspath(os.environ.get('FLOORBIT_DATA_FILE') or 'agent-data.json')), 'agent-heartbeat')


def health(path=None, now=None, out=print):
    """``python -m floorbit_agent health``, the container's health check: 0 when the agent got through
    to the hosted Floorbit in the last HEALTHY_FOR seconds, 1 otherwise. Fast: no app, no network."""
    path = path or heartbeat_file()
    now = time.time() if now is None else now
    try:
        age = now - os.path.getmtime(path)
    except OSError:
        out('unhealthy: the agent has not reached the hosted Floorbit yet')
        return 1
    if age > HEALTHY_FOR:
        out(f'unhealthy: the hosted Floorbit last answered {age:.0f} s ago')
        return 1
    out(f'healthy: the hosted Floorbit answered {age:.0f} s ago')
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in ('check', 'run', 'health', 'plugins'):
        print('Usage: python -m floorbit_agent [run | check | health | plugins [approve <id> | all]]',
              file=sys.stderr)
        return 2
    if argv and argv[0] == 'health':
        return health()
    if argv and argv[0] == 'plugins':
        os.environ.setdefault('FLOORBIT_DATA_FILE', os.path.abspath('agent-data.json'))
        from .bundles import cli
        return cli(argv[1:], state_root())
    url = os.environ.get('FLOORBIT_URL', '').strip()
    key = os.environ.get('FLOORBIT_AGENT_KEY', '').strip()
    if not url or not key:
        print('Set FLOORBIT_URL (the hosted Floorbit) and FLOORBIT_AGENT_KEY (from Settings > Local agents).',
              file=sys.stderr)
        return 2
    if not url.startswith('https://') and os.environ.get('FLOORBIT_AGENT_INSECURE') != '1':
        print('FLOORBIT_URL must start with https://: the key and the event data go over it. '
              'For a test on a trusted network, set FLOORBIT_AGENT_INSECURE=1.', file=sys.stderr)
        return 2
    # The agent keeps no floor plan: its settings file is its own, next to nothing else.
    os.environ.setdefault('FLOORBIT_DATA_FILE', os.path.abspath('agent-data.json'))
    from floorbit import create_app
    # The agent serves no pages, so the web app's first-run hints (make an account, a setup code)
    # would only mislead whoever reads its log.
    with contextlib.redirect_stdout(io.StringIO()):
        app = create_app(start_background=False)
    turn_on(app, os.environ.get('FLOORBIT_AGENT_PLUGINS', ''))
    version = agent_version()
    plugins_ref = {}
    agent = Agent(url, key, local_executor(app, plugins_ref), version=version)
    agent.heartbeat = heartbeat_file()
    host = plugins_ref['host'] = start_plugins(agent, app)
    if argv and argv[0] == 'check':
        good = check(agent, agent.execute)
        try:
            host.pull()
        except Exception as exc:  # noqa: BLE001
            print(f'  ✗ the plugin settings from the hosted Floorbit: {exc}')
            good = False
        good = host.check() and good
        host.stop()
        return 0 if good else 1
    relays = [start_syslog(agent, os.environ.get('FLOORBIT_AGENT_SYSLOG_PORT', '')),
              start_flows(agent, os.environ.get('FLOORBIT_AGENT_FLOWS_PORT', '')),
              start_captive(agent, os.environ), start_front_door(agent, os.environ)]
    from . import metrics as agent_metrics
    agent.metrics.collect(agent_metrics.relay_collector(relays[0] or None, relays[1] or None))
    metrics_server = agent_metrics.serve(agent.metrics, agent, os.environ.get('FLOORBIT_AGENT_METRICS', '0.0.0.0:9520'),
                                         HEALTHY_FOR, os.environ.get('FLOORBIT_AGENT_METRICS_TOKEN') or None)
    keepalived = start_vrrp(agent, os.environ)
    host.vrrp = keepalived or None
    agent.metrics.collect(host.collect)
    host.start()
    if False in relays or keepalived is False:
        host.stop()
        for relay in relays:
            if relay:
                relay.stop()
        if metrics_server:
            metrics_server.shutdown()
        return 2
    # docker stop sends SIGTERM: leave the way Ctrl+C does, so the syslog backlog goes out first.
    signal.signal(signal.SIGTERM, _stop_on_term)
    try:
        agent.run()
    except Refused as exc:
        print(f'[agent] {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        agent.stop()
        print('[agent] stopping')
    finally:
        host.stop()
        if keepalived:
            keepalived.stop()
        for relay in relays:
            if relay:
                relay.stop()
        if metrics_server:
            metrics_server.shutdown()
    return 0
