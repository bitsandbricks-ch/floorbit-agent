"""Switch SSH for the hosted Floorbit (its floorbit/switch_link.py): port actions, switch commands,
config backups and the browser terminal, opened here on the event network.

The hosted side decides who may do what, opens the stored login for this one call, keeps the audit
log and the pinned host keys, and sends the switch's pinned key along. This side only checks that
the address is one the agent may reach, runs the port plugin it turned on from the hosted settings
(``ssh-ports``), and forgets the login when the call is done.

- ``switch_call`` ``[plugin, method, target]``: ``bounce_port``, ``set_port`` or ``send_commands``.
- ``shell_open`` ``[plugin, target]``: an interactive shell, kept here; answers its id.
- ``shell_io`` ``[id, data, size, after, wait]``: types ``data``, resizes to ``size`` ([cols, rows]) and
  answers the output after character ``after``, waiting up to ``wait`` seconds for some.
- ``shell_close`` ``[id]``.

A shell nobody reads for ``SHELL_IDLE`` seconds (the hosted Floorbit restarted, or lost the link)
is closed here, and at most ``MAX_SHELLS`` are open at once.
"""
import contextlib
import secrets
import threading
import time

METHODS = ('bounce_port', 'set_port', 'send_commands')
MAX_SHELLS = 10
SHELL_IDLE = 90           # seconds without a shell_io: nobody is reading any more
SHELL_BUFFER = 256 * 1024
SHELL_MAX_WAIT = 15
MAX_INPUT = 4096


def _error(text):
    from floorbit.plugins.base import PluginError
    return PluginError(text)


def _spec(registry, plugin_id):
    """The port plugin the hosted side named, if it is on here."""
    for spec in registry.feeders('ports') if registry is not None else []:
        if spec.id == plugin_id and spec.id != 'demo':
            return spec
    raise _error(f'The port plugin {plugin_id} is not on on the agent. It turns on with the settings from '
                 'the hosted Floorbit within a minute.')


def _target(raw, env=None):
    """The target with its host checked and resolved: only addresses the agent may reach."""
    from . import switch_target
    if not isinstance(raw, dict) or not raw.get('switch') or not raw.get('host'):
        raise _error('The switch call was asked for wrongly.')
    target = dict(raw)
    target['host'] = switch_target(str(raw['host']), env)
    login = raw.get('login')
    target['login'] = dict(login) if isinstance(login, dict) else None
    return target


def call(registry, args, env=None):
    """One port action or batch of commands. Answers ``{result, pinned}``."""
    try:
        plugin_id, method, raw = args
    except (TypeError, ValueError):
        raise _error('The switch call was asked for wrongly.') from None
    if method not in METHODS:
        raise _error(f'{method} is not a switch call the agent runs.')
    target = _target(raw, env)
    try:
        result = registry.call(_spec(registry, plugin_id), method, target)
    finally:
        if target.get('login'):
            target['login'].clear()
    return {'result': result, 'pinned': target.get('pinned_new')}


class Shell:
    def __init__(self, sid, switch, shell):
        self.id, self.switch, self.shell = sid, switch, shell
        self.out, self.offset = '', 0
        self.closed = False
        self.touched = time.monotonic()
        self.cond = threading.Condition()
        threading.Thread(target=self._read, name=f'shell-{switch}', daemon=True).start()

    def _read(self):
        while not self.closed:
            try:
                data = self.shell.recv()
            except Exception:  # noqa: BLE001  # a dead connection ends the shell
                data = None
            if data is None:
                self.close()
                return
            if data:
                with self.cond:
                    self.out += data
                    if len(self.out) > SHELL_BUFFER:
                        cut = len(self.out) - SHELL_BUFFER
                        self.out, self.offset = self.out[cut:], self.offset + cut
                    self.cond.notify_all()
            else:
                time.sleep(0.03)

    def close(self):
        with self.cond:
            if self.closed:
                return
            self.closed = True
            self.cond.notify_all()
        with contextlib.suppress(Exception):
            self.shell.close()

    def read(self, after, wait):
        deadline = time.monotonic() + wait
        with self.cond:
            while self.offset + len(self.out) <= after and not self.closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            start = max(after - self.offset, 0)
            return {'data': self.out[start:], 'next': self.offset + len(self.out), 'closed': self.closed}


class Shells:
    def __init__(self):
        self._lock = threading.Lock()
        self._shells = {}
        self._reaper = None

    def _reap(self):
        """Close shells nobody reads, while any are open: the hosted side may be gone for good."""
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return

            def loop():
                while self.count():
                    time.sleep(SHELL_IDLE / 3)
                    self.prune()
            self._reaper = threading.Thread(target=loop, name='shell-reaper', daemon=True)
            self._reaper.start()

    def prune(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            stale = [s for s in self._shells.values() if s.closed or now - s.touched > SHELL_IDLE]
            for s in stale:
                self._shells.pop(s.id, None)
        for s in stale:
            s.close()

    def count(self):
        with self._lock:
            return len(self._shells)

    def open(self, registry, args, env=None):
        try:
            plugin_id, raw = args
        except (TypeError, ValueError):
            raise _error('The terminal call was asked for wrongly.') from None
        self.prune()
        if self.count() >= MAX_SHELLS:
            raise _error(f'The agent has {MAX_SHELLS} terminals open. Close one first.')
        target = _target(raw, env)
        try:
            shell = registry.call(_spec(registry, plugin_id), 'open_shell', target)
        finally:
            if target.get('login'):
                target['login'].clear()
        s = Shell(secrets.token_urlsafe(18), target['switch'], shell)
        with self._lock:
            self._shells[s.id] = s
        self._reap()
        return {'id': s.id, 'pinned': target.get('pinned_new')}

    def _get(self, sid):
        with self._lock:
            s = self._shells.get(str(sid))
        if s is None:
            raise _error('That terminal is closed.')
        s.touched = time.monotonic()
        return s

    def io(self, args):
        try:
            sid, data, size, after, wait = args
        except (TypeError, ValueError):
            raise _error('The terminal call was asked for wrongly.') from None
        s = self._get(sid)
        if data:
            if not isinstance(data, str) or len(data) > MAX_INPUT:
                raise _error('Bad input.')
            if s.closed:
                raise _error('That terminal is closed.')
            s.shell.send(data)
        if size:
            cols, rows = (int(v) for v in size)
            s.shell.resize(min(max(cols, 40), 300), min(max(rows, 10), 120))
        return s.read(int(after or 0), min(max(float(wait or 0), 0), SHELL_MAX_WAIT))

    def close(self, args):
        with self._lock:
            s = self._shells.pop(str(args[0]) if args else '', None)
        if s is not None:
            s.close()
        return True

    def close_all(self):
        with self._lock:
            shells, self._shells = list(self._shells.values()), {}
        for s in shells:
            s.close()


shells = Shells()
