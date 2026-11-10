"""systemd drop-in and LaunchAgent rendering (P6). Golden output for the Pi values of spec 2.4.3."""
from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import deploy as D  # noqa: E402
from mirakit import unitgen as U  # noqa: E402

GOLDEN_PI = """# Rendered by `mira unit apply` from the core.unit_* knobs in mira.json. Do not edit by hand:
# change a value with `mira set core.unit_nice=5` and render again.
[Service]
ExecStart=
ExecStart=%h/mira-listen/venv/bin/python -B %h/mira-listen/current/mira_launch.py --config %h/.config/mira/mira.json
MemoryMax=200M
CPUQuota=40%
Nice=5
Restart=always
RestartSec=3
"""


class QuietRunner(D.Runner):
    """Records systemctl calls instead of running them."""

    def __init__(self):
        super().__init__(None)
        self.calls = []

    def _exec(self, argv, input, timeout):
        self.calls.append(argv)
        return D.Result(0)


PI_VALUES = {"unit_memory_max": "200M", "unit_cpu_quota": "40%", "unit_nice": 5, "unit_restart": "always",
             "unit_restart_sec": 3.0}


def test_pi_dropin_golden():
    assert U.render_dropin(PI_VALUES) == GOLDEN_PI
    assert U.render_dropin({}) == GOLDEN_PI          # schema defaults are the Pi values


def test_empty_execstart_comes_before_the_real_one():
    lines = U.render_dropin({}).splitlines()
    first = lines.index("ExecStart=")
    assert lines[first + 1].startswith("ExecStart=%h/mira-listen/venv/bin/python")
    assert lines.index("[Service]") < first


def test_values_flow_through_and_restart_sec_prints_without_a_trailing_zero():
    text = U.render_dropin({"unit_memory_max": "300M", "unit_cpu_quota": "60%", "unit_nice": -5,
                            "unit_restart": "on-failure", "unit_restart_sec": 2.5})
    assert "MemoryMax=300M\nCPUQuota=60%\nNice=-5\nRestart=on-failure\nRestartSec=2.5\n" in text


def test_defaults_match_the_spec_table():
    assert U.PI_DEFAULTS == {"unit_memory_max": "200M", "unit_cpu_quota": "40%", "unit_nice": 5,
                             "unit_restart": "always", "unit_restart_sec": 3.0}
    assert U.SPEECH_DEFAULTS == {"unit_nice": 5, "unit_throttle_s": 15, "unit_keep_alive": True}


def test_values_from_file_overlays_defaults_and_survives_garbage(tmp_path):
    cfg = tmp_path / "mira.json"
    cfg.write_text(json.dumps({"core": {"unit_nice": 7, "unit_memory_max": "250M", "engine": "plugins"}}))
    values = U.values_from_file(cfg, "listener")
    assert values["unit_nice"] == 7 and values["unit_memory_max"] == "250M" and values["unit_cpu_quota"] == "40%"
    assert "engine" not in values
    cfg.write_text("{not json")
    assert U.values_from_file(cfg, "listener") == U.PI_DEFAULTS
    assert U.values_from_file(tmp_path / "missing.json", "speech") == U.SPEECH_DEFAULTS


def test_plist_replaces_only_the_three_keys():
    base = (WALL / "speech" / "org.aryavora.mira-speech.plist").read_bytes()
    out = U.render_plist({"unit_nice": 8, "unit_throttle_s": 30, "unit_keep_alive": False}, base)
    new, old = plistlib.loads(out), plistlib.loads(base)
    assert (new["Nice"], new["ThrottleInterval"], new["KeepAlive"]) == (8, 30, False)
    for key in old:
        if key not in ("Nice", "ThrottleInterval", "KeepAlive"):
            assert new[key] == old[key], key
    again = plistlib.loads(U.render_plist(U.SPEECH_DEFAULTS, base))
    assert (again["Nice"], again["ThrottleInterval"], again["KeepAlive"]) == (5, 15, True)


