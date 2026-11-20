"""Runs the agent parts of Floorbit plugins (floorbit/plugins/agent.py in Floorbit).

The hosted Floorbit says which plugins run here and with which settings
(``GET /api/agent/v1/plugins``, pulled every minute and when a ``plugins_reload`` call comes).
Their code is in the Floorbit this image is built on (``FLOORBIT_REF``); nothing is downloaded.

Each running plugin gets an AgentContext: its settings, a log, ``send()`` (a disk spool per
plugin, posted to ``/api/agent/v1/plugins/<id>/<kind>`` behind it), health and numbers that go
into ``/metrics`` and the report, threads and listeners that stop with it.

Calls and actions from the hosted side come as tunnel method ``plugin``
``[plugin id, "call" | "action", name, args]``. An action runs only when
``FLOORBIT_AGENT_ALLOW_ACTIONS`` allows it (``lancache.clear_cache``, ``lancache.*``, ``*``):
whoever runs the hall decides what the hosted side may change in it.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

from . import http_limits, logs

PULL_EVERY = 60
# The hosted side's word in a split brain (docs/vrrp.md) counts this long after it was last heard;
# with the uplink gone longer, VRRP alone decides again.
HA_HINT_FRESH = 3 * PULL_EVERY
PULL_UNSUPPORTED = 600     # a hosted Floorbit without agent plugins: ask again this rarely
SEND_RETRY = 10
# A plugin thread that raises is started again, waiting longer each time (5 s, 10 s, ... up to
# 5 min), so one bad device doesn't stop a plugin for the rest of the event. After RESTART_LOUD
# restarts in a row its health says it keeps failing; a run of RESTART_RESET seconds counts as healthy.
RESTART_FIRST = 5
RESTART_MAX = 300
RESTART_LOUD = 5
RESTART_RESET = 600
# What a plugin keeps on disk while the hosted side is away, each plugin on its own; the defaults
# are those of the syslog buffer from before syslog was a plugin.
SPOOL_MAX_MB = 2048
SPOOL_MAX_HOURS = 24


def spool_limits(env):
    """The disk buffer's size and age from ``FLOORBIT_AGENT_BUFFER_MAX_MB`` and ``_MAX_HOURS``.
    Anything that is no size (``0``, ``-1``, ``nan``, ``lots``) keeps the default: without the
    buffer a plugin's records would have nowhere to wait."""
    def number(name, default):
        try:
            value = float(env.get(name) or default)
        except ValueError:
            return float(default)
        return value if math.isfinite(value) and value > 0 else float(default)
    return (int(number('FLOORBIT_AGENT_BUFFER_MAX_MB', SPOOL_MAX_MB) * 1024 ** 2),
            number('FLOORBIT_AGENT_BUFFER_MAX_HOURS', SPOOL_MAX_HOURS) * 3600)


BATCH = 50                 # records a sender posts before it looks at the spool again
# A plugin's start() or stop() that doesn't return within this is given up on: it must not hold
# the host's lock, which the report and every other plugin need.
START_TIMEOUT = 30
STOP_TIMEOUT = 15
# Calls and actions from the hosted side running at once per plugin; past that, "busy" at once.
CALLS_PER_PLUGIN = 4
# A listener's request: the most it reads, and how long a slow client may take.
HTTP_MAX_BODY = 32 * 1024 * 1024
HTTP_TIMEOUT = 30


def _bounded(fn, timeout, name):
    """Run ``fn`` on its own thread for at most ``timeout`` seconds: (finished, error)."""
    box = {}

    def run():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001  # handed back to the caller
            box['error'] = exc
    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive(), box.get('error')


