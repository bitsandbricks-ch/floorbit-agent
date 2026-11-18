"""Signed plugin updates from the hosted Floorbit (floorbit_agent/bundles.py) against a fake hosted
side: only signed, newer bundles for this agent; approval on the agent; rollback when the new
version fails; the bundles checked again at start. No network."""
import json
import textwrap

import pytest
from floorbit.plugins import bundles

import floorbit_agent as agent_mod
from floorbit_agent import bundles as agent_bundles
from floorbit_agent.bundles import Updater, cli
from floorbit_agent.plugin_host import PluginHost

YAML = """
id: hallthing
name: Hall thing
version: "1.0"
uses: [agent]
agent:
  sends: {stats: {privacy: aggregate}}
"""

AGENT_PY = """
from floorbit.plugins.agent import AgentPlugin as Base

VERSION = {version!r}

class AgentPlugin(Base):
    def start(self):
        if VERSION == 'broken':
            raise RuntimeError('cannot start')
        self.ctx.health({status!r}, VERSION)

    def call_version(self, args):
        return VERSION
"""

CONFIG = {"version": "1.0", "settings": {}, "sends": {"stats": {"privacy": "aggregate"}}, "calls": ["version"],
          "actions": []}


def write(folder, version, status="ok", code=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.yaml").write_text(textwrap.dedent(YAML))
    (folder / "agent.py").write_text(textwrap.dedent(AGENT_PY.format(version=code or version, status=status)))
    return folder


class Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content = status, body or {}, content

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Hosted:
    def __init__(self):
        self.files, self.offer, self.etag = {}, {}, 0

    def publish(self, tmp_path, pem, version, status="ok", code=None, pid="hallthing", agent_api=1):
        folder = write(tmp_path / "build" / f"{pid}-{version}", version, status, code)
        bundles.sign(folder, pem, version, agent_api)
        data = bundles.pack(folder)
        name = f"{pid}-{version}.tar.gz"
        self.files[name] = data
        self.offer = {pid: {"version": version, "file": name, "sha256": bundles.sha256(data), "agent_api": agent_api}}
        self.etag += 1
        return data

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/api/agent/v1/plugins"):
            return Resp(200, {"plugins": {"hallthing": CONFIG}, "bundles": self.offer, "etag": str(self.etag)})
        name = url.rsplit("/", 1)[-1]
        if name in self.files:
            return Resp(200, content=self.files[name])
        return Resp(404)


class Registry:
    def __init__(self, specs):
        self.specs = specs

    def _register_settings(self):
        pass


@pytest.fixture
def hall(tmp_path):
    import sys

    from floorbit.plugins import _load
    sys.modules.pop("floorbit_agent_plugin_hallthing", None)    # another test's copy
    pem, pub = bundles.keygen()
    keys = {bundles.key_id(bundles.load_public(pub)): bundles.load_public(pub)}
    image_spec = _load(write(tmp_path / "image" / "hallthing", "0.4.0"), "builtin", "0.4.0")
    assert not image_spec.error
    registry = Registry({"hallthing": image_spec})
    hosted = Hosted()
    agent = agent_mod.Agent("https://floor.example.com", "fba_x_y", lambda m, a: [], http=hosted, log=lambda *_: None)
    lines = []
    clock = [1000.0]
    env = {}

    def make(policy="auto", with_keys=True, image=True):
        # Each one as a fresh agent process: the image's plugins, nothing loaded yet.
        registry.specs = {"hallthing": image_spec} if image else {}
        sys.modules.pop("floorbit_agent_plugin_hallthing", None)
        env["FLOORBIT_AGENT_PLUGIN_UPDATES"] = policy
        host = PluginHost(agent, dict(registry.specs), str(tmp_path / "state"), env={}, log=lines.append)
        host.updater = Updater(agent, registry, str(tmp_path / "state"), "0.4.0", env=env,
                               keys=keys if with_keys else {}, log=lines.append, clock=lambda: clock[0])
        host.updater.restore()
        host.specs = dict(registry.specs)
        return host

    hosts = []

    def started(*args, **kw):
        host = make(*args, **kw)
        hosts.append(host)
        return host

    yield started, hosted, pem, clock, tmp_path, lines
    for h in hosts:
        h.stop()
    sys.modules.pop("floorbit_agent_plugin_hallthing", None)     # the next test's plugin has the same id


def runs(host):
    return host.call(["hallthing", "call", "version", {}])


def test_a_signed_newer_bundle_is_installed_and_kept(hall):
    started, hosted, pem, clock, tmp_path, _ = hall
    host = started()
    host.pull()
    assert runs(host) == "0.4.0"
    hosted.publish(tmp_path, pem, "0.5.0")
    host.pull()
    assert runs(host) == "0.5.0"
    report = host.updater.report()
    assert report["plugins"]["hallthing"] == {"version": "0.5.0", "source": "bundle", "pending": None,
                                              "probation": True, "rolled_back": []}
    clock[0] += 301
    host.updater.watch(host)
    assert [e["event"] for e in host.updater.report()["events"]] == ["installed", "kept"]


def test_a_plugin_the_image_lacks_comes_from_the_marketplace(hall):
    # Installed on the hosted side from the Marketplace: versions of its own, lower than the image's.
    started, hosted, pem, _clock, tmp_path, _ = hall
    host = started(image=False)
    hosted.publish(tmp_path, pem, "0.1.0")
    host.pull()
    assert runs(host) == "0.1.0"
    assert host.updater.report()["plugins"]["hallthing"]["version"] == "0.1.0"
    host.stop()
    again = started(image=False)                                  # restarted: checked again and kept
    again.pull()
    assert runs(again) == "0.1.0"


def test_older_or_same_versions_are_never_installed(hall):
    started, hosted, pem, _clock, tmp_path, _ = hall
    host = started()
    hosted.publish(tmp_path, pem, "0.4.0", code="same")
    host.pull()
    assert runs(host) == "0.4.0"
    hosted.publish(tmp_path, pem, "0.6.0")
    host.pull()
    hosted.publish(tmp_path, pem, "0.5.0", code="older")       # a hosted side rolled back, or replaying
    host.pull()
    assert runs(host) == "0.6.0"


def test_unsigned_tampered_or_foreign_bundles_are_refused(hall):
    started, hosted, pem, _clock, tmp_path, _lines = hall
    host = started()
    other, _ = bundles.keygen()
    hosted.publish(tmp_path, other, "0.5.0", code="evil")        # someone else's key
    host.pull()
    assert runs(host) == "0.4.0"
    assert host.updater.report()["plugins"]["hallthing"]["pending"]["why"].startswith("refused: signed with key")
    # Signed, but the hosted side changed the file it hands out (and its checksum to match).
    data = hosted.publish(tmp_path, pem, "0.5.1")
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data)) as src, tarfile.open(fileobj=buf, mode="w:gz") as dst:
        for m in src.getmembers():
            body = src.extractfile(m).read()
            if m.name == "agent.py":
                body = body.replace(b"VERSION = '0.5.1'", b"VERSION = 'evil'")
                m.size = len(body)
            dst.addfile(m, io.BytesIO(body))
    hosted.files["hallthing-0.5.1.tar.gz"] = buf.getvalue()
    hosted.offer["hallthing"]["sha256"] = bundles.sha256(buf.getvalue())
    hosted.etag += 1
    host.pull()
    assert runs(host) == "0.4.0"
    assert "differ" in host.updater.report()["plugins"]["hallthing"]["pending"]["why"]
    assert not (tmp_path / "state" / "bundles" / "hallthing" / "0.5.1").exists()