def test_plist_without_a_base_is_minimal_but_valid():
    doc = plistlib.loads(U.render_plist({}, None))
    assert doc["Label"] == "org.aryavora.mira-speech" and doc["Nice"] == 5


def test_check_reports_drift_and_match(tmp_path):
    ok, diff = U.check("listener", PI_VALUES, tmp_path)
    assert not ok and "mira.conf" in diff                    # nothing installed yet
    U.apply("listener", PI_VALUES, tmp_path, QuietRunner())
    assert U.check("listener", PI_VALUES, tmp_path) == (True, "")
    ok, diff = U.check("listener", dict(PI_VALUES, unit_nice=9), tmp_path)
    assert not ok and "-Nice=5" in diff and "+Nice=9" in diff


def test_apply_writes_reloads_and_never_restarts(tmp_path):
    runner = QuietRunner()
    done = U.apply("listener", PI_VALUES, tmp_path, runner)
    dest = tmp_path / ".config/systemd/user/mira-listen.service.d/mira.conf"
    assert dest.read_text() == GOLDEN_PI
    assert any("daemon-reload" in line for line in done)
    flat = " ".join(" ".join(c) for c in runner.calls)
    assert "daemon-reload" in flat and "restart" not in flat
    assert not list(dest.parent.glob("*.tmp"))


def test_apply_on_the_mini_writes_the_plist_and_calls_nothing(tmp_path):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "org.aryavora.mira-speech.plist").write_bytes((WALL / "speech" / "org.aryavora.mira-speech.plist").read_bytes())
    runner = QuietRunner()
    done = U.apply("speech", dict(U.SPEECH_DEFAULTS, unit_nice=9), tmp_path, runner)
    assert runner.calls == [] and any("kickstart" in line for line in done)
    assert plistlib.loads((agents / "org.aryavora.mira-speech.plist").read_bytes())["Nice"] == 9
    assert U.check("speech", dict(U.SPEECH_DEFAULTS, unit_nice=9), tmp_path)[0]


def test_files_have_no_en_or_em_dash():
    bad = chr(0x2013), chr(0x2014)
    for path in (WALL / "deploy" / "mira-listen.dropin.tmpl", WALL / "deploy" / "manifest.json",
                 WALL / "mirakit" / "unitgen.py"):
        assert not any(c in path.read_text(encoding="utf-8") for c in bad), path


def test_apply_points_current_at_a_release_like_spec_7_1_step_3(tmp_path):
    root = tmp_path / "mira-listen"
    (root / "releases" / "aaa").mkdir(parents=True)
    (root / "releases" / "bbb").mkdir()
    import os
    os.utime(root / "releases" / "aaa", (1, 1))
    done = U.apply("listener", PI_VALUES, tmp_path, QuietRunner())
    assert os.readlink(root / "current") == "releases/bbb" and "current -> releases/bbb" in done[1]     # newest staged
    done = U.apply("listener", PI_VALUES, tmp_path, QuietRunner())
    assert os.readlink(root / "current") == "releases/bbb" and done[1].startswith("current stays")      # left alone
    done = U.apply("listener", PI_VALUES, tmp_path, QuietRunner(), release="aaa")
    assert os.readlink(root / "current") == "releases/aaa" and not (root / ".current.tmp").exists()
    for bad in ("../x", "nope", "a b"):
        with pytest.raises(ValueError, match="not staged"):
            U.apply("listener", PI_VALUES, tmp_path, QuietRunner(), release=bad)
    assert os.readlink(root / "current") == "releases/aaa"


def test_apply_without_any_release_says_so_and_still_writes_the_dropin(tmp_path):
    done = U.apply("listener", PI_VALUES, tmp_path, QuietRunner())
    assert any("no release is staged" in line for line in done)
    assert (tmp_path / ".config/systemd/user/mira-listen.service.d/mira.conf").exists()
    assert not (tmp_path / "mira-listen" / "current").exists()
