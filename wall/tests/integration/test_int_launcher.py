"""Integration: the listener launcher (spec 4.3): engine selection, config trouble, ImportError fallback, start
counting with planned markers, the boot guard at 3 unplanned starts, legacy at 4, and the legacy argv. os.execv is
stubbed and mira_listen.main is replaced, so nothing starts."""
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parents[2]
if str(WALL) not in sys.path:
    sys.path.insert(0, str(WALL))
import mira_launch as L  # noqa: E402


class Exec(Exception):
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for k in ("MIRA_CONFIG", "MIRA_ENGINE", "MIRA_BOOT_GUARD"):
        monkeypatch.delenv(k, raising=False)
    execs, mains = [], []
    monkeypatch.setattr(L.os, "execv", lambda exe, argv: execs.append((exe, list(argv))))
    fake = types.ModuleType("mira_listen")
    fake.main = lambda argv: (mains.append(list(argv)), 0)[1]
    monkeypatch.setitem(sys.modules, "mira_listen", fake)
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    return types.SimpleNamespace(tmp=tmp_path, home=home, cfgdir=cfgdir, execs=execs, mains=mains, monkeypatch=monkeypatch,
                                 path=cfgdir / "mira.json")


def write_cfg(env, engine="plugins", **core):
    env.path.write_text(json.dumps({"format": 1, "machine": "listener", "core": {"engine": engine, **core}, "plugins": {}}))
    return str(env.path)


def starts(env):
    f = env.cfgdir / "state" / "starts"
    return f.read_text().splitlines() if f.exists() else []


def test_plugins_engine_runs_mira_listen_main_with_the_argv(env):
    cfg = write_cfg(env)
    assert L.launch(["--config", cfg, "--wav", "x.wav"]) == 0
    assert env.mains == [["--config", cfg, "--wav", "x.wav"]] and env.execs == []
    assert starts(env)[0].endswith(" unplanned")


def test_legacy_engine_execs_v1_with_the_argv_minus_config_plus_legacy_args(env):
    cfg = write_cfg(env, engine="legacy")
    (env.cfgdir / "legacy.args").write_text("--kws-threshold\n0.05\n--agc-target\n0.06\n")
    L.launch(["--config", cfg, "--wav", "x.wav"])
    (exe, argv), = env.execs
    assert exe == sys.executable
    assert argv == [sys.executable, "-B", str(WALL / "mira_listen_v1.py"), "--wav", "x.wav", "--kws-threshold", "0.05",
                    "--agc-target", "0.06"]
    assert "--config" not in argv and cfg not in argv and env.mains == []


def test_config_equals_form_is_removed_too(env):
    cfg = write_cfg(env, engine="legacy")
    L.launch([f"--config={cfg}", "--wav", "x.wav"])
    assert env.execs[0][1][3:] == ["--wav", "x.wav"]


def test_missing_core_engine_means_legacy(env):
    env.path.write_text(json.dumps({"format": 1, "plugins": {}}))
    L.launch(["--config", str(env.path)])
    assert env.execs and not env.mains


@pytest.mark.parametrize("text", ["{not json", "[]", ""])
def test_broken_config_runs_legacy_and_says_so(env, capsys, text):
    env.path.write_text(text)
    L.launch(["--config", str(env.path)])
    assert env.execs and not env.mains
    assert "config unreadable" in capsys.readouterr().err


def test_missing_config_file_runs_legacy(env, capsys):
    L.launch(["--config", str(env.cfgdir / "nope.json")])
    assert env.execs and "config unreadable" in capsys.readouterr().err


def test_no_config_uses_env_engine_then_default_legacy(env):
    L.launch(["--wav", "x.wav"])
    assert env.execs[-1][1][2:4] == [str(WALL / "mira_listen_v1.py"), "--wav"]
    env.monkeypatch.setenv("MIRA_ENGINE", "plugins")
    L.launch(["--wav", "x.wav"])
    assert env.mains == [["--wav", "x.wav"]]


