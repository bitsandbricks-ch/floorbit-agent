"""Soak test: the agent under steady load for a long time, against a fake hosted Floorbit on
127.0.0.1 (issue #3). Memory, threads and open files must stay flat.

    python scripts/soak.py --minutes 120 --calls-per-second 50 --drop-link-every 300

Every so often the fake drops the WebSocket link, so reconnects are part of the run. Prints a line
per minute and exits 1 when memory grew more than --max-growth-mb or a call went unanswered.
Nothing here talks to a real Floorbit, LibreNMS or NetBox.
"""
import argparse
import contextlib
import json
import os
import pathlib
import random
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import requests
import simple_websocket
from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response

import floorbit_agent as agent_mod


def rss_mb():
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def open_files():
    try:
        return len(os.listdir('/proc/self/fd'))
    except OSError:
        return -1


class Host:
    def __init__(self, rate, drop_every):
        self.rate, self.drop_every = rate, drop_every
        self.sent, self.answered, self.links = 0, set(), 0
        self.lock = threading.Lock()
        self.server = make_server('127.0.0.1', 0, self.app, threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def _next_calls(self, n):
        methods = ('devices', 'arp', 'port_stats', 'resolve_ip', 'health_feeds')
        with self.lock:
            calls = [{'id': str(self.sent + i), 'method': random.choice(methods), 'args': []} for i in range(n)]
            self.sent += n
        return calls

    def app(self, environ, start_response):
        req = Request(environ)
        if req.path == '/api/agent/v1/hello':
            body = {'name': 'Soak', 'server_version': 'soak', 'protocol': 2, 'protocols': [1, 2],
                    'link': '/api/agent/v2/link'}
            return Response(json.dumps(body), mimetype='application/json')(environ, start_response)
        if req.path == '/api/agent/v2/link':
            ws = simple_websocket.Server(environ)
            self.links += 1
            opened = time.monotonic()
            reader_stop = threading.Event()

            def read():
                while not reader_stop.is_set():
                    try:
                        raw = ws.receive(timeout=1)
                    except simple_websocket.ConnectionClosed:
                        return
                    if raw:
                        message = json.loads(raw)
                        with self.lock:
                            self.answered.update(a['id'] for a in message.get('answers') or [])
            threading.Thread(target=read, daemon=True).start()
            try:
                while time.monotonic() - opened < self.drop_every:
                    ws.send(json.dumps({'type': 'calls', 'calls': self._next_calls(self.rate)}))
                    time.sleep(1)
            except simple_websocket.ConnectionClosed:
                pass
            finally:
                time.sleep(2)              # let the last answers in
                reader_stop.set()
                with contextlib.suppress(simple_websocket.ConnectionClosed):
                    ws.close()
            return Response()(environ, start_response)
        if req.path == '/api/agent/v1/calls':
            time.sleep(0.5)
            return Response(json.dumps({'calls': []}), mimetype='application/json')(environ, start_response)
        if req.path == '/api/agent/v1/answers':
            with self.lock:
                self.answered.update(a['id'] for a in json.loads(req.get_data())['answers'])
            return Response('{"taken": 1}', mimetype='application/json')(environ, start_response)
        return Response('{}', status=404)(environ, start_response)


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--minutes', type=float, default=10)
    p.add_argument('--calls-per-second', type=int, default=50)
    p.add_argument('--drop-link-every', type=float, default=120, help='seconds')
    p.add_argument('--max-growth-mb', type=float, default=30)
    args = p.parse_args()

    agent_mod.LINK_STEADY = 0          # a planned drop is not a broken proxy: reconnect straight away
    host = Host(args.calls_per_second, args.drop_link_every)
    http = requests.Session()
    http.trust_env = False
    work = lambda method, a: [{'name': f'sw-{n}', 'ip': f'192.0.2.{n}'} for n in range(20)]  # noqa: E731
    agent = agent_mod.Agent(host.url, 'fba_soak_x', work, http=http, log=lambda *_: None)
    threading.Thread(target=agent.run, daemon=True).start()

    time.sleep(10)                     # warm up: imports, pools, the first link
    base_rss, base_threads, base_fds = rss_mb(), threading.active_count(), open_files()
    print(f'start: {base_rss:.1f} MB, {base_threads} threads, {base_fds} open files')
    end = time.monotonic() + args.minutes * 60
    worst = base_rss
    while time.monotonic() < end:
        time.sleep(min(60, max(1, end - time.monotonic())))
        worst = max(worst, rss_mb())
        print(f'{time.strftime("%H:%M:%S")} sent {host.sent} answered {len(host.answered)} '
              f'links {host.links} rss {rss_mb():.1f} MB threads {threading.active_count()} '
              f'files {open_files()}', flush=True)
    agent.stop()
    time.sleep(3)
    lost = host.sent - len(host.answered)
    growth = worst - base_rss
    print(f'end: {host.sent} calls, {lost} unanswered, {host.links} links, '
          f'memory +{growth:.1f} MB, threads {threading.active_count()} (start {base_threads}), '
          f'files {open_files()} (start {base_fds})')
    # Calls in flight when the run stops may go unanswered; more than a second's worth is a leak.
    ok = growth <= args.max_growth_mb and lost <= args.calls_per_second * 2
    print('OK' if ok else 'FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
