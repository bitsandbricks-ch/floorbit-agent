"""Syslog through the local agent (issue #2). See Floorbit's docs/agent.md, "Syslog".

A hosted Floorbit can't take syslog from the event network: the switches' UDP datagrams would have
to cross the internet, unencrypted and unauthenticated. So the agent listens instead
(the plugin "Syslog from the hall", turned on in Floorbit) and sends the lines on over its HTTPS connection,
in batches, with the address each came from and when it arrived. The hosted side checks those
senders against its allow-list as if they had sent there (floorbit/syslog.py, ``relay``).

Lines wait in memory for the next send, up to ``BACKLOG``; past that the newest are dropped and
counted, so a flood can't eat the machine's memory. While the hosted Floorbit can't be reached,
they go to the disk buffer (spool.py) instead, which survives a restart and is sent oldest first
once the hosted side is back, behind the live lines (floorbit#465).

Each line carries an ID made of where it came from and what it says, the same on every agent that
heard it, so the hosted side can keep one copy when two agents, or a replay, send it twice.

In a group of agents that splits its devices (floorbit#1441), devices log to every agent, and only
a device's owner sends its lines on. ``keep(sender)`` says which are this agent's; the others' lines
are held for ``HOLD`` seconds and sent after all when ``take_over()`` finds they became its own.
"""
import contextlib
import hashlib
import json
import socket
import threading
import time
from collections import deque

from . import logs

MAX_LINE = 2048          # as floorbit/syslog.py
BACKLOG = 20_000
RECEIVE_BUFFER = 8 * 1024 ** 2   # bytes asked of the kernel (capped at net.core.rmem_max)
BATCH = 2000             # the hosted side takes at most this many per post
BATCH_BYTES = 8 * 1024 ** 2   # and a post stays well under its 32 MB: 2000 long lines of emoji
                              # come to 47 MB as JSON, which it refused (413) or, gzipped, took as
                              # empty, losing them all
EVERY = 2.0              # seconds between sends while lines come in
HOLD = 60                # seconds another agent's lines are kept for a takeover
REPLAY = 5               # buffered batches per round once the hosted side is back: the backlog
                         # catches up at 5,000 lines a second without crowding out live lines


class TurnedOff(RuntimeError):
    """The hosted Floorbit has syslog turned off: buffering won't help."""


