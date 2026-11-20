"""The agent's HTTP listeners (captive page, metrics) cap connections: idle ones from the hall held a
thread and a file descriptor each, for ever."""
import contextlib
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

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


def test_the_front_door_uses_the_limits_and_trusts_its_proxy_for_many():
    from floorbit_agent import front_door
    door = front_door.FrontDoor("https://floor.example.com", "fba_0123abcd_example-key-for-tests-only",
                                http=object(), proxies=["192.0.2.9"], log=lambda *a: None)
    server = front_door.serve(door, "127.0.0.1:0", log=lambda *a: None)
    try:
        assert isinstance(server, LimitedHTTPServer) and server.shared == {"192.0.2.9"}
        assert server.RequestHandlerClass.timeout == front_door.IDLE
    finally:
        server.shutdown()
        server.server_close()


# ── browsers that hang up or use the wrong scheme ──────────────────────────────────────────────

def test_a_dropped_connection_prints_nothing(capsys):
    said = []
    server = LimitedHTTPServer(("127.0.0.1", 0), Handler, log=said.append, name="front-door")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        for _ in range(3):
            s = socket.create_connection(("127.0.0.1", port))
            # SO_LINGER 0: close with a reset, as a browser dropping a preconnect does
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            s.send(b"GET / HT")
            s.close()
        time.sleep(0.3)
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read() == b"ok"
    finally:
        server.shutdown()
    assert said == [] and "Traceback" not in capsys.readouterr().err


def test_https_on_the_plain_port_says_so_once():
    import ssl
    said = []
    server = LimitedHTTPServer(("127.0.0.1", 0), Handler, log=said.append, name="front-door")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        for _ in range(3):
            context = ssl.create_default_context()
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
                    context.wrap_socket(raw, server_hostname="checkin.lan")
            except (ssl.SSLError, OSError):
                pass
        assert wait_for(lambda: said)
        time.sleep(0.2)
    finally:
        server.shutdown()
    assert len(said) == 1
    assert said[0].startswith("[front-door] a browser at 127.0.0.1 spoke HTTPS to the plain HTTP port")


def test_plain_http_on_the_https_port_says_so(tmp_path):
    import ssl
    import subprocess
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=checkin.lan.example"], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    said = []
    server = LimitedHTTPServer(("127.0.0.1", 0), Handler, log=said.append, name="front-door", context=context)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        with contextlib.suppress(Exception):     # the answer is garbage to a plain client: fine
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)
        assert wait_for(lambda: said)
        client = ssl.create_default_context()
        client.check_hostname, client.verify_mode = False, ssl.CERT_NONE
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/", timeout=5, context=client) as resp:
            assert resp.read() == b"ok"
    finally:
        server.shutdown()
    assert said == [f"[front-door] a browser at 127.0.0.1 spoke plain HTTP to the HTTPS port {port}: open https://"]
