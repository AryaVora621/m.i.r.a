"""Deploy planning and execution (P6) on temp dirs with a local stand-in for the remote machine.

The remote Runner executes real shell commands against temp directories, except systemctl, journalctl and
launchctl, which are recorded. Git answers come from a canned runner, so no repository is needed.
"""
from __future__ import annotations

import io
import json
import os
import posixpath
import stat
import sys
import tarfile
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import deploy as D  # noqa: E402

SHA = "abc123def456"


class GitStub(D.Runner):
    """Answers the three git calls deploy makes; the archive is built from a dict of repo paths."""

    def __init__(self, tree: dict[str, bytes], dirty: bool = False):
        super().__init__(None)
        self.tree, self.dirty, self.calls = tree, dirty, []

    def _exec(self, argv, input, timeout):
        self.calls.append(list(argv))
        assert argv[0] == "git"
        sub = argv[3]
        if sub == "rev-parse":
            return D.Result(0, (SHA + "\n").encode())
        if sub == "status":
            return D.Result(0, b" M wall/mirakit/x.py\n" if self.dirty else b"")
        if sub == "archive":
            paths = argv[argv.index("--") + 1:]
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for name, data in sorted(self.tree.items()):
                    if any(name == p or name.startswith(p.rstrip("/") + "/") for p in paths):
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
            return D.Result(0, buf.getvalue())
        return D.Result(1, b"", b"unexpected git call")


class FakeRemote(D.Runner):
    """Real shell on temp dirs; service managers are recorded."""

    def __init__(self, journal_ok: bool = True):
        super().__init__(None)
        self.service_calls: list[str] = []
        self.journal_ok = journal_ok

    def _exec(self, argv, input, timeout):
        text = " ".join(argv)
        if argv[0] == "sh" and any(w in text for w in ("systemctl", "journalctl", "launchctl")):
            self.service_calls.append(argv[2])
            if "journalctl" in text:
                return D.Result(0, b"12:00:00 mira-listen up (mic), kws threshold 0.05\n" if self.journal_ok else b"")
            return D.Result(0)
        return super()._exec(argv, input, timeout)


def repo_tree() -> dict[str, bytes]:
    return {
        "wall/mira_launch.py": b"launch v1\n", "wall/mira_listen.py": b"listen v1\n",
        "wall/mira_listen_v1.py": b"frozen\n", "wall/mira_shadow.py": b"shadow\n",
        "wall/mirakit/__init__.py": b"VERSION = 1\n", "wall/mirakit/core.py": b"core\n",
        "wall/mirakit/__pycache__/core.cpython-313.pyc": b"junk",
        "wall/listener_plugins/__init__.py": b"", "wall/listener_plugins/agc_mic.py": b"agc\n",
        "wall/speech/server.py": b"server v2\n", "wall/speech/server_v1.py": b"server v1\n",
        "wall/speech_plugins/__init__.py": b"", "wall/speech_plugins/voices/k.json": b"{}\n",
        "wall/tests/test_x.py": b"never shipped\n",
    }


@pytest.fixture
def world(tmp_path):
    """A working tree on disk (for --allow-dirty), a manifest pointing at temp roots, and both runners."""
    wall = tmp_path / "repo" / "wall"
    for name, data in repo_tree().items():
        path = tmp_path / "repo" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (wall / "deploy").mkdir(exist_ok=True)
    manifest = D.load_manifest(WALL)
    manifest["pi"].update(root=str(tmp_path / "pi" / "mira-listen"), config_dir=str(tmp_path / "pi" / "cfg"), wait_s=3)
    manifest["speech"].update(root=str(tmp_path / "mini" / "mira-speech"), config_dir=str(tmp_path / "mini" / "cfg"))
    (wall / "deploy" / "manifest.json").write_text(json.dumps(manifest))
    fake_py = tmp_path / "fakepy"
    fake_py.write_text('#!/bin/sh\necho "checked $@" > "$PWD/CHECKED"\nexit ${FAKEPY_RC:-0}\n')
    os.chmod(fake_py, 0o755)
    clock = {"t": 1_700_000_000.0}

    def sleep(s):
        clock["t"] += s

    def make(machine, *, dirty=False, journal_ok=True, remote=None):
        remote = remote or FakeRemote(journal_ok)
        out = []
        dep = D.Deployer(machine, wall, remote, local=GitStub(repo_tree(), dirty), out=out.append, sleep=sleep,
                         now=lambda: clock["t"], python_for_check=str(fake_py))
        return dep, remote, out

    return tmp_path, wall, make, clock


def files_under(path: Path) -> set[str]:
    return {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}


