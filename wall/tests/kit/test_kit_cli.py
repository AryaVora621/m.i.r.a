"""`mira` command line (P6): every command shape, exit codes 0 to 9, online and offline paths, no secret output.

Online tests talk to the real control server on an ephemeral loopback port, backed by FakeCore. Offline tests hand
the CLI an OfflineBackend over a FakeCore through the `offline_opener` hook; the real Core is exercised by P8.
"""
from __future__ import annotations

import json
import types
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import cli as CLI  # noqa: E402
from mirakit import control as C  # noqa: E402
from mirakit import deploy as D  # noqa: E402
from mirakit import plugin as P  # noqa: E402
from mirakit import testkit as TK  # noqa: E402

OWNER = "cliOWNER-secret-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
AGENT = "cliAGENT-secret-ffffffffffffffffffffffffffffffffffff"
PY = sys.executable


def listener_specs():
    def create(ctx, cfg):
        return P.PluginBase(ctx, cfg)

    agc = P.PluginSpec(
        name="agc.mic", kind="agc", version="1", summary="Mic gain", machine="listener", provides=("agc",), create=create,
        knobs=(
            P.Knob("target", "float", 0.05, "Speech level the gain aims for.", unit="rms", min=0.005, max=0.3,
                   agent="bounded", agent_min=0.03, agent_max=0.10, agent_step=0.01, topic="hearing",
                   spoken="microphone boost", effect="hears quiet speech", phrases=("you can't hear me",), env="MIRA_AGC_TARGET"),
            P.Knob("rest", "float", 3.0, "Gain in a quiet room.", unit="x", min=1, max=40, env="MIRA_AGC_REST"),
            P.Knob("floor_init", "float", 0.003, "Starting noise floor.", unit="rms", min=0, max=0.1, applies="restart"),
            P.Knob("tag", "str", "x", "A tag.", min=1, max=10),
        ))
    ctl = P.PluginSpec(
        name="control.http", kind="control", version="1", summary="Control API", machine="listener", provides=(),
        create=create, default_enabled=False, required=False, toggle="live",
        knobs=(P.Knob("agents_locked", "bool", False, "Kill switch."),))
    core = P.PluginSpec(
        name="core", kind="core", version="1", summary="Core", machine="listener", provides=(), create=create,
        core=True, knobs=(P.Knob("engine", "enum", "plugins", "Engine.", choices=("legacy", "plugins"), applies="restart"),
                          P.Knob("unit_nice", "int", 5, "Nice.", unit="x", min=-10, max=19, applies="restart")))
    wake = P.PluginSpec(
        name="wake.kws", kind="wake", version="1", summary="Wake", machine="listener", provides=("wake",), create=create,
        knobs=(P.Knob("threshold", "float", 0.15, "Sensitivity.", unit="score", min=0.01, max=0.5, applies="rebuild",
                        topic="wake", spoken="wake sensitivity"),))
    return [agc, ctl, core, wake]


@pytest.fixture
def world(tmp_path):
    (tmp_path / "owner").write_text(OWNER)
    (tmp_path / "agent").write_text(AGENT)
    core = TK.FakeCore(listener_specs(), clock=time.time())
    import logging
    cfg = P.PluginConfig("control.http", dict(bind="127.0.0.1", port=0, owner_token_file=str(tmp_path / "owner"),
                                              agent_token_file=str(tmp_path / "agent"), speech_url="", speech_token_file="",
                                              wait_s_default=0.0))
    handle = C.start_server(core, cfg, machine="listener", log=logging.getLogger("mira.test.cli"))
    host, port = handle.address
    state = dict(core=core, url=f"http://{host}:{port}", tmp=tmp_path, out=[], err=[], handle=handle)
    yield state
    handle.stop()


def run(world, *argv, token="owner", env=None, **hooks):
    world["out"].clear()
    world["err"].clear()
    tmp = world["tmp"]
    full_env = {"HOME": str(tmp), "MIRA_MACHINE": "listener", **(env or {})}
    args = list(argv)
    if "--offline" not in args and hooks.get("online", True):
        args += ["--url", world["url"], "--token-file", str(tmp / token)]
    hooks.pop("online", None)
    code = CLI.main(args, env=full_env, home=tmp, out=world["out"].append, err=world["err"].append, root=tmp / "wall", **hooks)
    return code, "\n".join(world["out"]), "\n".join(world["err"])


# ---------------------------------------------------------------- help and parsing
def test_help_exits_zero_and_lists_every_command_of_2_8(capsys):
    assert CLI.main(["--help"]) == 0
    text = capsys.readouterr().out
    for name in ("status", "inventory", "describe", "get", "set", "reset", "undo", "confirm", "changes", "session",
                 "enable", "disable", "reload", "restart", "agents", "preview", "voices", "schema", "tuning-guide",
                 "config", "adopt", "unit", "rollback", "deploy", "tokens", "doctor", "check", "docs", "plugin"):
        assert any(line.strip().startswith(name + " ") or line.strip() == name for line in text.splitlines()), name
    assert set(CLI.COMMANDS) == set(CLI.HANDLERS)
    assert "9 speech unreachable" in text