class AgentContext:
    """What one plugin's agent part works with. Same shape as floorbit.plugins.agent.MemoryContext."""

    def __init__(self, host, plugin_id, config):
        from floorbit.plugins.agent import AuthBlocks, Health
        self.host = host
        self.id = plugin_id
        self.config = config
        self.settings = dict(config.get('settings') or {})
        self.spec_block = {'sends': config.get('sends') or {}, 'fetches': config.get('fetches') or []}
        self._latest = {}          # kind -> the newest record of a ``latest`` kind, not yet sent
        self._latest_lock = threading.Lock()
        self.stopping = threading.Event()
        self.health_ = Health()
        self.tls = False
        self.tls_wanted = False
        self._threads = []
        self._closers = []
        self.state_dir = os.path.join(host.state_root, 'plugins', plugin_id)
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self.auth = AuthBlocks(os.path.join(self.state_dir, 'auth-blocks.json'))
        self._spool = None
        self._spool_lock = threading.Lock()
        self._wake = threading.Event()
        self._send_lock = threading.Lock()     # one sender at a time: a record goes out once

    def primary(self):
        """True unless VRRP runs here and another agent holds the first VIP: only the primary
        polls devices and logs in, so two agents in one hall never do it twice (docs/vrrp.md)."""
        return self.host.primary()

    # logging and health ---------------------------------------------------------------
    def log(self, message):
        self.host.log(logs.at(getattr(message, 'level', None), f'[{self.id}] {message}'))

    def warning(self, message):
        self.log(logs.warning(message))

    def debug(self, message):
        """Only with FLOORBIT_AGENT_LOG_LEVEL=debug: the detail nobody needs while things work."""
        if logs.enabled('debug'):
            self.log(logs.debug(message))

    def health(self, status, message='', hint=''):
        try:
            self.health_.set(status, message, hint)
        except TypeError:      # a Floorbit from before health hints (FLOORBIT_REF)
            self.health_.set(status, message)

    def error(self, message):
        self.health_.error(message)
        self.log(logs.warning(message))

    def counter(self, name, n=1):
        self.health_.count(name, n)

    def auth_failed(self, device, credential, rev=None, message=''):
        """The device refused the credential: never try it again until a tech clears it."""
        if self.auth.failed(device, credential, rev, message):
            self.counter('auth_blocked')
            self.error(f'login to {device} with {credential} failed: {message}; not trying again until cleared')
            self.host.report_soon()

    def may_login(self, device, credential, rev=None):
        # A standby leaves logins to the primary: two agents never try the same one (lockouts).
        return self.primary() and self.auth.may_login(device, credential, rev)

    def gauge(self, name, value):
        self.health_.gauge(name, value)

    # sending --------------------------------------------------------------------------
    def send(self, kind, payload):
        """Queue ``payload`` for the hosted part's ``agent_<kind>``. Kept on disk until it went
        out; refused here already when it breaks the plugin's privacy rule."""
        from floorbit.plugins.agent import SEND_MAX_BYTES, check_payload
        from floorbit.plugins.base import PluginError
        err = check_payload(self.spec_block, kind, payload)
        if err:
            self.counter('refused')
            raise PluginError(err)
        record = {'kind': kind, 'payload': payload, 'at': time.time()}
        if len(json.dumps(record, default=str)) > SEND_MAX_BYTES:
            raise PluginError(f'{kind} is larger than {SEND_MAX_BYTES // 1024 ** 2} MB')
        if (self.spec_block['sends'].get(kind) or {}).get('latest'):
            # Only the newest counts (a summary): one waits in memory, replacing the one before.
            self.spool()
            with self._latest_lock:
                self._latest[kind] = record
            self._wake.set()
            return
        if not self.spool().put([json.loads(json.dumps(record, default=str))]):
            self.counter('dropped')
        self._wake.set()

    def spool(self):
        with self._spool_lock:
            if self._spool is None:
                from .spool import Spool
                max_bytes, max_age = spool_limits(os.environ)
                self._spool = Spool(os.path.join(self.state_dir, 'outbox'), max_bytes, max_age,
                                    log=self.host.log)
                self.thread(self._sender, name=f'plugin-{self.id}-send')
            return self._spool

    def _sender(self):
        while not self.stopping.is_set():
            self._wake.clear()              # before sending, so a send() meanwhile isn't missed
            try:
                sent = self._send_some()
            except Exception as exc:  # noqa: BLE001  # a sender that dies would leave the spool to fill up
                self.error(f'sending failed: {type(exc).__name__}: {exc}; trying again')
                sent = None
            if sent:
                self.debug(f'sent {sent} records to the hosted Floorbit, {self.backlog()} waiting')
            if sent is None:                # failed: wait before trying again
                self.stopping.wait(SEND_RETRY)
            elif sent == 0:
                self._wake.wait(SEND_RETRY)

    def _send_some(self):
        """Post up to BATCH spooled records, oldest first. None when the hosted side was away."""
        with self._send_lock:
            return self._send_batch()

    def _send_batch(self):
        sent = 0
        with self._latest_lock:
            latest = list(self._latest.items())
        for kind, record in latest:
            status = self.host.post(self.id, kind, record['payload'])
            if status is None:
                return None
            with self._latest_lock:
                if self._latest.get(kind) is record:     # a newer one may have come meanwhile
                    del self._latest[kind]
            self._count_answer(record, status)
            sent += 1
        while sent < BATCH and not self.stopping.is_set():
            got = self._spool.oldest()
            if got is None:
                return sent
            name, records = got
            for record in records:
                status = self.host.post(self.id, record['kind'], record['payload'])
                if status is None:
                    return None
                self._count_answer(record, status)
            self._spool.done(name)
            sent += len(records)
        return sent

    def _count_answer(self, record, status):
        if status >= 400:          # refused (off, bad payload): sending again won't help
            self.counter('rejected')
            self.error(f'the hosted Floorbit refused {record["kind"]} (HTTP {status})')
        else:
            self.counter('sent')

    def fetch(self, kind):
        """What the hosted part's ``agent_get_<kind>`` answers (listed under agent.fetches).
        PluginError when it isn't listed or the hosted side can't answer now."""
        from floorbit.plugins.base import PluginError
        if kind not in self.spec_block['fetches']:
            raise PluginError(f'agent.fetches does not list {kind}')
        return self.host.fetch(self.id, kind)

    def backlog(self):
        return (len(self._spool) if self._spool is not None else 0) + len(self._latest)

    # threads and listeners --------------------------------------------------------------
    def thread(self, fn, name=None):
        """Run ``fn`` in a thread of the plugin's; it should return once ``ctx.stopping`` is set."""
        label = getattr(fn, '__name__', 'thread')

        def run():
            failures = 0
            while not self.stopping.is_set():
                began = time.monotonic()
                try:
                    fn()
                    return
                except Exception as exc:  # noqa: BLE001  # say it, wait, start it again
                    if time.monotonic() - began >= RESTART_RESET:
                        failures = 0
                    failures += 1
                    wait = min(RESTART_MAX, RESTART_FIRST * 2 ** (failures - 1))
                    why = f'{label} stopped: {type(exc).__name__}: {exc}'
                    self.counter('thread_restarts')
                    self.error(f'{why}; starting it again in {wait} s')
                    if failures >= RESTART_LOUD:
                        self.health('error', f'{why} (failed {failures} times in a row; next try in {wait} s)')
                    else:
                        self.health('error', f'{why}; starting it again in {wait} s')
                    self.stopping.wait(wait)
        t = threading.Thread(target=run, name=name or f'plugin-{self.id}', daemon=True)
        self._threads.append(t)
        t.start()
        return t

    def on_stop(self, fn):
        self._closers.append(fn)

    def listen_udp(self, port, handler, host='0.0.0.0', buffer_bytes=8 * 1024 * 1024):
        """``handler(data, (address, port))`` for each datagram. The address is the sender's (a
        switch, a firewall): keep it out of what an aggregate kind sends."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffer_bytes)
        sock.bind((host, int(port)))
        sock.settimeout(1)
        self.on_stop(sock.close)

        def receive():
            while not self.stopping.is_set():
                try:
                    data, peer = sock.recvfrom(65535)
                except TimeoutError:
                    continue
                except OSError:
                    return
                try:
                    handler(data, peer)
                except Exception as exc:  # noqa: BLE001
                    self.counter('handler_errors')
                    self.health_.error(f'udp handler: {type(exc).__name__}: {exc}')
        self.thread(receive, name=f'plugin-{self.id}-udp{port}')
        return sock.getsockname()[1]

    def listen_http(self, port, handler, host='0.0.0.0', tls=False):
        """``handler(method, path, headers, body) -> (status, body bytes or str)``, for pushes from
        the hall (Telegraf's line protocol, a webhook). Returns the port.

        ``tls=True`` serves HTTPS with FLOORBIT_AGENT_TLS_CERT and FLOORBIT_AGENT_TLS_KEY when the
        hall set them, and plain HTTP (said once in the log) when it didn't. ``self.tls`` tells which."""
        ctx = self
        self.tls_wanted = self.tls_wanted or tls     # a new certificate restarts it (restart_tls)
        context = self._tls_context() if tls else None

        class Handler(BaseHTTPRequestHandler):
            timeout = HTTP_TIMEOUT        # a client that stalls gives its thread back

            def _reply(self, status, out=b''):
                self.send_response(status)
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def _chunked(self):
                """A chunked body (Telegraf's HTTP output sends gzip that way, with no
                Content-Length), up to HTTP_MAX_BODY. None when the chunks are malformed, False
                when they add up to too much."""
                body = bytearray()
                while True:
                    line = self.rfile.readline(1026)
                    try:
                        size = int(line.split(b';', 1)[0].strip(), 16)
                    except ValueError:
                        return None
                    if size < 0:
                        return None
                    if size == 0:
                        while self.rfile.readline(1026) not in (b'\r\n', b'\n', b''):
                            pass          # trailers: nothing here uses them
                        return bytes(body)
                    if len(body) + size > HTTP_MAX_BODY:
                        return False
                    chunk = self.rfile.read(size)
                    if len(chunk) != size:
                        return None
                    body += chunk
                    self.rfile.readline(3)

            def _any(self):
                if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
                    body = self._chunked()
                    if body is False:
                        ctx.counter('http_refused')
                        self.close_connection = True
                        return self._reply(413, b'too large')
                    if body is None:
                        ctx.counter('http_refused')
                        self.close_connection = True
                        return self._reply(400, b'bad chunked body')
                    return self._handle(body)
                try:
                    size = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    size = -1
                if size < 0:
                    # read(-1) would read until the client stops: as much as it likes.
                    ctx.counter('http_refused')
                    return self._reply(400, b'bad Content-Length')
                if size > HTTP_MAX_BODY:
                    ctx.counter('http_refused')
                    return self._reply(413, b'too large')
                return self._handle(self.rfile.read(size) if size else b'')

            def _handle(self, body):
                try:
                    status, out = handler(self.command, self.path, dict(self.headers), body)
                except Exception as exc:  # noqa: BLE001
                    ctx.counter('handler_errors')
                    ctx.health_.error(f'http handler: {type(exc).__name__}: {exc}')
                    status, out = 500, 'error'
                out = out.encode() if isinstance(out, str) else (out or b'')
                self._reply(status, out)

            do_GET = do_POST = do_PUT = _any

            def log_message(self, *args):
                pass

        # Capped like every listener in the hall, and quiet about browsers that hang up.
        server = http_limits.LimitedHTTPServer((host, int(port)), Handler, per_client=64, name=self.id,
                                               log=self.host.log, context=context)
        self.tls = context is not None
        self.on_stop(server.shutdown)
        self.on_stop(server.server_close)
        self.thread(server.serve_forever, name=f'plugin-{self.id}-http{port}')
        return server.server_address[1]

    def _tls_context(self):
        env = self.host.env
        cert, key = env.get('FLOORBIT_AGENT_TLS_CERT', '').strip(), env.get('FLOORBIT_AGENT_TLS_KEY', '').strip()
        if not cert or not key:
            self.log('no FLOORBIT_AGENT_TLS_CERT/KEY: listening on plain HTTP, keep it on the event network')
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(cert, key)
        except (OSError, ssl.SSLError) as exc:
            from floorbit.plugins.base import PluginError
            raise PluginError(f'cannot use the TLS certificate {cert}: {exc}') from exc
        return context

    def close(self):
        self.stopping.set()
        self._wake.set()
        for fn in reversed(self._closers):
            with contextlib.suppress(Exception):
                fn()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=5)