# ---------------------------------------------------------------- pure helpers
def test_manifest_has_the_spec_file_lists():
    m = D.load_manifest(WALL)
    assert m["pi"]["files"] == ["mira_launch.py", "mira_listen.py", "mira_listen_v1.py", "mira_shadow.py", "mirakit",
                                "listener_plugins"]
    assert D.manifest_entries(m["speech"]) == [("speech/server.py", "server.py"), ("speech/server_v1.py", "server_v1.py"),
                                               ("mirakit", "mirakit"), ("speech_plugins", "speech_plugins")]
    assert m["pi"]["root"] == "/home/mira/mira-listen" and m["pi"]["unit"] == "mira-listen"


def test_collect_from_tar_maps_paths_and_skips_junk():
    tree = repo_tree()
    blob = D.build_tar({k: v for k, v in tree.items()})
    entries = D.manifest_entries(D.load_manifest(WALL)["speech"])
    got = D.collect_from_tar(blob, entries)
    assert sorted(got) == ["mirakit/__init__.py", "mirakit/core.py", "server.py", "server_v1.py",
                           "speech_plugins/__init__.py", "speech_plugins/voices/k.json"]
    assert got["server.py"] == b"server v2\n"


def test_build_tar_is_deterministic_and_sorted():
    a = D.build_tar({"b": b"2", "a": b"1"}, {"R": b"x"})
    assert a == D.build_tar({"a": b"1", "b": b"2"}, {"R": b"x"})
    with tarfile.open(fileobj=io.BytesIO(a)) as tar:
        assert tar.getnames() == ["R", "a", "b"] and all(m.mtime == 0 for m in tar.getmembers())


def test_diff_and_parse_sha256sum():
    local = D.hashes({"a": b"1", "b": b"2", "c": b"3"})
    remote = D.hashes({"a": b"1", "b": b"changed", "d": b"4"})
    assert D.diff_hashes(local, remote) == ["M b", "A c", "D d"]
    text = f"{local['a']}  ./a\n{local['b']} *b\nbad line\n{local['c']}  x/__pycache__/y.pyc\n"
    assert D.parse_sha256sum(text) == {"a": local["a"], "b": local["b"]}


def test_runner_builds_ssh_commands_and_records_dry_runs():
    r = D.Runner("mira@pi")
    assert r._final(["ls", "a b"]) == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mira@pi", "ls 'a b'"]
    assert D.Runner()._final(["ls", "a b"]) == ["ls", "a b"]
    dry = D.Runner("mira@pi", dry_run=True)
    res = dry.run(["rm", "-rf", "/x"])
    assert res.ok and dry.log == ["+ [mira@pi] rm -rf /x"]
    class Spy(D.Runner):
        ran = []
        def _exec(self, argv, input, timeout):
            Spy.ran.append(argv)
            return D.Result(0, b"hello")
    spy = Spy(None, dry_run=True)
    assert spy.run(["cat", "f"], mutating=False).out == "hello" and Spy.ran == [["cat", "f"]]
    assert spy.run(["rm", "f"]).out == "" and len(Spy.ran) == 1
    with pytest.raises(D.DeployError):
        D.Runner().run(["false"], check=True)
    assert D.Runner().run(["definitely-not-a-command-xyz"]).rc == 127


def test_write_bytes_sets_mode_and_creates_parents(tmp_path):
    r = D.Runner()
    target = tmp_path / "a" / "b" / "f"
    r.write_bytes(str(target), b"data", 0o600)
    assert target.read_bytes() == b"data" and stat.S_IMODE(target.stat().st_mode) == 0o600


# ---------------------------------------------------------------- Pi
def test_stage_ships_head_into_releases_and_runs_check(world):
    tmp, wall, make, _ = world
    dep, remote, out = make("pi")
    name = dep.stage_pi()
    release = tmp / "pi" / "mira-listen" / "releases" / SHA
    assert name == SHA
    assert files_under(release) - {"CHECKED"} == {"mira_launch.py", "mira_listen.py", "mira_listen_v1.py", "mira_shadow.py",
                                    "mirakit/__init__.py", "mirakit/core.py", "listener_plugins/__init__.py",
                                    "listener_plugins/agc_mic.py", "RELEASE.json"}
    assert (release / "mira_listen.py").read_bytes() == b"listen v1\n"
    assert json.loads((release / "RELEASE.json").read_text())["release"] == SHA
    assert "checked -B -m mirakit.cli check --fast" in (release / "CHECKED").read_text()
    assert any("staged" in line for line in out)
    assert dep.local.calls[1][3] == "status"


