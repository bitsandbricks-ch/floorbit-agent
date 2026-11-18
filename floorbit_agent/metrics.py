"""What the agent is doing, for Prometheus and for a quick look (issue #3).

``GET /metrics`` answers in the Prometheus text format; ``GET /healthz`` answers 200 while the hosted
Floorbit answered in the last HEALTHY_FOR seconds and 503 otherwise, with the reason as JSON.

Listens on ``FLOORBIT_AGENT_METRICS`` (default ``0.0.0.0:9520`` inside the container; the compose
file publishes it on the host's 127.0.0.1 only). ``off`` turns it off. With
``FLOORBIT_AGENT_METRICS_TOKEN`` set, ``/metrics`` needs ``Authorization: Bearer <token>``;
``/healthz`` never does, so local checks need nothing.

Only counts and timings: no IPs, names, log lines or keys, so it is safe to scrape from anywhere the
crew trusts.

Written by hand instead of with prometheus_client: a few dozen lines, one dependency less in an
image that runs on event networks.
"""
import hmac
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler

from . import http_limits

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

HELP = {
    'floorbit_agent_info': ('gauge', 'Always 1; the labels say which agent version speaks which protocol.'),
    'floorbit_agent_up_seconds': ('gauge', 'Seconds since the agent started.'),
    'floorbit_agent_connected': ('gauge', '1 while the hosted Floorbit answers.'),
    'floorbit_agent_link_up': ('gauge', '1 while the WebSocket link is open (0: long poll or nothing).'),
    'floorbit_agent_last_contact_age_seconds': ('gauge', 'Seconds since the hosted Floorbit last answered.'),
    'floorbit_agent_reconnects_total': ('counter', 'Times the connection to the hosted Floorbit was lost.'),
    'floorbit_agent_link_drops_total': ('counter', 'Times the WebSocket link dropped after it was open.'),
    'floorbit_agent_calls_total': ('counter',
                                   'Calls from the hosted Floorbit by method and outcome (ok, error, busy).'),
    'floorbit_agent_call_seconds': ('histogram', 'How long a call took to run here, by method.'),
    'floorbit_agent_answers_total': ('counter', 'Answers sent back, by way (link, https) or failed.'),
    'floorbit_agent_hosted_request_seconds': ('histogram', 'Round trip of requests to the hosted Floorbit, by kind.'),
    'floorbit_agent_hosted_errors_total': ('counter', 'Requests to the hosted Floorbit that failed, by kind.'),
    'floorbit_agent_calls_running': ('gauge', 'Calls running right now.'),
    'floorbit_agent_syslog_received_total': ('counter', 'Syslog lines received from the event network.'),
    'floorbit_agent_syslog_sent_total': ('counter', 'Syslog lines sent to the hosted Floorbit.'),
    'floorbit_agent_syslog_dropped_total': ('counter', 'Syslog lines dropped: memory full, or past the buffer limits.'),
    'floorbit_agent_syslog_backlog': ('gauge', 'Syslog lines waiting to be sent, in memory and in the disk buffer.'),
    'floorbit_agent_syslog_failing': ('gauge', '1 while sending syslog to the hosted Floorbit fails.'),
    'floorbit_agent_buffer_records': ('gauge', 'Records waiting in the disk buffer, by kind.'),
    'floorbit_agent_buffer_bytes': ('gauge', 'Size of the disk buffer, by kind.'),
    'floorbit_agent_syslog_replayed_total': ('counter', 'Syslog lines sent from the disk buffer.'),
    'floorbit_agent_vrrp_running': ('gauge', '1 while keepalived runs (VRRP mode).'),
    'floorbit_agent_vrrp_master': ('gauge', '1 while this agent holds the virtual IP.'),
    'floorbit_agent_flows_packets_total': ('counter', 'NetFlow/IPFIX packets received from the event network.'),
    'floorbit_agent_flows_snapshots_sent_total': ('counter', 'Flow snapshots sent to the hosted Floorbit.'),
    'floorbit_agent_flows_failing': ('gauge', '1 while sending flow snapshots fails.'),
    'floorbit_agent_captive_answers_total': ('counter', 'Captive portal answers, by answer '
                                             '(captive: the note shows, done: it does not).'),
    'floorbit_agent_captive_checked_in': ('gauge',
                                          'Clients the last list from the hosted Floorbit counts as checked in.'),
    'floorbit_agent_captive_list_age_seconds': ('gauge', 'Seconds since the hosted Floorbit last sent the list.'),
    'floorbit_agent_captive_failing': ('gauge', '1 while asking the hosted Floorbit for the list fails.'),
}


def _labels(labels):
    if not labels:
        return ''
    inner = ','.join('{}="{}"'.format(k, str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' '))
                     for k, v in sorted(labels.items()))
    return '{' + inner + '}'


