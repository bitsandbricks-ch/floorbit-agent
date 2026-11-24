"""The Captive Portal API (RFC 8908) for the event network (Floorbit issue #473).

The agent's front door (front_door.py, the plugin "Front door for attendees") answers
``GET /.well-known/captive-portal`` with ``application/captive+json``. The DHCP server hands out the
URL of that endpoint in option 114 (RFC 8910). Phones and laptops then ask it, and while the answer
says ``"captive": true`` they show a quiet "sign in to network" note that opens ``user-portal-url``.

Nothing is ever blocked: this is only that note. A seat that checked in is "done", and every IP
on that seat's port counts, so a reboot, a new lease or a new MAC doesn't bring the note back.
The hosted Floorbit decides which IPs are done (floorbit/captive.py there). The agent asks it every
``REFRESH`` seconds and keeps the last answer, so it goes on answering when the hosted side is away.
Until the first answer, or while check-in is off there, everyone is done: when in doubt, say nothing.

The same endpoint answers the crew's seat walk-through (Floorbit docs/seat-walk.md):
``GET /floorbit/hello/<code>?back=<url>``. A hosted Floorbit, or one behind NAT, only sees the hall's
public address, so the walk-through page sends the crew laptop here once with a one-time code. The
agent sees the laptop's own IP, reports it with the code over its key (``say_hello``), and sends the
browser back to Floorbit. ``back`` must be on the Floorbit the agent talks to; anything else gets a
short page instead of a redirect, so this is no open redirect.

RFC 8908 wants HTTPS for this URL: give the agent a certificate (FLOORBIT_AGENT_TLS_CERT and _KEY),
or put a reverse proxy in front and its address in the plugin's trusted proxies, so the client's own
IP is taken from ``X-Forwarded-For``.
"""
import html
import ipaddress
import json
import re
import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit

from . import logs

PATH = '/.well-known/captive-portal'
HELLO_PATH = re.compile(r'^/floorbit/hello/([A-Za-z0-9_-]{16,64})\Z')
KIND = 'application/captive+json'
REFRESH = 30          # seconds between asks; a check-in shows up here within this
RETRY = 10            # after a failed ask
HELLO_TRIES = 10      # hellos per laptop address and minute: the crew needs one, a script many
HELLO_WINDOW = 60
HELLO_ADDRESSES = 4096   # addresses remembered; past that the quietest are forgotten


def _ip(text):
    try:
        return str(ipaddress.ip_address(str(text or '').strip().split('%')[0]))
    except ValueError:
        return ''


