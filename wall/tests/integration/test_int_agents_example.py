"""Integration: the worked example of wall/AGENTS.md (spec 2.13 item 3), executed step by step in a scratch copy of
`wall/` and `voice/models/kws`: scaffold the intent.clock plugin, register it, write its code and test, run the test,
add it to the example file, `mira docs`, `mira check`, `mira --offline describe intent.clock`. The scratch copy is
removed afterwards; the real tree is never touched. The plugin and test bodies below are the ones printed in the
agent contract."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parents[2]
REPO = WALL.parent

PLUGIN = '''"""intent.clock: answers "what time is it" locally. FIXED: PRIORITY (runs after music)."""
import re
import time
from mirakit.plugin import Knob, PluginBase, PluginSpec

PRIORITY = 50
ASK = re.compile(r"(what time is it|what's the time|what is the time)( please)?")

class ClockIntent:
    mark, log_name, done_reply, fail_reply = "clock", "clock", "Done.", "I could not tell the time."
    priority = PRIORITY
    def __init__(self, reply_format):
        self.reply_format = reply_format
    def match(self, text):
        return "time" if ASK.fullmatch(text) else None
    def handle(self, intent):
        return self.reply_format.format(time=time.strftime("%-I:%M %p"))

class ClockPlugin(PluginBase):
    def start(self):
        self.intent = ClockIntent(self.cfg["reply_format"])
        self.ctx.provide("intent", self.intent)
    def apply(self, changed, cfg):
        self.cfg = cfg
        self.intent.reply_format = cfg["reply_format"]

def create(ctx, cfg):
    return ClockPlugin(ctx, cfg)

SPEC = PluginSpec(
    name="intent.clock", kind="intent", version="1", machine="listener", provides=("intent",),
    summary="Answers what time it is without asking Hermes.", create=create, default_enabled=False,
    tests=("tests/plugins/test_lp_intent_clock.py",),
    knobs=(Knob("reply_format", "str", "It is {time}.", "sentence she says; {time} becomes the clock time", min=1, max=80,
                applies="live", agent="deny", spoken="the time sentence", topic="system"),),
)
'''

TEST = '''from mirakit.testkit import FakeCtx, run_plugin_contract
from listener_plugins.intent_clock import SPEC


def test_contract():
    run_plugin_contract(SPEC, start=False)


def test_match_and_apply():
    ctx = FakeCtx.with_specs(SPEC)
    cfg = ctx.cfg_of(SPEC.name)
    plugin = SPEC.create(ctx, cfg)
    plugin.start()
    try:
        intent = ctx.get("intent")
        assert intent.match("what time is it") == "time"
        assert intent.match("play some jazz") is None
        plugin.apply({"reply_format": "Now {time}."}, cfg.replace(reply_format="Now {time}."))
        assert intent.handle("time").startswith("Now ")
    finally:
        plugin.stop()
        ctx.release_all()
'''


@pytest.fixture
def scratch(tmp_path):
    root = tmp_path / "mira"
    (root / "voice" / "models").mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(WALL, root / "wall", ignore=ignore)
    shutil.copytree(REPO / "voice" / "models" / "kws", root / "voice" / "models" / "kws")
    return root / "wall"


def mira(wall, *args, machine="listener"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIRA_") and k != "PYTHONPATH"}
    env.update(HOME=str(wall.parent / "home"), PYTHONDONTWRITEBYTECODE="1", MIRA_MACHINE=machine)
    (wall.parent / "home").mkdir(exist_ok=True)
    p = subprocess.run([sys.executable, "-B", "-m", "mirakit.cli", *args], cwd=wall, env=env, capture_output=True,
                       text=True, timeout=300)
    return p.returncode, p.stdout, p.stderr


def test_the_worked_example_runs_as_written(scratch):
    wall = scratch
    rc, base_out, _ = mira(wall, "check")
    assert rc == 0
    n_before = int(re.search(r"ok \((\d+) plugins", base_out).group(1))

    # 1. scaffold: two files, nothing registered, the line to add is printed
    rc, out, err = mira(wall, "plugin", "new", "intent", "clock")
    assert rc == 0, err
    assert out.splitlines()[:2] == ["created listener_plugins/intent_clock.py", "created tests/plugins/test_lp_intent_clock.py"]
    assert out.splitlines()[2] == "add this line to specs():" and "intent_clock.SPEC," in out.splitlines()[3]
    assert (wall / "listener_plugins" / "intent_clock.py").exists()
    rc, out, _ = mira(wall, "check")
    assert rc == 0 and f"ok ({n_before} plugins" in out          # unregistered: the scaffold changes nothing the lint sees

    # 2. register it in listener_plugins/__init__.py, replace the body and the test
    init = wall / "listener_plugins" / "__init__.py"
    text = init.read_text()
    text = text.replace("from . import (agc_mic,", "from . import (intent_clock, agc_mic,", 1)
    text = text.replace("control_http)]", "control_http, intent_clock)]", 1)
    assert "intent_clock" in text.split("return")[1]
    init.write_text(text)
    (wall / "listener_plugins" / "intent_clock.py").write_text(PLUGIN)
    (wall / "tests" / "plugins" / "test_lp_intent_clock.py").write_text(TEST)

    # 3. its test
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/plugins/test_lp_intent_clock.py"], cwd=wall, capture_output=True, text=True,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=300)
    assert p.returncode == 0, p.stdout + p.stderr
    assert re.search(r"2 passed in [\d.]+s", p.stdout.strip().splitlines()[-1])

    # 4. the lint names the missing example entry, plus the generated files that went stale
    rc, out, _ = mira(wall, "check")
    assert rc == 1
    assert "example: mira.example.json: plugin intent.clock is missing" in out
    assert out.count("stale-generated:") == 3

    # 5. add it to the example file, regenerate, lint
    ex = wall / "deploy" / "mira.example.json"
    s = ex.read_text()
    i = s.index('  "intent.music"')
    ex.write_text(s[:i] + '  "intent.clock": {"enabled": false, "config": {"reply_format": "It is {time}."}},\n' + s[i:])
    json.loads(ex.read_text())
    rc, out, _ = mira(wall, "docs")
    assert rc == 0 and out.splitlines() == ["wrote docs/PLUGINS.md", "wrote docs/KNOBS.md",
                                            "wrote docs/contracts/schema.generated.json"]
    rc, out, _ = mira(wall, "check")
    assert rc == 0 and out.strip() == f"mira check: ok ({n_before + 1} plugins)"
    assert "intent.clock" in (wall / "docs" / "PLUGINS.md").read_text()

    # 6. describe, offline, as the listener machine
    rc, out, err = mira(wall, "--offline", "describe", "intent.clock")
    assert rc == 0, err
    assert out == (
        "intent.clock v1: Answers what time it is without asking Hermes.\n"
        "provides intent; needs -\n"
        "key           value          default        source   bounds  unit  applies  agent\n"
        "reply_format  It is {time}.  It is {time}.  default  1..80   -     live     deny\n")
    assert not (wall.parent / "home" / ".config").exists() or not list((wall.parent / "home" / ".config").rglob("listen.env"))


def test_without_a_machine_hint_a_mac_describes_the_speech_machine(scratch):
    """The agent contract's `mira --offline describe intent.clock` assumes the Pi. On the Mac the CLI picks the speech
    machine unless MIRA_MACHINE=listener or a listener --config is given, and says the plugin is unknown."""
    if sys.platform != "darwin":
        pytest.skip("the default machine is the listener off the Mac")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIRA_") and k != "PYTHONPATH"}
    env.update(HOME=str(scratch.parent / "home"), PYTHONDONTWRITEBYTECODE="1")
    (scratch.parent / "home").mkdir(exist_ok=True)
    p = subprocess.run([sys.executable, "-B", "-m", "mirakit.cli", "--offline", "describe", "agc.mic"], cwd=scratch, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 2 and "unknown plugin agc.mic" in p.stderr
