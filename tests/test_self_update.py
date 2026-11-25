"""Updates of the agent itself (issue #98): signed bundles, the boot loader's probation and rollback,
and the installer. No network: the hosted side is a fake."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import floorbit_agent_boot as boot
from floorbit_agent import self_update

ROOT = Path(__file__).resolve().parent.parent
IMAGE = 'abcdef0123456789'


def _script():
    spec = importlib.util.spec_from_file_location('agent_bundle_script', ROOT / 'scripts' / 'agent_bundle.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


script = _script()


@pytest.fixture(scope='module')
def keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return pem, pub


def image_home(tmp_path, pub, version='1.0.0', main='return 0'):
    """A folder laid out like /app: the image's agent, its keys, VERSION and IMAGE_ID."""
    home = tmp_path / 'app'
    pkg = home / 'floorbit_agent'
    (pkg / 'keys').mkdir(parents=True)
    (pkg / 'keys' / 'agent.pub').write_text(pub)
    (pkg / 'VERSION').write_text(version)
    (pkg / '__init__.py').write_text(f"def main(argv):\n    print('image', flush=True)\n    {main}\n")
    (home / 'IMAGE_ID').write_text(IMAGE)
    return home


def make_bundle(folder, pem, version, image=IMAGE, main="print('bundle', flush=True)\n    return 0"):
    pkg = folder / 'floorbit_agent'
    pkg.mkdir(parents=True)
    (pkg / '__init__.py').write_text(f'def main(argv):\n    {main}\n')
    (pkg / 'VERSION').write_text(version)
    (folder / 'VERSION').write_text('9.9.9')   # Floorbit's
    script.sign(folder, pem, version, image, 2)
    return folder


def install(root, pem, version, **kw):
    """A bundle as the installer leaves it: unpacked, current and on trial."""
    st = boot.State(root)
    make_bundle(st.folder(version), pem, version, **kw)
    st.data.update(current=version, trial={'version': version, 'starts': 0})
    st.save()


# signing ---------------------------------------------------------------------------------------
def test_a_signed_bundle_verifies_and_a_changed_one_does_not(tmp_path, keypair):
    pem, pub = keypair
    keys = {boot.key_id(boot.load_public(pub)): boot.load_public(pub)}
    folder = make_bundle(tmp_path / 'b', pem, '1.1.0')
    assert boot.verify(folder, keys, version='1.1.0', image=IMAGE)['version'] == '1.1.0'
    with pytest.raises(boot.BundleError, match='another agent image'):
        boot.verify(folder, keys, image='0000000000000000')
    (folder / 'floorbit_agent' / '__init__.py').write_text('def main(argv):\n    return 1\n')
    with pytest.raises(boot.BundleError, match='differ'):
        boot.verify(folder, keys)
    with pytest.raises(boot.BundleError, match='does not trust'):
        boot.verify(folder, {})


def test_pack_and_unpack_keep_the_bundle_and_refuse_strangers(tmp_path, keypair):
    pem, pub = keypair
    keys = {boot.key_id(boot.load_public(pub)): boot.load_public(pub)}
    data = boot.pack(make_bundle(tmp_path / 'b', pem, '1.1.0'))
    boot.unpack(data, tmp_path / 'out')
    boot.verify(tmp_path / 'out', keys, version='1.1.0', image=IMAGE)
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        info = tarfile.TarInfo('etc/passwd')
        info.size = 1
        tar.addfile(info, io.BytesIO(b'x'))
    with pytest.raises(boot.BundleError, match='not part of an agent bundle'):
        boot.unpack(buf.getvalue(), tmp_path / 'bad')


def test_the_image_id_ignores_comments_but_not_dependencies():
    a = boot.compute_image_id('FROM python:3.11-slim\n# a note\nRUN x\n', 'requests>=2\n')
    assert a == boot.compute_image_id('# other note\nFROM python:3.11-slim\n\nRUN x\n', 'requests>=2\n')
    assert a != boot.compute_image_id('FROM python:3.11-slim\nRUN x\n', 'requests>=2\nflask\n')
    assert a != boot.compute_image_id('FROM python:3.12-slim\nRUN x\n', 'requests>=2\n')


def test_the_pinned_agent_key_is_the_release_key():
    assert list(boot.pinned_keys(ROOT)) == ['1687a3ed9b528f3b']


# picking the code ------------------------------------------------------------------------------
def test_the_image_runs_when_nothing_was_installed(tmp_path, keypair):
    home = image_home(tmp_path, keypair[1])
    assert boot.choose(tmp_path / 'state', home) is None