class _Running:
    __slots__ = ('config', 'ctx', 'plugin', 'started')

    def __init__(self, plugin, ctx, config):
        self.plugin, self.ctx, self.config = plugin, ctx, config
        self.started = time.time()


def _actions_allowed(env):
    return {a.strip() for a in str(env.get('FLOORBIT_AGENT_ALLOW_ACTIONS') or '').split(',') if a.strip()}


class PluginHost:
    def __init__(self, agent, specs, state_root, env=None, log=logs.say, local=None):
        """``specs``: {plugin id: floorbit Spec} of the plugins in this image that have an agent part.
        ``local``: a LocalPlugins for the plugins without one (LibreNMS, NetBox, ...)."""
        self.agent = agent
        self.local = local
        self.specs = specs
        self.state_root = state_root
        self.env = os.environ if env is None else env
        self.log = log
        self.running = {}
        self.failed = {}          # plugin id -> why it couldn't start
        self.etag = None
        self.supported = None     # None: not asked yet; False: the hosted side has no agent plugins
        self._lock = threading.RLock()
        self._reload = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._busy = {}           # plugin id -> calls from the hosted side running now
        self._busy_lock = threading.Lock()
        self.wanted = {}          # plugin id -> its config, as the hosted side last said
        self.updater = None       # bundles.Updater: signed plugin updates from the hosted side
        self.vrrp = None          # vrrp.Keepalived while VRRP runs here
        self.stand_by_for = None  # the agent the hosted side says polls while VRRP is split
        self._ha_heard = None     # when the hosted side last said so (monotonic)
        self._yielding = False    # for the log: standing by now

    # config -------------------------------------------------------------------------------
    def pull(self):
        """Ask the hosted Floorbit which plugins run here. True when it answered."""
        headers = self.agent._headers()
        if self.etag:
            headers['If-None-Match'] = f'"{self.etag}"'
        resp = self.agent.http.get(f'{self.agent.url}/api/agent/v1/plugins', headers=headers, timeout=30)
        if resp.status_code == 404:
            if self.supported is not False:
                self.log('[agent] the hosted Floorbit has no agent plugins yet; asking again in '
                         f'{PULL_UNSUPPORTED // 60} minutes')
            self.supported = False
            return False
        self.supported = True
        if resp.status_code == 304:
            self._ha_heard = time.monotonic()     # the etag covers the hint: still as it was
            return True
        body = self.agent._check(resp)
        ha = body.get('ha') if isinstance(body.get('ha'), dict) else {}
        other = ha.get('stand_by_for')
        self.stand_by_for = other if isinstance(other, str) and other else None
        self._ha_heard = time.monotonic()
        if isinstance(body.get('agent'), dict):
            # Before apply: a plugin started now reads the settings (Telegraf's port, TLS) it brings.
            self.agent.take_hosted(body['agent'])
        if self.updater is not None:
            # Before apply, so a plugin turned on together with its update starts on the new version.
            self.updater.offer(self, body.get('bundles') or {})
        self.apply(body.get('plugins') or {})
        if self.local is not None and isinstance(body.get('local'), dict):   # an older Floorbit sends none
            self.local.apply(body['local'])
        self.etag = body.get('etag')
        return True

    def apply(self, wanted):
        """Start, reconfigure and stop plugins so that what runs is ``wanted``."""
        with self._lock:
            self.wanted = {k: v for k, v in wanted.items() if isinstance(v, dict)}
            for pid in [p for p in self.running if p not in wanted]:
                self._stop_one(pid, 'turned off on the hosted Floorbit')
            for pid in [p for p in self.failed if p not in wanted]:
                self.failed.pop(pid)
            for pid, config in wanted.items():
                if not isinstance(config, dict):
                    continue
                current = self.running.get(pid)
                if current is None:
                    self._start(pid, config)
                elif current.config != config:
                    took = False
                    if current.config.get('sends') == config.get('sends'):
                        try:
                            took = bool(current.plugin.configure(dict(config.get('settings') or {})))
                        except Exception as exc:  # noqa: BLE001
                            current.ctx.error(f'configure failed: {exc}')
                    if took:
                        current.config = config
                        current.ctx.config = config
                        current.ctx.settings = dict(config.get('settings') or {})
                    else:
                        self._stop_one(pid, 'settings changed')
                        self._start(pid, config)

    def _load_class(self, spec):
        from floorbit.plugins.agent import AgentPlugin
        path = spec.path / spec.agent['module']
        modname = f"floorbit_agent_plugin_{spec.id.replace('-', '_')}"
        mod = sys.modules.get(modname)
        if mod is None:
            sp = importlib.util.spec_from_file_location(modname, path)
            mod = importlib.util.module_from_spec(sp)
            sys.modules[modname] = mod
            try:
                sp.loader.exec_module(mod)
            except BaseException:
                sys.modules.pop(modname, None)
                raise
        cls = getattr(mod, 'AgentPlugin', None)
        if not (isinstance(cls, type) and issubclass(cls, AgentPlugin)):
            raise TypeError(f'{spec.agent["module"]} has no class AgentPlugin(floorbit.plugins.agent.AgentPlugin)')
        return cls

    def _start(self, pid, config):
        spec = self.specs.get(pid)
        if spec is None:
            # The hosted Floorbit is newer than the one this agent is built on.
            self.failed[pid] = 'this agent does not have this plugin: update the agent'
            self.log(f'[agent] plugin {pid}: {self.failed[pid]}')
            return
        ctx = AgentContext(self, pid, config)
        try:
            plugin = self._load_class(spec)(ctx)
        except Exception as exc:  # noqa: BLE001  # one broken plugin must not stop the agent
            plugin, error = None, exc
        else:
            finished, error = _bounded(plugin.start, START_TIMEOUT, f'plugin-{pid}-start')
            if not finished:
                error = TimeoutError(f'start() did not return within {START_TIMEOUT} s')
        if error is not None:
            ctx.close()
            self.failed[pid] = f'{type(error).__name__}: {error}'[:300]
            self.log(f'[agent] plugin {pid} could not start: {self.failed[pid]}')
            return
        self.failed.pop(pid, None)
        self.running[pid] = _Running(plugin, ctx, config)
        self.log(f'[agent] plugin {pid} started')

    def _stop_one(self, pid, why):
        run = self.running.pop(pid, None)
        if run is None:
            return
        finished, error = _bounded(run.plugin.stop, STOP_TIMEOUT, f'plugin-{pid}-stop')
        if not finished:
            self.log(f'[agent] plugin {pid} did not stop within {STOP_TIMEOUT} s; leaving it behind')
        elif error is not None:
            self.log(f'[agent] plugin {pid} failed to stop cleanly: {error}')
        run.ctx.close()
        self.log(f'[agent] plugin {pid} stopped ({why})')

    def restart_tls(self):
        """A new certificate (hosted.py): the plugins serving HTTPS start again with it."""
        with self._lock:
            for pid in [p for p, run in self.running.items() if getattr(run.ctx, 'tls_wanted', False)]:
                config = self.running[pid].config
                self._stop_one(pid, 'new certificate')
                self._start(pid, config)

    # the loop -----------------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name='agent-plugins', daemon=True)
        self._thread.start()

    def reload(self):
        self._reload.set()
        return {'ok': True}

    def primary(self, now=None):
        vrrp = self.vrrp
        if not vrrp:
            return True
        from .vrrp import instance
        if vrrp.states.get(instance(1)) != 'MASTER':
            self._note_yield(False)
            return False
        # Split brain: the agents can't hear each other's VRRP and both hold the first VIP. The hosted
        # side hears both and names one; the other stands by, so no login is tried twice.
        now = time.monotonic() if now is None else now
        told = (self.stand_by_for is not None and self._ha_heard is not None
                and now - self._ha_heard < HA_HINT_FRESH)
        self._note_yield(told)
        return not told

    def _note_yield(self, yielding):
        if yielding == self._yielding:
            return
        self._yielding = yielding
        if yielding:
            self.log(f'[vrrp] split brain: another agent holds the first VIP too; the hosted Floorbit says '
                     f'{self.stand_by_for} polls and logs in, so this one stands by')
        else:
            self.log('[vrrp] no longer standing by for another agent')

    def report_soon(self):
        """A new failed login: the hosted side should hear of it now, not in half a minute."""
        self.agent._report_at = None

    def _run(self):
        while not self._stop.is_set():
            wait = PULL_EVERY
            try:
                self.pull()
                if self.updater is not None:
                    self.updater.watch(self)
                if self.supported is False:
                    wait = PULL_UNSUPPORTED
            except Exception as exc:  # noqa: BLE001  # the main loop says when the hosted side is away
                self.log(f'[agent] could not get the plugin settings: {exc}')
            self._reload.wait(wait)
            self._reload.clear()

    def stop(self):
        self._stop.set()
        self._reload.set()
        with self._lock:
            for pid in list(self.running):
                self._stop_one(pid, 'the agent stops')

    # what the agent part sends --------------------------------------------------------------
    def post(self, pid, kind, payload):
        """The HTTP status of one post, or None when the hosted side couldn't be reached."""
        try:
            resp = self.agent._post(f'/api/agent/v1/plugins/{pid}/{kind}', {'payload': payload})
        except Exception:  # noqa: BLE001
            self.agent.metrics.inc('floorbit_agent_hosted_errors_total', {'kind': 'plugin'})
            return None
        if resp.status_code == 401 or resp.status_code >= 500:
            return None
        return resp.status_code

    def fetch(self, pid, kind):
        from floorbit.plugins.base import PluginError
        try:
            resp = self.agent.http.get(f'{self.agent.url}/api/agent/v1/plugins/{pid}/{kind}',
                                       headers=self.agent._headers(), timeout=30)
        except Exception as exc:
            raise PluginError(f'cannot reach the hosted Floorbit: {exc}') from exc
        if resp.status_code != 200:
            try:
                why = (resp.json() or {}).get('error')
            except ValueError:
                why = None
            why = why or f'the hosted Floorbit answered HTTP {resp.status_code}'
            if resp.status_code in (404, 409, 422):      # it answered, and said no
                from floorbit.plugins.agent import HostedSaidNo
                raise HostedSaidNo(why)
            raise PluginError(why)
        return (resp.json() or {}).get('data')

    # calls from the hosted side ---------------------------------------------------------------
    def call(self, args):
        """Tunnel method ``plugin``: ``[plugin id, "call" | "action", name, args]``."""
        from floorbit.plugins.base import PluginError
        if not isinstance(args, (list, tuple)) or len(args) != 4:
            raise PluginError('plugin calls are [plugin, way, name, args]')
        pid, way, name, params = args
        with self._lock:
            run = self.running.get(pid)
        if run is None:
            raise PluginError(self.failed.get(pid) or f'{pid} is not running on this agent')
        if way == 'unblock':
            p = params if isinstance(params, dict) else {}
            cleared = run.ctx.auth.clear(p.get('device'), p.get('credential'))
            run.ctx.log(f"login to {p.get('device')} cleared by the hosted Floorbit ({cleared})")
            return {'cleared': cleared}
        listed = run.config.get('calls' if way == 'call' else 'actions') or [] if way in ('call', 'action') else []
        if name not in listed:
            raise PluginError(f'{pid} has no agent {way} {name}')
        if way == 'action':
            allowed = _actions_allowed(self.env)
            if not ({f'{pid}.{name}', f'{pid}.*', '*'} & allowed):
                raise PluginError(f'This agent does not allow {pid}.{name}: add it to FLOORBIT_AGENT_ALLOW_ACTIONS '
                                  'on the agent machine to let the hosted Floorbit run it.')
            run.ctx.log(f'action {name} asked by the hosted Floorbit')
        method = getattr(run.plugin, f'{way}_{name}', None)
        if not callable(method):
            raise PluginError(f'{pid} has no {way}_{name}()')
        with self._busy_lock:
            if self._busy.get(pid, 0) >= CALLS_PER_PLUGIN:
                run.ctx.counter('calls_refused')
                raise PluginError(f'{pid} is still busy with earlier calls on this agent; try again shortly')
            self._busy[pid] = self._busy.get(pid, 0) + 1
        try:
            return method(params if isinstance(params, dict) else {})
        finally:
            with self._busy_lock:
                self._busy[pid] -= 1

    # health -------------------------------------------------------------------------------------
    def report(self):
        """{plugin id: {status, message, metrics, errors, running}} for the report."""
        out = {}
        with self._lock:
            running = dict(self.running)
            failed = dict(self.failed)
        for pid, run in running.items():
            h = run.ctx.health_.report()
            h['metrics'] = {**h['metrics'], 'backlog': run.ctx.backlog()}
            out[pid] = {**h, 'running': True, 'auth_blocked': run.ctx.auth.list()}
        for pid, why in failed.items():
            out[pid] = {'status': 'error', 'message': why, 'metrics': {}, 'errors': [], 'running': False}
        return out

    def collect(self, m):
        """Each plugin's numbers as floorbit_agent_plugin_<name>{plugin="<id>"}, for /metrics."""
        for pid, h in self.report().items():
            m.set('floorbit_agent_plugin_up', int(h['running'] and h['status'] != 'error'), {'plugin': pid})
            for name, value in h['metrics'].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    m.set(f'floorbit_agent_plugin_{name}', value, {'plugin': pid})

    def check(self, out=print):
        """For ``python -m floorbit_agent check``: True when every plugin that runs is fine."""
        good = True
        with self._lock:
            running = dict(self.running)
            failed = dict(self.failed)
        for pid, run in running.items():
            try:
                out(f'  ✓ plugin {pid}: {run.plugin.check()}')
            except Exception as exc:  # noqa: BLE001
                out(f'  ✗ plugin {pid}: {exc}')
                good = False
        for pid, why in failed.items():
            out(f'  ✗ plugin {pid}: {why}')
            good = False
        return good