def test_the_help_gate_as_a_subprocess():
    proc = subprocess.run([PY, "-B", "-m", "mirakit.cli", "--help"], cwd=WALL, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert proc.returncode == 0 and "tuning-guide" in proc.stdout


def test_usage_errors_exit_1_not_argparse_2(world):
    assert run(world, "set")[0] == 1
    assert run(world, "bogus")[0] == 1
    assert run(world, "set", "novalue")[0] == 1
    assert run(world, "set", "nodot=1")[0] == 1
    assert CLI.main([]) == 1


def test_cli_imports_no_heavy_modules():
    code = ("import sys; import mirakit.cli; bad=[m for m in ('numpy','sherpa_onnx','mirakit.core','mirakit.envmap',"
            "'mira_listen','speech_plugins','listener_plugins') if m in sys.modules]; print(bad)")
    proc = subprocess.run([PY, "-B", "-c", code], cwd=WALL, capture_output=True, text=True)
    assert proc.stdout.strip() == "[]", proc.stdout + proc.stderr


# ---------------------------------------------------------------- online reads
def test_status_inventory_describe_get(world):
    code, out, _ = run(world, "status")
    assert code == 0 and "listener plugins" in out and "agents locked" in out
    code, out, _ = run(world, "inventory")
    assert code == 0 and "agc.mic" in out and "wake.kws" in out
    code, out, _ = run(world, "describe", "agc.mic")
    assert code == 0 and "target" in out and "0.03..0.1 step 0.01" in out and "deny" in out
    code, out, _ = run(world, "get", "agc.mic.target", "--effective")
    assert code == 0 and out.strip() == "agc.mic.target = 0.05  (default)"
    code, out, _ = run(world, "get", "agc.mic")
    assert code == 0 and "agc.mic.rest = 3" in out and "agc.mic.target = 0.05" in out
    code, out, err = run(world, "get", "nope.nothing")
    assert code == 2 and "nothing matches" in err
    code, out, err = run(world, "describe", "agc.mik")
    assert code == 2 and "unknown_plugin" in err


def test_json_output_is_the_api_shape(world):
    code, out, _ = run(world, "status", "--json")
    st = json.loads(out)
    assert code == 0 and st["machine"] == "listener" and "revision" in st
    code, out, _ = run(world, "inventory", "--json")
    assert isinstance(json.loads(out), list)
    code, out, _ = run(world, "set", "agc.mic.target=0.06", "--json")
    change = json.loads(out)
    assert change["edits"][0]["new"] == 0.06 and change["status"] == "applied" and change["revision"] == change["rev_after"]
    code, out, _ = run(world, "changes", "--json")
    assert json.loads(out)[0]["id"] == change["id"]


def test_schema_and_tuning_guide(world):
    code, out, _ = run(world, "schema")
    assert code == 0 and "agc.mic" in out
    code, out, _ = run(world, "schema", "agc.mic")
    assert code == 0 and "target" in out and "0.03..0.1 step 0.01" in out
    assert run(world, "schema", "agc.mik")[0] == 2
    code, out, _ = run(world, "tuning-guide", "hearing")
    assert code == 0 and "you can't hear me" in out and "0.03 0.04 0.05" in out
    code, out, _ = run(world, "tuning-guide", "wake")
    assert code == 0 and "owner only: wake.kws.threshold" in out
    assert run(world, "tuning-guide", "bogus")[0] == 1


# ---------------------------------------------------------------- writes and exit codes
def test_set_prints_one_line_per_edit_and_exits_0(world):
    code, out, _ = run(world, "set", "agc.mic.target=0.06", "--reason", "test")
    assert code == 0 and out == "agc.mic.target 0.05 -> 0.06 applied (L-000001)"
    code, out, _ = run(world, "set", "agc.mic.target=0.07", "agc.mic.rest=4")
    assert code == 0 and out.splitlines() == ["agc.mic.target 0.06 -> 0.07 applied (L-000002)",
                                              "agc.mic.rest 3 -> 4 applied (L-000002)"]
    assert world["core"].values["agc.mic.rest"] == 4.0


def test_exit_codes_for_every_outcome(world):
    # 0 applied, covered above; 1 invalid value
    assert run(world, "set", "agc.mic.target=loud")[0] == 1
    assert run(world, "set", "agc.mic.target=9")[0] == 1
    # 2 unknown key and unknown plugin
    code, _, err = run(world, "set", "agc.mic.targt=0.06")
    assert code == 2 and "unknown_key" in err
    assert run(world, "set", "agc.mik.target=0.06")[0] == 2
    # 4 policy refused: the agent token drives the CLI, a deny knob
    code, out, err = run(world, "set", "wake.kws.threshold=0.06", token="agent")
    assert code == 4 and "owner_command" in err and "mira set wake.kws.threshold" in err
    code, _, err = run(world, "set", "agc.mic.target=0.2", token="agent")
    assert code == 4 and "allowed" in err
    world["core"].agents_locked = True
    assert run(world, "set", "agc.mic.target=0.06", token="agent")[0] == 4
    world["core"].agents_locked = False
    # 5 conflict
    code, _, err = run(world, "set", "agc.mic.target=0.06", "--expect-rev", "99")
    assert code == 5 and "stale_revision" in err
    # 7 pending restart
    code, out, _ = run(world, "set", "agc.mic.floor_init=0.004")
    assert code == 7 and "pending_restart" in out
    # a wrong token is a policy-class refusal
    (world["tmp"] / "bad").write_text("nope")
    assert run(world, "status", token="bad")[0] == 4


def test_exit_3_queued_and_6_failed(tmp_path):
    import logging

    class Odd(TK.FakeCore):
        mode = "queued"

        def submit(self, edits, **kw):
            change = super().submit(edits, **kw)
            change.status = Odd.mode
            return change

    (tmp_path / "owner").write_text(OWNER)
    core = Odd(listener_specs())
    cfg = P.PluginConfig("control.http", dict(bind="127.0.0.1", port=0, owner_token_file=str(tmp_path / "owner"),
                                              agent_token_file=""))
    handle = C.start_server(core, cfg, machine="listener", log=logging.getLogger("t"))
    try:
        w = dict(url="http://%s:%d" % handle.address, tmp=tmp_path, out=[], err=[])
        assert run(w, "set", "agc.mic.target=0.06")[0] == 3
        Odd.mode = "failed"
        code, out, _ = run(w, "set", "agc.mic.target=0.07")
        assert code == 6 and "failed" in out
    finally:
        handle.stop()


def test_exit_8_when_nothing_answers_and_offline_is_not_given(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (tmp_path / "owner").write_text(OWNER)
    w = dict(url=f"http://127.0.0.1:{port}", tmp=tmp_path, out=[], err=[])
    code, out, err = run(w, "set", "agc.mic.target=0.06")
    assert code == 8 and "--offline" in err and str(port) in err
    assert run(w, "status")[0] == 8 and run(w, "inventory")[0] == 8


def test_exit_9_when_the_mini_does_not_answer(tmp_path, world):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    (world["tmp"] / "pi").write_text("tok")
    world["core"].register_remote("speech", C.HttpRemote(f"http://127.0.0.1:{dead}", str(world["tmp"] / "pi"), 1.0))
    code, _, err = run(world, "inventory", "--machine", "speech")
    assert code == 9 and "speech_unreachable" in err
    assert run(world, "voices")[0] == 9
    assert run(world, "set", "tts.engine.speed=1.1")[0] == 9


def test_unreadable_token_file_is_a_usage_error_not_a_crash(world):
    code, _, err = run(world, "status", token="nothing-here")
    assert code == 1 and "tokens install" in err


def test_reset_reads_the_default_and_writes_it(world):
    run(world, "set", "agc.mic.target=0.08")
    code, out, _ = run(world, "reset", "agc.mic.target", "agc.mic.rest")
    assert code == 0 and world["core"].values["agc.mic.target"] == 0.05
    assert "agc.mic.target 0.08 -> 0.05 applied" in out
    assert run(world, "reset", "agc.mic.nope")[0] == 2
    assert run(world, "reset", "agc.mik.target")[0] == 2


def test_undo_confirm_changes_session_toggle_preview(world):
    code, out, _ = run(world, "set", "agc.mic.target=0.06", "--trial", "300")
    assert code == 0 and "trial until" in out
    cid = out.split("(")[1].split(")")[0]
    code, out, _ = run(world, "changes", "-n", "5")
    assert code == 0 and cid in out and "owner" in out and "applied" in out
    code, out, _ = run(world, "undo", cid, "--force")
    assert code == 0 and "agc.mic.target 0.06 -> 0.05" in out
    code, out, _ = run(world, "set", "agc.mic.target=0.07", "--trial", "300")
    cid = out.split("(")[1].split(")")[0]
    code, out, _ = run(world, "confirm", cid)
    assert code == 0 and "confirmed" in out
    code, out, _ = run(world, "session")
    assert code == 0 and "no voice session" in out
    code, out, _ = run(world, "session", "start", "--topic", "voice")
    assert code == 0 and out.startswith("V-0001 open topic voice")
    code, out, _ = run(world, "session", "end")
    assert code == 0 and "ended" in out
    code, out, _ = run(world, "disable", "agc.mic")
    assert code == 7 and "agc.mic.enabled true -> false pending_restart" in out      # a restart-toggled plugin
    assert run(world, "enable", "agc.mic")[0] == 7 and run(world, "reload", "agc.mic")[0] == 0
    code, out, _ = run(world, "preview", "--voice", "jenny", "--text", "hello", "--speed", "1.1")
    assert code == 0 and out.startswith("played voice jenny")
    assert world["core"].previews[-1]["text"] == "hello"
    code, out, _ = run(world, "agents", "lock")
    assert code == 0 and "control.http.agents_locked false -> true" in out
    assert run(world, "agents", "unlock")[0] == 0


def test_restart_writes_the_planned_marker_and_asks_the_process(world):
    code, out, _ = run(world, "restart")
    assert code == 0 and world["core"].restart_requested
    marker = world["tmp"] / ".config" / "mira" / "state" / "planned"
    assert marker.read_text().strip().isdigit()


class RecRunner(D.Runner):
    def __init__(self, **kw):
        super().__init__(None)
        self.calls = []

    def _exec(self, argv, input, timeout):
        self.calls.append(argv)
        return D.Result(0)


def test_restart_falls_back_to_the_service_manager_and_mini_uses_launchctl(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (tmp_path / "owner").write_text(OWNER)
    rec = []
    w = dict(url=f"http://127.0.0.1:{port}", tmp=tmp_path, out=[], err=[])
    code, out, err = run(w, "restart", runner_factory=lambda **kw: rec.append(RecRunner()) or rec[-1])
    assert code == 0 and "systemctl --user restart" in rec[0].calls[0][2] and "mira-listen" in rec[0].calls[0][-1]
    assert (tmp_path / ".config" / "mira" / "state" / "planned").exists()
    rec.clear()
    code, out, err = run(w, "restart", env={"MIRA_MACHINE": "speech"}, runner_factory=lambda **kw: rec.append(RecRunner()) or rec[-1])
    assert code == 0 and "launchctl kickstart -k" in rec[0].calls[0][2] and rec[0].calls[0][-1] == "org.aryavora.mira-speech"


# ---------------------------------------------------------------- offline
def offline_world(tmp_path):
    core = TK.FakeCore(listener_specs())
    opened = []

    def opener(ctx):
        opened.append(ctx.cfg_path)
        return CLI.OfflineBackend(core)

    return core, opener, opened


def test_offline_set_runs_the_same_dispatcher_in_process(tmp_path):
    core, opener, opened = offline_world(tmp_path)
    w = dict(tmp=tmp_path, out=[], err=[])
    code, out, _ = run(w, "set", "agc.mic.target=0.06", "--offline", offline_opener=opener)
    assert code == 0 and out == "agc.mic.target 0.05 -> 0.06 applied (L-000001)" and core.values["agc.mic.target"] == 0.06
    assert opened == [tmp_path / ".config" / "mira" / "mira.json"]
    code, out, _ = run(w, "status", "--offline", "--json", offline_opener=opener)
    assert code == 0 and json.loads(out)["machine"] == "listener"
    code, out, err = run(w, "set", "wake.kws.nope=1", "--offline", offline_opener=opener)
    assert code == 2 and "unknown_key" in err


def test_offline_treats_queued_as_done_because_nothing_is_running(tmp_path):
    class Queued(TK.FakeCore):
        def submit(self, edits, **kw):
            change = super().submit(edits, **kw)
            change.status = "queued"
            return change

    core = Queued(listener_specs())
    w = dict(tmp=tmp_path, out=[], err=[])
    code, out, _ = run(w, "set", "agc.mic.target=0.06", "--offline", offline_opener=lambda ctx: CLI.OfflineBackend(core))
    assert code == 0 and "queued" in out


def test_offline_without_the_core_module_is_a_clean_error(tmp_path, monkeypatch):
    w = dict(tmp=tmp_path, out=[], err=[])
    monkeypatch.setitem(sys.modules, "mirakit.core", None)        # makes `from mirakit.core import Core` an ImportError
    code, out, err = run(w, "status", "--offline")
    assert code == 1 and "offline mode needs the core module" in err


# ---------------------------------------------------------------- rollback
def test_rollback_sets_legacy_marks_planned_restarts_and_lists_what_legacy_loses(world):
    run(world, "set", "agc.mic.tag=custom")                       # no env, no arg: legacy cannot carry it
    run(world, "set", "agc.mic.rest=4")                           # has an env var: representable
    rec = []
    code, out, err = run(world, "rollback", runner_factory=lambda **kw: rec.append(RecRunner()) or rec[-1])
    assert code == 0
    assert world["core"].values["core.engine"] == "legacy"
    assert "core.engine plugins -> legacy" in out
    assert "agc.mic.tag = custom (legacy default x)" in out and "agc.mic.rest" not in out.split("cannot represent")[1]
    assert (world["tmp"] / ".config" / "mira" / "state" / "planned").exists()
    assert "systemctl --user restart" in rec[0].calls[0][2]


def test_rollback_works_on_the_file_when_the_process_is_down(tmp_path):
    core, opener, _ = offline_world(tmp_path)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (tmp_path / "owner").write_text(OWNER)
    w = dict(url=f"http://127.0.0.1:{port}", tmp=tmp_path, out=[], err=[])
    rec = []
    code, out, err = run(w, "rollback", offline_opener=opener, runner_factory=lambda **kw: rec.append(RecRunner()) or rec[-1])
    assert code == 0 and core.values["core.engine"] == "legacy" and "working on the file instead" in err


# ---------------------------------------------------------------- config
def test_config_render_full_and_reload_and_diff(world):
    code, out, _ = run(world, "config", "render", "--full")
    doc = json.loads(out)
    assert code == 0 and doc["machine"] == "listener" and doc["plugins"]["agc.mic"]["config"]["target"] == 0.05
    assert run(world, "config", "render")[0] == 1
    code, out, _ = run(world, "config", "reload")
    assert code == 0 and "settings file" in out
    # diff compares the file with the newest history revision
    cfgdir = world["tmp"] / ".config" / "mira"
    (cfgdir / "history").mkdir(parents=True)
    (cfgdir / "mira.json").write_text('{"a": 2}\n')
    (cfgdir / "history" / "listener.3.json").write_text('{"a": 1}\n')
    (cfgdir / "history" / "listener.12.json").write_text('{"a": 0}\n')
    code, out, _ = run(world, "config", "diff")
    assert code == 0 and '-{"a": 0}' in out and '+{"a": 2}' in out
    code, out, _ = run(world, "config", "diff", "3")
    assert '-{"a": 1}' in out
    assert run(world, "config", "diff", "99")[0] == 1


def test_config_edit_submits_the_difference_as_one_batch(world):
    def editor(argv):
        path = Path(argv[1])
        doc = json.loads(path.read_text())
        doc["plugins"]["agc.mic"]["config"]["target"] = 0.07
        doc["plugins"]["agc.mic"]["config"]["rest"] = 5.0
        doc["plugins"]["wake.kws"]["enabled"] = False
        path.write_text(json.dumps(doc))
        return 0

    code, out, _ = run(world, "config", "edit", editor=editor)
    assert code == 7                                              # the wake plugin toggles with a restart
    assert "agc.mic.target 0.05 -> 0.07 applied" in out and "agc.mic.rest 3 -> 5 applied" in out
    assert "wake.kws.enabled true -> false" in out
    assert world["core"].values["agc.mic.target"] == 0.07 and world["core"].enabled["wake.kws"] is False
    # no change, bad JSON, editor failure, and a batch that fails validation changes nothing
    code, out, _ = run(world, "config", "edit", editor=lambda argv: 0)
    assert code == 0 and "no changes" in out
    assert run(world, "config", "edit", editor=lambda argv: Path(argv[1]).write_text("{nope") and 0)[0] == 1
    assert run(world, "config", "edit", editor=lambda argv: 3)[0] == 1

    def bad_editor(argv):
        path = Path(argv[1])
        doc = json.loads(path.read_text())
        doc["plugins"]["agc.mic"]["config"]["target"] = 0.08
        doc["plugins"]["agc.mic"]["config"]["rest"] = 999
        path.write_text(json.dumps(doc))
        return 0

    before = dict(world["core"].values)
    code, _, err = run(world, "config", "edit", editor=bad_editor)
    assert code == 1 and world["core"].values == before


def test_config_validate_uses_the_real_core_on_a_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))      # a Core built from a file must never see the real home
    example = WALL / "deploy" / "mira.example.json"
    w = dict(tmp=tmp_path, out=[], err=[])
    cfg_before = {p for p in tmp_path.rglob("*")}
    code = CLI.main(["config", "validate", str(example)], env={"HOME": str(tmp_path), "MIRA_MACHINE": "listener"},
                    home=tmp_path, out=w["out"].append, err=w["err"].append, root=WALL)
    assert code == 0, w["err"]
    assert w["out"] == [f"ok: {example} is valid for the listener"]
    assert {p for p in tmp_path.rglob("*")} == cfg_before             # nothing written under the home
    bad = tmp_path / "bad.json"
    doc = json.loads(example.read_text())
    doc["plugins"]["agc.mic"]["config"]["target"] = 99
    bad.write_text(json.dumps(doc))
    w["out"].clear(), w["err"].clear()
    assert CLI.main(["config", "validate", str(bad)], env={"HOME": str(tmp_path)}, home=tmp_path, out=w["out"].append,
                    err=w["err"].append, root=WALL) == 1
    nothing = tmp_path / "nothing.json"
    assert CLI.main(["config", "validate", str(nothing)], env={"HOME": str(tmp_path)}, home=tmp_path,
                    out=lambda s: None, err=lambda s: None, root=WALL) == 1
    nothing.write_text("{nope")
    assert CLI.main(["config", "validate", str(nothing)], env={"HOME": str(tmp_path)}, home=tmp_path,
                    out=lambda s: None, err=lambda s: None, root=WALL) == 1


# ---------------------------------------------------------------- adopt
def test_adopt_speech_dry_run_then_write(tmp_path):
    plist = tmp_path / "speech.plist"
    plist.write_bytes((WALL / "speech" / "org.aryavora.mira-speech.plist").read_bytes())
    out, err = [], []
    env = {"HOME": str(tmp_path), "MIRA_MACHINE": "speech"}
    cfg = tmp_path / ".config" / "mira" / "speech.json"
    code = CLI.main(["adopt", "--from-plist", str(plist)], env=env, home=tmp_path, out=out.append, err=err.append, root=WALL)
    assert code == 0 and not cfg.exists()
    text = "\n".join(out)
    assert "legacy effective" in text and "stt.engine.kind" in text and "faster-whisper:tiny.en" in text and "plist" in text
    assert "dry run" in text
    out.clear()
    code = CLI.main(["adopt", "--from-plist", str(plist), "--write"], env=env, home=tmp_path, out=out.append, err=err.append,
                    root=WALL)
    assert code == 0 and cfg.exists() and stat.S_IMODE(cfg.stat().st_mode) == 0o600
    doc = json.loads(cfg.read_text())
    assert doc["machine"] == "speech" and doc["core"]["engine"] == "legacy" and doc["core"]["legacy_env"] == "ignore"
    assert doc["plugins"]["stt.engine"]["config"]["kind"] == "faster-whisper:tiny.en"
    assert CLI.main(["adopt", "--from-plist", str(tmp_path / "none.plist")], env=env, home=tmp_path,
                    out=out.append, err=err.append, root=WALL) == 1


def test_adopt_listener_reads_the_unit_and_env_file(tmp_path):
    cfgdir = tmp_path / ".config" / "mira"
    cfgdir.mkdir(parents=True)
    (cfgdir / "listen.env").write_text("MIRA_AGC_TARGET=0.07\n# comment\nMIRA_KWS_THRESHOLD='0.05'\n")
    unit = tmp_path / ".config" / "systemd" / "user" / "mira-listen.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=%h/mira-listen/venv/bin/python %h/mira-listen/mira_listen.py \\\n"
                    "  --kws-dir %h/mira-listen/kws --end-silence 0.8\n")
    out, err = [], []
    code = CLI.main(["adopt"], env={"HOME": str(tmp_path), "MIRA_MACHINE": "listener"}, home=tmp_path, out=out.append,
                    err=err.append, root=WALL)
    text = "\n".join(out)
    assert code == 0, err
    assert "agc.mic.target" in text and "0.07" in text and "env" in text
    assert "vad.silero.end_silence" in text and "0.8" in text and "flag" in text
    assert not (cfgdir / "mira.json").exists()