def test_an_installed_update_runs_and_counts_its_starts(tmp_path, keypair):
    pem, pub = keypair
    home, root = image_home(tmp_path, pub), tmp_path / 'state'
    install(root, pem, '1.1.0')
    assert boot.choose(root, home) == boot.State(root).folder('1.1.0')
    assert boot.State(root).data['trial']['starts'] == 1


def test_an_update_that_never_reaches_floorbit_goes_back_after_two_starts(tmp_path, keypair):
    pem, pub = keypair
    home, root = image_home(tmp_path, pub), tmp_path / 'state'
    install(root, pem, '1.1.0')
    boot.choose(root, home)
    boot.choose(root, home)
    assert boot.choose(root, home) is None
    st = boot.State(root)
    assert st.data['bad'] == ['1.1.0'] and st.data['current'] is None and st.data['trial'] is None


def test_a_bundle_changed_on_disk_or_for_another_image_never_runs(tmp_path, keypair):
    pem, pub = keypair
    home, root = image_home(tmp_path, pub), tmp_path / 'state'
    install(root, pem, '1.1.0', image='0000000000000000')
    assert boot.choose(root, home) is None
    assert boot.State(root).data['bad'] == ['1.1.0']
    install(root, pem, '1.2.0')
    (boot.State(root).folder('1.2.0') / 'floorbit_agent' / 'extra.py').write_text('x = 1\n')
    assert boot.choose(root, home) is None
    assert boot.State(root).data['bad'] == ['1.1.0', '1.2.0']


def test_a_rolled_back_update_goes_to_the_one_before(tmp_path, keypair):
    pem, pub = keypair
    home, root = image_home(tmp_path, pub), tmp_path / 'state'
    install(root, pem, '1.1.0')
    st = boot.State(root)
    st.data['trial'] = None
    st.save()
    install(root, pem, '1.2.0')
    st = boot.State(root)
    st.data['previous'] = '1.1.0'
    st.roll_back('test')
    st.save()
    assert boot.choose(root, home) == boot.State(root).folder('1.1.0')


def test_a_newer_image_wins_over_an_older_update_without_marking_it_bad(tmp_path, keypair):
    pem, pub = keypair
    home, root = image_home(tmp_path, pub, version='1.2.0'), tmp_path / 'state'
    install(root, pem, '1.1.0')
    assert boot.choose(root, home) is None
    assert boot.State(root).data['bad'] == []


# probation -------------------------------------------------------------------------------------
def test_the_watchdog_keeps_an_update_that_reached_floorbit(tmp_path, keypair):
    pem = keypair[0]
    root = tmp_path / 'state'
    install(root, pem, '1.1.0')
    env = {'FLOORBIT_AGENT_HEARTBEAT': str(tmp_path / 'beat')}
    now = [1000.0]
    dog = boot.Watchdog(root, env=env, clock=lambda: now[0], stop=lambda: None)
    assert dog.check() is None
    (tmp_path / 'beat').touch()
    os.utime(tmp_path / 'beat', (1010, 1010))
    now[0] = 1000 + boot.STEADY
    assert dog.check() == 'kept'
    assert boot.State(root).data['trial'] is None
    assert boot.State(root).data['current'] == '1.1.0'


def test_the_watchdog_rolls_back_an_update_that_never_reached_floorbit(tmp_path, keypair):
    pem = keypair[0]
    root = tmp_path / 'state'
    install(root, pem, '1.1.0')
    now = [1000.0]
    dog = boot.Watchdog(root, env={'FLOORBIT_AGENT_HEARTBEAT': str(tmp_path / 'none')},
                        clock=lambda: now[0], stop=lambda: None)
    now[0] += boot.PROBATION
    assert dog.check() == 'rolled back'
    st = boot.State(root)
    assert st.data['bad'] == ['1.1.0'] and st.data['current'] is None


# the boot loader, as the image runs it ---------------------------------------------------------
def run_boot(home, root, *args):
    (home / 'floorbit_agent_boot.py').write_text((ROOT / 'floorbit_agent_boot.py').read_text())
    env = {**os.environ, 'FLOORBIT_AGENT_STATE': str(root), 'FLOORBIT_DATA_FILE': str(root / 'agent-data.json'),
           'PYTHONPATH': ''}
    return subprocess.run([sys.executable, '-m', 'floorbit_agent_boot', *args], cwd=home, env=env,
                          capture_output=True, text=True, timeout=60)


