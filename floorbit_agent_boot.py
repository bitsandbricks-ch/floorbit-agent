"""Starts the agent from its newest signed update, or from the image's own code (issue #98).

The image runs ``python -m floorbit_agent_boot``. Before anything of the agent or Floorbit is
imported, this picks the code to run:

* ``state/agent/current``: an agent bundle the agent installed (self_update.py), used only when it
  is signed with a key pinned in this image (floorbit_agent/keys/), is newer than the image, was
  built for this image (``image``: the same Dockerfile and Floorbit requirements) and was never
  rolled back here;
* else the image's own code in /app.

So no Docker access is needed: after an install the agent stops cleanly and this starts the same
process again (``os.execv``, same PID). A new version is on probation: it must reach the hosted
Floorbit within PROBATION seconds of starting, and start at most MAX_STARTS times without doing so,
else this goes back to the version before (or the image's) and never tries it again here.

This file is never replaced by an update: what decides whether an update is good stays the image's.
It uses only the standard library and ``cryptography``, so it can check a bundle before importing it.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE_ID = 'floorbit-agent'
SIGFILE = 'SIGNATURE.json'
RESTART = 75                 # floorbit_agent.main returns this after installing an update
PROBATION = 300              # seconds a new version has to reach the hosted Floorbit
STEADY = 60                  # and stay up this long after it did
MAX_STARTS = 2               # starts without reaching it (a crash, a restarted container)
STOP_GRACE = 60              # seconds a stop asked by the watchdog may take before execv anyway
SKIP = {'__pycache__', '.pytest_cache'}
VERSION_RE = re.compile(r'^[0-9][0-9A-Za-z.+-]{0,39}\Z')
POLICIES = ('auto', 'approve', 'off')
# What a bundle carries, at the top of its folder; nothing else is put on the path.
PARTS = ('floorbit_agent', 'floorbit', 'auth', 'integrations', 'VERSION')


class BundleError(Exception):
    """An agent bundle that must not run here."""


# where things are ------------------------------------------------------------------------------
def state_root(env=None):
    env = os.environ if env is None else env
    return Path(env.get('FLOORBIT_AGENT_STATE') or os.path.dirname(os.path.abspath(
        env.get('FLOORBIT_DATA_FILE') or 'agent-data.json')))


def heartbeat_file(env=None):
    env = os.environ if env is None else env
    return Path(env.get('FLOORBIT_AGENT_HEARTBEAT') or Path(os.path.dirname(os.path.abspath(
        env.get('FLOORBIT_DATA_FILE') or 'agent-data.json'))) / 'agent-heartbeat')


def policy(env=None):
    env = os.environ if env is None else env
    value = str(env.get('FLOORBIT_AGENT_UPDATES') or 'auto').strip().lower()
    return value if value in POLICIES else 'auto'


def image_version(home=HERE):
    path = Path(home) / 'floorbit_agent' / 'VERSION'
    try:
        return path.read_text().strip() or '0'
    except OSError:
        return '0'


def image_id(home=HERE):
    """What this image was built from (Dockerfile writes it); '' outside an image: no updates."""
    try:
        return (Path(home) / 'IMAGE_ID').read_text().strip()
    except OSError:
        return ''


def compute_image_id(dockerfile: str, requirements: str) -> str:
    """The same for every image built from this Dockerfile and these Floorbit requirements, so a
    bundle built at release time knows which images it runs on (comments and blank lines don't count)."""
    def lines(text):
        return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith('#')]
    doc = json.dumps({'dockerfile': lines(dockerfile), 'requirements': lines(requirements)},
                     sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(doc.encode()).hexdigest()[:16]


# versions --------------------------------------------------------------------------------------
def version_key(text):
    return tuple(int(p) for p in re.findall(r'\d+', str(text))[:3]) or (0,)


def newer(a, b):
    x, y = version_key(a), version_key(b)
    n = max(len(x), len(y))
    return x + (0,) * (n - len(x)) > y + (0,) * (n - len(y))


# signatures ------------------------------------------------------------------------------------
def _crypto():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    return serialization, Ed25519PublicKey


def key_id(pub) -> str:
    serialization, _ = _crypto()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def load_public(text: str):
    serialization, pub_cls = _crypto()
    text = text.strip()
    if text.startswith('-----BEGIN'):
        key = serialization.load_pem_public_key(text.encode())
    else:
        key = pub_cls.from_public_bytes(base64.b64decode(text))
    if not isinstance(key, pub_cls):
        raise ValueError('not an Ed25519 public key')
    return key


def pinned_keys(home=HERE) -> dict:
    """{key id: key} pinned in floorbit_agent/keys/. Empty: no update is ever installed."""
    out = {}
    folder = Path(home) / 'floorbit_agent' / 'keys'
    if folder.is_dir():
        for f in sorted(folder.glob('*.pub')):
            try:
                k = load_public(f.read_text())
            except (ValueError, TypeError):
                continue
            out[key_id(k)] = k
    return out


def file_hashes(root: Path) -> dict:
    out = {}
    root = Path(root)
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if any(part in SKIP for part in rel.parts) or rel.suffix == '.pyc' or rel.as_posix() == SIGFILE:
            continue
        if p.is_symlink():
            raise BundleError(f'symlinks are not allowed in a bundle ({rel})')
        if rel.parts[0] not in PARTS:
            raise BundleError(f'{rel.parts[0]} is not part of an agent bundle')
        if p.is_file():
            out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def payload(doc) -> bytes:
    return json.dumps({'id': doc['id'], 'version': doc['version'], 'image': doc['image'],
                       'protocol': doc['protocol'], 'files': doc['files']},
                      sort_keys=True, separators=(',', ':')).encode()


def verify(root: Path, keys: dict, version=None, image=None) -> dict:
    """The signature document when ``root`` is exactly what a pinned key signed; else BundleError."""
    from cryptography.exceptions import InvalidSignature
    sigfile = Path(root) / SIGFILE
    if not sigfile.is_file():
        raise BundleError('not signed')
    try:
        doc = json.loads(sigfile.read_text(encoding='utf-8'))
        doc = {'id': str(doc['id']), 'version': str(doc['version']), 'image': str(doc['image']),
               'protocol': int(doc['protocol']), 'files': dict(doc['files']), 'key_id': str(doc['key_id']),
               'signature': str(doc['signature'])}
    except (ValueError, KeyError, TypeError) as exc:
        raise BundleError(f'unreadable signature: {exc}') from None
    pub = keys.get(doc['key_id'])
    if pub is None:
        raise BundleError(f"signed with key {doc['key_id']}, which this image does not trust")
    try:
        pub.verify(base64.b64decode(doc['signature']), payload(doc))
    except (InvalidSignature, ValueError, TypeError):
        raise BundleError('bad signature') from None
    if doc['id'] != BUNDLE_ID:
        raise BundleError(f"signed for {doc['id']}, not {BUNDLE_ID}")
    if version is not None and doc['version'] != str(version):
        raise BundleError(f"signed as version {doc['version']}, not {version}")
    if image is not None and doc['image'] != image:
        raise BundleError('built for another agent image: needs a new image')
    if file_hashes(root) != doc['files']:
        raise BundleError('the files differ from what was signed')
    return doc


# packing ---------------------------------------------------------------------------------------
MAX_BUNDLE = 32 * 1024 * 1024    # packed; the agent with Floorbit's Python parts is about 1 MB
MAX_FILES = 3000


def pack(root: Path) -> bytes:
    """The folder as a .tar.gz with fixed times and owners, so the same folder packs the same."""
    import io
    import tarfile
    buf = io.BytesIO()
    names = [*sorted(file_hashes(root)), SIGFILE]
    with tarfile.open(fileobj=buf, mode='w:gz', format=tarfile.PAX_FORMAT) as tar:
        for name in names:
            data = (Path(root) / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(data: bytes, dest: Path) -> None:
    """Regular files only, inside ``dest``; anything else refuses the whole bundle."""
    import io
    import tarfile
    if len(data) > MAX_BUNDLE:
        raise BundleError('bundle too large')
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            total = 0
            for count, m in enumerate(tar, 1):
                parts = Path(m.name).parts
                if count > MAX_FILES:
                    raise BundleError('too many files in the bundle')
                if not m.isfile() or Path(m.name).is_absolute() or '..' in parts or not parts:
                    raise BundleError(f'refused entry in the bundle: {m.name}')
                if parts[0] not in PARTS and m.name != SIGFILE:
                    raise BundleError(f'{parts[0]} is not part of an agent bundle')
                total += m.size
                if total > 4 * MAX_BUNDLE:
                    raise BundleError('bundle too large')
                target = dest.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(m).read())
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BundleError(f'unreadable bundle: {exc}') from None


# state -----------------------------------------------------------------------------------------
class State:
    """state/agent/state.json: which update runs, the one before, the ones rolled back, the trial."""

    def __init__(self, root):
        self.dir = Path(root) / 'agent'
        self.path = self.dir / 'state.json'
        self.data = self._read()

    def _read(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                value = json.load(f)
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        value.setdefault('current', None)
        value.setdefault('previous', None)
        value.setdefault('bad', [])
        value.setdefault('trial', None)
        value.setdefault('events', [])
        return value

    def reload(self):
        self.data = self._read()

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as exc:
            say(f'[agent] could not save the update state: {exc}')
            tmp.unlink(missing_ok=True)

    def event(self, what, version, detail=''):
        ev = {'at': int(time.time()), 'event': what, 'version': str(version), 'detail': str(detail)[:300]}
        self.data['events'] = [*self.data.get('events', []), ev][-20:]
        say(f'[agent] agent update {version}: {what}{": " + detail if detail else ""}')

    def folder(self, version):
        return self.dir / str(version)

    def roll_back(self, why):
        """Mark the version on trial (or current) bad and go back to the one before."""
        bad = (self.data.get('trial') or {}).get('version') or self.data.get('current')
        if not bad:
            return
        self.data['bad'] = sorted({*self.data.get('bad', []), bad})
        back = self.data.get('previous')
        self.data.update(current=back if back != bad else None, previous=None, trial=None)
        self.event('rolled back', bad, f"{why} (now {self.data['current'] or 'the image'})")


def say(text):
    print(text, file=sys.stderr, flush=True)


# picking the code ------------------------------------------------------------------------------
def choose(root, home=HERE, keys=None, now=None):
    """The folder to put first on the path, or None for the image's own code. Saves the state."""
    now = time.time() if now is None else now
    st = State(root)
    keys = pinned_keys(home) if keys is None else keys
    ident = image_id(home)
    while True:
        version = st.data.get('current')
        if not version:
            st.data['trial'] = None
            st.save()
            return None
        why = ''
        if not VERSION_RE.match(str(version)) or '..' in str(version):
            why = 'not a version'
        elif version in st.data.get('bad', []):
            why = 'rolled back here before'
        elif not newer(version, image_version(home)):
            why = f'the image already has {image_version(home)}'
        elif not ident:
            why = 'this image does not say what it was built from'
        else:
            try:
                verify(st.folder(version), keys, version=version, image=ident)
            except BundleError as exc:
                why = str(exc)
        trial = st.data.get('trial')
        if not why and trial and trial.get('version') == version:
            if int(trial.get('starts') or 0) >= MAX_STARTS:
                st.roll_back(f'did not reach the hosted Floorbit in {MAX_STARTS} starts')
                continue
            trial['starts'] = int(trial.get('starts') or 0) + 1
            trial['started'] = now
        if why:
            if why.startswith('the image already has'):
                # A newer image came: its own code wins, and the update isn't bad, only old.
                st.event('dropped', version, why)
                st.data.update(current=None, previous=None, trial=None)
            else:
                st.data['trial'] = {'version': version}
                st.roll_back(why)
            continue
        st.save()
        return st.folder(version)


# probation -------------------------------------------------------------------------------------
class Watchdog(threading.Thread):
    """While a new version is on trial: keep it once it reached the hosted Floorbit and stayed up,
    roll it back when it doesn't within PROBATION."""

    def __init__(self, root, env=None, clock=time.time, stop=None, sleep=5):
        super().__init__(name='update-watchdog', daemon=True)
        self.root, self.env, self.clock, self.sleep = Path(root), env, clock, sleep
        self.started = clock()
        self.stop_agent = stop or (lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.done = threading.Event()
        self.outcome = None

    def check(self):
        """'kept', 'rolled back' or None (still on trial)."""
        st = State(self.root)
        trial = st.data.get('trial')
        if not trial:
            return 'kept'
        now = self.clock()
        try:
            beat = os.path.getmtime(heartbeat_file(self.env))
        except OSError:
            beat = 0
        if beat > self.started and now - self.started >= STEADY:
            st.data['trial'] = None
            st.event('kept', trial.get('version'), 'reached the hosted Floorbit')
            st.save()
            return 'kept'
        if now - self.started >= PROBATION:
            st.roll_back(f'did not reach the hosted Floorbit in {PROBATION // 60} minutes')
            st.save()
            return 'rolled back'
        return None

    def run(self):
        while not self.done.is_set():
            self.outcome = self.check()
            if self.outcome == 'kept':
                return
            if self.outcome == 'rolled back':
                self.stop_agent()
                # A stop that hangs must not keep a bad version running.
                if not self.done.wait(STOP_GRACE):
                    restart()
                return
            self.done.wait(self.sleep)


def restart():
    say('[agent] starting again')
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, '-m', 'floorbit_agent_boot', *sys.argv[1:]])


# the command line: python -m floorbit_agent_boot update [approve [<version>] | retry] -----------
def update_cli(argv, root, out=print, env=None):
    st = State(root)
    approved_path = st.dir / 'approved.json'
    if not argv:
        out(f'Agent updates: {policy(env)}. Image {image_version()}, '
            f"running {st.data.get('current') or 'the image'}.")
        pending = st.data.get('pending')
        if pending:
            out(f"Offered {pending.get('version')}: {pending.get('why')}")
        if st.data.get('bad'):
            out(f"Rolled back here: {', '.join(st.data['bad'])}")
        for ev in st.data.get('events', [])[-5:]:
            out(f"  {time.strftime('%d.%m.%Y %H:%M', time.localtime(ev['at']))} {ev['version']}: {ev['event']}"
                f"{' (' + ev['detail'] + ')' if ev.get('detail') else ''}")
        return 0
    if argv[0] == 'approve' and len(argv) <= 2:
        target = argv[1] if len(argv) == 2 else '*'
        if target != '*' and not VERSION_RE.match(target):
            out(f'{target}: not a version')
            return 2
        st.dir.mkdir(parents=True, exist_ok=True)
        with open(approved_path, 'w', encoding='utf-8') as f:
            json.dump([target], f)
        out(f"Approved {'the next update' if target == '*' else target}; the agent installs it within a minute.")
        return 0
    if argv[0] == 'retry' and len(argv) == 1:
        st.data['bad'] = []
        st.save()
        out('Versions rolled back before may be tried again.')
        return 0
    out('usage: python -m floorbit_agent_boot update [approve [<version>] | retry]')
    return 2


def main(argv=None, home=HERE):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ['image-id'] and len(argv) == 3:
        print(compute_image_id(Path(argv[1]).read_text(), Path(argv[2]).read_text()))
        return 0
    os.environ.setdefault('FLOORBIT_DATA_FILE', os.path.abspath('agent-data.json'))
    root = state_root()
    if argv[:1] == ['update']:
        return update_cli(argv[1:], root)
    running = not argv or argv[0] == 'run'
    folder = None
    if running:
        try:
            folder = choose(root, home)
        except Exception as exc:  # noqa: BLE001  # whatever goes wrong here, the image's code runs
            say(f'[agent] could not check the agent updates, running the image: {exc}')
    if folder is not None:
        sys.path.insert(0, str(folder))
    os.environ['FLOORBIT_AGENT_BOOT'] = '1'
    watchdog = None
    st = State(root)
    if running and folder is not None and st.data.get('trial'):
        watchdog = Watchdog(root)
        watchdog.start()
    try:
        import floorbit_agent
        rc = floorbit_agent.main(argv)
    except Exception as exc:
        st.reload()
        # Only a version on trial goes back: one that ran fine before gets the container's restart.
        if folder is None or not st.data.get('trial'):
            raise
        st.roll_back(f'stopped with an error: {exc!r}'[:300])
        st.save()
        rc = RESTART
    finally:
        if watchdog is not None:
            watchdog.done.set()
    if watchdog is not None and watchdog.outcome == 'rolled back':
        rc = RESTART
    if running and rc == RESTART:
        restart()
    return rc


if __name__ == '__main__':
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