def _listener_adopt_setup(tmp_path):
    cfgdir = tmp_path / ".config" / "mira"
    cfgdir.mkdir(parents=True)
    original = "MIRA_AGC_TARGET=0.07\nMIRA_KWS_THRESHOLD=0.05\n"
    (cfgdir / "listen.env").write_text(original)
    unit = tmp_path / ".config" / "systemd" / "user" / "mira-listen.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=%h/mira-listen/venv/bin/python %h/mira-listen/mira_listen.py --end-silence 0.8\n")
    return cfgdir, original


def test_adopt_listener_write_freezes_values_and_writes_the_legacy_mirror(tmp_path, monkeypatch):
    """Cutover step 7.1.2: mira.json (engine still legacy), listen.env regenerated with the hand-written one kept as
    .bak-adopt-<date>, legacy.args written. Runs the real Core in mirror mode on a temp home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfgdir, original = _listener_adopt_setup(tmp_path)
    out, err = [], []
    code = CLI.main(["adopt", "--write"], env={"HOME": str(tmp_path), "MIRA_MACHINE": "listener"}, home=tmp_path,
                    out=out.append, err=err.append, root=WALL)
    assert code == 0, err
    cfg = cfgdir / "mira.json"
    doc = json.loads(cfg.read_text())
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert doc["core"]["engine"] == "legacy" and doc["core"]["legacy_env"] == "ignore"
    assert doc["plugins"]["agc.mic"]["config"]["target"] == 0.07
    assert doc["plugins"]["wake.kws"]["config"]["threshold"] == 0.05
    assert doc["plugins"]["vad.silero"]["config"]["end_silence"] == 0.8
    assert (cfgdir / "listen.env").read_text().startswith("# generated by mirakit")
    assert "MIRA_AGC_TARGET=0.07" in (cfgdir / "listen.env").read_text()
    baks = list(cfgdir.glob("listen.env.bak-adopt-*"))
    assert len(baks) == 1 and baks[0].read_text() == original
    assert (cfgdir / "legacy.args").exists()
    assert "--end-silence" in (cfgdir / "legacy.args").read_text().split()
    assert "wrote the legacy mirror" in "\n".join(out)


def test_legacy_args_written_by_adopt_are_accepted_by_the_v1_parser(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfgdir, _ = _listener_adopt_setup(tmp_path)
    CLI.main(["adopt", "--write"], env={"HOME": str(tmp_path), "MIRA_MACHINE": "listener"}, home=tmp_path,
             out=lambda s: None, err=lambda s: None, root=WALL)
    import importlib
    v1 = importlib.import_module("mira_listen_v1")
    tokens = (cfgdir / "legacy.args").read_text().splitlines()
    v1.build_parser().parse_args(tokens)


def test_adopt_refuses_to_overwrite_an_existing_file_and_force_keeps_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfgdir, _ = _listener_adopt_setup(tmp_path)
    cfg = cfgdir / "mira.json"
    existing = json.dumps({"format": 1, "machine": "listener", "revision": 7, "core": {"engine": "plugins"},
                           "plugins": {"agc.mic": {"config": {"target": 0.09}}}}) + "\n"
    cfg.write_text(existing)
    env = {"HOME": str(tmp_path), "MIRA_MACHINE": "listener"}
    out, err = [], []
    code = CLI.main(["adopt", "--write"], env=env, home=tmp_path, out=out.append, err=err.append, root=WALL)
    assert code == CLI.EXIT_USAGE and "--force" in "\n".join(err)
    assert cfg.read_text() == existing and not (cfgdir / "history").exists()
    out, err = [], []
    code = CLI.main(["adopt", "--write", "--force"], env=env, home=tmp_path, out=out.append, err=err.append, root=WALL)
    assert code == 0, err
    assert (cfgdir / "history" / "listener.7.json").read_text() == existing
    assert json.loads(cfg.read_text())["core"]["engine"] == "legacy"


def test_rollback_style_sanitize_drops_flags_v1_does_not_define(tmp_path):
    cfgdir = tmp_path / ".config" / "mira"
    cfgdir.mkdir(parents=True)
    (cfgdir / "legacy.args").write_text("--end-silence\n0.8\n--preroll-chunks\n15\n--offline-text\nSpeech is offline\n")
    ctx = types.SimpleNamespace(machine="listener", cfgdir=cfgdir)
    assert CLI.sanitize_legacy_args(ctx) == ["--preroll-chunks", "--offline-text"]
    assert (cfgdir / "legacy.args").read_text() == "--end-silence\n0.8\n"


def test_env_file_and_execstart_parsers(tmp_path):
    env = tmp_path / "e"
    env.write_text('A=1\n# c\nexport B="two words"\nC=\'x\'\nnoequals\n')
    assert CLI.read_env_file(env) == {"A": "1", "B": "two words", "C": "x"}
    assert CLI.read_env_file(tmp_path / "missing") == {}
    text = "ExecStart=\nExecStart=%h/v/python -B %h/m/mira_listen.py \\\n   --a 1 --b\n"
    assert CLI.execstart_argv(text, Path("/h")) == ["--a", "1", "--b"]
    assert CLI.execstart_argv("[Service]\n", Path("/h")) == []


# ---------------------------------------------------------------- unit, deploy, tokens
def test_unit_render_check_apply(tmp_path):
    out, err = [], []
    env = {"HOME": str(tmp_path), "MIRA_MACHINE": "listener"}
    rec = []

    def call(*argv):
        out.clear()
        code = CLI.main(list(argv), env=env, home=tmp_path, out=out.append, err=err.append, root=WALL,
                        runner_factory=lambda **kw: rec.append(RecRunner()) or rec[-1])
        return code, "\n".join(out)

    code, text = call("unit", "render")
    assert code == 0 and "MemoryMax=200M" in text and "RestartSec=3" in text and "would install" in text
    code, text = call("unit", "render", "--check")
    assert code == 1 and "+MemoryMax=200M" in text
    code, text = call("unit", "apply")
    assert code == 0 and (tmp_path / ".config/systemd/user/mira-listen.service.d/mira.conf").exists()
    assert "daemon-reload" in rec[-1].calls[0][2]
    code, text = call("unit", "render", "--check")
    assert code == 0 and "matches" in text


def test_deploy_dry_run_lists_commands_and_refuses_ambiguity(tmp_path):
    from test_kit_deploy import FakeRemote, GitStub, repo_tree
    wall = tmp_path / "repo" / "wall"
    for name, data in repo_tree().items():
        path = tmp_path / "repo" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (wall / "deploy").mkdir(exist_ok=True)
    (wall / "deploy" / "manifest.json").write_text((WALL / "deploy" / "manifest.json").read_text())
    out, err = [], []
    remote = FakeRemote()
    remote.dry_run = True

    def call(*argv, remote=remote):
        out.clear(), err.clear()
        code = CLI.main(list(argv), env={"HOME": str(tmp_path), "MIRA_MACHINE": "listener"}, home=tmp_path, out=out.append,
                        err=err.append, root=wall, runner_factory=lambda **kw: remote, local_runner=GitStub(repo_tree()))
        return code, "\n".join(out)

    code, text = call("deploy", "pi", "--dry-run")
    assert code == 0 and "planned commands:" in text and "ln -sfn" in text and "systemctl" in text
    assert remote.service_calls == []
    code, text = call("deploy", "speech", "--dry-run")
    assert code == 0 and "launchctl kickstart -k gui/501/org.aryavora.mira-speech" in text
    code, text = call("deploy", "pi")
    assert code == 1 and "choose one of" in "\n".join(err)
    code, _ = call("deploy", "bogus")
    assert code == 1


def test_deploy_dirty_tree_is_exit_6(tmp_path):
    from test_kit_deploy import FakeRemote, GitStub, repo_tree
    wall = tmp_path / "wall"
    (wall / "deploy").mkdir(parents=True)
    (wall / "deploy" / "manifest.json").write_text((WALL / "deploy" / "manifest.json").read_text())
    err = []
    code = CLI.main(["deploy", "pi", "--stage"], env={"HOME": str(tmp_path)}, home=tmp_path, out=lambda s: None, err=err.append,
                    root=wall, runner_factory=lambda **kw: FakeRemote(), local_runner=GitStub(repo_tree(), dirty=True))
    assert code == 6 and "uncommitted" in "\n".join(err)


def test_tokens_install_never_prints_a_value(tmp_path):
    out, err = [], []
    rec = []
    code = CLI.main(["tokens", "install", "--machine", "speech"], env={"HOME": str(tmp_path), "MIRA_MACHINE": "speech"},
                    home=tmp_path, out=out.append, err=err.append, root=WALL, runner_factory=lambda **kw: rec.append(RecRunner()) or rec[-1])
    assert code == 0
    cfg = tmp_path / ".config" / "mira"
    text = "\n".join(out) + "\n".join(err)
    assert "created" in text and "0600" in text
    for name in ("control.owner.token", "control.pi.token"):
        assert (cfg / name).read_text().strip() not in text and (cfg / name).exists()
    out.clear()
    CLI.main(["tokens", "install", "--machine", "speech", "--json"], env={"HOME": str(tmp_path)}, home=tmp_path,
             out=out.append, err=err.append, root=WALL)
    assert [r["state"] for r in json.loads("\n".join(out))] == ["exists", "exists"]


# ---------------------------------------------------------------- doctor, check, docs, plugin
def test_doctor_flags_open_tokens_wildcard_binds_and_ignored_env(tmp_path):
    cfgdir = tmp_path / ".config" / "mira"
    cfgdir.mkdir(parents=True)
    (cfgdir / "control.owner.token").write_text("owner-secret-doctor")
    os.chmod(cfgdir / "control.owner.token", 0o644)
    (cfgdir / "mira.json").write_text(json.dumps({
        "format": 1, "machine": "listener", "revision": 4,
        "core": {"engine": "plugins", "legacy_env": "ignore"},
        "plugins": {"control.http": {"enabled": True, "config": {"bind": "0.0.0.0", "port": 59999}}}}))
    os.chmod(cfgdir / "mira.json", 0o600)
    (cfgdir / "listen.env").write_text("MIRA_AGC_TARGET=0.07\n")
    unit = tmp_path / ".config" / "systemd" / "user" / "mira-listen.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=%h/v/python %h/m/mira_listen.py --kws-dir %h/k --end-silence 0.8\n")
    ctx_out, ctx_err = [], []
    code = CLI.main(["doctor", "--json"], env={"HOME": str(tmp_path), "MIRA_MACHINE": "listener"}, home=tmp_path,
                    out=ctx_out.append, err=ctx_err.append, root=WALL)
    rows = {r["check"]: r for r in json.loads("\n".join(ctx_out))}
    assert code == 1
    assert rows["token modes"]["status"] == "fail" and "owner 0644" in rows["token modes"]["detail"]
    assert rows["binds"]["status"] == "fail" and "0.0.0.0" in rows["binds"]["detail"]
    assert rows["env ignored"]["status"] == "warn" and "MIRA_AGC_TARGET" in rows["env ignored"]["detail"]
    assert rows["argv overrides"]["status"] == "warn" and "--end-silence" in rows["argv overrides"]["detail"]
    assert "--kws-dir" not in rows["argv overrides"]["detail"]
    assert rows["last good"]["status"] == "warn" and rows["unit"]["status"] == "warn"
    assert "owner-secret-doctor" not in "\n".join(ctx_out)
    assert {"config", "unit", "pins", "port"} <= set(rows)


def test_doctor_is_clean_on_a_tidy_machine(tmp_path):
    cfgdir = tmp_path / ".config" / "mira"
    cfgdir.mkdir(parents=True)
    for name in ("control.owner.token",):
        (cfgdir / name).write_text("x")
        os.chmod(cfgdir / name, 0o600)
    (cfgdir / "mira.json").write_text(json.dumps({"format": 1, "machine": "listener", "revision": 1, "core": {"engine": "plugins"},
                                                  "plugins": {}}))
    os.chmod(cfgdir / "mira.json", 0o600)
    (cfgdir / "mira.json.lastgood").write_text("{}")
    from mirakit import unitgen
    unitgen.apply("listener", unitgen.PI_DEFAULTS, tmp_path, RecRunner())
    ctx = CLI.Ctx(args=CLI.build_parser().parse_args(["doctor"]), env={}, home=tmp_path, out=print, err=print, root=WALL,
                  machine="listener", cfg_path=cfgdir / "mira.json")
    rows = {r["check"]: r for r in CLI.doctor_checks(ctx, port_probe=lambda host, port: False)}
    assert rows["unit"]["status"] == "ok" and rows["config"]["status"] == "ok" and rows["last good"]["status"] == "ok"
    assert rows["env ignored"]["status"] == "ok" and rows["argv overrides"]["status"] == "ok"
    assert rows["port"]["status"] == "ok" and rows["binds"]["status"] == "ok"
    assert rows["token modes"]["status"] in ("ok", "warn")


@pytest.fixture
def temp_plugin_root(tmp_path, monkeypatch):
    names = ("listener_plugins", "speech_plugins")
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in names}
    for k in saved:
        del sys.modules[k]
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p or ".").resolve() != WALL])
    root = tmp_path / "wall"
    (root / "listener_plugins").mkdir(parents=True)
    yield root
    for k in [k for k in sys.modules if k.split(".")[0] in names]:
        del sys.modules[k]
    sys.modules.update(saved)


def write_pkg(root, bad=False):
    from mirakit import scaffold
    scaffold.new_plugin(root, "intent", "clock")
    if bad:
        mod = root / "listener_plugins" / "intent_clock.py"
        mod.write_text(mod.read_text().replace('"Example delay; replace this knob with a real one."', '""'))
    (root / "listener_plugins" / "__init__.py").write_text(
        "def specs():\n    from . import intent_clock\n    return [intent_clock.SPEC]\n")


def test_plugin_new_check_and_docs_round_trip(temp_plugin_root, tmp_path):
    root = temp_plugin_root
    out, err = [], []
    env = {"HOME": str(tmp_path), "MIRA_MACHINE": "listener"}

    def call(*argv):
        out.clear(), err.clear()
        return CLI.main(list(argv), env=env, home=tmp_path, out=out.append, err=err.append, root=root)

    assert call("plugin", "new", "intent", "clock") == 0
    text = "\n".join(out)
    assert "created listener_plugins/intent_clock.py" in text and "created tests/plugins/test_lp_intent_clock.py" in text
    assert "intent_clock.SPEC" in text
    assert call("plugin", "new", "intent", "clock") == 1 and "overwrite" in "\n".join(err)
    assert call("plugin", "new", "Bad", "x") == 1
    (root / "listener_plugins" / "__init__.py").write_text(
        "def specs():\n    from . import intent_clock\n    return [intent_clock.SPEC]\n")
    assert call("check", "--fast") == 0 and "mira check: ok (1 plugins, fast)" in "\n".join(out)
    assert call("check") == 1 and "stale-generated" in "\n".join(out)         # docs not generated yet
    assert call("docs") == 0
    assert (root / "docs" / "PLUGINS.md").exists() and "intent.clock" in (root / "docs" / "KNOBS.md").read_text()
    # the scaffolded test lives under tests/plugins, relative to wall/, so the "wall/..." test path resolves via parent
    (root.parent / "wall").mkdir(exist_ok=True)
    assert call("check") == 0, out


def test_check_reports_a_bad_knob_with_file_and_knob(temp_plugin_root, tmp_path):
    write_pkg(temp_plugin_root, bad=True)
    out, err = [], []
    code = CLI.main(["check", "--fast"], env={"HOME": str(tmp_path)}, home=tmp_path, out=out.append, err=err.append,
                    root=temp_plugin_root)
    assert code == 1 and any(line.startswith("knob-description: intent.clock.example_s:") for line in out)
    assert "1 finding" in "\n".join(err)


def test_check_and_docs_with_no_plugins_are_usage_errors(tmp_path, monkeypatch):
    names = ("listener_plugins", "speech_plugins")
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in names}
    for k in saved:
        del sys.modules[k]
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p or ".").resolve() != WALL])
    try:
        err = []
        assert CLI.main(["check"], env={"HOME": str(tmp_path)}, home=tmp_path, out=lambda s: None, err=err.append,
                        root=tmp_path) == 1
        assert "nothing was checked" in "\n".join(err)
        assert CLI.main(["docs"], env={"HOME": str(tmp_path)}, home=tmp_path, out=lambda s: None, err=err.append,
                        root=tmp_path) == 1
    finally:
        sys.modules.update(saved)


# ---------------------------------------------------------------- the shell wrapper
def test_wrapper_picks_current_then_newest_release_then_mini(tmp_path):
    script = WALL / "bin" / "mira"
    assert os.access(script, os.X_OK)
    pi = tmp_path / "pi"
    (pi / "venv" / "bin").mkdir(parents=True)
    py = pi / "venv" / "bin" / "python"
    py.write_text('#!/bin/sh\necho "py=$0 path=$PYTHONPATH machine=$MIRA_MACHINE args=$*"\n')
    os.chmod(py, 0o755)
    env = dict(os.environ, MIRA_PI_ROOT=str(pi), MIRA_SPEECH_ROOT=str(tmp_path / "none"), PYTHONPATH="")
    env.pop("PYTHONPATH")
    run_ = lambda *a: subprocess.run([str(script), *a], capture_output=True, text=True, env=env)
    res = run_("status")
    assert res.returncode == 127 and "no release" in res.stderr
    (pi / "releases" / "old").mkdir(parents=True)
    os.utime(pi / "releases" / "old", (1, 1))
    (pi / "releases" / "new").mkdir()
    res = run_("status", "--json")
    assert f"path={pi}/releases/new" in res.stdout and "machine=listener" in res.stdout
    assert "args=-B -m mirakit.cli status --json" in res.stdout
    (pi / "current").mkdir()
    assert f"path={pi}/current" in run_("x").stdout
    mini = tmp_path / "mini"
    (mini / "venv" / "bin").mkdir(parents=True)
    mpy = mini / "venv" / "bin" / "python"
    mpy.write_text('#!/bin/sh\necho "path=$PYTHONPATH machine=$MIRA_MACHINE"\n')
    os.chmod(mpy, 0o755)
    env2 = dict(env, MIRA_PI_ROOT=str(tmp_path / "nopi"), MIRA_SPEECH_ROOT=str(mini))
    res = subprocess.run([str(script)], capture_output=True, text=True, env=env2)
    assert f"path={mini} machine=speech" in res.stdout
    env3 = dict(env, MIRA_PI_ROOT=str(tmp_path / "nopi"), MIRA_SPEECH_ROOT=str(tmp_path / "nomini"))
    res = subprocess.run([str(script)], capture_output=True, text=True, env=env3)
    assert res.returncode == 127 and "no install found" in res.stderr


# ---------------------------------------------------------------- secrets
def test_no_command_prints_a_token(world):
    blob = []
    for argv in (["status"], ["inventory"], ["describe", "agc.mic"], ["get", "--effective"], ["set", "agc.mic.target=0.06"],
                 ["changes"], ["config", "render", "--full"], ["schema", "--json"]):
        code, out, err = run(world, *argv)
        blob.append(out + err)
    text = "\n".join(blob)
    assert OWNER not in text and AGENT not in text and "Bearer" not in text


def test_cli_module_has_no_dashes():
    bad = chr(0x2013), chr(0x2014)
    assert not any(c in (WALL / "mirakit" / "cli.py").read_text(encoding="utf-8") for c in bad)
    assert not any(c in (WALL / "bin" / "mira").read_text(encoding="utf-8") for c in bad)