def test_boot_runs_the_update_and_goes_back_to_the_image_when_it_crashes(tmp_path, keypair):
    pem, pub = keypair
    home, root = image_home(tmp_path, pub), tmp_path / 'state'
    install(root, pem, '1.1.0')
    out = run_boot(home, root)
    assert out.stdout.split() == ['bundle'], out.stderr
    install(root, pem, '1.2.0', main="raise RuntimeError('broken')")
    out = run_boot(home, root)
    # The crash rolled it back, and the same process started again on the image's code.
    assert out.stdout.split() == ['image'], out.stderr
    assert 'rolled back' in out.stderr
    assert boot.State(root).data['bad'] == ['1.2.0']


def test_boot_starts_again_on_the_new_code_after_an_install(tmp_path, keypair):
    pem, pub = keypair
    root = tmp_path / 'state'
    # The image's agent "installs" 1.1.0 and asks for a restart, as SelfUpdater does.
    home = image_home(tmp_path, pub, main=textwrap.dedent(f"""\
        import floorbit_agent_boot as b
        st = b.State({str(root)!r})
        st.data.update(current='1.1.0', trial={{'version': '1.1.0', 'starts': 0}})
        st.save()
        return b.RESTART""").replace('\n', '\n    '))
    make_bundle(boot.State(root).folder('1.1.0'), pem, '1.1.0')
    out = run_boot(home, root)
    assert out.stdout.split() == ['image', 'bundle'], out.stderr
    assert out.returncode == 0


def test_the_update_command_approves_and_retries(tmp_path):
    root = tmp_path / 'state'
    lines = []
    assert boot.update_cli(['approve'], root, out=lines.append) == 0
    assert json.loads((root / 'agent' / 'approved.json').read_text()) == ['*']
    st = boot.State(root)
    st.data['bad'] = ['1.1.0']
    st.save()
    assert boot.update_cli(['retry'], root, out=lines.append) == 0
    assert boot.State(root).data['bad'] == []
    assert boot.update_cli(['approve', '../x'], root, out=lines.append) == 2


# the installer ---------------------------------------------------------------------------------
class FakeAgent:
    url = 'https://floorbit.example.com'

    def __init__(self, files):
        self.files, self.stopped, self.asked = files, False, []
        agent = self

        class Http:
            @staticmethod
            def get(url, headers=None, timeout=None):
                agent.asked.append(url)
                name = url.rsplit('/', 1)[-1]

                class Resp:
                    status_code = 200 if name in agent.files else 404
                    content = agent.files.get(name, b'')
                return Resp()
        self.http = Http()

    def _headers(self):
        return {}

    def stop(self):
        self.stopped = True


def offer_for(tmp_path, pem, version='1.1.0', image=IMAGE):
    data = boot.pack(make_bundle(tmp_path / f'build-{version}', pem, version, image=image))
    name = f'floorbit-agent-{version}.tar.gz'
    return {name: data}, {'version': version, 'file': name, 'sha256': hashlib.sha256(data).hexdigest(),
                          'image': image}


def updater(tmp_path, pub, files, env=None, running='1.0.0'):
    home = image_home(tmp_path, pub) if not (tmp_path / 'app').exists() else tmp_path / 'app'
    agent = FakeAgent(files)
    u = self_update.SelfUpdater(agent, tmp_path / 'state', env={'FLOORBIT_AGENT_BOOT': '1', **(env or {})},
                                home=home, log=lambda _m: None)
    u.version = running
    return u, agent


def test_an_offered_update_is_installed_on_trial_and_the_agent_stops_for_it(tmp_path, keypair):
    pem, pub = keypair
    files, offer = offer_for(tmp_path, pem)
    u, agent = updater(tmp_path, pub, files)
    assert u.offer(offer) is True
    assert agent.stopped and u.restart
    assert agent.asked == ['https://floorbit.example.com/api/agent/v1/agent-bundles/floorbit-agent-1.1.0.tar.gz']
    st = boot.State(tmp_path / 'state')
    assert st.data['current'] == '1.1.0' and st.data['trial']['version'] == '1.1.0'
    assert boot.choose(tmp_path / 'state', tmp_path / 'app') == st.folder('1.1.0')


def test_a_wrong_checksum_or_signature_is_refused(tmp_path, keypair):
    pem, pub = keypair
    files, offer = offer_for(tmp_path, pem)
    u, agent = updater(tmp_path, pub, files)
    assert u.offer({**offer, 'sha256': '0' * 64}) is False
    assert 'checksum' in u.pending['why'] and not agent.stopped
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    other = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption())
    files, offer = offer_for(tmp_path, other, version='1.2.0')
    u, agent = updater(tmp_path, pub, files)
    assert u.offer(offer) is False
    assert 'does not trust' in u.pending['why'] and not agent.stopped
    assert boot.State(tmp_path / 'state').data['current'] is None


