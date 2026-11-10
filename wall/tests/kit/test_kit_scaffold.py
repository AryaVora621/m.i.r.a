"""`mira plugin new` (P6): output passes the contract test and the lint, refuses to overwrite, runs standalone."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import lint as L  # noqa: E402
from mirakit import scaffold as S  # noqa: E402
from mirakit.testkit import run_plugin_contract  # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def root(tmp_path):
    return tmp_path / "wall"


def test_new_listener_plugin_writes_module_and_test(root):
    written, line = S.new_plugin(root, "intent", "clock", "listener")
    assert [p.relative_to(root).as_posix() for p in written] == [
        "listener_plugins/intent_clock.py", "tests/plugins/test_lp_intent_clock.py"]
    assert "intent_clock.SPEC" in line and "listener_plugins/__init__.py" in line
    text = written[0].read_text()
    assert "FIXED:" in text and "default_enabled=False" in text and "class IntentClock(PluginBase)" in text
    for method in ("def start", "def stop", "def apply", "def status"):
        assert method in text


def test_new_speech_plugin_uses_the_speech_names(root):
    written, line = S.new_plugin(root, "tts", "demo_two", "speech")
    assert [p.relative_to(root).as_posix() for p in written] == [
        "speech_plugins/tts_demo_two.py", "tests/speech/test_sp_tts_demo_two.py"]
    assert "speech_plugins/__init__.py" in line


def test_refuses_to_overwrite_and_bad_names(root):
    S.new_plugin(root, "intent", "clock")
    before = (root / "listener_plugins" / "intent_clock.py").read_text()
    with pytest.raises(S.ScaffoldError, match="overwrite"):
        S.new_plugin(root, "intent", "clock")
    (root / "tests/plugins/test_lp_intent_clock.py").unlink()
    with pytest.raises(S.ScaffoldError):                    # the module alone still blocks it
        S.new_plugin(root, "intent", "clock")
    assert (root / "listener_plugins" / "intent_clock.py").read_text() == before
    for kind, variant, machine in (("Intent", "x", "listener"), ("a", "b c", "listener"), ("a.b", "c", "listener"),
                                   ("1a", "b", "listener"), ("a", "", "listener"), ("a", "b", "mac")):
        with pytest.raises(S.ScaffoldError):
            S.new_plugin(root, kind, variant, machine)


def test_scaffolded_spec_passes_the_contract_and_the_lint(root):
    S.new_plugin(root, "intent", "clock")
    module = load(root / "listener_plugins" / "intent_clock.py", "scaffold_probe_intent_clock")
    spec = module.SPEC
    assert spec.default_enabled is False and spec.name == "intent.clock" and spec.machine == "listener"
    run_plugin_contract(spec)
    findings = (L.lint([spec], root, fast=True) + L.check_tests_exist([spec], root) + L.check_dashes(root))
    assert findings == [], [str(f) for f in findings]


def test_scaffolded_speech_spec_passes_too(root):
    S.new_plugin(root, "tts", "demo", "speech")
    module = load(root / "speech_plugins" / "tts_demo.py", "scaffold_probe_tts_demo")
    run_plugin_contract(module.SPEC)
    assert L.lint([module.SPEC], root, fast=True) == [] and L.check_tests_exist([module.SPEC], root) == []


def test_generated_test_file_runs_green_on_its_own(root):
    S.new_plugin(root, "intent", "clock")
    (root / "listener_plugins" / "__init__.py").write_text("")
    env = dict(os.environ, PYTHONPATH=str(WALL), PYTHONDONTWRITEBYTECODE="1", HOME=str(root))
    proc = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           str(root / "tests/plugins/test_lp_intent_clock.py")],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


def test_scaffold_files_have_no_en_or_em_dash(root):
    S.new_plugin(root, "intent", "clock")
    bad = chr(0x2013), chr(0x2014)
    for path in root.rglob("*.py"):
        assert not any(c in path.read_text(encoding="utf-8") for c in bad)
    assert not any(c in (WALL / "mirakit" / "scaffold.py").read_text(encoding="utf-8") for c in bad)
