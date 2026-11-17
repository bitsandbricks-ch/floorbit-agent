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
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PULL_EVERY = 60
PULL_UNSUPPORTED = 600     # a hosted Floorbit without agent plugins: ask again this rarely
SEND_RETRY = 10
SPOOL_MAX_BYTES = 256 * 1024 ** 2
SPOOL_MAX_AGE = 24 * 3600
BATCH = 50                 # records a sender posts before it looks at the spool again


class AgentContext:
    """What one plugin's agent part works with. Same shape as floorbit.plugins.agent.MemoryContext."""

    def __init__(self, host, plugin_id, config):
        from floorbit.plugins.agent import Health
        self.host = host
        self.id = plugin_id
        self.config = config
        self.settings = dict(config.get('settings') or {})
        self.spec_block = {'sends': config.get('sends') or {}}
        self.stopping = threading.Event()
        self.health_ = Health()
        self._threads = []
        self._closers = []
        self.state_dir = os.path.join(host.state_root, 'plugins', plugin_id)
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self._spool = None
        self._spool_lock = threading.Lock()
        self._wake = threading.Event()

    # logging and health ---------------------------------------------------------------
    def log(self, message):
        self.host.log(f'[{self.id}] {message}')

    def health(self, status, message=''):
        self.health_.set(status, message)

    def error(self, message):
        self.health_.error(message)
        self.log(message)

    def counter(self, name, n=1):
        self.health_.count(name, n)

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
        if not self.spool().put([json.loads(json.dumps(record, default=str))]):
            self.counter('dropped')
        self._wake.set()

    def spool(self):
        with self._spool_lock:
            if self._spool is None:
                from .spool import Spool
                self._spool = Spool(os.path.join(self.state_dir, 'outbox'), SPOOL_MAX_BYTES, SPOOL_MAX_AGE,
                                    log=self.host.log)
                self.thread(self._sender, name=f'plugin-{self.id}-send')
            return self._spool

    def _sender(self):
        while not self.stopping.is_set():
            sent = self._send_some()
            if sent is None:                # failed: wait before trying again
                self._wake.clear()
                self.stopping.wait(SEND_RETRY)
            elif sent == 0:
                self._wake.wait(SEND_RETRY)
                self._wake.clear()

    def _send_some(self):
        """Post up to BATCH spooled records, oldest first. None when the hosted side was away."""
        sent = 0
        while sent < BATCH and not self.stopping.is_set():
            got = self._spool.oldest()
            if got is None:
                return sent
            name, records = got
            for record in records:
                status = self.host.post(self.id, record['kind'], record['payload'])
                if status is None:
                    return None
                if status >= 400:          # refused (off, bad payload): sending again won't help
                    self.counter('rejected')
                    self.error(f'the hosted Floorbit refused {record["kind"]} (HTTP {status})')
                else:
                    self.counter('sent')
            self._spool.done(name)
            sent += len(records)
        return sent

    def backlog(self):
        return len(self._spool) if self._spool is not None else 0

    # threads and listeners --------------------------------------------------------------
    def thread(self, fn, name=None):
        """Run ``fn`` in a thread of the plugin's; it should return once ``ctx.stopping`` is set."""
        def run():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001  # say it; the rest of the agent goes on
                self.health('error', f'{getattr(fn, "__name__", "thread")} stopped: {type(exc).__name__}: {exc}')
                self.error(f'{getattr(fn, "__name__", "thread")} stopped: {type(exc).__name__}: {exc}')
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

    def listen_http(self, port, handler, host='0.0.0.0'):
        """``handler(method, path, headers, body) -> (status, body bytes or str)``, for pushes from
        the hall (Telegraf's line protocol, a webhook). Plain HTTP: keep it on the event network."""
        ctx = self

        class Handler(BaseHTTPRequestHandler):
            def _any(self):
                size = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(min(size, 32 * 1024 * 1024)) if size else b''
                try:
                    status, out = handler(self.command, self.path, dict(self.headers), body)
                except Exception as exc:  # noqa: BLE001
                    ctx.counter('handler_errors')
                    ctx.health_.error(f'http handler: {type(exc).__name__}: {exc}')
                    status, out = 500, 'error'
                out = out.encode() if isinstance(out, str) else (out or b'')
                self.send_response(status)
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = _any

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer((host, int(port)), Handler)
        server.daemon_threads = True
        self.on_stop(server.shutdown)
        self.on_stop(server.server_close)
        self.thread(server.serve_forever, name=f'plugin-{self.id}-http{port}')
        return server.server_address[1]

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
    def __init__(self, agent, specs, state_root, env=None, log=print):
        """``specs``: {plugin id: floorbit Spec} of the plugins in this image that have an agent part."""
        self.agent = agent
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
            return True
        body = self.agent._check(resp)
        self.apply(body.get('plugins') or {})
        self.etag = body.get('etag')
        return True

    def apply(self, wanted):
        """Start, reconfigure and stop plugins so that what runs is ``wanted``."""
        with self._lock:
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
            plugin.start()
        except Exception as exc:  # noqa: BLE001  # one broken plugin must not stop the agent
            ctx.close()
            self.failed[pid] = f'{type(exc).__name__}: {exc}'[:300]
            self.log(f'[agent] plugin {pid} could not start: {self.failed[pid]}')
            return
        self.failed.pop(pid, None)
        self.running[pid] = _Running(plugin, ctx, config)
        self.log(f'[agent] plugin {pid} started')

    def _stop_one(self, pid, why):
        run = self.running.pop(pid, None)
        if run is None:
            return
        try:
            run.plugin.stop()
        except Exception as exc:  # noqa: BLE001
            self.log(f'[agent] plugin {pid} failed to stop cleanly: {exc}')
        run.ctx.close()
        self.log(f'[agent] plugin {pid} stopped ({why})')

    # the loop -----------------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name='agent-plugins', daemon=True)
        self._thread.start()

    def reload(self):
        self._reload.set()
        return {'ok': True}

    def _run(self):
        while not self._stop.is_set():
            wait = PULL_EVERY
            try:
                self.pull()
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
        return method(params if isinstance(params, dict) else {})

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
            out[pid] = {**h, 'running': True}
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