def test_approve_waits_for_the_crew_on_the_agent(tmp_path, keypair):
    pem, pub = keypair
    files, offer = offer_for(tmp_path, pem)
    u, agent = updater(tmp_path, pub, files, env={'FLOORBIT_AGENT_UPDATES': 'approve'})
    assert u.offer(offer) is False
    assert u.pending == {'version': '1.1.0', 'why': 'waits for approval on the agent'}
    boot.update_cli(['approve', '1.1.0'], tmp_path / 'state', out=lambda _l: None)
    assert u.offer(offer) is True and agent.stopped
    assert not (tmp_path / 'state' / 'agent' / 'approved.json').exists()


@pytest.mark.parametrize(('env', 'booted', 'image', 'why'), [
    ({'FLOORBIT_AGENT_UPDATES': 'off'}, True, IMAGE, 'agent updates are off here (FLOORBIT_AGENT_UPDATES)'),
    ({}, False, IMAGE, 'this agent was not started by floorbit_agent_boot: pull the new image'),
    ({}, True, '0000000000000000', 'needs a new image'),
])
def test_an_offer_waits_when_the_hall_or_the_image_says_so(tmp_path, keypair, env, booted, image, why):
    pem, pub = keypair
    files, offer = offer_for(tmp_path, pem, image=image)
    u, agent = updater(tmp_path, pub, files, env=env)
    u.booted = booted
    assert u.offer(offer) is False
    assert u.pending == {'version': '1.1.0', 'why': why}
    assert agent.asked == [] and not agent.stopped


def test_nothing_older_bad_or_malformed_is_installed(tmp_path, keypair):
    pem, pub = keypair
    files, offer = offer_for(tmp_path, pem)
    u, agent = updater(tmp_path, pub, files, running='1.1.0')
    assert u.offer(offer) is False and u.pending is None
    u, agent = updater(tmp_path, pub, files)
    st = boot.State(tmp_path / 'state')
    st.data['bad'] = ['1.1.0']
    st.save()
    assert u.offer(offer) is False and u.pending['why'] == 'rolled back here before'
    assert u.offer({**offer, 'file': '../../etc/passwd'}) is False
    assert u.offer({**offer, 'version': '../1'}) is False
    assert agent.asked == []


def test_the_report_says_what_runs_and_what_waits(tmp_path, keypair):
    pem, pub = keypair
    files, offer = offer_for(tmp_path, pem, image='0000000000000000')
    u, _agent = updater(tmp_path, pub, files)
    u.offer(offer)
    report = u.report()
    assert report['policy'] == 'auto' and report['running'] == '1.0.0' and report['image_id'] == IMAGE
    assert report['pending'] == {'version': '1.1.0', 'why': 'needs a new image'}
    assert report['booted'] is True and report['trial'] is False


def test_the_release_script_builds_a_bundle_an_image_accepts(tmp_path, keypair, monkeypatch):
    pem, pub = keypair
    floorbit = tmp_path / 'floorbit'
    for name in ('floorbit', 'auth', 'integrations'):
        (floorbit / name).mkdir(parents=True)
        (floorbit / name / '__init__.py').write_text('')
    (floorbit / 'VERSION').write_text('9.9.9\n')
    (floorbit / 'requirements.txt').write_text('flask\n')
    (tmp_path / 'key.pem').write_bytes(pem)
    keys = {boot.key_id(boot.load_public(pub)): boot.load_public(pub)}
    monkeypatch.setattr(script.boot, 'pinned_keys', lambda _home=None: keys)
    script.main(['build', '--floorbit', str(floorbit), '--key', str(tmp_path / 'key.pem'),
                 '--out', str(tmp_path / 'dist'), '--version', '5.0.0'])
    meta = json.loads((tmp_path / 'dist' / 'floorbit-agent-bundle.json').read_text())
    image = boot.compute_image_id((ROOT / 'Dockerfile').read_text(), 'flask\n')
    assert meta['version'] == '5.0.0' and meta['image'] == image and meta['protocol'] == 2
    data = (tmp_path / 'dist' / meta['file']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == meta['sha256']
    boot.unpack(data, tmp_path / 'out')
    boot.verify(tmp_path / 'out', keys, version='5.0.0', image=image)
    assert (tmp_path / 'out' / 'floorbit_agent' / 'VERSION').read_text().strip() == '5.0.0'
    assert (tmp_path / 'out' / 'floorbit_agent' / 'self_update.py').is_file()
    assert (tmp_path / 'out' / 'floorbit' / '__init__.py').is_file()
