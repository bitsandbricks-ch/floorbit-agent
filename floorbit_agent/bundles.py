"""Plugin updates without a new agent image: signed bundles from the hosted Floorbit.

The hosted side lists, with the plugins' settings, a bundle for each plugin it runs (``bundles``).
This agent installs one only when all of these hold:

* its signature verifies against a key built into this image (floorbit/keys/plugins/);
  the hosted side holds no key, so a stolen hosted login can't make one;
* it is newer than what runs here (the image's own copy or an earlier bundle): never a downgrade;
* it needs no newer agent (``agent_api``) and wasn't rolled back here before;
* FLOORBIT_AGENT_PLUGIN_UPDATES allows it: ``auto`` (default), ``approve`` (waits for
  ``python -m floorbit_agent plugins approve <id>`` on this machine) or ``off``.

A plugin that fails to start, or reports an error in its first minutes on the new version, goes
back to the version it ran before, and that version is never tried again here.
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

PROBATION = 300          # seconds a new version must stay healthy
POLICIES = ('auto', 'approve', 'off')
MAX_EVENTS = 20
# What the hosted side offers becomes folder names here: only these shapes, so a stolen hosted
# login can't point an install (and its clean-up) at another folder ("../../...").
ID_RE = re.compile(r'^[a-z][a-z0-9-]{0,39}$')
VERSION_RE = re.compile(r'^[0-9][0-9A-Za-z.+-]{0,39}$')
FILE_RE = re.compile(r'^[a-z][a-z0-9-]{0,39}-[0-9][0-9A-Za-z.+-]{0,39}\.tar\.gz$')


def policy(env):
    value = str(env.get('FLOORBIT_AGENT_PLUGIN_UPDATES') or 'auto').strip().lower()
    return value if value in POLICIES else 'auto'


class Updater:
    def __init__(self, agent, registry, state_root, image_version, env=None, keys=None, log=print, clock=time.time):
        from floorbit.plugins import bundles
        self.b = bundles
        self.agent, self.registry, self.log, self.clock = agent, registry, log, clock
        self.env = os.environ if env is None else env
        self.policy = policy(self.env)
        self.keys = bundles.pinned_keys() if keys is None else keys
        self.image_version = str(image_version or '0')
        self.root = Path(state_root) / 'bundles'
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / 'state.json'
        self.approved_path = self.root / 'approved.json'
        self.builtin = {}        # plugin id -> the image's own Spec, to go back to
        # Plugins the image itself has. Others (from the Marketplace, docs/marketplace.md in Floorbit)
        # have versions of their own, so nothing here counts as newer than them.
        self.in_image = set(getattr(registry, 'specs', {}) or {})
        self.events = []         # the last rollouts, for the report and the hosted audit log
        self._lock = threading.RLock()
        self.state = self._read(self.state_path, {})

    # files ---------------------------------------------------------------------------------
    @staticmethod
    def _read(path, default):
        try:
            with open(path, encoding='utf-8') as f:
                value = json.load(f)
            return value if isinstance(value, type(default)) else default
        except (OSError, ValueError):
            return default

    def _save(self):
        tmp = self.state_path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def _entry(self, pid):
        return self.state.setdefault(pid, {'current': None, 'previous': None, 'bad': [], 'pending': None})

    # what runs -----------------------------------------------------------------------------
    def running_version(self, pid):
        current = (self.state.get(pid) or {}).get('current')
        return current or self.image_version_of(pid)

    def image_version_of(self, pid):
        return self.image_version if pid in self.in_image else '0'

    def _event(self, pid, what, version, detail=''):
        ev = {'at': int(self.clock()), 'plugin': pid, 'event': what, 'version': str(version), 'detail': detail[:300]}
        self.events = [*self.events, ev][-MAX_EVENTS:]
        self.log(f'[agent] plugin {pid} {version}: {what}{": " + detail if detail else ""}')

    def _load(self, pid, path, version):
        from floorbit.plugins import _load
        spec = _load(Path(path), 'builtin', self.image_version)
        if spec is None or spec.error or spec.id != pid:
            raise self.b.BundleError(spec.error if spec is not None and spec.error else 'not a plugin folder')
        return spec

    def _swap(self, host, pid, spec):
        """Put ``spec`` in place of what runs for ``pid``, restarting its agent part when it runs."""
        import sys
        if pid not in self.builtin and pid in self.registry.specs:
            self.builtin[pid] = self.registry.specs[pid]
        self.registry.specs[pid] = spec
        try:
            self.registry._register_settings()
        except Exception as exc:  # noqa: BLE001
            self.log(f'[agent] plugin {pid}: settings not registered: {exc}')
        sys.modules.pop(f"floorbit_agent_plugin_{pid.replace('-', '_')}", None)
        if host is None:
            return
        with host._lock:
            if spec.agent and not spec.error:
                host.specs[pid] = spec
            else:
                host.specs.pop(pid, None)
            run = host.running.get(pid)
            if run is not None:
                host._stop_one(pid, 'new version')
            if pid in host.specs and pid in host.wanted:
                host._start(pid, host.wanted[pid])

    def restore(self):
        """At start: the bundles installed before, checked again (a changed file drops it)."""
        with self._lock:
            for pid, entry in sorted(self.state.items()):
                version = entry.get('current')
                if not version:
                    continue
                path = self.root / pid / version
                try:
                    if not self.b.newer(version, self.image_version_of(pid)):
                        raise self.b.BundleError(f'the image already has {self.image_version}')
                    self.b.verify(path, self.keys, pid, version)
                    self._swap(None, pid, self._load(pid, path, version))
                except self.b.BundleError as exc:
                    self._event(pid, 'dropped', version, str(exc))
                    entry['current'] = None
            self._save()

    # offers --------------------------------------------------------------------------------
    def offer(self, host, offered):
        """``offered``: the hosted config's ``bundles``. Installs what may be installed."""
        if not isinstance(offered, dict):
            return
        with self._lock:
            approved = set(self._read(self.approved_path, []))
            changed = False
            for pid, b in sorted(offered.items()):
                if not isinstance(b, dict):
                    continue
                version = str(b.get('version') or '')
                if (not ID_RE.match(str(pid)) or not VERSION_RE.match(version) or '..' in version
                        or not FILE_RE.match(str(b.get('file') or ''))):
                    self.log(f'[agent] ignored a bundle offer with a bad id, version or file: {str(pid)[:60]!r}')
                    continue
                entry = self.state.get(pid) or {}
                if (not version or not self.b.newer(version, self.running_version(pid))
                        or version in (entry.get('bad') or [])):
                    if entry.get('pending') and entry['pending'].get('version') == version:
                        entry['pending'] = None
                        changed = True
                    continue
                if int(b.get('agent_api') or 1) > self._agent_api():
                    self._mark_pending(pid, b, 'needs a newer agent image')
                    changed = True
                    continue
                if not self.keys:
                    self._mark_pending(pid, b, 'no signing key in this agent image')
                    changed = True
                    continue
                if self.policy == 'off':
                    continue
                if self.policy == 'approve' and not ({pid, f'{pid}@{version}', '*'} & approved):
                    self._mark_pending(pid, b, 'waits for approval on the agent')
                    changed = True
                    continue
                self._install(host, pid, b)
                changed = True
            if approved:
                with contextlib.suppress(OSError):
                    self.approved_path.unlink()
            if changed:
                self._save()

    def _agent_api(self):
        from floorbit.plugins.agent import AGENT_API
        return AGENT_API

    def _mark_pending(self, pid, b, why):
        entry = self._entry(pid)
        pending = {'version': str(b.get('version')), 'why': why}
        if entry.get('pending') != pending:
            entry['pending'] = pending
            self._event(pid, 'waiting', b.get('version'), why)

    def _install(self, host, pid, b):
        version = str(b['version'])
        entry = self._entry(pid)
        final = self.root / pid / version
        tmp = self.root / pid / f'.{version}.new'
        try:
            name = str(b.get('file') or '')
            resp = self.agent.http.get(f'{self.agent.url}/api/agent/v1/bundles/{name}',
                                       headers=self.agent._headers(), timeout=60)
            if resp.status_code != 200:
                raise self.b.BundleError(f'the hosted Floorbit answered {resp.status_code}')
            data = resp.content
            if len(data) > self.b.MAX_BUNDLE:
                raise self.b.BundleError('bundle too large')
            if hashlib.sha256(data).hexdigest() != str(b.get('sha256')):
                raise self.b.BundleError('checksum differs from what the hosted side listed')
            shutil.rmtree(tmp, ignore_errors=True)
            self.b.unpack(data, tmp)
            doc = self.b.verify(tmp, self.keys, pid, version)
            if int(doc['agent_api']) > self._agent_api():
                raise self.b.BundleError('needs a newer agent image')
            spec = self._load(pid, tmp, version)
            shutil.rmtree(final, ignore_errors=True)
            os.replace(tmp, final)
            spec = self._load(pid, final, version)
        except Exception as exc:  # noqa: BLE001  # a bad offer must not stop the agent
            shutil.rmtree(tmp, ignore_errors=True)
            entry['pending'] = {'version': version, 'why': f'refused: {exc}'[:300]}
            self._event(pid, 'refused', version, str(exc))
            return
        entry.update(previous=entry.get('current'), current=version, pending=None,
                     since=int(self.clock()), probation=True)
        self._swap(host, pid, spec)
        self._event(pid, 'installed', version, f'was {entry["previous"] or self.image_version_of(pid)}')

    # probation -----------------------------------------------------------------------------
    def watch(self, host):
        """Roll back a new version that didn't start or reported an error during probation."""
        with self._lock:
            changed = False
            for pid, entry in sorted(self.state.items()):
                if not entry.get('probation'):
                    continue
                why = ''
                if host is not None and pid in host.failed:
                    why = host.failed[pid]
                elif host is not None and pid in host.running:
                    h = host.running[pid].ctx.health_.report()
                    if h.get('status') == 'error':
                        why = h.get('message') or 'reported an error'
                spec = self.registry.specs.get(pid)
                if not why and spec is not None and spec.error:
                    why = spec.error
                if why:
                    self._rollback(host, pid, entry, why)
                    changed = True
                elif self.clock() - int(entry.get('since') or 0) >= PROBATION:
                    entry['probation'] = False
                    self._event(pid, 'kept', entry['current'])
                    changed = True
            if changed:
                self._save()

    def _rollback(self, host, pid, entry, why):
        bad = entry['current']
        entry['bad'] = sorted(set(entry.get('bad') or []) | {bad})
        back = entry.get('previous')
        entry.update(current=back, previous=None, probation=False)
        spec = None
        if back:
            try:
                self.b.verify(self.root / pid / back, self.keys, pid, back)
                spec = self._load(pid, self.root / pid / back, back)
            except self.b.BundleError:
                entry['current'] = back = None
        if spec is None:
            spec = self.builtin.get(pid)
        if spec is not None:
            self._swap(host, pid, spec)
        elif host is not None:
            # A plugin only the bundle brought: nothing to go back to, so it stops.
            with host._lock:
                host._stop_one(pid, 'rolled back')
                host.specs.pop(pid, None)
            self.registry.specs.pop(pid, None)
        self._event(pid, 'rolled back', bad, f'{why} (now {back or self.image_version_of(pid)})')
        shutil.rmtree(self.root / pid / bad, ignore_errors=True)

    # report --------------------------------------------------------------------------------
    def report(self):
        with self._lock:
            plugins = {}
            for pid, entry in sorted(self.state.items()):
                plugins[pid] = {'version': entry.get('current') or self.image_version_of(pid),
                                'source': 'bundle' if entry.get('current') else 'image',
                                'pending': entry.get('pending'), 'probation': bool(entry.get('probation')),
                                'rolled_back': list(entry.get('bad') or [])}
            return {'policy': self.policy, 'image': self.image_version, 'keys': sorted(self.keys),
                    'plugins': plugins, 'events': list(self.events)}


# the command line: python -m floorbit_agent plugins [approve <id>|all] --------------------------
def cli(argv, state_root, out=print):
    root = Path(state_root) / 'bundles'
    state = Updater._read(root / 'state.json', {})
    if not argv:
        if not state:
            out('No plugin updates yet.')
        for pid, entry in sorted(state.items()):
            pending = entry.get('pending') or {}
            line = f"{pid}: {entry.get('current') or 'image'}"
            if pending:
                line += f" (offered {pending.get('version')}: {pending.get('why')})"
            if entry.get('bad'):
                line += f" rolled back: {', '.join(entry['bad'])}"
            out(line)
        return 0
    if argv[0] == 'approve' and len(argv) == 2:
        target = argv[1]
        names = ['*'] if target == 'all' else [target]
        if target != 'all' and target not in state:
            out(f'{target}: no update offered for it')
            return 1
        path = root / 'approved.json'
        approved = set(Updater._read(path, [])) | set(names)
        root.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(sorted(approved), f)
        out(f'Approved {target}; the agent installs it within a minute.')
        return 0
    out('usage: python -m floorbit_agent plugins [approve <plugin id>|all]')
    return 2
