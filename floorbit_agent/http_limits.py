"""HTTP listeners on the event network with a cap on connections.

``ThreadingHTTPServer`` starts a thread per connection and waits for a request for ever: anyone in
the hall could open thousands of idle connections and hold a thread and a file descriptor each,
until the agent ran out of either and stopped reaching the hosted Floorbit. Here a connection that
sends nothing for ``IDLE_SECONDS`` is closed (the handlers set ``timeout``), one address holds at most
``PER_CLIENT`` at a time and the server ``TOTAL``. Past that, a new connection is closed at once.

Loopback is never refused: keepalived and Docker ask ``/healthz`` from there, and a flood from the
hall must not read as the agent being down (that would move the VRRP address).

Browsers drop connections all the time (preconnects, a closed tab) and some try HTTPS on a plain
port: none of that is the agent's trouble, so it is not printed as a traceback. A wrong scheme gets
one line now and then that says what to open instead.
"""
import contextlib
import ipaddress
import socket
import ssl
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from . import logs

IDLE_SECONDS = 10
PER_CLIENT = 16
TOTAL = 256
SAY_EVERY = 60          # seconds between two lines about the same kind of wrong request
# A client that went away: normal on a network full of browsers, nothing to log.
GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError, socket.timeout)
TLS_HELLO = 0x16        # the first byte of a TLS handshake record


def _loopback(ip):
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, total=TOTAL, per_client=PER_CLIENT, shared=(), name='listener',
                 log=logs.say, context=None):
        """``shared``: addresses many clients come through (a reverse proxy), held to ``total`` only.
        ``context``: an ``ssl.SSLContext`` to serve HTTPS; the handshake runs in the request's thread,
        so a slow or wrong client can't hold up the listener."""
        self.total, self.per_client, self.shared = total, per_client, frozenset(shared)
        self.refused = 0
        self.name, self.log = name, log
        self._open, self._per = 0, {}
        self._count_lock = threading.Lock()
        self._said = {}
        super().__init__(address, handler)
        self.tls = context is not None
        if context is not None:
            self.socket = context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    @property
    def port(self):
        return self.server_address[1]

    def say_once(self, kind, text, now=None):
        """One line per kind and SAY_EVERY: a browser retrying must not flood the log."""
        now = time.monotonic() if now is None else now
        with self._count_lock:
            if now - self._said.get(kind, -SAY_EVERY) < SAY_EVERY:
                return False
            self._said[kind] = now
        self.log(f'[{self.name}] {text}')
        return True

    def finish_request(self, request, client_address):
        if not self.tls and self._speaks_tls(request):
            self.say_once('tls', f'a browser at {client_address[0]} spoke HTTPS to the plain HTTP port {self.port}: '
                                 f'open http://, or give the agent a certificate (FLOORBIT_AGENT_TLS_CERT and _KEY)')
            return
        super().finish_request(request, client_address)

    @staticmethod
    def _speaks_tls(request):
        """Peek at the first byte without taking it: a TLS ClientHello starts with 0x16, HTTP with a letter."""
        before = request.gettimeout()
        try:
            request.settimeout(IDLE_SECONDS)
            first = request.recv(1, socket.MSG_PEEK)
        except OSError:
            return False          # gone or silent: the handler's own timeout deals with it
        finally:
            with contextlib.suppress(OSError):
                request.settimeout(before)
        return bool(first) and first[0] == TLS_HELLO

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, ssl.SSLError):
            if 'HTTP_REQUEST' in str(exc) or 'http request' in str(exc).lower():
                self.say_once('plain', f'a browser at {client_address[0]} spoke plain HTTP to the HTTPS port '
                                       f'{self.port}: open https://')
            return                # any other failed handshake: a client that gave up or doesn't trust us
        if isinstance(exc, GONE):
            return
        if isinstance(exc, OSError):
            self.say_once(f'os:{type(exc).__name__}', f'connection from {client_address[0]} failed: '
                                                      f'{type(exc).__name__}: {exc}')
            return
        super().handle_error(request, client_address)     # a real bug: keep the traceback

    def _take(self, ip):
        if _loopback(ip):
            return True
        with self._count_lock:
            if self._open >= self.total or (ip not in self.shared and self._per.get(ip, 0) >= self.per_client):
                self.refused += 1
                return False
            self._open += 1
            self._per[ip] = self._per.get(ip, 0) + 1
            return True

    def _give_back(self, ip):
        if _loopback(ip):
            return
        with self._count_lock:
            self._open -= 1
            left = self._per.get(ip, 1) - 1
            if left > 0:
                self._per[ip] = left
            else:
                self._per.pop(ip, None)

    def process_request(self, request, client_address):
        ip = client_address[0]
        if not self._take(ip):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._give_back(ip)           # the thread didn't start: its slot is free again
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._give_back(client_address[0])