def test_no_pinned_key_or_a_newer_agent_api_waits(hall):
    started, hosted, pem, _clock, tmp_path, _ = hall
    host = started(with_keys=False)
    hosted.publish(tmp_path, pem, "0.5.0")
    host.pull()
    assert runs(host) == "0.4.0"
    assert host.updater.report()["plugins"]["hallthing"]["pending"]["why"] == "no signing key in this agent image"
    host = started()
    hosted.publish(tmp_path, pem, "0.6.0", agent_api=99)
    host.pull()
    assert host.updater.report()["plugins"]["hallthing"]["pending"]["why"] == "needs a newer agent image"


@pytest.mark.parametrize("api", ["2", [1], 1.5, True, None])
def test_an_odd_agent_api_waits_and_never_stops_the_round(hall, api):
    started, hosted, pem, _clock, tmp_path, _ = hall
    host = started()
    hosted.publish(tmp_path, pem, "0.6.0")
    hosted.offer["hallthing"]["agent_api"] = api
    hosted.etag += 1
    host.pull()
    assert runs(host) == "0.4.0"
    assert host.updater.report()["plugins"]["hallthing"]["pending"]["why"] == "needs a newer agent image"


def test_approve_on_the_agent(hall):
    started, hosted, pem, _clock, tmp_path, _ = hall
    host = started(policy="approve")
    hosted.publish(tmp_path, pem, "0.5.0")
    host.pull()
    assert runs(host) == "0.4.0"
    out = []
    assert cli([], str(tmp_path / "state"), out=out.append) == 0
    assert "offered 0.5.0: waits for approval on the agent" in out[0]
    assert cli(["approve", "hallthing"], str(tmp_path / "state"), out=out.append) == 0
    hosted.etag += 1
    host.pull()
    assert runs(host) == "0.5.0"
    assert not (tmp_path / "state" / "bundles" / "approved.json").exists()