def test_config_from_env_and_from_the_default_path(env):
    cfg = write_cfg(env)
    env.monkeypatch.setenv("MIRA_CONFIG", cfg)
    L.launch(["--wav", "x.wav"])
    assert env.mains == [["--wav", "x.wav", "--config", cfg]]       # main() gets the file even without the flag
    env.monkeypatch.delenv("MIRA_CONFIG")
    default = env.home / ".config" / "mira"
    default.mkdir(parents=True)
    (default / "mira.json").write_text(json.dumps({"core": {"engine": "plugins"}}))
    L.launch([])
    assert env.mains[-1] == ["--config", str(default / "mira.json")]
    assert (default / "state" / "starts").exists()


def test_import_error_falls_back_to_legacy(env, capsys):
    cfg = write_cfg(env)
    env.monkeypatch.delitem(sys.modules, "mira_listen")
    bad = env.tmp / "badpath"
    bad.mkdir()
    (bad / "mira_listen.py").write_text("raise ImportError('plugin tree broken')\n")
    env.monkeypatch.setattr(L, "HERE", bad)
    env.monkeypatch.setattr(sys, "path", [str(bad)] + sys.path)
    L.launch(["--config", cfg])
    assert env.execs and "ImportError" in capsys.readouterr().err
    assert env.execs[0][1][2] == str(bad / "mira_listen_v1.py")     # siblings come from the launcher's folder


def test_planned_marker_makes_a_planned_start_and_is_consumed(env):
    cfg = write_cfg(env)
    (env.cfgdir / "state").mkdir()
    marker = env.cfgdir / "state" / "planned"
    marker.write_text("")
    L.launch(["--config", cfg])
    assert starts(env)[-1].endswith(" planned") and not marker.exists()
    L.launch(["--config", cfg])
    assert starts(env)[-1].endswith(" unplanned")
    marker.write_text("")
    old = time.time() - 120
    os.utime(marker, (old, old))                                   # a stale marker does not count, and is dropped
    L.launch(["--config", cfg])
    assert starts(env)[-1].endswith(" unplanned") and not marker.exists()


def test_boot_guard_at_three_unplanned_starts_and_legacy_at_four(env, capsys):
    cfg = write_cfg(env, crash_window_s=600, crash_guard_after=3, crash_legacy_after=4)
    for n in (1, 2):
        L.launch(["--config", cfg])
        assert "MIRA_BOOT_GUARD" not in os.environ, n
    env.monkeypatch.delenv("MIRA_BOOT_GUARD", raising=False)
    L.launch(["--config", cfg])
    assert os.environ.get("MIRA_BOOT_GUARD") == "1" and len(env.mains) == 3 and not env.execs
    os.environ.pop("MIRA_BOOT_GUARD")
    L.launch(["--config", cfg, "--wav", "x.wav"])
    assert len(env.mains) == 3 and len(env.execs) == 1 and "MIRA_BOOT_GUARD" not in os.environ
    assert capsys.readouterr().err.count("crash loop: running the legacy listener") == 1
    assert len(starts(env)) == 4                                    # legacy starts are counted too


def test_planned_starts_do_not_count_and_old_starts_fall_out_of_the_window(env):
    cfg = write_cfg(env, crash_window_s=100)
    state = env.cfgdir / "state"
    state.mkdir()
    now = time.time()
    (state / "starts").write_text(f"{now - 500:.0f} unplanned\n" * 5 + f"{now - 10:.0f} planned\n" * 5)
    L.launch(["--config", cfg])
    assert env.mains and not env.execs and "MIRA_BOOT_GUARD" not in os.environ


def test_starts_file_is_trimmed(env):
    cfg = write_cfg(env, crash_window_s=1)
    state = env.cfgdir / "state"
    state.mkdir()
    (state / "starts").write_text("1 planned\n" * 400)
    L.launch(["--config", cfg])
    assert len(starts(env)) <= L.KEEP_STARTS


def test_launcher_is_stdlib_only():
    src = (WALL / "mira_launch.py").read_text()
    imports = {ln.split()[1].split(".")[0] for ln in src.splitlines() if ln.startswith(("import ", "from "))}
    assert imports <= {"json", "os", "sys", "time", "pathlib"}