def agent_specs(registry):
    """The plugins in this image that have an agent part."""
    if registry is None:
        return {}
    return {s.id: s for s in registry.specs.values() if getattr(s, 'agent', None) and not s.error}


class LocalPlugins:
    """The plugins without an agent part that the agent runs in its own copy of Floorbit to answer
    the hosted side's calls (LibreNMS, NetBox, Pi-hole, ...). The hosted Floorbit says which are on
    and with which settings; the settings stay in memory (they hold tokens). The agent's own
    environment no longer sets them (env_settings.py)."""

    def __init__(self, app, registry, state_root, log=logs.say):
        self.app, self.registry, self.log = app, registry, log
        self.path = os.path.join(state_root, 'local-plugins.json')
        try:
            with open(self.path, encoding='utf-8') as f:
                self.on = set(json.load(f))      # turned on because the hosted side said so
        except (OSError, ValueError, TypeError):
            self.on = set()
        self.missing = set()

    def apply(self, local):
        from floorbit import runtime
        from floorbit.plugins.base import PluginError
        store = self.registry.settings_store
        values = {}
        for config in local.values():
            if isinstance(config, dict):
                values.update(config.get('settings') or {})
        store.set_remote(values)
        runtime.apply(store)
        with self.app.app_context():
            for pid in sorted(local):
                spec = self.registry.specs.get(pid)
                if spec is None:
                    if pid not in self.missing:
                        self.log(f'[agent] the hosted Floorbit turned on {pid}, which this agent does not have: '
                                 'update the agent')
                    self.missing.add(pid)
                    continue
                if self.registry.enabled(spec):
                    continue
                try:
                    self.registry.set_enabled(pid, True)
                except PluginError as exc:
                    self.log(f'[agent] cannot turn on {pid}: {exc}')
                    continue
                self.on.add(pid)
                self.log(f'[agent] turned on {pid} (on in the hosted Floorbit)')
            for pid in sorted(self.on - set(local)):
                self.on.discard(pid)
                if pid not in self.registry.specs:
                    continue
                self.registry.set_enabled(pid, False)
                self.log(f'[agent] turned off {pid} (off in the hosted Floorbit)')
        self.missing &= set(local)
        try:
            with open(self.path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(sorted(self.on), f)
            os.replace(self.path + '.tmp', self.path)
        except OSError as exc:
            self.log(f'[agent] cannot keep which plugins the hosted Floorbit turned on: {exc}')
