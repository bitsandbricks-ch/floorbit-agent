"""The front door: the hosted Floorbit's pages, served from the event network (Floorbit's
docs/agent.md, "Attendees through the agent").

A hosted Floorbit only sees the hall's public NAT address, so it can't tell which seat's PC is
asking: check-in can't find the switch port, the speed test measures the internet line, and every
attendee shares one sign-in lockout. With the plugin "Front door for attendees" turned on in Floorbit, the
agent answers on the event network and passes each request on to the hosted Floorbit over HTTPS,
with two headers it alone can set:

- ``Floorbit-Front-Door``: the agent's key, so the hosted side knows the request came through it;
- ``Floorbit-Client-IP``: the address the request came from here (the last ``X-Forwarded-For`` hop
  when it came through one of the plugin's trusted proxies).

Whatever a client sends under those names is dropped. The agent's own API (``/api/agent/``) is not
passed on, and nothing else is reachable through it but the hosted Floorbit.

The speed test's data (``/help/<seat>/speed/ping``, ``down``, ``up``) is answered right here, so it
measures the hall's network up to the agent, which is what "is it the network?" asks. The result
the page posts afterwards goes to the hosted side as usual.

Point the event DNS name of the check-in page (``CHECKIN_ADDRESS``, for example ``checkin.lan``) at
the agent. With ``FLOORBIT_AGENT_TLS_CERT`` and ``FLOORBIT_AGENT_TLS_KEY`` it serves HTTPS; crew
sign-in needs that, because session cookies are only sent over HTTPS.
"""
import ipaddress
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from . import logs

DOOR_HEADER = 'Floorbit-Front-Door'
CLIENT_HEADER = 'Floorbit-Client-IP'
HOST_HEADER = 'Floorbit-Front-Door-Host'     # the name the attendee typed (checkin.lan lands on /checkin)
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
       'transfer-encoding', 'upgrade', 'host', 'content-length'}
# What a client could use to name another address: the hosted side only believes CLIENT_HEADER.
SPOOFABLE = {'x-forwarded-for', 'x-real-ip', 'forwarded', DOOR_HEADER.lower(), CLIENT_HEADER.lower(),
             HOST_HEADER.lower()}
