"""VRRP virtual IPs with keepalived, an optional mode of the agent image (issue #3).

Two or more agents share virtual IPs: each VIP is held by one agent and moves to another within
about three seconds when that agent stops. With two VIPs and two agents, each holds one while both
are up (active/active), so a captive API or check-in page behind both VIPs keeps answering.

The agent writes keepalived's config and runs it as a child: when the agent stops, keepalived stops
and the VIPs move. keepalived runs no scripts (they would need root): it tells the agent about state
changes through a FIFO (``notify_fifo``), and the agent tells it about its health through a file
(``track_file``: 0 healthy, 1 not), so an agent that stops answering gives its VIPs away.

Off unless FLOORBIT_AGENT_VRRP_VIPS is set. Needs ``network_mode: host`` and ``cap_add: NET_ADMIN``
(compose.vrrp.yml).
"""
import contextlib
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import NamedTuple

STATES = ('MASTER', 'BACKUP', 'FAULT', 'STOP')
PREFERRED, OTHER = 150, 100        # priorities: the preferred holder wins while it is up
CHECK_EVERY = 2                    # seconds between health checks
_IFACE = re.compile(r'^[A-Za-z0-9_.:@-]{1,15}$')


class Settings(NamedTuple):
    vips: tuple            # ip_interface, one VRRP instance each
    interface: str
    router_id: int         # the first instance's virtual_router_id; the others count up
    prefer: frozenset      # positions (from 1) of the VIPs this agent holds while it is up
    peers: tuple           # unicast peers; empty: multicast


def _items(text):
    return [part.strip() for part in (text or '').replace(' ', ',').split(',') if part.strip()]


def settings_from(env):
    """Settings from FLOORBIT_AGENT_VRRP_*, None when VRRP is off. ValueError says what's wrong."""
    vips = _items(env.get('FLOORBIT_AGENT_VRRP_VIPS'))
    if not vips:
        return None
    try:
        parsed = tuple(ipaddress.ip_interface(v if '/' in v else f'{v}/32') for v in vips)
    except ValueError as exc:
        raise ValueError(f'FLOORBIT_AGENT_VRRP_VIPS: {exc}') from None
    if len(set(parsed)) != len(parsed):
        raise ValueError('FLOORBIT_AGENT_VRRP_VIPS lists an address twice.')
    interface = (env.get('FLOORBIT_AGENT_VRRP_INTERFACE') or '').strip()
    if not _IFACE.match(interface):
        raise ValueError('FLOORBIT_AGENT_VRRP_INTERFACE must name the network interface the VIPs live on, '
                         'like eth0.')
    try:
        router_id = int(env.get('FLOORBIT_AGENT_VRRP_ID') or 51)
    except ValueError:
        raise ValueError('FLOORBIT_AGENT_VRRP_ID must be a number from 1 to 255.') from None
    if not 1 <= router_id <= 256 - len(parsed):
        raise ValueError(f'FLOORBIT_AGENT_VRRP_ID must be from 1 to {256 - len(parsed)} '
                         f'(each VIP takes the next number).')
    try:
        prefer = frozenset(int(p) for p in _items(env.get('FLOORBIT_AGENT_VRRP_PREFER')))
    except ValueError:
        raise ValueError('FLOORBIT_AGENT_VRRP_PREFER lists VIPs by position: 1, 2, ...') from None
    if not prefer <= set(range(1, len(parsed) + 1)):
        raise ValueError(f'FLOORBIT_AGENT_VRRP_PREFER: there are only {len(parsed)} VIPs.')
    try:
        peers = tuple(ipaddress.ip_address(p) for p in _items(env.get('FLOORBIT_AGENT_VRRP_PEERS')))
    except ValueError as exc:
        raise ValueError(f'FLOORBIT_AGENT_VRRP_PEERS: {exc}') from None
    return Settings(parsed, interface, router_id, prefer, peers)


def instance(n):
    return f'floorbit_{n}'


def render(settings, fifo, health_file):
    """keepalived.conf for these settings."""
    lines = ['global_defs {', '  router_id floorbit-agent', f'  notify_fifo {fifo}', '}', '',
             'vrrp_track_file floorbit_agent {', f'  file {health_file}', '}']
    for n, vip in enumerate(settings.vips, start=1):
        lines += ['', f'vrrp_instance {instance(n)} {{',
                  # All start as BACKUP: the priorities decide, so a restarted agent doesn't grab
                  # a VIP the other one holds before it has heard the other's adverts.
                  '  state BACKUP',
                  f'  interface {settings.interface}',
                  f'  virtual_router_id {settings.router_id + n - 1}',
                  f'  priority {PREFERRED if n in settings.prefer else OTHER}',
                  '  advert_int 1',
                  # Taking a VIP back after a restart waits a little, so a flapping agent can't
                  # move it back and forth.
                  '  preempt_delay 10',
                  '  virtual_ipaddress {', f'    {vip.with_prefixlen}', '  }',
                  '  track_file {', '    floorbit_agent weight 0', '  }']
        if settings.peers:
            lines += ['  unicast_peer {'] + [f'    {p}' for p in settings.peers] + ['  }']
        lines.append('}')
    return '\n'.join(lines) + '\n'


def parse_fifo_line(line):
    """('floorbit_1', 'MASTER') from keepalived's ``INSTANCE "floorbit_1" MASTER 150``, else None."""
    m = re.match(r'^INSTANCE "([^"]+)" (\w+)', line.strip())
    return (m.group(1), m.group(2)) if m and m.group(2) in STATES else None