class Relay:
    def __init__(self, send, port, bind='0.0.0.0', backlog=BACKLOG, log=logs.say, spool=None, keep=None):
        """``send(lines, dropped)`` posts one batch; it raises on failure. ``spool``: a
        spool.Spool for the lines that can't be sent now; without it they wait in memory.
        ``keep(sender)``: False for a device another agent of the group owns (None: all)."""
        self.send = send
        self.keep = keep
        self.held = deque()      # other agents' lines, oldest first, for a takeover
        self.spool = spool
        self.replayed = 0
        self.port = port
        self.bind = bind
        self.backlog = backlog
        self.log = log
        self.lines = deque()
        self.dropped = 0
        self.sent = 0
        self.received = 0
        self._lock = threading.Lock()
        # One sender at a time: the last send on the way out raced the send thread, and both took
        # the same lines from the left ("pop from an empty deque", the backlog spilled twice).
        self._sending = threading.RLock()
        self._stop = threading.Event()
        self._server = None
        self._failing = False

    def receive(self, raw, sender, now=None):
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        raw = raw.strip('\x00\r\n ')[:MAX_LINE]
        if not raw:
            return
        with self._lock:
            self.received += 1
            # The newest go, not the oldest: what came first explains what came after.
            if len(self.lines) >= self.backlog:
                self.dropped += 1
                return
            line = {'raw': raw, 'from': sender, 'at': time.time() if now is None else now,
                    'id': line_id(sender, raw)}
            if self.keep is not None and not self.keep(sender):
                self._hold(line)
                return
            self.lines.append(line)

    def _hold(self, line):
        """Call with the lock held."""
        self.held.append(line)
        while self.held and (len(self.held) > self.backlog or self.held[0]['at'] < line['at'] - HOLD):
            self.held.popleft()

    def take_over(self, keep=None, now=None):
        """New owners from Floorbit: lines held from devices that are now this agent's go out, the
        last minute of them, so a failover loses none. Returns how many."""
        now = time.time() if now is None else now
        with self._lock:
            if keep is not None:
                self.keep = keep
            mine = [line for line in self.held if line['at'] >= now - HOLD
                    and (self.keep is None or self.keep(line['from']))]
            self.held = deque(line for line in self.held if line['at'] >= now - HOLD
                              and not (self.keep is None or self.keep(line['from'])))
            room = max(0, self.backlog - len(self.lines))
            self.dropped += max(0, len(mine) - room)
            self.lines.extend(mine[:room])
        return min(len(mine), room)

    @property
    def all_dropped(self):
        return self.dropped + (self.spool.dropped if self.spool is not None else 0)

    @property
    def buffered(self):
        return len(self.spool) if self.spool is not None else 0

    def _batch_size(self):
        """How many of the oldest lines make one post: up to BATCH, and BATCH_BYTES as JSON (at
        least one, which MAX_LINE keeps far below it). Call with the lock held."""
        size = 0
        for n in range(min(BATCH, len(self.lines))):
            size += len(json.dumps(self.lines[n])) + 1
            if size > BATCH_BYTES and n:
                return n
        return min(BATCH, len(self.lines))

    def spill(self):
        """Move what waits in memory to the disk buffer, oldest first, a batch per file."""
        if self.spool is None:
            return
        with self._sending:
            while True:
                with self._lock:
                    batch = [self.lines.popleft() for _ in range(self._batch_size())]
                if not batch:
                    return
                self.spool.put(batch)

    def flush_once(self):
        """Send what waits: the live lines first, then up to REPLAY batches from the disk buffer.
        Returns how many went; raises when a send fails (live lines then go to the disk buffer)."""
        try:
            total = self._send_live()
        except TurnedOff:
            raise
        except Exception:
            self.spill()
            raise
        return total + self._replay()

    def _replay(self):
        total = 0
        for _ in range(REPLAY if self.spool is not None else 0):
            found = self.spool.oldest()
            if found is None:
                break
            name, batch = found
            try:
                self.send(batch, self.all_dropped)
            except TurnedOff:
                self.spool.done(name, dropped=True)
                raise
            self.spool.done(name)
            self.replayed += len(batch)
            self.sent += len(batch)
            total += len(batch)
        return total

    def _send_live(self):
        with self._sending:
            return self._send_live_locked()

    def _send_live_locked(self):
        total = 0
        while True:
            with self._lock:
                batch = [self.lines[i] for i in range(self._batch_size())]
                dropped = self.all_dropped
            if not batch:
                return total
            try:
                self.send(batch, dropped)
            except TurnedOff:
                # Keeping them would only fill the backlog: the hosted side won't take them.
                with self._lock:
                    for _ in batch:
                        self.lines.popleft()
                raise
            with self._lock:
                # Only this thread takes from the left, so these are the lines just sent.
                for _ in batch:
                    self.lines.popleft()
            self.sent += len(batch)
            total += len(batch)

    def _flushing(self):
        while not self._stop.wait(EVERY):
            try:
                self.flush_once()
                if self._failing:
                    self.log(f'[agent] syslog is getting through again ({len(self.lines) + self.buffered} '
                             'lines waiting)')
                self._failing = False
            except Exception as exc:  # noqa: BLE001  # keep the lines, try again next round
                if not self._failing:
                    keeping = (f'keeping them on disk in {self.spool.folder}' if self.spool is not None
                               else f'keeping up to {self.backlog} lines')
                    self.log(f'[agent] could not send syslog: {exc}; {keeping}')
                self._failing = True

    def start(self):
        # One thread reading one socket with a big receive buffer: a thread per datagram (what
        # socketserver does) can't keep up with a burst, and the kernel drops what doesn't fit.
        family = socket.AF_INET6 if ':' in self.bind else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER)
        try:
            sock.bind((self.bind, self.port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.5)          # so stop() is noticed
        self._server = sock
        threading.Thread(target=self._receiving, args=(sock,), name='agent-syslog', daemon=True).start()
        threading.Thread(target=self._flushing, name='agent-syslog-send', daemon=True).start()
        self.log(f'[agent] listening for syslog on UDP {sock.getsockname()[1]}')
        return sock.getsockname()[1]

    def _receiving(self, sock):
        while not self._stop.is_set():
            try:
                data, sender = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return                # closed by stop()
            self.receive(data, sender[0])

    def stop(self, flush=True):
        self._stop.set()
        if self._server is not None:
            self._server.close()
            self._server = None
        if flush and self.lines:
            # On the way out (docker stop): one last try, so a restart doesn't lose the backlog.
            try:
                self._send_live()
            except Exception as exc:  # noqa: BLE001
                if self.spool is not None:
                    self.spill()
                    self.log(f'[agent] syslog lines kept on disk for the next start ({exc})')
                else:
                    self.log(f'[agent] {len(self.lines)} syslog lines could not be sent before stopping: {exc}')


def line_id(sender, raw):
    """The same for the same line from the same switch, whichever agent heard it. The hosted side
    keeps one copy of an ID within ten seconds of its time (floorbit#479): a copy from another
    agent or a batch sent again; the same text later is a real repeat."""
    return hashlib.sha256(f'{sender}\n{raw}'.encode()).hexdigest()[:24]
