"""The agent's log: one line per event on stdout (``docker compose logs agent``), each with the time,
the level and the part that speaks::

    2026-11-19T10:51:14+0100 INFO  [telemetry] Telegraf started (pid 41)

``FLOORBIT_AGENT_LOG_LEVEL`` (debug, info, warning, error; default info) sets how much: debug adds
every call from the hosted Floorbit, every send with its count, and Telegraf's own lines. Every
``FLOORBIT_AGENT_LOG_SUMMARY`` seconds (default 300, 0 for never) one summary line says how the
agent and each plugin are doing, and a plugin whose state changes says so straight away.

Everything in the agent logs through ``say``: one argument, so a test can pass ``lines.append``
instead. A message's level travels with it (``Line``), or is guessed from its words.
"""
import logging
import re
import sys
import threading
import time

LEVELS = {'debug': logging.DEBUG, 'info': logging.INFO, 'warning': logging.WARNING, 'warn': logging.WARNING,
          'error': logging.ERROR}
SUMMARY_EVERY = 300
FORMAT = '%(asctime)s %(levelname)-5s %(message)s'
DATE = '%Y-%m-%dT%H:%M:%S%z'
_PART = re.compile(r'\[([A-Za-z0-9_.-]{1,40})\] ')
# Words that make an untagged line a warning: something did not work.
_TROUBLE = re.compile(r'\b(cannot|could not|can\'t|failed|fails|refused|lost|dropped|not reach|did not|'
                      r'error|unhealthy|split brain|timed out)\b', re.I)


class Line(str):
    """A log message that knows its level; still a plain str for whatever collects it."""
    level = None


def at(level, text):
    line = Line(text)
    line.level = level
    return line


def debug(text):
    return at('debug', text)


def warning(text):
    return at('warning', text)


def error(text):
    return at('error', text)


def level_of(text):
    level = getattr(text, 'level', None)
    if level:
        return LEVELS.get(level, logging.INFO)
    return logging.WARNING if _TROUBLE.search(str(text)) else logging.INFO


def setup(env, stream=None):
    """Log to stdout at FLOORBIT_AGENT_LOG_LEVEL. Returns the level's name (an unknown one is info)."""
    name = (env.get('FLOORBIT_AGENT_LOG_LEVEL') or env.get('LOG_LEVEL') or 'info').strip().lower()
    if name not in LEVELS:
        name = 'info'
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter(FORMAT, DATE))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(LEVELS[name])
    # The libraries underneath only matter when they go wrong.
    for noisy in ('urllib3', 'requests', 'websocket', 'werkzeug'):
        logging.getLogger(noisy).setLevel(max(LEVELS[name], logging.WARNING))
    return name


def say(*parts, **_print_kwargs):
    """Log one message: ``[part] text``. Takes print's arguments, so it can stand in for print."""
    text = parts[0] if len(parts) == 1 else ' '.join(str(p) for p in parts)
    level = level_of(text)
    text = str(text)
    m = _PART.match(text)
    logging.getLogger(f'floorbit_agent.{m.group(1)}' if m else 'floorbit_agent').log(level, text)


def enabled(level):
    return logging.getLogger('floorbit_agent').isEnabledFor(LEVELS.get(level, logging.INFO))


# the summary line ------------------------------------------------------------------------------
def _n(v):
    return f'{v:,.0f}' if isinstance(v, (int, float)) and float(v).is_integer() else str(v)


def summary(data):
    """One line from status_page.status(): the link, the counters that matter, each plugin."""
    from .status_page import _duration
    health = data.get('health') or {}
    a = data.get('agent') or {}
    if health.get('ok'):
        link = 'WebSocket' if health.get('link') == 'websocket' else 'long poll'
        head = f'connected over {link}'
    else:
        head = 'NOT connected: ' + str(health.get('reason') or 'no answer')
    bits = [head, f'up {_duration(a.get("up_seconds"))}']
    for key, word in (('reconnects', 'reconnects'), ('link_drops', 'link drops'),
                      ('answers_failed', 'answers failed'), ('calls_busy', 'calls refused as busy')):
        if a.get(key):
            bits.append(f'{_n(a[key])} {word}')
    plugins = []
    for pid, p in sorted((data.get('plugins') or {}).items()):
        state = (p.get('status') or 'ok') if p.get('running') else 'not running'
        nums = ', '.join(f'{_n(v)} {k.replace("_", " ")}' for k, v in sorted((p.get('metrics') or {}).items())
                         if k in SUMMARY_NUMBERS)
        plugins.append(f'{pid} {state}' + (f' ({nums})' if nums else ''))
    return '[agent] ' + '; '.join(bits) + (' | ' + '; '.join(plugins) if plugins else '')


# The numbers worth a place in the summary; the rest are on the status page and /metrics.
SUMMARY_NUMBERS = {'points', 'sent', 'series', 'devices', 'devices_heard', 'devices_failing', 'backlog',
                   'received', 'dropped', 'rejected', 'refused_writes', 'telegraf_restarts', 'thread_restarts'}


class Reporter:
    """Every ``every`` seconds the summary line; a plugin whose state changes, at once (checked
    every few seconds)."""

    def __init__(self, status, every=SUMMARY_EVERY, log=say, check_every=5):
        self.status, self.every, self.log, self.check_every = status, every, log, check_every
        self.states = {}
        self._stop = threading.Event()
        self._thread = None

    def changes(self, data):
        for pid, p in sorted((data.get('plugins') or {}).items()):
            state = (p.get('status') or 'ok') if p.get('running') else 'not running'
            message = p.get('message') or ''
            before = self.states.get(pid)
            self.states[pid] = (state, message)
            if before == (state, message) or (before is None and state == 'ok'):
                continue
            text = f'[{pid}] now {state}' + (f': {message}' if message else '')
            self.log(warning(text) if state in ('warning', 'error', 'not running') else at('info', text))
        for pid in set(self.states) - set(data.get('plugins') or {}):
            del self.states[pid]

    def tick(self):
        data = self.status()
        self.changes(data)
        return data

    def _run(self):
        last = time.monotonic()
        while not self._stop.wait(self.check_every):
            try:
                data = self.tick()
                if self.every and time.monotonic() - last >= self.every:
                    last = time.monotonic()
                    self.log(at('info', summary(data)))
            except Exception as exc:  # noqa: BLE001  # the log must never take the agent down
                self.log(f'[agent] could not write the summary: {type(exc).__name__}: {exc}')

    def start(self):
        self._thread = threading.Thread(target=self._run, name='agent-log-summary', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


def summary_every(env):
    try:
        return max(0, int(env.get('FLOORBIT_AGENT_LOG_SUMMARY') or SUMMARY_EVERY))
    except ValueError:
        return SUMMARY_EVERY