def test_stage_refuses_a_dirty_tree_unless_allowed(world):
    tmp, wall, make, _ = world
    dep, remote, _ = make("pi", dirty=True)
    with pytest.raises(D.DeployError, match="uncommitted"):
        dep.stage_pi()
    assert not (tmp / "pi").exists()                           # nothing was copied
    (wall / "mirakit" / "core.py").write_bytes(b"edited, uncommitted\n")
    (wall / "mirakit" / "__pycache__").mkdir(exist_ok=True)
    (wall / "mirakit" / "__pycache__" / "x.pyc").write_bytes(b"x")
    name = dep.stage_pi(allow_dirty=True)
    release = tmp / "pi" / "mira-listen" / "releases" / name
    assert name == SHA + "-dirty" and (release / "mirakit" / "core.py").read_bytes() == b"edited, uncommitted\n"
    assert not [p for p in release.rglob("*") if "__pycache__" in p.parts]


def test_stage_fails_loudly_when_the_check_fails(world, monkeypatch):
    tmp, wall, make, _ = world
    monkeypatch.setenv("FAKEPY_RC", "3")
    dep, remote, _ = make("pi")
    with pytest.raises(D.DeployError, match="check --fast failed"):
        dep.stage_pi()
    assert dep.stage_pi(run_check=False) == SHA                # an explicit skip still stages


def test_stage_fails_when_a_manifest_file_is_not_in_head(world):
    tmp, wall, make, _ = world
    dep, remote, _ = make("pi")
    dep.local.tree.pop("wall/mira_shadow.py")
    with pytest.raises(D.DeployError, match="not in HEAD: mira_shadow.py"):
        dep.stage_pi()


def test_activate_writes_marker_flips_current_restarts_and_waits(world):
    tmp, wall, make, clock = world
    dep, remote, out = make("pi")
    dep.stage_pi()
    root = tmp / "pi" / "mira-listen"
    dep.activate_pi(SHA)
    assert os.readlink(root / "current") == f"releases/{SHA}"
    marker = tmp / "pi" / "cfg" / "state" / "planned"
    assert marker.read_text().strip() == str(int(1_700_000_000))
    flat = "\n".join(remote.service_calls)
    assert "systemctl --user restart mira-listen" in flat and "journalctl --user -u mira-listen" in flat
    order = [i for i, line in enumerate(remote.log) if "planned" in line or "systemctl" in line or "ln -sfn" in line]
    assert order == sorted(order) and len(order) >= 3
    assert any("startup line seen" in line for line in out)


def test_activate_flips_back_when_the_startup_line_never_appears(world):
    tmp, wall, make, clock = world
    root = tmp / "pi" / "mira-listen"
    good, remote_ok, _ = make("pi")
    good.stage_pi()
    good.activate_pi(SHA)
    good.local.tree["wall/mira_listen.py"] = b"listen v2\n"
    bad, remote_bad, out = make("pi", journal_ok=False)
    bad.sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
    # a second release to activate
    (root / "releases" / "second").mkdir()
    (root / "releases" / "second" / "x").write_text("x")
    with pytest.raises(D.DeployError, match="flipped back to " + SHA):
        bad.activate_pi("second")
    assert os.readlink(root / "current") == f"releases/{SHA}"
    restarts = [c for c in remote_bad.service_calls if "systemctl --user restart" in c]
    assert len(restarts) == 2                                  # the failed one and the flip back
    assert any("flipping back" in line for line in out)


def test_activate_refuses_an_unstaged_release_and_bad_names(world):
    tmp, wall, make, _ = world
    dep, remote, _ = make("pi")
    with pytest.raises(D.DeployError, match="not staged"):
        dep.activate_pi("nope")
    for bad in ("../etc", "a b", "x;rm", ""):
        with pytest.raises(D.DeployError, match="not a release name"):
            dep.activate_pi(bad)
    assert remote.service_calls == []


def test_rollback_goes_to_the_previous_release(world):
    tmp, wall, make, _ = world
    root = tmp / "pi" / "mira-listen"
    dep, remote, _ = make("pi")
    dep.stage_pi()
    (root / "releases" / "older").mkdir()
    os.symlink("releases/older", root / "current")
    dep.activate_pi(SHA)
    assert os.readlink(root / "current") == f"releases/{SHA}"
    assert (root / "releases" / ".previous").read_text().strip() == "older"
    assert dep.rollback_pi() == "older"
    assert os.readlink(root / "current") == "releases/older"
    assert dep.rollback_pi(SHA) == SHA                         # explicit name
    fresh, _, _ = make("pi", remote=FakeRemote())
    (root / "releases" / ".previous").unlink()
    with pytest.raises(D.DeployError, match="no previous release"):
        fresh.rollback_pi()


