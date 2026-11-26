"""Updates of the agent itself, offered by the hosted Floorbit (issue #98).

The hosted side names, with the plugins' settings, an agent bundle (``agent_update``: version,
file, sha256, image). This agent installs it only when:

* it was started by floorbit_agent_boot (the image's CMD), which can start it again on the new code;
* FLOORBIT_AGENT_UPDATES allows it: ``auto`` (default), ``approve`` (waits for
  ``python -m floorbit_agent_boot update approve`` on this machine) or ``off``;
* it is newer than what runs here and wasn't rolled back here before;
* it was built for this image, and its signature verifies against a key pinned in the image.

It then unpacks the bundle into ``state/agent/<version>/``, marks it current and on trial, and
stops the agent cleanly; floorbit_agent_boot starts it again on the new code and rolls it back
when it doesn't reach the hosted Floorbit (floorbit_agent_boot.py, "probation").
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import floorbit_agent_boot as boot

from . import logs

FILE_RE = re.compile(r'^floorbit-agent-[0-9][0-9A-Za-z.+-]{0,39}\.tar\.gz\Z')
IMAGE_RE = re.compile(r'^[0-9a-f]{16}\Z')
INSTALL_TRIES = 3             # failed installs of one version before it waits for the next offer


class SelfUpdater:
    def __init__(self, agent, state_root, env=None, home=None, keys=None, log=logs.say, clock=time.time):
        self.agent, self.log, self.clock = agent, log, clock
        self.env = os.environ if env is None else env
        self.root = Path(state_root)
        # The image's own files, not this bundle's: an update can't change what an update needs.
        self.home = Path(home) if home is not None else boot.HERE
        self.policy = boot.policy(self.env)
        self.keys = boot.pinned_keys(self.home) if keys is None else keys
        self.image = boot.image_id(self.home)
        self.image_version = boot.image_version(self.home)
        self.booted = self.env.get('FLOORBIT_AGENT_BOOT') == '1'
        self.version = None           # what runs; None: this package's VERSION
        self.offered = None           # what the hosted side offered last (plugin_host.pull)
        self.pending = None           # {'version', 'why'}: offered but not installed
        self.restart = False          # main() returns boot.RESTART once the agent stopped
        self._tries = {}              # version -> failed installs
        self._lock = threading.Lock()

    @property
    def running(self):
        from . import agent_version
        return self.version or agent_version()

    def _wait(self, version, why):
        pending = {'version': version, 'why': why}
        if self.pending != pending:
            self.pending = pending
            self.log(f'[agent] agent update {version}: {why}')

    def offer(self, offered):
        """``offered``: the hosted config's ``agent_update``, or nothing."""
        if not isinstance(offered, dict) or not offered:
            self.pending = None
            return False
        with self._lock:
            version = str(offered.get('version') or '')
            name = str(offered.get('file') or '')
            image = str(offered.get('image') or '')
            if not boot.VERSION_RE.match(version) or '..' in version or not FILE_RE.match(name):
                self.log('[agent] ignored an agent update offer with a bad version or file')
                return False
            st = boot.State(self.root)
            if not boot.newer(version, self.running) or self.restart:
                self.pending = None
                return False
            if version in st.data.get('bad', []):
                self._wait(version, 'rolled back here before')
                return False
            if not self.booted:
                self._wait(version, 'this agent was not started by floorbit_agent_boot: pull the new image')
                return False
            if not self.image or (IMAGE_RE.match(image) and image != self.image):
                self._wait(version, 'needs a new image')
                return False
            if not self.keys:
                self._wait(version, 'no signing key in this agent image')
                return False
            if self.policy == 'off':
                self._wait(version, 'agent updates are off here (FLOORBIT_AGENT_UPDATES)')
                return False
            if self.policy == 'approve' and not self._approved(st, version):
                self._wait(version, 'waits for approval on the agent')
                return False
            if self._tries.get(version, 0) >= INSTALL_TRIES:
                return False
            return self._install(st, version, name, offered)

    def _approved(self, st, version):
        try:
            with open(st.dir / 'approved.json', encoding='utf-8') as f:
                approved = json.load(f)
        except (OSError, ValueError):
            return False
        return isinstance(approved, list) and bool({'*', version} & {str(a) for a in approved})

    def _install(self, st, version, name, offered):
        final = st.folder(version)
        tmp = st.dir / f'.{version}.new'
        try:
            resp = self.agent.http.get(f'{self.agent.url}/api/agent/v1/agent-bundles/{name}',
                                       headers=self.agent._headers(), timeout=120)
            if resp.status_code != 200:
                raise boot.BundleError(f'the hosted Floorbit answered {resp.status_code}')
            data = resp.content
            if hashlib.sha256(data).hexdigest() != str(offered.get('sha256')):
                raise boot.BundleError('checksum differs from what the hosted side listed')
            shutil.rmtree(tmp, ignore_errors=True)
            boot.unpack(data, tmp)
            boot.verify(tmp, self.keys, version=version, image=self.image)
            shutil.rmtree(final, ignore_errors=True)
            os.replace(tmp, final)
        except Exception as exc:  # noqa: BLE001  # a bad offer must not stop the agent
            shutil.rmtree(tmp, ignore_errors=True)
            self._tries[version] = self._tries.get(version, 0) + 1
            self._wait(version, f'refused: {exc}'[:300])
            st.event('refused', version, str(exc))
            st.save()
            return False
        before = st.data.get('current')
        # Keep the one before only: older folders go.
        for old in st.dir.iterdir():
            if old.is_dir() and old.name not in {version, before} and not old.name.startswith('.'):
                shutil.rmtree(old, ignore_errors=True)
        st.data.update(current=version, previous=before, trial={'version': version, 'starts': 0},
                       pending=None)
        st.event('installed', version, f'was {before or self.image_version}; starting again')
        st.save()
        with contextlib.suppress(OSError):
            (st.dir / 'approved.json').unlink()
        self.pending = None
        self.restart = True
        self.agent.stop()
        return True

    def report(self):
        st = boot.State(self.root)
        events = st.data.get('events') or []
        return {'policy': self.policy, 'running': self.running, 'image': self.image_version,
                'image_id': self.image, 'booted': self.booted, 'keys': sorted(self.keys),
                'pending': self.pending, 'trial': bool(st.data.get('trial')),
                'rolled_back': list(st.data.get('bad') or []), 'events': events[-5:]}