class Captive:
    def __init__(self, ask, portal_url='', proxies=(), log=logs.say, say_hello=None, floorbit_url='', note=True):
        self.note = note                     # answer the phone note at all (the hello works either way)
        self.ask = ask                       # () -> {"done": [ip], "portal_url"}; RuntimeError: off there
        self.say_hello = say_hello           # (code, ip) -> None; raises when the hosted side refuses
        self.floorbit_url = floorbit_url.rstrip('/')
        self.portal_override = (portal_url or '').strip()
        self.proxies = {p for p in (_ip(x) for x in proxies) if p}
        self.log = log
        self.done = frozenset()
        self.portal = ''
        self.loaded = False                  # an answer came, and check-in is on
        self.asked_at = None
        self.error = None
        self._stop = threading.Event()
        self._count = threading.Lock()
        self.asked = {'captive': 0, 'done': 0}   # answers given, for /metrics: counts only, no IPs
        self._hellos = {}                    # ip -> deque of time.monotonic() of its recent hellos
        self._hello_lock = threading.Lock()

    def refresh(self, now=None):
        """Ask the hosted Floorbit once. Returns the seconds until the next ask."""
        try:
            body = self.ask() or {}
        except RuntimeError as exc:          # 409: check-in is off on the hosted side
            if self.loaded or self.error != str(exc):
                self.log(f'[captive] {exc} Everyone counts as done.')
            self.loaded, self.done, self.error = False, frozenset(), str(exc)
            return REFRESH
        except Exception as exc:  # noqa: BLE001  # the hosted side is away: keep the last answer
            if self.error != str(exc):
                self.log(f'[captive] cannot ask the hosted Floorbit, keeping the last answer: {exc}')
            self.error = str(exc)
            return RETRY
        self.done = frozenset(ip for ip in (_ip(x) for x in body.get('done') or []) if ip)
        self.portal = self.portal_override or str(body.get('portal_url') or '')
        if not self.loaded or self.error:
            self.log(f'[captive] answering: {len(self.done)} clients checked in'
                     + ('' if self.portal else ', but no portal URL yet (PUBLIC_URL on the hosted side)'))
        self.loaded, self.error = True, None
        self.asked_at = time.time() if now is None else now
        return REFRESH

    def answer(self, ip):
        """The RFC 8908 body for a client."""
        ip = _ip(ip)
        captive = bool(self.loaded and self.portal and ip and ip not in self.done)
        with self._count:
            self.asked['captive' if captive else 'done'] += 1
        if not captive:
            return {'captive': False}
        return {'captive': True, 'user-portal-url': self.portal}

    def _hello_allowed(self, ip, now=None):
        """Each hello is a request to the hosted Floorbit: anyone on the hall network could
        otherwise make the agent send them as fast as it answers."""
        now = time.monotonic() if now is None else now
        with self._hello_lock:
            seen = self._hellos.get(ip)
            if seen is None:
                if len(self._hellos) >= HELLO_ADDRESSES:
                    for old in [k for k, v in self._hellos.items() if not v or now - v[-1] >= HELLO_WINDOW]:
                        del self._hellos[old]
                    if len(self._hellos) >= HELLO_ADDRESSES:     # all busy: drop the oldest entry
                        del self._hellos[next(iter(self._hellos))]
                seen = self._hellos[ip] = deque()
            while seen and now - seen[0] >= HELLO_WINDOW:
                seen.popleft()
            if len(seen) >= HELLO_TRIES:
                return False
            seen.append(now)
            return True

    def hello(self, code, ip, back, now=None):
        """The seat walk-through's hello: ``(status, location or None, text)``."""
        if self.say_hello is None:
            return 404, None, 'This agent does not take seat walk-through hellos.'
        if not self._hello_allowed(ip, now):
            return 429, None, 'Too many hellos from this laptop. Wait a minute, then try again.'
        try:
            self.say_hello(code, ip)
        except Exception as exc:  # noqa: BLE001  # the page says why, the crew tries again
            return 502, None, f'Floorbit did not take it: {exc}'
        # urlsplit drops tabs and line breaks before it looks, but send_header would not: a back
        # with "\r\n" in it passed the check and split the response (a header or a page of its own).
        if back and self.floorbit_url and not any(ord(c) < 32 or ord(c) == 127 for c in back):
            want, got = urlsplit(self.floorbit_url), urlsplit(back)
            if (got.scheme, got.netloc) == (want.scheme, want.netloc):
                return 303, back, ''
        return 200, None, f'This laptop ({ip}) is identified. Go back to Floorbit.'

    def client_ip(self, peer, forwarded):
        """The client's IP: the peer's, or the last X-Forwarded-For hop when the peer is our proxy."""
        peer = _ip(peer)
        if peer in self.proxies and forwarded:
            return _ip(forwarded.split(',')[-1])
        return peer

    def run(self):
        while not self._stop.is_set():
            self._stop.wait(self.refresh())

    def stop(self):
        self._stop.set()

    def route(self, path, query, peer, forwarded=''):
        """What the front door answers for the note and the hello: ``(status, headers, body, kind)``,
        or None for a path that isn't ours."""
        hello = HELLO_PATH.match(path)
        if hello:
            ip = self.client_ip(peer, forwarded)
            back = (parse_qs(query).get('back') or [''])[0]
            status, location, text = self.hello(hello.group(1), ip, back)
            if location:
                return status, {'Location': location}, b'', 'text/plain'
            page = f'<!doctype html><meta charset="utf-8"><title>Floorbit</title><p>{html.escape(text)}</p>\n'
            return status, {}, page.encode(), 'text/html; charset=utf-8'
        if path == PATH and self.note:
            body = (json.dumps(self.answer(self.client_ip(peer, forwarded))) + '\n').encode()
            # RFC 8908: per client, never shared
            return 200, {'Cache-Control': 'private, no-store'}, body, KIND
        return None
