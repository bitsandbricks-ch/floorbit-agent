"""HTTP listeners on the event network with a cap on connections.

``ThreadingHTTPServer`` starts a thread per connection and waits for a request for ever: anyone in
the hall could open thousands of idle connections and hold a thread and a file descriptor each,
until the agent ran out of either and stopped reaching the hosted Floorbit. Here a connection that
sends nothing for ``IDLE_SECONDS`` is closed (the handlers set ``timeout``), one address holds at most
``PER_CLIENT`` at a time and the server ``TOTAL``. Past that, a new connection is closed at once.

Loopback is never refused: keepalived and Docker ask ``/healthz`` from there, and a flood from the
hall must not read as the agent being down (that would move the VRRP address).
"""
import ipaddress
import threading
from http.server import ThreadingHTTPServer

IDLE_SECONDS = 10
PER_CLIENT = 16
TOTAL = 256


def _loopback(ip):
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, total=TOTAL, per_client=PER_CLIENT, shared=()):
        """``shared``: addresses many clients come through (a reverse proxy), held to ``total`` only."""
        self.total, self.per_client, self.shared = total, per_client, frozenset(shared)
        self.refused = 0
        self._open, self._per = 0, {}
        self._count_lock = threading.Lock()
        super().__init__(address, handler)

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