def test_dry_run_prints_commands_and_diffs_without_touching_the_pi(world):
    tmp, wall, make, _ = world
    root = tmp / "pi" / "mira-listen"
    live, remote, _ = make("pi")
    live.stage_pi()
    live.activate_pi(SHA)
    (root / "current" / "mira_listen.py").write_bytes(b"older listen\n")        # drift on the Pi
    (root / "current" / "listener_plugins" / "gone.py").write_bytes(b"x")
    (root / "current" / "listener_plugins" / "agc_mic.py").unlink()
    snapshot_before = files_under(tmp / "pi")
    dry_remote = FakeRemote()
    dry_remote.dry_run = True
    dep, _, out = make("pi", remote=dry_remote)
    diff = dep.dry_run_pi()
    assert "M mira_listen.py" in diff and "D listener_plugins/gone.py" in diff and "A listener_plugins/agc_mic.py" in diff
    dep.stage_pi()
    dep.activate_pi(SHA)
    assert files_under(tmp / "pi") == snapshot_before
    assert any(line.startswith("+ ") and "systemctl" in line for line in dry_remote.log)
    assert any("ln -sfn" in line and line.startswith("+ ") for line in dry_remote.log)
    assert dry_remote.service_calls == []                      # nothing reached the service manager


# ---------------------------------------------------------------- mini
def test_speech_deploy_snapshots_backs_up_copies_and_never_restarts(world):
    tmp, wall, make, _ = world
    root = tmp / "mini" / "mira-speech"
    (root / "mirakit").mkdir(parents=True)
    (root / "server.py").write_bytes(b"old server\n")
    (root / "mirakit" / "old.py").write_bytes(b"old\n")
    dep, remote, out = make("speech")
    name = dep.deploy_speech()
    assert name == SHA
    assert (root / "server.py").read_bytes() == b"server v2\n"            # src speech/server.py -> dst server.py
    assert (root / "server_v1.py").read_bytes() == b"server v1\n"
    assert (root / "speech_plugins" / "voices" / "k.json").exists() and (root / "mirakit" / "core.py").exists()
    snaps = list((root / "snapshots").glob("*.tar"))
    assert len(snaps) == 1
    with tarfile.open(snaps[0]) as tar:
        assert set(tar.getnames()) >= {"server.py", "mirakit", "mirakit/old.py"}
        assert tar.extractfile("server.py").read() == b"old server\n"
    baks = list(root.glob("server.py.bak-plugins-*"))
    assert len(baks) == 1 and baks[0].read_bytes() == b"old server\n"
    assert remote.service_calls == []
    assert any("launchctl kickstart -k gui/501/org.aryavora.mira-speech" in line for line in out)
    assert not any("kickstart" in c for c in remote.log if "launchctl" in c and not c.startswith("["))


def test_speech_deploy_on_a_fresh_install_and_second_run_keeps_the_first_backup(world):
    tmp, wall, make, _ = world
    root = tmp / "mini" / "mira-speech"
    dep, remote, _ = make("speech")
    dep.deploy_speech()                                        # nothing there yet: no backup, no failure
    assert (root / "server.py").read_bytes() == b"server v2\n" and not list(root.glob("server.py.bak-*"))
    dep.local.tree["wall/speech/server.py"] = b"server v3\n"
    dep.deploy_speech()
    assert len(list(root.glob("server.py.bak-plugins-*"))) == 1
    assert next(root.glob("server.py.bak-plugins-*")).read_bytes() == b"server v2\n"


def test_speech_rollback_restores_snapshot_and_backup(world):
    tmp, wall, make, clock = world
    root = tmp / "mini" / "mira-speech"
    (root / "mirakit").mkdir(parents=True)
    (root / "server.py").write_bytes(b"old server\n")
    (root / "mirakit" / "old.py").write_bytes(b"old\n")
    dep, remote, out = make("speech")
    dep.deploy_speech()
    assert (root / "server.py").read_bytes() == b"server v2\n"
    dep.rollback_speech()
    assert (root / "server.py").read_bytes() == b"old server\n" and (root / "mirakit" / "old.py").read_bytes() == b"old\n"
    assert remote.service_calls == [] and any("kickstart" in line for line in out)
    empty, _, _ = make("speech", remote=FakeRemote())
    import shutil
    shutil.rmtree(root / "snapshots")
    with pytest.raises(D.DeployError, match="no snapshot"):
        empty.rollback_speech()


def test_speech_dry_run_diffs_the_install_dir(world):
    tmp, wall, make, _ = world
    root = tmp / "mini" / "mira-speech"
    dep, remote, _ = make("speech")
    dep.deploy_speech()
    (root / "server.py").write_bytes(b"hand edited\n")
    (root / "mirakit" / "extra.py").write_bytes(b"x")
    diff = dep.dry_run_speech()
    assert "M server.py" in diff and "D mirakit/extra.py" in diff and len(diff) == 2


def test_deploy_module_has_no_dashes():
    bad = chr(0x2013), chr(0x2014)
    for path in (WALL / "mirakit" / "deploy.py", WALL / "deploy" / "manifest.json"):
        assert not any(c in path.read_text(encoding="utf-8") for c in bad)
