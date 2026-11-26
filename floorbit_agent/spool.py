"""A disk buffer for what the agent collects while the hosted Floorbit can't take it
(bitsandbricks-ch/floorbit#465).

Records are written in small files, at most one batch each, named by a counter so their order is
the order they came in. Replay sends the oldest file and removes it once the hosted side took it,
so a crash halfway sends one batch twice at most, and stable record IDs let the hosted side drop
the copy. The buffer has an age limit; past it the oldest files go first, counted. Its size counts
toward the agent's one disk budget (floorbit/disk_budget.py, #1485) as the kind ``buffer``: past
the budget the oldest files of every kind go first, and these with them.

It lives in the state volume, so it survives a restart of the agent.
"""
import json
import os
import re
import threading
import time
import weakref

from . import logs

MAX_AGE = 24 * 3600
_NAME = re.compile(r'^(\d{12})\.jsonl\Z')


class Spool:
    def __init__(self, folder, max_bytes=None, max_age=MAX_AGE, log=logs.say):
        self.folder = folder
        self.max_bytes = max_bytes
        self.max_age = max_age
        self.log = log
        self.dropped = 0          # records thrown away: too old, too much, or the disk was full
        self._lock = threading.Lock()
        self._failing = False
        self._trim_said = 0
        os.makedirs(folder, mode=0o700, exist_ok=True)
        files = self._files()
        self._next = (int(_NAME.match(files[-1]).group(1)) + 1) if files else 0
        # Sizes and counts, kept as files come and go, so /metrics doesn't list the folder.
        self._bytes = sum(self._size(f) for f in files)
        self._records = sum(self._count(f) for f in files)
        # A file half-written when the agent died is finished or left out; never read twice.
        for name in os.listdir(folder):
            if name.endswith('.tmp'):
                os.remove(os.path.join(folder, name))
        BUFFERS.add(self)

    def _files(self):
        return sorted(n for n in os.listdir(self.folder) if _NAME.match(n))

    def _size(self, name):
        try:
            return os.path.getsize(os.path.join(self.folder, name))
        except OSError:
            return 0

    def _count(self, name):
        try:
            with open(os.path.join(self.folder, name), 'rb') as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    @property
    def size(self):
        return self._bytes

    def __len__(self):
        return self._records

    def put(self, records):
        """Write one batch as one file. False when the disk took nothing (full, read-only): the
        caller counts them as dropped."""
        if not records:
            return True
        data = ''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in records).encode()
        with self._lock:
            name = f'{self._next:012d}.jsonl'
            self._next += 1
            path = os.path.join(self.folder, name)
            try:
                # Written aside and renamed: a reader never sees half a file.
                with open(path + '.tmp', 'wb') as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(path + '.tmp', path)
            except OSError as exc:
                with_tmp = path + '.tmp'
                if os.path.exists(with_tmp):
                    os.remove(with_tmp)
                self.dropped += len(records)
                if not self._failing:
                    self.log(f'[agent] cannot write the buffer in {self.folder}: {exc}; dropping what does not fit')
                self._failing = True
                return False
            if self._failing:
                self.log(f'[agent] the buffer in {self.folder} can be written again')
            self._failing = False
            self._bytes += len(data)
            self._records += len(records)
        self.trim()
        return True

    def oldest(self):
        """(name, records) of the oldest batch, or None when the buffer is empty."""
        with self._lock:
            files = self._files()
        for name in files:
            try:
                with open(os.path.join(self.folder, name), encoding='utf-8') as f:
                    return name, [json.loads(line) for line in f if line.strip()]
            except (OSError, ValueError) as exc:
                # A damaged file can't be sent; keeping it would block everything behind it.
                self.log(f'[agent] dropping a damaged buffer file {name}: {exc}')
                self.done(name, dropped=True)
        return None

    def done(self, name, dropped=False):
        """The batch went out (or can't): remove it."""
        path = os.path.join(self.folder, name)
        with self._lock:
            size, count = self._size(name), self._count(name)
            try:
                os.remove(path)
            except FileNotFoundError:
                return
            self._bytes -= size
            self._records -= count
            if dropped:
                self.dropped += count

    def trim(self, now=None):
        """Drop the oldest batches past the size or age limit. Returns how many records went."""
        now = time.time() if now is None else now
        gone = 0
        for name in self._files():
            path = os.path.join(self.folder, name)
            try:
                too_old = now - os.path.getmtime(path) > self.max_age
            except OSError:
                continue
            if not too_old and (self.max_bytes is None or self._bytes <= self.max_bytes):
                break
            before = self.dropped
            self.done(name, dropped=True)
            gone += self.dropped - before
        if gone and now - self._trim_said > 60:     # once a minute while it stays full
            self.log(f'[agent] the buffer in {self.folder} is past its limits: dropping the oldest records '
                     f'({self.dropped} so far)')
            self._trim_said = now
        return gone


class Buffers:
    """Every spool of this agent as one kind for the disk budget. Removing goes through the spool,
    so its sizes and counts stay right and the records count as dropped."""

    def __init__(self):
        self._spools = weakref.WeakSet()
        self._lock = threading.Lock()
        self._registered = False

    def add(self, spool):
        with self._lock:
            self._spools.add(spool)
            first, self._registered = not self._registered, True
        if first:
            from floorbit import disk_budget
            disk_budget.register('buffer', self)

    @property
    def path(self):
        spools = self._all()
        return spools[0].folder if spools else '.'

    def _all(self):
        with self._lock:
            return list(self._spools)

    def files(self):
        """[(folder/name, mtime, size)] of every spool's batches."""
        out = []
        for spool in self._all():
            with spool._lock:
                names = spool._files()
            for name in names:
                try:
                    st = os.stat(os.path.join(spool.folder, name))
                except OSError:
                    continue
                out.append((os.path.join(spool.folder, name), st.st_mtime, st.st_size))
        return out

    def remove(self, path):
        folder, name = os.path.split(path)
        for spool in self._all():
            if spool.folder == folder:
                spool.done(name, dropped=True)
                return


BUFFERS = Buffers()
