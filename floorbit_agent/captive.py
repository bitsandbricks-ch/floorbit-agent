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

RFC 8908 wants HTTPS for this URL, so serve it behind the event's reverse proxy with a certificate.
Put that proxy's address in ``FLOORBIT_AGENT_CAPTIVE_PROXIES`` so the client's own IP is taken from
``X-Forwarded-For``.
"""
import ipaddress
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = '/.well-known/captive-portal'
KIND = 'application/captive+json'
REFRESH = 30          # seconds between asks; a check-in shows up here within this
RETRY = 10            # after a failed ask


def _ip(text):
    try:
        return str(ipaddress.ip_address(str(text or '').strip().split('%')[0]))
    except ValueError:
        return ''


class Captive:
    def __init__(self, ask, portal_url='', proxies=(), log=print):
        self.ask = ask                       # () -> {"done": [ip], "portal_url"}; RuntimeError: off there
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
            if self.path.split('?', 1)[0] != PATH:
                self._send(404, {'error': 'not found'}, 'application/json')
                return
            ip = captive.client_ip(self.client_address[0], self.headers.get('X-Forwarded-For', ''))
            self._send(200, captive.answer(ip), KIND)

        def _send(self, status, body, kind):
            data = (json.dumps(body) + '\n').encode()
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