def _num(value):
    if value == math.inf:
        return '+Inf'
    return repr(float(value)) if isinstance(value, float) and not value.is_integer() else str(int(value))


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._values = {}       # (name, frozen labels) -> number
        self._hist = {}         # (name, frozen labels) -> [counts per bucket..., sum, count]
        self._collectors = []   # callables adding gauges at scrape time
        self.started = time.time()

    @staticmethod
    def _key(name, labels):
        return name, tuple(sorted((labels or {}).items()))

    def inc(self, name, labels=None, by=1):
        key = self._key(name, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + by

    def set(self, name, value, labels=None):
        with self._lock:
            self._values[self._key(name, labels)] = value

    def get(self, name, labels=None):
        return self._values.get(self._key(name, labels), 0)

    def observe(self, name, seconds, labels=None):
        key = self._key(name, labels)
        with self._lock:
            h = self._hist.setdefault(key, [0] * len(BUCKETS) + [0.0, 0])
            for i, edge in enumerate(BUCKETS):
                if seconds <= edge:
                    h[i] += 1
            h[-2] += seconds
            h[-1] += 1

    def timed(self, name, labels=None):
        """``with metrics.timed(...)``: observe how long the block took."""
        return _Timer(self, name, labels)

    def collect(self, fn):
        """``fn(metrics)`` runs before each scrape, to set gauges from live objects."""
        self._collectors.append(fn)

    def total(self, name, **match):
        """The sum of a metric over all its labels, or over those matching ``match``."""
        with self._lock:
            return sum(v for (n, labels), v in self._values.items()
                       if n == name and all(dict(labels).get(k) == want for k, want in match.items()))

    def refresh(self):
        """Run the collectors, so gauges read now."""
        for fn in list(self._collectors):
            try:
                fn(self)
            except Exception:  # noqa: BLE001  # one broken collector must not hide the rest
                self.inc('floorbit_agent_collector_errors_total')
        self.set('floorbit_agent_up_seconds', round(time.time() - self.started, 1))

    def render(self):
        self.refresh()
        with self._lock:
            values, hists = dict(self._values), {k: list(v) for k, v in self._hist.items()}
        lines, names = [], sorted({k[0] for k in values} | {k[0] for k in hists})
        for name in names:
            kind, text = HELP.get(name, ('untyped', ''))
            if text:
                lines.append(f'# HELP {name} {text}')
            lines.append(f'# TYPE {name} {kind}')
            for (n, labels), value in sorted(values.items()):
                if n == name:
                    lines.append(f'{name}{_labels(dict(labels))} {_num(value)}')
            for (n, labels), h in sorted(hists.items()):
                if n != name:
                    continue
                base = dict(labels)
                for i, edge in enumerate(BUCKETS):
                    lines.append(f'{name}_bucket{_labels({**base, "le": _num(edge)})} {h[i]}')
                lines.append(f'{name}_bucket{_labels({**base, "le": "+Inf"})} {h[-1]}')
                lines.append(f'{name}_sum{_labels(base)} {round(h[-2], 6)}')
                lines.append(f'{name}_count{_labels(base)} {h[-1]}')
        return '\n'.join(lines) + '\n'


class _Timer:
    def __init__(self, metrics, name, labels):
        self.metrics, self.name, self.labels = metrics, name, labels

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.metrics.observe(self.name, time.monotonic() - self.start, self.labels)
        return False


def health_of(agent, healthy_for, now=None):
    """(ok, details) for /healthz: has the hosted Floorbit answered lately?"""
    now = time.time() if now is None else now
    last = getattr(agent, 'last_contact', 0) or 0
    # max(0, …): a clock that stepped back (NTP at boot) must not read as "from the future".
    age = max(0.0, now - last) if last else None
    ok = age is not None and age <= healthy_for
    details = {'ok': ok, 'link': 'websocket' if agent.link_up else ('poll' if ok else 'none'),
               'protocol': agent.protocol,
               'last_contact_seconds': None if age is None else round(age, 1)}
    if not ok:
        details['reason'] = ('the agent has not reached the hosted Floorbit yet' if age is None
                             else f'the hosted Floorbit last answered {age:.0f} s ago')
    return ok, details


def serve(metrics, agent, address, healthy_for, token=None, log=print):
    """Start the endpoint in a thread; returns the server (``.shutdown()`` stops it), or None when
    turned off or the address can't be used."""
    address = (address or '').strip()
    if not address or address.lower() == 'off':
        return None
    host, _, port = address.rpartition(':')
    try:
        port = int(port)
    except ValueError:
        log(f'[agent] FLOORBIT_AGENT_METRICS must be host:port or off, not "{address}"')
        return None

    class Handler(BaseHTTPRequestHandler):
        timeout = http_limits.IDLE_SECONDS

        def _send(self, status, body, kind):
            data = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # the name http.server asks for
            path = self.path.split('?', 1)[0]
            if path == '/healthz':
                # Never behind the token: local checks (Docker, keepalived, a curl) just ask. It
                # says only up or not, the link and how long ago.
                ok, details = health_of(agent, healthy_for)
                self._send(200 if ok else 503, json.dumps(details) + '\n', 'application/json')
                return
            if token:
                given = self.headers.get('Authorization', '')
                if not hmac.compare_digest(given.encode(), f'Bearer {token}'.encode()):
                    self._send(401, '{"error": "token required"}\n', 'application/json')
                    return
            if path == '/metrics':
                self._send(200, metrics.render(), 'text/plain; version=0.0.4; charset=utf-8')
            else:
                self._send(404, '{"error": "not found"}\n', 'application/json')

        def log_message(self, *args):   # a scrape every 15 s is not news
            pass

    try:
        server = http_limits.LimitedHTTPServer((host or '0.0.0.0', port), Handler)
    except OSError as exc:
        log(f'[agent] cannot serve metrics on {address}: {exc}')
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='agent-metrics', daemon=True).start()
    log(f'[agent] metrics on http://{address}/metrics')
    return server