class Keepalived:
    """keepalived as the agent's child, with its state in the agent's /metrics."""

    def __init__(self, settings, metrics, log=print, binary=None, healthy=None):
        self.settings = settings
        self.metrics = metrics
        self.log = log
        self.binary = binary or os.environ.get('FLOORBIT_AGENT_KEEPALIVED') or 'keepalived'
        self.healthy = healthy or (lambda: True)
        self.states = {instance(n): 'BACKUP' for n in range(1, len(settings.vips) + 1)}
        self.vip_of = {instance(n): str(v.ip) for n, v in enumerate(settings.vips, start=1)}
        self.proc = None
        self.dir = None
        self._was_healthy = None
        self._stop = threading.Event()
        metrics.collect(self._collect)

    def _collect(self, m):
        m.set('floorbit_agent_vrrp_running', int(self.proc is not None and self.proc.poll() is None))
        for name, state in self.states.items():
            m.set('floorbit_agent_vrrp_master', int(state == 'MASTER'), {'vip': self.vip_of[name]})

    def holds(self):
        """The VIPs this agent holds now."""
        return [self.vip_of[name] for name, state in self.states.items() if state == 'MASTER']

    def start(self):
        """Write the config and start keepalived. Raises RuntimeError with what to do."""
        binary = shutil.which(self.binary)
        if not binary:
            raise RuntimeError(f'VRRP is on (FLOORBIT_AGENT_VRRP_VIPS) but there is no {self.binary}: '
                               'use the floorbit-agent image, or install keepalived.')
        # A fresh folder each start: no FIFO or config left over from a run that died.
        self.dir = tempfile.mkdtemp(prefix='floorbit-vrrp-')
        fifo, health, conf = (os.path.join(self.dir, n) for n in ('state.fifo', 'healthy', 'keepalived.conf'))
        os.mkfifo(fifo, 0o600)
        self._write_health(True)
        with open(conf, 'w', encoding='utf-8') as f:
            f.write(render(self.settings, fifo, health))
        # O_RDWR: the open doesn't wait for keepalived, and reads never see an end of file when it
        # restarts its VRRP child.
        fd = os.open(fifo, os.O_RDWR)
        threading.Thread(target=self._read_fifo, args=(fd,), daemon=True, name='vrrp-fifo').start()
        try:
            self.proc = subprocess.Popen(
                [binary, '--dont-fork', '--log-console', '--vrrp', '--use-file', conf,
                 '--pid', os.path.join(self.dir, 'keepalived.pid'),
                 '--vrrp_pid', os.path.join(self.dir, 'vrrp.pid')],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except PermissionError as exc:
            # The image's keepalived carries the network capabilities; without cap_add the kernel
            # won't run it.
            raise RuntimeError('keepalived may not run: give the container cap_add NET_ADMIN '
                               f'(compose.vrrp.yml) ({exc})') from exc
        threading.Thread(target=self._read_log, daemon=True, name='vrrp-log').start()
        threading.Thread(target=self._watch, daemon=True, name='vrrp-watch').start()
        vips = ', '.join(f'{v.ip}{" (preferred)" if n in self.settings.prefer else ""}'
                         for n, v in enumerate(self.settings.vips, start=1))
        self.log(f'[vrrp] keepalived started on {self.settings.interface} for {vips}')

    def _read_fifo(self, fd):
        with open(fd, encoding='utf-8', errors='replace') as f:
            for line in f:
                found = parse_fifo_line(line)
                if found and found[0] in self.states and self.states[found[0]] != found[1]:
                    self.states[found[0]] = found[1]
                    self.log(f'[vrrp] {self.vip_of[found[0]]}: {found[1]}')

    def _read_log(self):
        # keepalived's own lines, kept for when it refuses to start: it says why.
        for line in self.proc.stdout:
            if any(word in line for word in ('ERROR', 'WARNING', 'Non-existent', 'Terminating', 'permission')):
                self.log(f'[vrrp] keepalived: {line.strip()}')

    def _write_health(self, ok):
        if ok == self._was_healthy:
            return
        path = os.path.join(self.dir, 'healthy')
        # Written whole and renamed: keepalived never reads half a file.
        with open(path + '.new', 'w', encoding='utf-8') as f:
            f.write('0\n' if ok else '1\n')
        os.replace(path + '.new', path)
        if self._was_healthy is not None:
            self.log('[vrrp] healthy again: taking part again' if ok
                     else '[vrrp] not healthy: giving the VIPs away')
        self._was_healthy = ok

    def _watch(self):
        said = False
        while not self._stop.wait(CHECK_EVERY):
            if self.proc.poll() is not None:
                if not said:
                    self.log(f'[vrrp] keepalived stopped (exit code {self.proc.returncode}): '
                             'this agent holds no VIP now')
                    self.states = dict.fromkeys(self.states, 'STOP')
                    said = True
                continue
            try:
                ok = bool(self.healthy())
            except Exception:  # noqa: BLE001  # a check that breaks counts as not healthy
                ok = False
            self._write_health(ok)

    def stop(self):
        """Stop keepalived: it sends priority 0 on the way out, so the others take over at once."""
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)


def local_check(address, timeout=2):
    """A health check: the agent's own /healthz answers at all (200 or 503). It doesn't ask the
    hosted Floorbit: a local VIP must stay up while the uplink is away."""
    import http.client

    host, _, port = address.rpartition(':')
    host = '127.0.0.1' if host in ('', '0.0.0.0', '::', '[::]') else host.strip('[]')

    def check():
        conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
        with contextlib.closing(conn):
            conn.request('GET', '/healthz')
            return conn.getresponse().status in (200, 503)
    return check


def wait_for_state(keepalived, state, seconds=10):
    """For tests and scripts/vrrp_check.py: True once every instance reached ``state``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if all(s == state for s in keepalived.states.values()):
            return True
        time.sleep(0.1)
    return False