def test_off_installs_nothing(hall):
    started, hosted, pem, _clock, tmp_path, _ = hall
    host = started(policy="off")
    hosted.publish(tmp_path, pem, "0.5.0")
    host.pull()
    assert runs(host) == "0.4.0"


def test_a_version_that_fails_is_rolled_back_and_never_tried_again(hall):
    started, hosted, pem, _clock, tmp_path, _ = hall
    host = started()
    hosted.publish(tmp_path, pem, "0.5.0")
    host.pull()
    hosted.publish(tmp_path, pem, "0.6.0", code="broken")
    host.pull()
    assert "hallthing" in host.failed
    host.updater.watch(host)
    assert runs(host) == "0.5.0" and "hallthing" not in host.failed
    report = host.updater.report()
    assert report["plugins"]["hallthing"]["rolled_back"] == ["0.6.0"]
    assert report["events"][-1]["event"] == "rolled back"
    hosted.etag += 1
    host.pull()
    assert runs(host) == "0.5.0"


def test_an_error_during_probation_goes_back_to_the_image(hall):
    started, hosted, pem, _clock, tmp_path, _ = hall
    host = started()
    host.pull()
    hosted.publish(tmp_path, pem, "0.5.0", status="error")
    host.pull()
    host.updater.watch(host)
    assert runs(host) == "0.4.0"
    assert host.updater.report()["plugins"]["hallthing"]["source"] == "image"


def test_installed_bundles_are_checked_again_at_start(hall):
    started, hosted, pem, _clock, tmp_path, lines = hall
    host = started()
    hosted.publish(tmp_path, pem, "0.5.0")
    host.pull()
    host.stop()
    again = started()
    again.pull()
    assert runs(again) == "0.5.0"
    again.stop()
    # Someone changed the installed code on the agent's disk: back to the image's.
    path = tmp_path / "state" / "bundles" / "hallthing" / "0.5.0" / "agent.py"
    path.write_text(path.read_text().replace("'0.5.0'", "'changed'"))
    hosted.offer = {}
    third = started()
    third.pull()
    assert runs(third) == "0.4.0"
    assert any("dropped" in line and "differ" in line for line in lines)
    # Offered again: a fresh, checked copy replaces it.
    hosted.publish(tmp_path, pem, "0.5.0")
    third.pull()
    assert runs(third) == "0.5.0" and "changed" not in path.read_text()


def test_the_cli_says_how_to_use_it(tmp_path):
    out = []
    assert cli(["approve"], str(tmp_path), out=out.append) == 2
    assert cli(["approve", "nothing"], str(tmp_path), out=out.append) == 1
    assert cli([], str(tmp_path), out=out.append) == 0 and out[-1] == "No plugin updates yet."
    assert json.loads(json.dumps(out))


@pytest.mark.parametrize("pid, version, file", [
    ("../../escape", "0.5.0", "hallthing-0.5.0.tar.gz"),
    ("hallthing", "9/../../../escape", "hallthing-0.5.0.tar.gz"),
    ("hallthing", "0.5.0", "../hallthing-0.5.0.tar.gz"),
    ("hallthing", "0.5..0", "hallthing-0.5.0.tar.gz"),
])
def test_an_offer_with_a_path_in_it_touches_nothing(hall, pid, version, file):
    started, hosted, pem, _clock, tmp_path, lines = hall
    host = started()
    hosted.publish(tmp_path, pem, "0.5.0")
    entry = hosted.offer.pop("hallthing")
    hosted.offer[pid] = {**entry, "version": version, "file": file}
    hosted.etag += 1
    canary = tmp_path / "escape"
    canary.mkdir(exist_ok=True)
    host.pull()
    assert runs(host) == "0.4.0"
    assert canary.exists()
    assert any("bad id, version or file" in line for line in lines)


@pytest.mark.parametrize('regex, value', [
    (agent_bundles.ID_RE, 'lancache\n'), (agent_bundles.VERSION_RE, '1.0.0\n'),
    (agent_bundles.FILE_RE, 'lancache-1.0.0.tar.gz\n'),
])
def test_a_trailing_newline_is_no_valid_name(regex, value):
    # "$" matches before a final newline; these names become folders on the agent.
    assert regex.match(value) is None and regex.match(value.strip())


def test_a_full_disk_still_applies_the_settings(hall, monkeypatch):
    # The state file can't be written: the update still runs, and the round still applies settings.
    started, hosted, pem, _clock, tmp_path, lines = hall
    host = started()
    host.pull()
    real_open = open

    def full(path, mode="r", *a, **kw):
        if "w" in mode and str(path).endswith("state.tmp"):
            raise OSError(28, "No space left on device")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", full)
    hosted.publish(tmp_path, pem, "0.5.0")
    host.pull()
    assert runs(host) == "0.5.0"
    assert any("could not save the plugin update state" in line for line in lines)
