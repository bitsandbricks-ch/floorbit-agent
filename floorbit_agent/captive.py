"""The Captive Portal API (RFC 8908) for the event network (Floorbit issue #473).

With ``FLOORBIT_AGENT_CAPTIVE`` set (``host:port``), the agent answers
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

RFC 8908 wants HTTPS for this URL, so serve it behind the event's reverse proxy with a certificate.
Put that proxy's address in ``FLOORBIT_AGENT_CAPTIVE_PROXIES`` so the client's own IP is taken from
``X-Forwarded-For``.
"""
import html
import ipaddress
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PATH = '/.well-known/captive-portal'
HELLO_PATH = re.compile(r'^/floorbit/hello/([A-Za-z0-9_-]{16,64})\Z')
KIND = 'application/captive+json'
REFRESH = 30          # seconds between asks; a check-in shows up here within this
RETRY = 10            # after a failed ask


def _ip(text):
    try:
        return str(ipaddress.ip_address(str(text or '').strip().split('%')[0]))
    except ValueError:
        return ''


class Captive:
    def __init__(self, ask, portal_url='', proxies=(), log=print, say_hello=None, floorbit_url=''):
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
        self.loaded, self.error = True, None
        self.asked_at = time.time() if now is None else now
        return REFRESH

    def answer(self, ip):
        """The RFC 8908 body for a client."""
        ip = _ip(ip)
        if not self.loaded or not self.portal or not ip or ip in self.done:
            return {'captive': False}
        return {'captive': True, 'user-portal-url': self.portal}

    def hello(self, code, ip, back):
        """The seat walk-through's hello: ``(status, location or None, text)``."""
        if self.say_hello is None:
            return 404, None, 'This agent does not take seat walk-through hellos.'
        try:
            self.say_hello(code, ip)
        except Exception as exc:  # noqa: BLE001  # the page says why, the crew tries again
            return 502, None, f'Floorbit did not take it: {exc}'
        if back and self.floorbit_url:
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


def serve(captive, address, log=print):
    """Start the endpoint and the refresh in threads; returns the server, or None when off.
    Raises ValueError or OSError when the address is wrong or taken."""
    address = (address or '').strip()
    if not address or address.lower() == 'off':
        return None
    host, _, port = address.rpartition(':')
    port = int(port)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # the name http.server asks for
            path, _, query = self.path.partition('?')
            hello = HELLO_PATH.match(path)
            if hello:
                ip = captive.client_ip(self.client_address[0], self.headers.get('X-Forwarded-For', ''))
                back = (parse_qs(query).get('back') or [''])[0]
                status, location, text = captive.hello(hello.group(1), ip, back)
                if location:
                    self.send_response(status)
                    self.send_header('Location', location)
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                page = f'<!doctype html><meta charset="utf-8"><title>Floorbit</title><p>{html.escape(text)}</p>\n'
                self._send(status, page, 'text/html; charset=utf-8', raw=True)
                return
            if path != PATH:
                self._send(404, {'error': 'not found'}, 'application/json')
                return
            ip = captive.client_ip(self.client_address[0], self.headers.get('X-Forwarded-For', ''))
            self._send(200, captive.answer(ip), KIND)

        def _send(self, status, body, kind, raw=False):
            data = body.encode() if raw else (json.dumps(body) + '\n').encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'private, no-store')   # RFC 8908: per client, never shared
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):   # every phone asks: not news
            pass

    server = ThreadingHTTPServer((host or '0.0.0.0', port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='agent-captive', daemon=True).start()
    threading.Thread(target=captive.run, name='agent-captive-refresh', daemon=True).start()
    log(f'[captive] Captive Portal API on http://{address}{PATH}')
    return server
