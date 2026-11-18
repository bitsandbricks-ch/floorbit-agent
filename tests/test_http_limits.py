"""The agent's HTTP listeners (captive page, metrics) cap connections: idle ones from the hall held a
thread and a file descriptor each, for ever."""
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

from floorbit_agent import http_limits
from floorbit_agent.http_limits import LimitedHTTPServer


class Handler(BaseHTTPRequestHandler):
    timeout = 0.5

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def serve(**kw):
    server = LimitedHTTPServer(("127.0.0.1", 0), Handler, **kw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_for(check, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_one_address_gets_so_many_connections_then_is_refused():
    server = LimitedHTTPServer(("127.0.0.1", 0), Handler, total=10, per_client=2, shared={"192.0.2.9"})
    try:
        assert server._take("192.0.2.50") and server._take("192.0.2.50")
        assert not server._take("192.0.2.50") and server.refused == 1
        assert server._take("192.0.2.51")                    # another laptop still gets in
        for _ in range(5):
            assert server._take("192.0.2.9")                 # the proxy: held to the total only
        assert server._take("192.0.2.52") and server._take("192.0.2.52")
        assert not server._take("192.0.2.53")                # the total is reached
        assert server._take("127.0.0.1")                     # keepalived's /healthz is never refused
        server._give_back("192.0.2.50")
        assert server._take("192.0.2.53")
        assert server._per["192.0.2.50"] == 1
    finally:
        server.server_close()


def test_a_refused_connection_is_closed_and_counted():
    server = LimitedHTTPServer(("127.0.0.1", 0), Handler, total=10, per_client=0)
    try:
        ours, theirs = socket.socketpair()
        server.process_request(theirs, ("192.0.2.50", 40000))
        ours.settimeout(1)
        assert ours.recv(10) == b"" and server.refused == 1
    finally:
        server.server_close()


def test_an_idle_connection_is_let_go_and_its_slot_comes_back():
    server = serve(total=10, per_client=10)
    port = server.server_address[1]
    try:
        server._take("192.0.2.50")                              # a slot counted the same way a real one is
        idle = socket.create_connection(("127.0.0.1", port))
        idle.sendall(b"GET / HT")                               # half a request, then nothing
        idle.settimeout(3)
        assert idle.recv(10) == b""                             # closed after the handler's timeout
        server._give_back("192.0.2.50")
        assert wait_for(lambda: server._open == 0)
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()


def test_the_captive_page_uses_the_limits_and_trusts_its_proxy_for_many():
    from floorbit_agent import captive as cap
    page = cap.Captive(ask=lambda: {}, proxies=["192.0.2.9"], log=lambda *a: None)
    server = cap.serve(page, "127.0.0.1:0", log=lambda *a: None)
    try:
        assert isinstance(server, LimitedHTTPServer) and server.shared == {"192.0.2.9"}
        assert server.RequestHandlerClass.timeout == http_limits.IDLE_SECONDS
    finally:
        page.stop()
        server.shutdown()
        server.server_close()