MAX_BODY = 32 * 1024 * 1024        # what the hosted Floorbit takes in one request, about
TIMEOUT = 60                       # seconds to wait for the hosted side to start answering
STREAM_TIMEOUT = 330               # a live-update stream or a long poll may be quiet this long
CHUNK = 64 * 1024
MAX_OPEN = 200                     # requests passed on at once; more get 503 at once
SPEED_RE = re.compile(r'^/help/([^/]{1,40})/speed/(ping|down|up)\Z')
BLOCKED = ('/api/agent/',)
# While the hosted side can't be reached: attendees see this, in both languages Floorbit speaks.
AWAY_PAGE = b'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>Floorbit</title><style>body{font:16px/1.5 system-ui,sans-serif;margin:0;display:grid;place-items:center;
min-height:100vh;background:#f4f4f5;color:#18181b}main{max-width:28rem;padding:1.5rem}h1{font-size:1.25rem}
@media (prefers-color-scheme:dark){body{background:#18181b;color:#f4f4f5}}</style></head><body><main>
<h1>Floorbit can't be reached right now</h1><p>The network in the hall works; only this page has to wait.
It tries again by itself every 30 seconds.</p>
<h1 lang="de">Floorbit ist gerade nicht erreichbar</h1><p lang="de">Das Netz in der Halle geht; nur diese
Seite muss warten. Sie versucht es alle 30 Sekunden von selbst wieder.</p></main></body></html>'''


def _ip(text):
    try:
        return str(ipaddress.ip_address(str(text or '').strip().split('%')[0]))
    except ValueError:
        return ''


class FrontDoor:
    def __init__(self, url, key, http=None, proxies=(), log=logs.say):
        import requests
        self.url = url.rstrip('/')
        self.key = key
        self.http = http or requests.Session()
        self.proxies = {p for p in (_ip(x) for x in proxies) if p}
        self.log = log
        self.open = 0
        self._lock = threading.Lock()
        self.counts = {'passed': 0, 'speed': 0, 'refused': 0, 'failed': 0, 'busy': 0}

    def client_ip(self, peer, forwarded):
        peer = _ip(peer)
        if peer in self.proxies and forwarded:
            return _ip(forwarded.split(',')[-1]) or peer
        return peer

    def count(self, what):
        with self._lock:
            self.counts[what] += 1

    def enter(self):
        with self._lock:
            if self.open >= MAX_OPEN:
                self.counts['busy'] += 1
                return False
            self.open += 1
            return True

    def leave(self):
        with self._lock:
            self.open -= 1

    def headers_out(self, headers, ip):
        out = {k: v for k, v in headers.items() if k.lower() not in HOP and k.lower() not in SPOOFABLE}
        out[DOOR_HEADER] = self.key
        out[CLIENT_HEADER] = ip
        out[HOST_HEADER] = str(headers.get('Host') or '')[:200]
        return out

    def location(self, value):
        """A redirect to the hosted Floorbit's own address stays on the front door."""
        if value and value.startswith(self.url + '/'):
            return value[len(self.url):]
        if value == self.url:
            return '/'
        return value


def _speed(handler, ip, seat, kind, method):
    """The speed test's bytes, answered here. Shares Floorbit's per-client and per-seat budget and stream cap."""
    from floorbit import speedtest
    if kind == 'ping':
        return handler.reply(200, b'', 'application/octet-stream')
    if kind == 'down' and method == 'GET':
        try:
            size = max(1, min(int(handler.query.get('bytes') or 0), speedtest.MAX_DOWN))
            speedtest.take(ip or '?', unquote(seat).strip().upper(), size)
        except ValueError:
            return handler.reply(400, b'{"error": "bytes is a number."}', 'application/json')
        except speedtest.SpeedtestError as exc:
            return handler.reply(exc.status, json.dumps({'error': str(exc)}).encode(), 'application/json')
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('Content-Length', str(size))
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        try:
            for piece in speedtest.download(size):
                handler.wfile.write(piece)
        except OSError:
            pass   # the browser stopped the test; download() freed the slot
        return None
    if kind == 'up' and method == 'POST':
        size = min(int(handler.headers.get('Content-Length') or 0), speedtest.MAX_UP)
        if size <= 0:
            return handler.reply(411, b'{"error": "Send the bytes to upload."}', 'application/json')
        try:
            speedtest.take(ip or '?', unquote(seat).strip().upper(), size)
        except speedtest.SpeedtestError as exc:
            return handler.reply(exc.status, json.dumps({'error': str(exc)}).encode(), 'application/json')
        got = speedtest.swallow(handler.rfile, size)
        return handler.reply(200, f'{{"bytes": {got}}}'.encode(), 'application/json')
    return handler.reply(405, b'', 'text/plain')


def handler_for(door):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'Floorbit-Agent'
        sys_version = ''

        def log_message(self, *args):   # attendees' requests aren't the agent log's business
            pass

        @property
        def query(self):
            from urllib.parse import parse_qs
            return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

        def reply(self, status, body, kind):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if body:
                self.wfile.write(body)

        def handle_any(self):
            path = urlsplit(self.path).path
            if not self.path.startswith('/') or path.startswith(BLOCKED):
                door.count('refused')
                return self.reply(404, b'Not found', 'text/plain')
            ip = door.client_ip(self.client_address[0], self.headers.get('X-Forwarded-For', ''))
            speed = SPEED_RE.match(path)
            if speed:
                door.count('speed')
                return _speed(self, ip, speed.group(1), speed.group(2), self.command)
            if not door.enter():
                return self.reply(503, b'The front door is busy. Try again in a moment.', 'text/plain')
            try:
                self.pass_on(ip)
            finally:
                door.leave()

        def pass_on(self, ip):
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_BODY:
                door.count('refused')
                return self.reply(413, b'Too large', 'text/plain')
            body = self.rfile.read(length) if length else None
            try:
                resp = door.http.request(self.command, door.url + self.path, data=body,
                                         headers=door.headers_out(self.headers, ip), stream=True,
                                         allow_redirects=False, timeout=(10, STREAM_TIMEOUT))
            except Exception as exc:  # noqa: BLE001  # the hosted side is away: say so, plainly
                door.count('failed')
                door.log(f'[front door] the hosted Floorbit did not answer: {type(exc).__name__}')
                return self.reply(502, AWAY_PAGE, 'text/html; charset=utf-8')
            door.count('passed')
            try:
                self.send_response(resp.status_code)
                for k, v in resp.raw.headers.items() if hasattr(resp.raw, 'headers') else resp.headers.items():
                    # Server and Date: http.server sends its own.
                    if k.lower() in HOP or k.lower() in ('content-encoding', 'server', 'date'):
                        continue
                    self.send_header(k, door.location(v) if k.lower() == 'location' else v)
                if self.command == 'HEAD':
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                for piece in resp.iter_content(CHUNK):
                    if piece:
                        self.wfile.write(b'%x\r\n%s\r\n' % (len(piece), piece))
                        self.wfile.flush()
                self.wfile.write(b'0\r\n\r\n')
            except OSError:
                self.close_connection = True     # the browser went away
            finally:
                resp.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = handle_any

    return Handler


def tls_context(env):
    cert, key = env.get('FLOORBIT_AGENT_TLS_CERT', '').strip(), env.get('FLOORBIT_AGENT_TLS_KEY', '').strip()
    if not cert or not key:
        return None
    from .hosted import server_context
    return server_context(env)


def serve(door, address, context=None):
    """Start answering in a thread; returns the server. Raises ValueError or OSError when the address
    is wrong or taken."""
    host, _, port = address.strip().rpartition(':')
    server = ThreadingHTTPServer((host or '0.0.0.0', int(port)), handler_for(door))
    server.daemon_threads = True
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, name='front-door', daemon=True).start()
    return server
