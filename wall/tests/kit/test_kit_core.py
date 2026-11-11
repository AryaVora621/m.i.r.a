"""mirakit.core: the one write path, safe points, trials, undo, sessions, previews, the speech variant.

The test thread plays the loop thread (it calls safe_point, turn_begin and turn_end); control calls that must
come from "another thread" are made from a helper thread. Nothing sleeps: a FakeClock moves time.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

WALL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WALL))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mirakit import core as core_mod  # noqa: E402
from mirakit.changelog import fmt_ts  # noqa: E402
from mirakit.core import Clock, Core  # noqa: E402
from mirakit.plugin import (RESET, RESET_STREAM, ConfigError, ConflictError, Edit, Knob, PluginBase,  # noqa: E402
                            PluginSpec, PolicyError, State)
from mirakit.testkit import FakeSource  # noqa: E402

E = Edit


class FakeClock(Clock):
    def __init__(self):
        self.t = 1_800_000_000.0
        self.m = 5000.0

    def wall(self):
        return self.t

    def mono(self):
        return self.m

    def advance(self, s):
        self.t += s
        self.m += s


# ---------------------------------------------------------------------------------------------
# synthetic specs
# ---------------------------------------------------------------------------------------------

class Svc(PluginBase):
    """Records every lifecycle call with the thread it ran on, and mirrors config onto its service object."""

    def __init__(self, ctx, cfg, spec_name, provides, journal, make_obj=None, fail_when=None):
        super().__init__(ctx, cfg)
        self.n, self.p, self.j, self.make_obj, self.fail_when = spec_name, provides, journal, make_obj, fail_when
        self.obj = None

    def _t(self):
        return threading.current_thread().name

    def start(self):
        self.j.append(("start", self.n, self._t()))
        if self.fail_when and self.fail_when(self.cfg):
            raise RuntimeError(f"{self.n} refused its config")
        self.obj = self.make_obj() if self.make_obj else SimpleNamespace()
        self.obj.name = self.n
        for k, v in self.cfg.items():
            setattr(self.obj, k, v)
        for s in self.p:
            self.ctx.provide(s, self.obj)

    def stop(self):
        self.j.append(("stop", self.n, self._t()))

    def apply(self, changed, cfg):
        self.j.append(("apply", self.n, dict(changed), self._t()))
        for k, v in changed.items():
            if "." not in k:
                setattr(self.obj, k, v)
        super().apply(changed, cfg)


def K(name, type_="float", default=1.0, **kw):
    kw.setdefault("unit", "s" if type_ in ("float", "int") else "")
    if type_ in ("float", "int"):
        kw.setdefault("min", 0)
        kw.setdefault("max", 100)
    return Knob(name, type_, default, f"{name} knob", **kw)


def mk(name, provides=(), knobs=(), journal=None, needs=(), reads=(), required=True, core=False, toggle="restart",
       enabled=True, make_obj=None, fail_when=None, validate=None, order=100, machine="listener"):
    j = journal if journal is not None else []
    return PluginSpec(name=name, kind=name.split(".")[0], version="1", summary="s", machine=machine,
                      provides=tuple(provides), needs=tuple(needs), reads=tuple(reads), required=required,
                      core=core, toggle=toggle, default_enabled=enabled, order=order, knobs=tuple(knobs),
                      validate=validate, create=lambda ctx, cfg: Svc(ctx, cfg, name, provides, j, make_obj, fail_when))


CONTROL_KNOBS = [
    K("bind", "str", "127.0.0.1", min=1, max=100, applies="restart"),
    K("port", "int", 8650, min=1024, max=65535, applies="restart", unit=""),
    K("agents_locked", "bool", False),
    K("agent_requires_voice_turn", "bool", True),
    K("agent_writes_per_10min", "int", 12, max=60, unit=""),
    K("agent_writes_per_knob_10min", "int", 3, min=1, max=20, unit=""),
    K("agent_undo_window_s", "float", 86400.0, min=600, max=604800),
    K("trial_default_s", "float", 600.0, min=60, max=3600),
    K("trial_max_s", "float", 1800.0, min=60, max=7200),
    K("session_max_s", "float", 900.0, min=60, max=3600),
    K("session_max_changes", "int", 12, min=1, max=50, unit=""),
    K("session_followup_s", "float", 15.0, min=8, max=30),
    K("session_idle_s", "float", 120.0, min=30, max=900),
    K("rebuild_quiet_s", "float", 10.0, min=0, max=120),
    K("previews_per_10min", "int", 6, max=30, unit=""),
    K("preview_max_chars", "int", 200, min=20, max=600, unit="chars"),
    K("preview_quiet", "time_window", ""),
    K("preview_text", "str", "This is {alias}. This is how I would sound.", min=1, max=200),
    K("changelog_max_bytes", "int", 5_000_000, min=100000, max=50_000_000, unit="bytes"),
    K("history_keep", "int", 50, min=5, max=500, unit=""),
    K("wait_s_default", "float", 5.0, min=0, max=30),
    K("busy_defer_s", "float", 10.0, min=0, max=120),
]


def _trial_rule(v):
    if v["trial_max_s"] < v["trial_default_s"]:
        raise ConfigError("trial_max_s must be at least trial_default_s", "control.http.trial_max_s")


class Playback:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.said = []

    def call(self, *cmd):
        self.calls.append(cmd)
        return 0

    def say_file(self, path, wav, set_level):
        assert os.path.exists(path)
        self.said.append((path, wav))
        self.calls.append(("say",))


class Reporter:
    def __init__(self):
        self.last = ("thinking", "turn on the lamp", "")
        self.updates = []

    def update(self, state, heard="", reply=""):
        self.updates.append((state, heard, reply))
        self.last = (state, heard, reply)

    def set_level(self, v):
        pass


class Tts:
    def __init__(self):
        self.renders = []
        self.fail = False

    def render_preview(self, text, voice, speed, peak):
        if self.fail:
            raise OSError("speech offline")
        self.renders.append((text, voice, speed, peak))
        return b"RIFFfakewav"


def specs(journal=None, control_enabled=False, extra=()):
    j = journal if journal is not None else []
    return [
        mk("agc.mic", ["agc"], [
            K("target", "float", 0.05, unit="rms", min=0.005, max=0.3, agent="bounded", agent_min=0.03,
              agent_max=0.10, agent_step=0.01, arg="agc_target", env="FAKE_AGC_TARGET", topic="hearing",
              spoken="microphone boost", effect="hears quiet speech from farther away",
              phrases=("you can't hear me",)),
            K("max_gain", "float", 10.0, unit="x", min=1, max=40, agent="bounded", agent_min=4, agent_max=14,
              agent_step=2, topic="hearing", spoken="most microphone boost"),
            K("rest", "float", 3.0, unit="x", min=1, max=40),
        ], j),
        mk("vad.silero", ["vad"], [
            K("end_silence", "float", 0.7, unit="s", min=0.2, max=3.0, agent="bounded", agent_min=0.5,
              agent_max=1.2, agent_step=0.1, arg="end_silence", topic="timing",
              spoken="how long I wait for you to finish"),
            K("threshold", "float", 0.5, unit="prob", min=0.1, max=0.9),
        ], j),
        mk("wake.kws", ["wake"], [
            K("threshold", "float", 0.15, unit="score", min=0.01, max=0.5, applies="rebuild", arg="kws_threshold",
              topic="wake", spoken="wake sensitivity"),
            K("score", "float", 1.0, unit="", min=0.5, max=3.0),
            K("threads", "int", 1, min=1, max=2, applies="restart", unit="threads"),
        ], j, fail_when=lambda cfg: cfg["threshold"] > 0.3),
        mk("reporter.fake", ["reporter"], [K("level_scale", "float", 8.0, unit="x", min=1, max=32)], j,
           make_obj=Reporter),
        mk("playback.fake", ["playback"], [K("level_lag", "float", 0.15)], j, reads=("reporter.fake.level_scale",),
           make_obj=Playback),
        mk("tts.fake", ["tts"], [K("voice", "str", "", min=0, max=200)], j, make_obj=Tts),
        mk("loop.turns", ["loop"], [
            K("followup_s", "float", 8.0, unit="s", min=0, max=30, agent="bounded", agent_min=4, agent_max=15,
              agent_step=2, arg="followup_s", topic="timing", spoken="how long I keep listening after answering"),
            K("preroll_chunks", "int", 15, min=5, max=30, applies="restart", unit="chunks(100ms)"),
            K("offline_text", "str", "Speech is offline", min=1, max=80),
        ], j, core=True),
        mk("intent.music", ["intent"], [K("priority", "int", 100, max=1000, unit="")], j, toggle="live",
           required=False),
        mk("control.http", ["control"], CONTROL_KNOBS, j, required=False, toggle="live", enabled=control_enabled,
           validate=_trial_rule),
        *extra,
    ]


class Rig:
    """A booted from_args core with a fake source, a fake clock and a journal."""

    def __init__(self, tmp_path, monkeypatch, *, spec_list=None, args=None, **kw):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir(exist_ok=True)
        self.tmp = tmp_path
        self.j = []
        self.clock = FakeClock()
        self.src = FakeSource([])
        self.specs = spec_list if spec_list is not None else specs(self.j)
        self.args = args if args is not None else types.SimpleNamespace()
        kw.setdefault("required_services", ())
        self.core = Core.from_args(self.args, specs=self.specs, source=self.src, clock=self.clock, **kw)
        self.core.boot()

    def svc(self, name):
        return self.core.service(name)

    def sp(self, armed=True):
        return self.core.safe_point(armed)

    def applied(self, name):
        return [e for e in self.j if e[0] == "apply" and e[1] == name]

    def last(self, change):
        return self.core.changes(limit=50)[0] if change is None else next(
            c for c in self.core.changes(limit=500) if c["id"] == change.id)

    def turn(self):
        """Open a voice brain phase the way the TurnBrain proxy does."""
        self.core.turn_begin()

    def quiet(self):
        """Let rebuild_quiet_s pass since the last turn event."""
        self.clock.advance(11)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.core.shutdown()


def agent(rig, edits, **kw):
    return rig.core.submit(edits, actor="hermes", scope="agent", **kw)


def owner(rig, edits, **kw):
    return rig.core.submit(edits, actor="owner", scope="owner", **kw)


# =============================================================================================
# from_args: no files, no ports, legacy-compatible
# =============================================================================================

def test_from_args_touches_nothing_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    r = Rig(tmp_path, monkeypatch)
    owner(r, [E("agc.mic.target", 0.06)])
    r.sp()
    r.core.submit([E("agc.mic.max_gain", 12)], actor="owner", scope="owner")
    r.core.shutdown()
    assert list(home.iterdir()) == [], "from_args must not write under HOME"
    assert list(tmp_path.glob("**/changes.jsonl")) == []
    assert not any((home / ".config").exists() for _ in [0])
    assert r.core.store.in_memory and r.core.log.path is None and not r.core.mirror


def test_from_args_values_come_from_the_namespace_then_env_then_default(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGC_TARGET", "0.08")
    args = SimpleNamespace(agc_target=None, end_silence=0.9, kws_threshold=0.2)
    r = Rig(tmp_path, monkeypatch, args=args)
    assert r.core.cfg("agc.mic")["target"] == 0.08                # arg None: env, exactly as MicAGC(target=None)
    assert r.core.cfg("vad.silero")["end_silence"] == 0.9         # arg present
    assert r.core.cfg("wake.kws")["threshold"] == 0.2
    assert r.core.cfg("agc.mic")["max_gain"] == 10.0              # default
    assert r.svc("agc").target == 0.08
    r.core.shutdown()


def test_from_args_keeps_out_of_range_legacy_values_but_bounds_new_writes(tmp_path, monkeypatch, caplog):
    """v1 had no bounds: golden scenario S9 runs --session-ttl 2 against a schema minimum. Boot must not raise."""
    args = SimpleNamespace(end_silence=0.05, agc_target=None)
    r = Rig(tmp_path, monkeypatch, args=args)
    assert r.core.cfg("vad.silero")["end_silence"] == 0.05 and r.svc("vad").end_silence == 0.05
    assert any("outside the schema" in rec.getMessage() for rec in caplog.records)
    with pytest.raises(ConfigError):
        owner(r, [E("vad.silero.end_silence", 0.01)])             # a new write is still bounded
    owner(r, [E("vad.silero.end_silence", 0.9)])
    r.core.shutdown()


def test_from_args_still_refuses_a_provider_conflict_when_lenient(tmp_path, monkeypatch):
    j = []
    sl = [mk("source.pipewire", ["source"], [], j), mk("source.wav", ["source"], [], j)]
    with pytest.raises(ConfigError):
        Core.from_args(SimpleNamespace(), specs=sl, required_services=(), clock=FakeClock())


def test_from_file_stays_strict_about_out_of_range_values(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        FileRig(tmp_path, monkeypatch, file_doc(**{"vad.silero": {"config": {"end_silence": 0.05}}}))


def test_a_failed_change_does_not_clobber_a_later_persisted_change_to_the_same_key(tmp_path, monkeypatch):
    j = []
    sl = specs(j)
    sl[2] = mk("wake.kws", ["wake"], [K("threshold", "float", 0.15, unit="score", min=0.01, max=0.5,
                                         applies="rebuild"),
                                      K("score", "float", 1.0, unit="", min=0.5, max=3.0)],
               j, fail_when=lambda cfg: cfg["threshold"] > 0.3)
    r = Rig(tmp_path, monkeypatch, spec_list=sl)
    bad = owner(r, [E("wake.kws.threshold", 0.4)])
    later = owner(r, [E("wake.kws.threshold", 0.2)])               # persisted behind the doomed rebuild
    r.quiet()
    r.sp()
    assert r.last(bad)["status"] == "failed" and r.core.store.value("wake.kws.threshold") == 0.2
    assert r.last(later)["status"] in ("queued", "applied")
    r.quiet()
    r.sp()
    assert r.last(later)["status"] == "applied" and r.core.cfg("wake.kws")["threshold"] == 0.2
    assert r.svc("wake").threshold == 0.2
    r.core.shutdown()


def test_boot_replay_with_every_key_moved_on_only_clears_the_trial(tmp_path, monkeypatch):
    clock = FakeClock()
    r = FileRig(tmp_path, monkeypatch, file_doc(), clock=clock).boot()
    c = owner(r, [E("agc.mic.target", 0.06)], trial_s=100)
    r.core.shutdown()
    d = r.disk()
    d["plugins"]["agc.mic"]["config"]["target"] = 0.09
    r.path.write_text(json.dumps(d))
    clock.advance(500)
    r2 = FileRig(tmp_path, monkeypatch, None, clock=clock).boot()
    rec = next(x for x in r2.core.changes() if x["id"] == c.id)
    assert rec["status"] == "applied" and rec["trial_expires"] is None
    assert not [x for x in r2.core.changes() if x["undo_of"]]          # no empty undo is logged
    assert r2.core.service("agc").target == 0.09


def test_from_args_never_starts_the_control_plane(tmp_path, monkeypatch):
    j = []
    sl = specs(j, control_enabled=True)
    r = Rig(tmp_path, monkeypatch, spec_list=sl)
    assert not any(e[1] == "control.http" for e in j if e[0] == "start")
    assert next(x for x in r.core.inventory() if x["name"] == "control.http")["state"] == "disabled"
    r.core.shutdown()


def test_from_args_registers_the_given_source_and_disables_both_source_plugins(tmp_path, monkeypatch):
    j = []
    extra = [mk("source.pipewire", ["source"], [K("queue_chunks", "int", 600, min=50, max=3000, unit="chunks")], j),
             mk("source.wav", ["source"], [K("path", "path", "")], j, enabled=False)]
    r = Rig(tmp_path, monkeypatch, spec_list=specs(j, extra=extra))
    assert r.core.service("source") is r.src
    assert not any(e[1].startswith("source.") for e in j if e[0] == "start")
    inv = {row["name"]: row for row in r.core.inventory()}
    assert inv["source.pipewire"]["enabled"] is False and inv["source.wav"]["enabled"] is False
    r.core.shutdown()


def test_from_args_wav_flag_is_an_in_memory_override(tmp_path, monkeypatch):
    j = []
    extra = [mk("source.pipewire", ["source"], [K("queue_chunks", "int", 600, min=50, max=3000, unit="chunks")], j),
             mk("source.wav", ["source"], [K("path", "path", "", arg="wav")], j, enabled=False)]
    monkeypatch.setenv("HOME", str(tmp_path))
    core = Core.from_args(SimpleNamespace(wav="/tmp/x.wav"), specs=specs(j, extra=extra), required_services=(),
                          clock=FakeClock())
    core.boot()
    started = [e[1] for e in j if e[0] == "start"]
    assert "source.wav" in started and "source.pipewire" not in started
    assert core.cfg("source.wav")["path"] == "/tmp/x.wav"
    core.shutdown()


def test_bind_args_adds_loop_turns_and_arg_knobs_and_keeps_them_in_sync(rig):
    ns = SimpleNamespace(followup_s=99)
    rig.core.bind_args(ns)
    assert ns.followup_s == 8.0                         # arg-mapped knob takes the applied value
    assert ns.preroll_chunks == 15 and ns.offline_text == "Speech is offline"     # loop.turns knobs by name
    assert ns.agc_target == 0.05 and ns.end_silence == 0.7 and ns.kws_threshold == 0.15
    owner(rig, [E("loop.turns.followup_s", 10), E("agc.mic.target", 0.07)])
    assert ns.followup_s == 8.0 and ns.agc_target == 0.05           # not before the safe point
    rig.sp()
    assert ns.followup_s == 10.0 and ns.agc_target == 0.07
    owner(rig, [E("loop.turns.preroll_chunks", 20)])                # restart class: not applied to args
    rig.sp()
    assert ns.preroll_chunks == 15


# =============================================================================================
# threading contract and the hot path
# =============================================================================================

def test_submit_from_another_thread_mutates_nothing_until_safe_point(rig):
    ns = SimpleNamespace()
    rig.core.bind_args(ns)
    agc = rig.svc("agc")
    rig.j.clear()
    errors = []

    def control():
        try:
            rig.core.submit([E("agc.mic.target", 0.09)], actor="owner", scope="owner")
            rig.core.submit([E("wake.kws.threshold", 0.2)], actor="owner", scope="owner")
        except Exception as exc:       # pragma: no cover - reported below
            errors.append(exc)

    t = threading.Thread(target=control, name="control-thread")
    t.start()
    t.join()
    assert errors == []
    assert agc.target == 0.05 and ns.agc_target == 0.05, "a control thread must not touch plugin or args attributes"
    assert rig.j == [], "no plugin method may run before safe_point"
    assert rig.core.cfg("agc.mic")["target"] == 0.05           # the applied snapshot too
    assert rig.core.store.value("agc.mic.target") == 0.09      # but it IS persisted
    rig.quiet()
    rig.sp()
    assert agc.target == 0.09 and ns.agc_target == 0.09
    assert all(e[-1] == threading.current_thread().name for e in rig.j if e[0] in ("apply", "start", "stop")), \
        "apply, rebuild and stop run on the thread that called safe_point"
    assert not any(e[-1] == "control-thread" for e in rig.j)


def test_submit_never_runs_plugin_code_for_any_apply_class(rig):
    rig.j.clear()
    done = threading.Event()

    def control():
        rig.core.submit([E("agc.mic.target", 0.06)], actor="owner", scope="owner")
        rig.core.submit([E("wake.kws.threshold", 0.2)], actor="owner", scope="owner")
        rig.core.submit([E("loop.turns.preroll_chunks", 20)], actor="owner", scope="owner")
        rig.core.set_enabled("intent.music", False, actor="owner", scope="owner")
        rig.core.reload_plugin("vad.silero", actor="owner", scope="owner")
        done.set()

    threading.Thread(target=control).start()
    assert done.wait(5)
    assert rig.j == []


def _count_slow_path(rig):
    """Count entries into the slow path. The fast path is a promise about work done (no lock, no timer scan, no
    drain), so it is measured by counting that work, not by a wall-clock limit a loaded machine can break."""
    core, calls = rig.core, []
    real = core._safe_point_slow

    def counting(armed):
        calls.append(armed)
        return real(armed)
    core._safe_point_slow = counting
    return calls


def test_empty_safe_point_is_cheap_and_returns_zero(rig):
    slow = _count_slow_path(rig)
    assert all(rig.core.safe_point(True) == 0 for _ in range(100000))
    assert slow == [], "an empty safe point must never reach the slow path"
    assert rig.sp() == 0 and rig.sp(False) == 0


def test_safe_point_with_a_long_due_timer_stays_on_the_fast_path(rig):
    owner(rig, [E("agc.mic.target", 0.06)], trial_s=600)
    rig.sp()
    assert rig.core._next_due > rig.core._wall() + 300, "the trial timer is far in the future, not due"
    slow = _count_slow_path(rig)
    assert all(rig.core.safe_point(True) == 0 for _ in range(100000))
    assert slow == [], "a timer that is not due must not pull safe_point onto the slow path"


def test_safe_point_goes_slow_once_the_timer_is_due(rig):
    """The counter above would pass for a safe_point that never goes slow, so prove it can see the slow path."""
    owner(rig, [E("agc.mic.target", 0.06)], trial_s=600)
    rig.sp()
    slow = _count_slow_path(rig)
    rig.core._next_due = rig.core._wall() - 1
    rig.core.safe_point(True)
    assert slow == [True]


def test_the_apply_queue_is_not_a_locked_queue(rig):
    q = rig.core._inbox
    assert not hasattr(q, "mutex"), "the empty check must not take a lock"


# =============================================================================================
# the write path: validation, persist before apply, dry run, revision
# =============================================================================================

def test_live_edit_persists_then_applies_at_the_next_safe_point(rig):
    c = owner(rig, [E("agc.mic.target", "0.06")], reason="tuning")
    assert c.status == "queued" and c.edits == [{"key": "agc.mic.target", "old": 0.05, "new": 0.06}]
    assert c.id == "L-000001" and c.applies == "live" and c.scope == "owner" and c.actor == "owner"
    assert rig.core.store.value("agc.mic.target") == 0.06 and rig.core.cfg("agc.mic")["target"] == 0.05
    assert rig.sp() == 0                                         # a live edit never resets the stream
    assert rig.svc("agc").target == 0.06 and rig.core.cfg("agc.mic")["target"] == 0.06
    assert rig.last(c)["status"] == "applied"
    assert rig.applied("agc.mic")[0][2] == {"target": 0.06}


def test_change_record_fields_and_rev(rig):
    c = owner(rig, [E("agc.mic.target", 0.06)], reason="x" * 300)
    assert len(c.reason) == 120
    assert (c.rev_before, c.rev_after) == (1, 2) and rig.core.status()["revision"] == 2
    assert c.trial_expires is None and c.undo_of is None and c.error is None and c.session is None
    assert c.machine == "listener" and c.ts.endswith(tuple("0123456789"))


def test_readers_get_dotted_keys_and_args_follow(rig):
    ns = SimpleNamespace()
    rig.core.bind_args(ns)
    owner(rig, [E("reporter.fake.level_scale", 12)])
    rig.j.clear()
    rig.sp()
    assert rig.applied("reporter.fake")[0][2] == {"level_scale": 12.0}
    assert rig.applied("playback.fake")[0][2] == {"reporter.fake.level_scale": 12.0}
    # the owning plugin first, then the readers
    assert [e[1] for e in rig.j if e[0] == "apply"] == ["reporter.fake", "playback.fake"]


def test_hard_bounds_unknown_key_and_coercion_errors(rig):
    with pytest.raises(ConfigError) as ei:
        owner(rig, [E("agc.mic.target", 5)])
    assert ei.value.field == "agc.mic.target" and "maximum" in ei.value.message
    with pytest.raises(ConfigError) as ei:
        owner(rig, [E("agc.mic.targt", 0.05)])
    assert ei.value.code == "unknown_key" and "agc.mic.target" in ei.value.message
    with pytest.raises(ConfigError) as ei:
        owner(rig, [E("agc.mik.target", 0.05)])
    assert ei.value.code == "unknown_plugin" and "agc.mic" in ei.value.message
    with pytest.raises(ConfigError):
        owner(rig, [E("agc.mic.target", "fast")])
    with pytest.raises(ConfigError):
        owner(rig, [E("agc.mic.target", 0.06), E("agc.mic.target", 0.07)])
    with pytest.raises(ConfigError):
        owner(rig, [])
    assert rig.core.changes() == [] and rig.core.store.revision == 1        # nothing was persisted


def test_a_batch_is_one_change_and_atomic(rig):
    c = owner(rig, [E("agc.mic.target", 0.06), E("agc.mic.max_gain", 12), E("vad.silero.end_silence", 0.9)])
    assert len(rig.core.changes()) == 1 and len(c.edits) == 3 and c.rev_after == 2
    with pytest.raises(ConfigError):
        owner(rig, [E("agc.mic.target", 0.07), E("agc.mic.max_gain", 999)])
    assert rig.core.store.value("agc.mic.target") == 0.06                    # the good half was not written
    rig.sp()
    assert rig.svc("vad").end_silence == 0.9 and rig.svc("agc").max_gain == 12.0


def test_reset_takes_the_schema_default(rig):
    owner(rig, [E("agc.mic.target", 0.09)])
    c = owner(rig, [E("agc.mic.target", RESET)])
    assert c.edits[0]["new"] == 0.05 and c.edits[0]["old"] == 0.09
    rig.sp()
    assert rig.svc("agc").target == 0.05


def test_stale_revision_and_dry_run(rig):
    with pytest.raises(ConflictError) as ei:
        owner(rig, [E("agc.mic.target", 0.06)], expect_revision=7)
    assert ei.value.code == "stale_revision" and ei.value.revision == 1
    c = owner(rig, [E("agc.mic.target", 0.06)], expect_revision=1)
    assert c.rev_after == 2
    d = owner(rig, [E("agc.mic.target", 0.07)], dry_run=True)
    assert d.status == "dry_run" and d.id == "L-000002" and d.edits[0]["new"] == 0.07
    assert rig.core.store.value("agc.mic.target") == 0.06 and rig.core.status()["revision"] == 2
    assert len(rig.core.changes()) == 1
    assert owner(rig, [E("agc.mic.target", 0.07)]).id == "L-000002"          # the dry run consumed no id


def test_cross_field_validate_hook_sees_the_full_candidate(rig):
    with pytest.raises(ConfigError, match="at least"):
        owner(rig, [E("control.http.trial_max_s", 100)])
    owner(rig, [E("control.http.trial_default_s", 100), E("control.http.trial_max_s", 200)])
    with pytest.raises(ConfigError, match="at least"):
        owner(rig, [E("control.http.trial_default_s", 500)])


def test_waiting_for_the_apply_returns_the_final_status(rig):
    result = {}

    def control():
        result["c"] = rig.core.submit([E("agc.mic.target", 0.06)], actor="owner", scope="owner", wait_s=5)

    t = threading.Thread(target=control)
    t.start()
    deadline = time.time() + 5
    while t.is_alive() and time.time() < deadline:
        rig.sp()
        time.sleep(0.005)
    t.join()
    assert result["c"].status == "applied"


def test_without_a_safe_point_wait_s_returns_queued(rig):
    c = owner(rig, [E("agc.mic.target", 0.06)], wait_s=0.05)
    assert c.status == "queued"                                   # accepted, never lost
    rig.sp()
    assert rig.last(c)["status"] == "applied"


def test_keys_held_by_a_flag_refuse_a_write(tmp_path, monkeypatch):
    path = tmp_path / "c" / "mira.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"format": 1, "machine": "listener"}))
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--end-silence", type=float, default=0.7)
    monkeypatch.setenv("HOME", str(tmp_path))
    core = Core.from_file(path, argv=["--end-silence", "0.9"], machine="listener", specs=specs(), parser=p,
                          required_services=(), clock=FakeClock())
    assert core.cfg("vad.silero")["end_silence"] == 0.9
    with pytest.raises(ConfigError, match="command line"):
        core.submit([E("vad.silero.end_silence", 1.0)], actor="owner", scope="owner")


# =============================================================================================
# apply classes: rebuild gating, FIFO blocking, restart, turn_end, failures
# =============================================================================================

def test_rebuild_waits_for_an_armed_safe_point(rig):
    c = owner(rig, [E("wake.kws.threshold", 0.05)])
    rig.quiet()
    assert rig.sp(armed=False) == 0 and rig.last(c)["status"] == "queued"
    assert rig.svc("wake").threshold == 0.15
    flags = rig.sp(armed=True)
    assert flags == RESET_STREAM and rig.last(c)["status"] == "applied"
    assert rig.svc("wake").threshold == 0.05 and rig.src.flush_calls == 1
    assert rig.core.cfg("wake.kws")["threshold"] == 0.05


def test_rebuild_waits_rebuild_quiet_s_after_the_last_turn_event(rig):
    rig.core.note("wake")
    c = owner(rig, [E("wake.kws.threshold", 0.05)])
    assert rig.sp() == 0 and rig.last(c)["status"] == "queued"
    rig.clock.advance(9.9)
    assert rig.sp() == 0
    rig.clock.advance(0.2)
    assert rig.sp() == RESET_STREAM and rig.last(c)["status"] == "applied"


def test_rebuild_quiet_s_is_a_knob(rig):
    owner(rig, [E("control.http.rebuild_quiet_s", 0)])
    rig.sp()
    rig.core.note("wake")
    c = owner(rig, [E("wake.kws.threshold", 0.05)])
    assert rig.sp() == RESET_STREAM and rig.last(c)["status"] == "applied"


def test_rebuild_waits_while_a_voice_session_is_open_and_applies_after(rig):
    rig.quiet()
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)])                      # opens a session
    rig.core.turn_end()
    c = owner(rig, [E("wake.kws.threshold", 0.05)])
    rig.quiet()
    assert rig.sp() == 0 and rig.last(c)["status"] == "queued"
    rig.core.session("end", actor="hermes", scope="agent")
    assert rig.sp() == RESET_STREAM and rig.last(c)["status"] == "applied"


def test_rebuild_edits_block_later_changes_to_the_same_plugin_but_not_others(rig):
    a = owner(rig, [E("wake.kws.threshold", 0.05)])               # rebuild: blocked (not armed)
    b = owner(rig, [E("wake.kws.score", 1.5)])                    # live, same plugin: waits behind a
    c = owner(rig, [E("vad.silero.end_silence", 0.9)])            # other plugin: proceeds
    rig.quiet()
    assert rig.sp(armed=False) == 0
    st = {x.id: rig.last(x)["status"] for x in (a, b, c)}
    assert st == {a.id: "queued", b.id: "queued", c.id: "applied"}
    assert rig.svc("vad").end_silence == 0.9 and rig.svc("wake").score == 1.0
    rig.sp(armed=True)
    assert {x.id: rig.last(x)["status"] for x in (a, b)} == {a.id: "applied", b.id: "applied"}
    order = [e for e in rig.j if (e[0] == "apply" and e[1] == "wake.kws") or (e[0] == "start" and e[1] == "wake.kws")]
    assert order[-2][0] == "start" and order[-1][0] == "apply"     # the rebuild ran before the later live edit


def test_turn_end_applies_queued_live_edits_only(rig):
    rig.quiet()
    rig.turn()
    live = owner(rig, [E("agc.mic.target", 0.06)])
    reb = owner(rig, [E("wake.kws.threshold", 0.05)])
    rig.core.turn_end()
    assert rig.last(live)["status"] == "applied" and rig.svc("agc").target == 0.06
    assert rig.last(reb)["status"] == "queued" and rig.svc("wake").threshold == 0.15
    assert rig.src.flush_calls == 0
    rig.quiet()
    assert rig.sp() == RESET_STREAM and rig.last(reb)["status"] == "applied"


def test_restart_class_edit_is_pending_restart_and_never_applied(rig):
    c = owner(rig, [E("wake.kws.threads", 2)])
    assert c.status == "pending_restart" and c.applies == "restart"
    rig.j.clear()
    rig.sp()
    assert rig.j == [] and rig.core.cfg("wake.kws")["threads"] == 1
    st = rig.core.status()
    assert st["pending_restart"] == ["wake.kws.threads"]
    row = next(r for r in rig.core.inventory() if r["name"] == "wake.kws")
    assert row["pending_restart"] == ["threads"]
    knob = next(k for k in rig.core.describe("wake.kws", scope="owner")["knobs"] if k["key"] == "threads")
    assert knob["pending"] == "restart" and knob["value"] == 2


def test_a_mixed_change_applies_the_live_part_and_reports_pending_restart(rig):
    c = owner(rig, [E("loop.turns.followup_s", 10), E("loop.turns.preroll_chunks", 20)])
    assert c.applies == "restart"
    rig.sp()
    assert rig.core.cfg("loop.turns")["followup_s"] == 10.0 and rig.core.cfg("loop.turns")["preroll_chunks"] == 15
    assert rig.last(c)["status"] == "pending_restart"


def test_failed_rebuild_restores_the_old_object_marks_failed_and_writes_the_old_value_back(rig):
    old = rig.svc("wake")
    c = owner(rig, [E("wake.kws.threshold", 0.4)])
    rig.quiet()
    assert rig.sp() == RESET_STREAM                              # the rebuild ran (and was undone), stream resets
    rec = rig.last(c)
    assert rec["status"] == "failed" and "refused its config" in rec["error"]
    assert rig.core.store.value("wake.kws.threshold") == 0.15, "the previous value is written back"
    assert rig.core.cfg("wake.kws")["threshold"] == 0.15
    assert rig.svc("wake").threshold == 0.15 and rig.svc("wake") is not old        # restored from the old config
    row = next(r for r in rig.core.inventory() if r["name"] == "wake.kws")
    assert row["state"] == "active" and row["last_error"]
    assert rig.core.status()["revision"] == 3                    # the write-back is a revision of its own


def test_failed_rebuild_whose_restore_also_fails_ends_the_process(tmp_path, monkeypatch):
    n = {"starts": 0}

    def fail(cfg):
        n["starts"] += 1
        return n["starts"] > 1

    j = []
    sl = specs(j)
    sl[2] = mk("wake.kws", ["wake"], [K("threshold", "float", 0.15, unit="score", min=0.01, max=0.5, applies="rebuild")],
               j, fail_when=fail)
    r = Rig(tmp_path, monkeypatch, spec_list=sl)
    fatal = []
    r.core.on_fatal = fatal.append
    c = owner(r, [E("wake.kws.threshold", 0.3)])
    r.quiet()
    r.sp()
    assert len(fatal) == 1 and "restore failed" in str(fatal[0])
    assert r.last(c)["status"] == "failed"
    r.core.shutdown()


def test_default_fatal_exits_the_process(rig):
    with pytest.raises(SystemExit) as ei:
        rig.core._default_fatal(RuntimeError("x"))
    assert ei.value.code == 70


def test_live_apply_exception_marks_failed_and_restores(tmp_path, monkeypatch):
    j = []

    class Picky(Svc):
        def apply(self, changed, cfg):
            if changed.get("target", 0) > 0.07:
                raise RuntimeError("too hot")
            super().apply(changed, cfg)

    sl = specs(j)
    sl[0] = PluginSpec(name="agc.mic", kind="agc", version="1", summary="s", machine="listener", provides=("agc",),
                       knobs=specs(j)[0].knobs, create=lambda ctx, cfg: Picky(ctx, cfg, "agc.mic", ("agc",), j))
    r = Rig(tmp_path, monkeypatch, spec_list=sl)
    c = owner(r, [E("agc.mic.target", 0.09)])
    r.sp()
    rec = r.last(c)
    assert rec["status"] == "failed" and "too hot" in rec["error"]
    assert r.core.store.value("agc.mic.target") == 0.05 and r.core.cfg("agc.mic")["target"] == 0.05
    r.core.shutdown()


def test_edits_to_a_disabled_plugin_only_update_the_snapshot(rig):
    owner(rig, [E("control.http.rebuild_quiet_s", 3)])
    rig.sp()
    assert rig.core.cfg("control.http")["rebuild_quiet_s"] == 3.0
    assert not any(e[1] == "control.http" for e in rig.j if e[0] == "apply")


# =============================================================================================
# the agent scope
# =============================================================================================

def test_agent_write_is_a_trial_in_a_session_during_a_voice_turn(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)], reason="owner said I cannot hear him")
    assert c.scope == "agent" and c.actor == "hermes" and c.trial_expires is not None
    assert c.session == "V-0001"
    sess = rig.core.session("get", actor="hermes", scope="agent")
    assert sess["id"] == "V-0001" and sess["changes"] == [c.id] and sess["state"] == "open"
    assert sess["topic"] == "hearing" and sess["followup_s"] == 15.0 and sess["turn"] == "T-000001"
    assert "microphone boost to 0.06" in c.announce and "from 0.05" in c.announce and "10 minutes" in c.announce
    rig.core.turn_end()
    assert rig.svc("agc").target == 0.06


def test_agent_trial_length_is_clamped(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)], trial_s=5)
    rig.clock.advance(0)
    from mirakit.changelog import parse_ts
    assert round(parse_ts(c.trial_expires) - rig.clock.t) in (60, 59)
    d = agent(rig, [E("vad.silero.end_silence", 0.8)], trial_s=10 ** 6)
    assert round(parse_ts(d.trial_expires) - rig.clock.t) in (1800, 1799)


def test_agent_policy_runs_before_hard_bounds_so_the_refusal_carries_the_range(rig):
    rig.turn()
    with pytest.raises(PolicyError) as ei:
        agent(rig, [E("agc.mic.target", 9.0)])                # also beyond the hard max: policy still speaks
    assert ei.value.code == "policy" and ei.value.allowed == {"min": 0.03, "max": 0.10, "step": 0.01}
    with pytest.raises(ConfigError):
        owner(rig, [E("agc.mic.target", 9.0)])                # the owner gets the hard-bound error


def test_agent_deny_knob_refusal_has_the_owner_command(rig):
    rig.turn()
    with pytest.raises(PolicyError) as ei:
        agent(rig, [E("wake.kws.threshold", "0.06")])
    assert ei.value.owner_command == "mira set wake.kws.threshold=0.06 --trial 600"
    assert rig.core.changes() == [] and rig.core.session("get", actor="x", scope="agent") is None


def test_agent_voice_turn_rule_for_write_confirm_session_start_and_keep(rig):
    for fn in (lambda: agent(rig, [E("agc.mic.target", 0.06)]),
               lambda: rig.core.confirm(actor="hermes", scope="agent"),
               lambda: rig.core.session("start", actor="hermes", scope="agent"),
               lambda: rig.core.session("keep", actor="hermes", scope="agent")):
        with pytest.raises(PolicyError) as ei:
            fn()
        assert ei.value.code == "not_in_voice_turn"
    assert rig.core.changes() == []


def test_undo_revert_end_and_get_need_no_voice_turn(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.turn_end()
    rig.sp()
    assert rig.core.status()["turn"] is None
    assert rig.core.session("get", actor="hermes", scope="agent")["id"] == "V-0001"
    undone = rig.core.undo(c.id, actor="hermes", scope="agent")
    assert undone[0].undo_of == c.id
    rig.turn()
    agent(rig, [E("vad.silero.end_silence", 0.8)])
    rig.core.turn_end()
    assert rig.core.session("revert", actor="hermes", scope="agent")["state"] == "reverted"
    rig.turn()
    agent(rig, [E("vad.silero.end_silence", 0.8)])
    rig.core.turn_end()
    assert rig.core.session("end", actor="hermes", scope="agent")["state"] == "ended"


def test_agents_locked_refuses_everything_but_reads_and_undo(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)])
    owner(rig, [E("control.http.agents_locked", True)])
    rig.sp()
    assert rig.core.status()["agents_locked"] is True
    for fn in (lambda: agent(rig, [E("agc.mic.target", 0.07)]),
               lambda: rig.core.confirm(actor="hermes", scope="agent"),
               lambda: rig.core.preview(actor="hermes", scope="agent"),
               lambda: rig.core.session("keep", actor="hermes", scope="agent")):
        with pytest.raises(PolicyError) as ei:
            fn()
        assert ei.value.code == "agents_locked"
    assert rig.core.config() and rig.core.status() and rig.core.inventory()          # reads still work
    rig.core.undo(c.id, actor="hermes", scope="agent")                                 # moving back is allowed


def test_agent_rate_limits_and_undo_confirm_exempt(rig):
    owner(rig, [E("control.http.agent_writes_per_10min", 4), E("control.http.session_max_changes", 50)])
    rig.sp()
    rig.turn()
    for key, v in (("agc.mic.target", 0.06), ("agc.mic.max_gain", 12), ("agc.mic.target", 0.07),
                   ("agc.mic.max_gain", 14)):
        agent(rig, [E(key, v)])
    with pytest.raises(PolicyError) as ei:
        agent(rig, [E("vad.silero.end_silence", 0.8)])
    assert ei.value.code == "rate_limited"
    rig.core.confirm(actor="hermes", scope="agent")                                    # exempt
    rig.core.undo(actor="hermes", scope="agent")                                       # exempt
    rig.clock.advance(601)
    agent(rig, [E("vad.silero.end_silence", 0.8)])


def test_three_writes_per_knob(rig):
    rig.turn()
    for v in (0.06, 0.07, 0.06):
        agent(rig, [E("agc.mic.target", v)])
    with pytest.raises(PolicyError) as ei:
        agent(rig, [E("agc.mic.target", 0.05)])
    assert ei.value.code == "rate_limited" and ei.value.field == "agc.mic.target"
    agent(rig, [E("agc.mic.max_gain", 12)])


def test_session_cap_after_session_max_changes(rig):
    owner(rig, [E("control.http.session_max_changes", 2), E("control.http.agent_writes_per_10min", 30)])
    rig.sp()
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)])
    agent(rig, [E("agc.mic.max_gain", 12)])
    with pytest.raises(PolicyError) as ei:
        agent(rig, [E("vad.silero.end_silence", 0.8)])
    assert ei.value.code == "session_cap"


def test_agent_cannot_toggle_plugins_or_use_owner_operations(rig):
    rig.turn()
    for fn in (lambda: rig.core.set_enabled("intent.music", False, actor="hermes", scope="agent"),
               lambda: rig.core.reload_plugin("vad.silero", actor="hermes", scope="agent"),
               lambda: rig.core.reload_file(actor="hermes", scope="agent"),
               lambda: rig.core.request_restart(actor="hermes", scope="agent"),
               lambda: rig.core.undo(actor="hermes", scope="agent", force=True),
               lambda: rig.core.submit([E("intent.music.enabled", False)], actor="hermes", scope="agent")):
        with pytest.raises(PolicyError):
            fn()


def test_public_submit_refuses_the_system_scope(rig):
    with pytest.raises(PolicyError):
        rig.core.submit([E("agc.mic.target", 0.06)], actor="expiry", scope="system")


def test_agent_voice_value_goes_through_the_voices_service_on_the_speech_machine(tmp_path, monkeypatch):
    j = []

    class Voices:
        allowed = {"amy#0": True, "heart#0": False}

        def resolve(self, v):
            if v == "bogus":
                raise ConfigError("no such voice", "tts.engine.voice", "unknown_voice")
            return {"id": {"amy": "amy#0", "heart": "heart#0"}.get(v, v)}

        def agent_allowed(self, entry):
            return self.allowed.get(entry["id"], False)

    sl = [mk("voices.catalog", ["voices"], [], j, make_obj=Voices, machine="speech"),
          mk("tts.engine", ["tts"], [K("voice", "voice", "amy#0", agent="bounded", topic="voice", spoken="my voice"),
                                     K("speed", "float", 1.0, unit="x", min=0.5, max=1.6, agent="bounded",
                                       agent_min=0.8, agent_max=1.3, agent_step=0.1, topic="voice")], j,
             machine="speech")]
    core = Core.from_args(SimpleNamespace(), specs=sl, machine="speech", required_services=(), clock=FakeClock(),
                          worker=False)
    core.boot()
    c = core.submit([E("tts.engine.voice", "amy")], actor="pi", scope="agent", session="V-0007")
    assert c.edits[0]["new"] == "amy#0" and c.id == "S-000001" and c.session == "V-0007"
    with pytest.raises(PolicyError) as ei:
        core.submit([E("tts.engine.voice", "heart")], actor="pi", scope="agent")
    assert ei.value.code == "policy" and "voice" in ei.value.message
    with pytest.raises(ConfigError) as ei:
        core.submit([E("tts.engine.voice", "bogus")], actor="pi", scope="agent")
    assert ei.value.code == "unknown_voice"
    owner_c = core.submit([E("tts.engine.voice", "heart")], actor="owner", scope="owner")     # the owner is not limited
    assert owner_c.edits[0]["new"] == "heart#0"
    core.shutdown()


# =============================================================================================
# undo and confirm
# =============================================================================================

def test_undo_restores_old_values_through_the_same_dispatch(rig):
    c = owner(rig, [E("agc.mic.target", 0.06), E("wake.kws.threshold", 0.05)])
    rig.quiet()
    rig.sp()
    (u,) = rig.core.undo(c.id, actor="owner", scope="owner")
    assert u.undo_of == c.id and u.applies == "rebuild" and u.status == "queued"
    assert {e["key"]: e["new"] for e in u.edits} == {"agc.mic.target": 0.05, "wake.kws.threshold": 0.15}
    assert rig.last(c)["status"] == "reverted"
    assert rig.core.store.value("agc.mic.target") == 0.05
    rig.j.clear()
    rig.quiet()
    assert rig.sp() == RESET_STREAM                              # a reverted rebuild knob rebuilds the same way
    assert rig.svc("wake").threshold == 0.15 and rig.svc("agc").target == 0.05
    assert rig.last(u)["status"] == "applied"


def test_undo_conflict_when_the_key_has_moved_and_force_overrides(rig):
    a = owner(rig, [E("agc.mic.target", 0.06)])
    owner(rig, [E("agc.mic.target", 0.08)])
    with pytest.raises(ConflictError) as ei:
        rig.core.undo(a.id, actor="owner", scope="owner")
    assert ei.value.code == "undo_conflict" and ei.value.current == 0.08
    assert rig.last(a)["status"] in ("queued", "applied")
    rig.core.undo(a.id, actor="owner", scope="owner", force=True)
    assert rig.core.store.value("agc.mic.target") == 0.05


def test_undo_with_no_id_takes_the_newest_change_of_the_callers_scope(rig):
    o = owner(rig, [E("agc.mic.target", 0.06)])
    rig.turn()
    a = agent(rig, [E("vad.silero.end_silence", 0.8)])
    (u,) = rig.core.undo(actor="owner", scope="owner")
    assert u.undo_of == o.id
    (u2,) = rig.core.undo(actor="hermes", scope="agent")
    assert u2.undo_of == a.id
    assert rig.core.undo(actor="owner", scope="owner") == []


def test_undoing_an_undo_is_a_redo(rig):
    c = owner(rig, [E("agc.mic.target", 0.06)])
    (u,) = rig.core.undo(c.id, actor="owner", scope="owner")
    (r,) = rig.core.undo(u.id, actor="owner", scope="owner")
    assert r.edits[0]["new"] == 0.06 and rig.core.store.value("agc.mic.target") == 0.06


def test_unknown_and_finished_changes_cannot_be_undone(rig):
    with pytest.raises(ConfigError):
        rig.core.undo("L-009999", actor="owner", scope="owner")
    c = owner(rig, [E("agc.mic.target", 0.06)])
    rig.core.undo(c.id, actor="owner", scope="owner")
    with pytest.raises(ConflictError):
        rig.core.undo(c.id, actor="owner", scope="owner")


def test_agents_undo_only_their_own_recent_changes(rig):
    o = owner(rig, [E("agc.mic.target", 0.06)])
    with pytest.raises(PolicyError):
        rig.core.undo(o.id, actor="hermes", scope="agent")
    rig.turn()
    a = agent(rig, [E("vad.silero.end_silence", 0.8)])
    rig.clock.advance(86401)
    with pytest.raises(PolicyError):
        rig.core.undo(a.id, actor="hermes", scope="agent")
    rig.core.undo(a.id, actor="owner", scope="owner")


def test_confirm_clears_the_expiry(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)])
    (done,) = rig.core.confirm(c.id, actor="hermes", scope="agent")
    assert done.status == "confirmed"
    rig.clock.advance(5000)
    rig.sp()
    assert rig.svc("agc").target == 0.06 and rig.last(c)["status"] == "confirmed"
    assert rig.last(c)["trial_expires"] is None


def test_a_confirmed_change_cannot_be_undone_but_a_reverted_one_cannot_be_confirmed(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.confirm(c.id, actor="hermes", scope="agent")
    with pytest.raises(ConflictError):
        rig.core.undo(c.id, actor="hermes", scope="agent")
    d = agent(rig, [E("vad.silero.end_silence", 0.8)])
    rig.core.undo(d.id, actor="hermes", scope="agent")
    with pytest.raises(ConflictError):
        rig.core.confirm(d.id, actor="hermes", scope="agent")


def test_confirm_of_a_still_queued_change_survives_its_apply(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.confirm(c.id, actor="hermes", scope="agent")
    rig.core.turn_end()
    assert rig.last(c)["status"] == "confirmed" and rig.svc("agc").target == 0.06


# =============================================================================================
# trials with a fake clock
# =============================================================================================

def test_owner_write_is_a_trial_only_with_trial_s(rig):
    plain = owner(rig, [E("agc.mic.target", 0.06)])
    trial = owner(rig, [E("vad.silero.end_silence", 0.9)], trial_s=120)
    assert plain.trial_expires is None and trial.trial_expires is not None
    rig.sp()
    rig.clock.advance(119)
    rig.sp()
    assert rig.svc("vad").end_silence == 0.9
    rig.clock.advance(2)
    rig.sp()
    assert rig.svc("vad").end_silence == 0.7 and rig.svc("agc").target == 0.06
    assert rig.last(trial)["status"] == "reverted"
    expiry = next(c for c in rig.core.changes() if c["undo_of"] == trial.id)
    assert expiry["actor"] == "expiry" and expiry["scope"] == "system" and expiry["status"] == "applied"


def test_trial_expiry_is_deferred_while_its_session_is_open_then_runs_on_its_own_clock(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)], trial_s=60)
    rig.core.turn_end()
    rig.clock.advance(61)
    rig.core.turn_begin()                                         # keep the session alive (a voice turn)
    rig.core.turn_end()
    rig.sp()
    assert rig.core.session("get", actor="x", scope="agent") is not None
    assert rig.svc("agc").target == 0.06 and rig.last(c)["status"] == "applied"
    rig.core.session("end", actor="hermes", scope="agent")
    rig.sp()
    assert rig.svc("agc").target == 0.05 and rig.last(c)["status"] == "reverted"


def test_expiry_of_a_rebuild_trial_obeys_the_safe_point_gate(rig):
    c = owner(rig, [E("wake.kws.threshold", 0.05)], trial_s=60)
    rig.quiet()
    rig.sp()
    assert rig.svc("wake").threshold == 0.05
    rig.clock.advance(61)
    rig.core.note("wake")                                         # a wake just happened: not quiet
    assert rig.sp() == 0
    assert rig.svc("wake").threshold == 0.05                      # the revert is queued, not applied
    undo = next(x for x in rig.core.changes() if x["undo_of"] == c.id)
    assert undo["status"] == "queued" and undo["actor"] == "expiry"
    rig.quiet()
    assert rig.sp(armed=False) == 0 and rig.svc("wake").threshold == 0.05
    assert rig.sp(armed=True) == RESET_STREAM and rig.svc("wake").threshold == 0.15


def test_a_chain_of_trials_unwinds_to_the_original_value(rig):
    a = owner(rig, [E("agc.mic.target", 0.06)], trial_s=60)
    b = owner(rig, [E("agc.mic.target", 0.08)], trial_s=300)
    rig.sp()
    rig.clock.advance(61)
    rig.sp()
    assert rig.core.store.value("agc.mic.target") == 0.05          # newest first, then the older one
    assert rig.last(a)["status"] == "reverted" and rig.last(b)["status"] == "reverted"
    rig.clock.advance(400)
    rig.sp()
    assert rig.core.store.value("agc.mic.target") == 0.05


def test_a_later_owner_change_survives_an_earlier_trial_expiring(rig):
    owner(rig, [E("agc.mic.target", 0.06)], trial_s=60)
    keep = owner(rig, [E("agc.mic.target", 0.09)])
    rig.sp()
    rig.clock.advance(61)
    rig.sp()
    assert rig.core.store.value("agc.mic.target") == 0.09 and rig.svc("agc").target == 0.09
    assert rig.last(keep)["status"] == "applied"
    assert next_due_is_far(rig)


def next_due_is_far(rig):
    return rig.core._next_due > rig.clock.t + 1000


def test_next_due_tracks_the_earliest_trial(rig):
    assert rig.core._next_due == float("inf")
    owner(rig, [E("agc.mic.target", 0.06)], trial_s=500)
    owner(rig, [E("vad.silero.end_silence", 0.9)], trial_s=100)
    assert rig.core._next_due == pytest.approx(rig.clock.t + 100)
    rig.sp()
    rig.clock.advance(101)
    rig.sp()
    assert rig.core._next_due == pytest.approx(rig.clock.t - 101 + 500)


# =============================================================================================
# voice sessions
# =============================================================================================

def test_session_start_get_end_by_the_owner_and_idempotent_start(rig):
    s = rig.core.session("start", actor="owner", scope="owner", topic="timing")
    assert s["id"] == "V-0001" and s["state"] == "open" and s["topic"] == "timing" and s["changes"] == []
    assert rig.core.session("start", actor="owner", scope="owner")["id"] == "V-0001"
    assert rig.core.status()["session"]["id"] == "V-0001"
    ended = rig.core.session("end", actor="owner", scope="owner")
    assert ended["state"] == "ended" and rig.core.session("get", actor="owner", scope="owner") is None
    assert rig.core.session("end", actor="owner", scope="owner") is None
    assert rig.core.session("start", actor="owner", scope="owner")["id"] == "V-0002"


def test_keep_confirms_every_change_of_the_session(rig):
    rig.turn()
    a = agent(rig, [E("agc.mic.target", 0.06)])
    b = agent(rig, [E("vad.silero.end_silence", 0.8)])
    kept = rig.core.session("keep", actor="hermes", scope="agent")
    assert kept["state"] == "kept" and kept["changes"] == [a.id, b.id]
    assert rig.last(a)["status"] == "confirmed" and rig.last(b)["status"] == "confirmed"
    assert rig.core.session("get", actor="x", scope="agent") is None
    rig.clock.advance(99999)
    rig.sp()
    assert rig.svc("agc").target == 0.06


def test_revert_undoes_newest_first(rig):
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)])
    agent(rig, [E("agc.mic.target", 0.07)])
    rig.core.turn_end()
    rig.sp()
    closed = rig.core.session("revert", actor="hermes", scope="agent")
    assert closed["state"] == "reverted"
    assert rig.core.store.value("agc.mic.target") == 0.05
    undos = [c for c in rig.core.changes() if c["undo_of"]]
    assert [c["undo_of"] for c in reversed(undos)] == ["L-000002", "L-000001"]   # newest change undone first
    rig.sp()
    assert rig.svc("agc").target == 0.05


def test_revert_still_unwinds_what_matches_when_the_owner_moved_a_key(rig):
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)])
    agent(rig, [E("vad.silero.end_silence", 0.8)])
    owner(rig, [E("agc.mic.target", 0.09)])
    rig.core.session("revert", actor="hermes", scope="agent")
    assert rig.core.store.value("agc.mic.target") == 0.09 and rig.core.store.value("vad.silero.end_silence") == 0.7


def test_session_ends_idle_after_session_idle_s_and_trials_keep_running(rig):
    rig.turn()
    c = agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.turn_end()
    rig.sp()
    rig.clock.advance(119)
    rig.sp()
    assert rig.core.session("get", actor="x", scope="agent") is not None
    rig.clock.advance(2)
    rig.sp()
    assert rig.core.session("get", actor="x", scope="agent") is None
    assert rig.svc("agc").target == 0.06 and rig.last(c)["status"] == "applied"      # its trial runs on its own clock
    rig.clock.advance(600)
    rig.sp()
    assert rig.svc("agc").target == 0.05


def test_a_voice_turn_keeps_the_session_from_idling(rig):
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.turn_end()
    for _ in range(5):
        rig.clock.advance(100)
        rig.core.turn_begin()
        rig.core.turn_end()
        rig.sp()
    assert rig.core.session("get", actor="x", scope="agent") is not None


def test_session_hard_cap_session_max_s(rig):
    owner(rig, [E("control.http.session_idle_s", 900)])
    rig.sp()
    rig.core.session("start", actor="owner", scope="owner")
    for _ in range(10):
        rig.clock.advance(100)
        rig.core.turn_begin()
        rig.core.turn_end()
        rig.sp()
    assert rig.core.session("get", actor="x", scope="agent") is None            # 900 s elapsed


def test_followup_overlay_is_applied_at_turn_end_and_restored_when_the_session_ends(rig):
    ns = SimpleNamespace()
    rig.core.bind_args(ns)
    assert ns.followup_s == 8.0
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)])                      # implicit session start
    assert ns.followup_s == 8.0                                  # the loop thread has not seen it yet
    rig.core.turn_end()
    assert ns.followup_s == 15.0                                 # session_followup_s, in time for handle_clip
    assert rig.core.cfg("loop.turns")["followup_s"] == 8.0       # not persisted, not applied as config
    assert rig.core.store.value("loop.turns.followup_s") == 8.0
    desc = next(k for k in rig.core.describe("loop.turns", scope="owner")["knobs"] if k["key"] == "followup_s")
    assert desc["source"] == "session" and desc["value"] == 15.0
    rig.sp()
    assert ns.followup_s == 15.0
    rig.core.session("end", actor="hermes", scope="agent")
    rig.sp()
    assert ns.followup_s == 8.0


def test_overlay_does_not_stop_the_owner_changing_followup_s_meanwhile(rig):
    ns = SimpleNamespace()
    rig.core.bind_args(ns)
    rig.core.session("start", actor="owner", scope="owner")
    rig.sp()
    assert ns.followup_s == 15.0
    owner(rig, [E("loop.turns.followup_s", 10)])
    rig.sp()
    assert ns.followup_s == 15.0 and rig.core.cfg("loop.turns")["followup_s"] == 10.0
    rig.core.session("end", actor="owner", scope="owner")
    rig.sp()
    assert ns.followup_s == 10.0


def test_fast_path_notices_a_session_opened_from_another_thread(rig):
    ns = SimpleNamespace()
    rig.core.bind_args(ns)
    t = threading.Thread(target=lambda: rig.core.session("start", actor="owner", scope="owner"))
    t.start()
    t.join()
    rig.core.safe_point(True)
    assert ns.followup_s == 15.0


# =============================================================================================
# previews
# =============================================================================================

def test_inline_preview_during_the_brain_phase_follows_the_sequence_and_flushes_next_safe_point(rig):
    pb, rp, tts = rig.svc("playback"), rig.svc("reporter"), rig.svc("tts")
    rig.turn()
    out = rig.core.preview(actor="owner", scope="owner", voice="amy", speed=1.1, peak=0.8, text="Hello {alias}.")
    assert out["played"] is True and out["voice"] == "amy" and isinstance(out["ms"], int)
    assert tts.renders == [("Hello amy.", "amy", 1.1, 0.8)]
    assert rp.updates[0] == ("speaking", "turn on the lamp", "Voice preview: amy")
    assert rp.updates[-1] == ("thinking", "turn on the lamp", "")             # the previous state is restored
    assert [c[0] for c in pb.calls] == ["duck", "say", "unduck"]
    assert len(pb.said) == 1 and not os.path.exists(pb.said[0][0]), "the temp file is unlinked"
    assert pb.said[0][1] == b"RIFFfakewav"
    assert rig.core.pending_flush is True and rig.src.flush_calls == 0
    rig.core.turn_end()
    assert rig.sp(armed=False) == RESET_STREAM
    assert rig.src.flush_calls == 1 and rig.core.pending_flush is False
    assert rig.sp() == 0


def test_inline_preview_unducks_even_when_playback_fails(rig):
    pb = rig.svc("playback")

    def boom(path, wav, set_level):
        raise RuntimeError("no sound card")

    pb.say_file = boom
    rig.turn()
    with pytest.raises(RuntimeError):
        rig.core.preview(actor="owner", scope="owner")
    assert [c[0] for c in pb.calls] == ["duck", "unduck"]
    assert rig.svc("reporter").last == ("thinking", "turn on the lamp", "")
    assert not pb.lock.locked()


def test_preview_text_defaults_to_the_knob_with_the_alias_filled(rig):
    rig.turn()
    rig.core.preview(actor="owner", scope="owner", voice="jenny")
    assert rig.svc("tts").renders[0][0] == "This is jenny. This is how I would sound."
    rig.core.preview(actor="owner", scope="owner")
    assert rig.svc("tts").renders[1][0] == "This is my voice. This is how I would sound."


def test_preview_text_longer_than_the_limit_is_refused(rig):
    rig.turn()
    with pytest.raises(ConfigError):
        rig.core.preview(actor="owner", scope="owner", text="x" * 201)


def test_owner_preview_outside_a_turn_is_queued_until_an_armed_safe_point(rig):
    pb = rig.svc("playback")
    out = rig.core.preview(actor="owner", scope="owner", wait_s=0)
    assert out["queued"] is True and pb.calls == []
    assert rig.sp(armed=False) == 0 and pb.calls == []
    assert rig.sp(armed=True) == RESET_STREAM
    assert [c[0] for c in pb.calls] == ["duck", "say", "unduck"]
    assert rig.src.flush_calls == 1
    assert rig.svc("reporter").last == ("thinking", "turn on the lamp", "")      # restored (the previous state)


def test_a_queued_preview_waiting_caller_gets_played_true(rig):
    result = {}
    t = threading.Thread(target=lambda: result.update(rig.core.preview(actor="owner", scope="owner", wait_s=5)))
    t.start()
    deadline = time.time() + 5
    while t.is_alive() and time.time() < deadline:
        rig.sp()
        time.sleep(0.005)
    t.join()
    assert result["played"] is True and result["voice"] == "my voice"


def test_agent_previews_follow_the_voice_turn_rule_the_lock_quiet_hours_and_the_rate_limit(rig):
    with pytest.raises(PolicyError) as ei:
        rig.core.preview(actor="hermes", scope="agent")
    assert ei.value.code == "not_in_voice_turn"
    rig.turn()
    for _ in range(6):
        assert rig.core.preview(actor="hermes", scope="agent")["played"] is True
    with pytest.raises(PolicyError) as ei:
        rig.core.preview(actor="hermes", scope="agent")
    assert ei.value.code == "rate_limited"
    rig.core.turn_end()


def test_quiet_hours_only_refuse_when_a_window_is_set(rig):
    rig.turn()
    local = time.localtime(rig.clock.t)
    start = f"{local.tm_hour:02d}:00"
    end = f"{(local.tm_hour + 1) % 24:02d}:00"
    assert rig.core.preview(actor="hermes", scope="agent")["played"] is True           # default: no window at all
    owner(rig, [E("control.http.preview_quiet", f"{start}-{end}")])
    rig.core.turn_end()
    rig.sp()
    rig.turn()
    with pytest.raises(PolicyError) as ei:
        rig.core.preview(actor="hermes", scope="agent")
    assert ei.value.code == "quiet_hours" and "night" not in ei.value.message.lower()
    assert rig.core.preview(actor="owner", scope="owner")["played"] is True            # owner is not subject to it


def test_a_render_failure_is_reported_without_playing(rig):
    rig.svc("tts").fail = True
    rig.turn()
    out = rig.core.preview(actor="owner", scope="owner", voice="amy")
    assert out == {"played": False, "error": "OSError", "voice": "amy"}
    assert rig.svc("playback").calls == [] and rig.core.pending_flush is False


def test_turn_end_waits_for_an_inline_preview_that_is_still_playing(rig):
    pb = rig.svc("playback")
    gate = threading.Event()
    release = threading.Event()
    real = pb.say_file

    def slow(path, wav, set_level):
        gate.set()
        release.wait(5)
        real(path, wav, set_level)

    pb.say_file = slow
    rig.turn()
    t = threading.Thread(target=lambda: rig.core.preview(actor="owner", scope="owner"))
    t.start()
    assert gate.wait(5)
    ended = threading.Event()
    t2 = threading.Thread(target=lambda: (rig.core.turn_end(), ended.set()))
    t2.start()
    assert not ended.wait(0.1), "the reply must not start while the preview is audible"
    release.set()
    t.join()
    assert ended.wait(5)


def test_a_preview_needs_its_services(tmp_path, monkeypatch):
    j = []
    sl = [mk("agc.mic", ["agc"], [K("target", "float", 0.05, max=1)], j)]
    core = Core.from_args(SimpleNamespace(), specs=sl, required_services=(), clock=FakeClock())
    core.boot()
    with pytest.raises(ConfigError) as ei:
        core.preview(actor="owner", scope="owner")
    assert ei.value.code == "missing_service"


def test_the_speech_machine_never_plays_a_preview(tmp_path, monkeypatch):
    core = Core.from_args(SimpleNamespace(), specs=[mk("agc.mic", ["agc"], [], [], machine="speech")],
                          machine="speech", required_services=(), clock=FakeClock(), worker=False)
    core.boot()
    with pytest.raises(ConfigError):
        core.preview(actor="owner", scope="owner")


# =============================================================================================
# enable, disable, reload, restart
# =============================================================================================

def test_live_toggle_applies_at_the_next_safe_point(rig):
    assert rig.core.service("intent")
    c = rig.core.set_enabled("intent.music", False, actor="owner", scope="owner")
    assert c.applies == "live" and c.status == "queued" and c.edits == [
        {"key": "intent.music.enabled", "old": True, "new": False}]
    assert rig.core.services("intent")
    rig.sp()
    assert rig.core.services("intent") == [] and rig.last(c)["status"] == "applied"
    row = next(r for r in rig.core.inventory() if r["name"] == "intent.music")
    assert row["state"] == "disabled" and row["enabled"] is False
    u = rig.core.undo(c.id, actor="owner", scope="owner")
    rig.sp()
    assert rig.core.services("intent") and u


def test_an_explicit_enable_replaces_the_in_memory_override(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, spec_list=specs(control_enabled=True))
    assert r.core.store.enabled("control.http") is False
    r.core.set_enabled("control.http", True, actor="owner", scope="owner")
    r.sp()
    assert r.core.store.enabled("control.http") is True
    assert next(x for x in r.core.inventory() if x["name"] == "control.http")["state"] == "active"
    r.core.shutdown()


def test_enabling_control_http_starts_it_live(rig):
    assert next(r for r in rig.core.inventory() if r["name"] == "control.http")["state"] == "disabled"
    rig.core.set_enabled("control.http", True, actor="owner", scope="owner")
    rig.sp()
    assert next(r for r in rig.core.inventory() if r["name"] == "control.http")["state"] == "active"


def test_restart_toggle_is_pending_restart(tmp_path, monkeypatch):
    j = []
    sl = specs(j, extra=[mk("stt.remote", ["stt"], [], j, required=False, toggle="restart")])
    r = Rig(tmp_path, monkeypatch, spec_list=sl)
    c = r.core.set_enabled("stt.remote", False, actor="owner", scope="owner")
    assert c.status == "pending_restart"
    assert "stt.remote.enabled" in r.core.status()["pending_restart"]
    assert r.core.service("stt")
    r.core.shutdown()


def test_toggle_refusals(rig):
    with pytest.raises(PolicyError):
        rig.core.set_enabled("loop.turns", False, actor="owner", scope="owner")
    with pytest.raises(ConfigError) as ei:
        rig.core.set_enabled("nope.nothing", False, actor="owner", scope="owner")
    assert ei.value.code == "unknown_plugin"
    with pytest.raises(ConfigError):
        rig.core.set_enabled("vad.silero", "maybe", actor="owner", scope="owner") if False else \
            rig.core.submit([E("vad.silero.enabled", "maybe")], actor="owner", scope="owner")


def test_disabling_a_provider_of_a_required_service_is_refused_at_runtime(tmp_path, monkeypatch):
    j = []
    r = Rig(tmp_path, monkeypatch, spec_list=specs(j), required_services=("vad",))
    with pytest.raises(ConfigError) as ei:
        r.core.set_enabled("vad.silero", False, actor="owner", scope="owner")
    assert ei.value.code == "missing_service"
    r.core.shutdown()


def test_runtime_enable_that_would_conflict_is_refused(tmp_path, monkeypatch):
    j = []
    extra = [mk("source.pipewire", ["source"], [], j, toggle="live"), mk("source.wav", ["source"], [], j, enabled=False,
                                                                         toggle="live")]
    core = Core.from_args(SimpleNamespace(), specs=specs(j, extra=extra), required_services=(), clock=FakeClock())
    core.boot()
    with pytest.raises(ConflictError) as ei:
        core.set_enabled("source.wav", True, actor="owner", scope="owner")
    assert ei.value.code == "provider_conflict"
    core.shutdown()


def test_reload_plugin_is_a_rebuild_with_the_same_config(rig):
    old = rig.svc("vad")
    c = rig.core.reload_plugin("vad.silero", actor="owner", scope="owner")
    assert c.edits == [] and c.applies == "rebuild" and c.status == "queued"
    rig.quiet()
    assert rig.sp(armed=False) == 0 and rig.svc("vad") is old
    assert rig.sp(armed=True) == RESET_STREAM
    assert rig.svc("vad") is not old and rig.last(c)["status"] == "applied"
    with pytest.raises(PolicyError):
        rig.core.reload_plugin("loop.turns", actor="owner", scope="owner")


def test_request_restart_writes_nothing_in_memory_mode_and_exits_at_the_next_safe_point(rig):
    out = rig.core.request_restart(actor="owner", scope="owner")
    assert out["ok"] is True and out["at"]
    with pytest.raises(SystemExit) as ei:
        rig.sp()
    assert ei.value.code == 0


def test_restart_hook_replaces_the_exit(rig):
    hit = []
    rig.core.on_restart = lambda: hit.append(1)
    rig.core.request_restart(actor="owner", scope="owner")
    rig.sp()
    assert hit == [1] and rig.sp() == 0


# =============================================================================================
# boot
# =============================================================================================

def test_boot_exits_78_when_a_required_service_has_no_provider(tmp_path, monkeypatch):
    j = []
    sl = [mk("agc.mic", ["agc"], [], j)]
    core = Core.from_args(SimpleNamespace(), specs=sl, required_services=("agc", "wake"), clock=FakeClock())
    with pytest.raises(SystemExit) as ei:
        core.boot()
    assert ei.value.code == 78
    assert any(e[0] == "stop" for e in j), "what started is stopped again"


def test_boot_exits_78_when_a_required_plugin_fails_but_not_for_an_optional_one(tmp_path, monkeypatch):
    j = []
    bad = lambda cfg: True  # noqa: E731
    core = Core.from_args(SimpleNamespace(), specs=[mk("a.one", ["a"], [], j, fail_when=bad)], required_services=(),
                          clock=FakeClock())
    with pytest.raises(SystemExit) as ei:
        core.boot()
    assert ei.value.code == 78
    core2 = Core.from_args(SimpleNamespace(), specs=[mk("a.one", ["a"], [], j, fail_when=bad, required=False)],
                           required_services=(), clock=FakeClock())
    core2.boot()
    row = core2.inventory()[0]
    assert row["state"] == "failed" and row["last_error"] and core2.status()["failed"] == ["a.one"]
    assert core2.status()["ok"] is False


def test_pending_required_plugin_exits_78_and_lists_missing(tmp_path, monkeypatch):
    j = []
    core = Core.from_args(SimpleNamespace(), specs=[mk("tts.remote", ["tts"], [], j, needs=("speech",))],
                          required_services=(), clock=FakeClock())
    with pytest.raises(SystemExit) as ei:
        core.boot()
    assert ei.value.code == 78


def test_pending_optional_plugin_lists_missing_in_inventory(tmp_path, monkeypatch):
    j = []
    core = Core.from_args(SimpleNamespace(), specs=[mk("tts.remote", ["tts"], [], j, needs=("speech",), required=False)],
                          required_services=(), clock=FakeClock())
    core.boot()
    row = core.inventory()[0]
    assert row["state"] == "pending" and row["missing"] == ["speech"]


def test_provider_conflict_at_construction_names_both(tmp_path, monkeypatch):
    j = []
    sl = [mk("source.pipewire", ["source"], [], j), mk("source.wav", ["source"], [], j)]
    with pytest.raises(ConfigError) as ei:
        Core.from_args(SimpleNamespace(), specs=sl, required_services=(), clock=FakeClock())
    assert ei.value.code == "provider_conflict" and "source.pipewire" in ei.value.message \
        and "source.wav" in ei.value.message


def test_boot_is_idempotent_and_shutdown_stops_in_reverse_order_once(rig):
    rig.core.boot()
    started = [e[1] for e in rig.j if e[0] == "start"]
    rig.j.clear()
    rig.core.shutdown()
    rig.core.shutdown()
    stopped = [e[1] for e in rig.j if e[0] == "stop"]
    assert stopped == list(reversed(started))
    assert not rig.core.services("vad")


def test_the_core_is_a_service_and_plugins_can_reach_it(rig):
    assert rig.core.service("core") is rig.core
    assert rig.core.service("nothing", "d") == "d"
    with pytest.raises(KeyError):
        rig.core.service("nothing")


def test_cfg_snapshots_are_immutable_and_cfg_of_matches(rig):
    cfg = rig.core.cfg("agc.mic")
    with pytest.raises(AttributeError):
        cfg.target = 1
    assert rig.svc("agc").target == cfg.target and rig.core.cfg_of("agc.mic") is cfg
    owner(rig, [E("agc.mic.target", 0.06)])
    rig.sp()
    assert cfg.target == 0.05 and rig.core.cfg("agc.mic").target == 0.06       # swapped by reference


def test_path_knobs_reach_plugins_expanded(tmp_path, monkeypatch):
    j = []
    sl = [mk("persona.file", ["persona"], [K("path", "path", "${HERE}/IDENTITY.md", unit=""),
                                           K("other", "path", "~/x", unit="")], j)]
    core = Core.from_args(SimpleNamespace(), specs=sl, required_services=(), clock=FakeClock(), here=tmp_path / "h",
                          home=tmp_path / "me")
    core.boot()
    assert core.cfg("persona.file")["path"] == f"{tmp_path / 'h'}/IDENTITY.md"
    assert core.cfg("persona.file")["other"] == f"{tmp_path / 'me'}/x"
    # but config() and describe() keep what is stored
    assert core.config(effective=False)["plugins"]["persona.file"]["config"]["path"] == "${HERE}/IDENTITY.md"
    core.shutdown()


# =============================================================================================
# events
# =============================================================================================

def test_setting_changed_event_and_a_failing_observer_is_swallowed(rig):
    seen = []
    rig.core.on("setting.changed", lambda p: (_ for _ in ()).throw(RuntimeError("bad observer")))
    rig.core.on("setting.changed", seen.append)
    c = owner(rig, [E("agc.mic.target", 0.06)])
    rig.sp()
    assert len(seen) == 1 and seen[0]["change_id"] == c.id and seen[0]["keys"] == ["agc.mic.target"]
    assert seen[0]["status"] == "applied" and seen[0]["event"] == "setting.changed" and seen[0]["turn"] is None


def test_plugin_state_event(tmp_path, monkeypatch):
    j = []
    core = Core.from_args(SimpleNamespace(), specs=[mk("a.one", ["a"], [], j)], required_services=(), clock=FakeClock())
    seen = []
    core.on("plugin.state", seen.append)
    core.boot()
    assert [e["state"] for e in seen] == ["loading", "active"]


def test_note_counts_kinds_and_keeps_no_text(rig):
    rig.core.note("wake")
    rig.core.note("wake")
    rig.core.note("empty")
    assert rig.core._counts == {"wake": 2, "empty": 1}


# =============================================================================================
# reads: shapes
# =============================================================================================

def _validator():
    import test_kit_contracts as tc
    schema = json.loads((WALL / "docs" / "contracts" / "control-api.schema.json").read_text())
    return lambda name, inst: tc.check_def(schema, name, inst)


def test_status_shape_and_values(rig):
    check = _validator()
    st = rig.core.status()
    check("Status", st)
    assert st["machine"] == "listener" and st["engine"] == "plugins" and st["loop"] == "armed" and st["turn"] is None
    assert st["session"] is None and st["pending_restart"] == [] and st["config_error"] is None and st["ok"] is True
    rig.sp(armed=False)
    assert rig.core.status()["loop"] == "followup"
    rig.turn()
    st = rig.core.status()
    check("Status", st)
    assert st["loop"] == "turn" and st["turn"]["phase"] == "brain" and st["turn"]["id"] == "T-000001"
    agent(rig, [E("agc.mic.target", 0.06)])
    check("Status", rig.core.status())
    assert rig.core.status()["session"]["id"] == "V-0001"


def test_mic_state_comes_from_the_mute_service(tmp_path, monkeypatch):
    j = []
    sl = [mk("mute.zero", ["mute"], [], j, make_obj=lambda: SimpleNamespace(state="muted"))]
    core = Core.from_args(SimpleNamespace(), specs=sl, required_services=(), clock=FakeClock())
    core.boot()
    assert core.status()["mic"] == "muted"


def test_inventory_rows_validate(rig):
    check = _validator()
    rows = rig.core.inventory()
    assert [r["name"] for r in rows][:2] == ["agc.mic", "vad.silero"]
    for r in rows:
        check("InventoryRow", r)
    assert next(r for r in rows if r["name"] == "loop.turns")["core"] is True


def test_describe_rows_validate_and_agents_see_agent_bounds(rig):
    check = _validator()
    d = rig.core.describe("agc.mic", scope="owner")
    for row in d["knobs"]:
        check("KnobRow", row)
    tgt = next(k for k in d["knobs"] if k["key"] == "target")
    assert (tgt["min"], tgt["max"]) == (0.005, 0.3) and tgt["settable_by_voice"] is True
    assert tgt["source"] == "default" and tgt["pending"] is None and tgt["secret"] is False
    a = next(k for k in rig.core.describe("agc.mic", scope="agent")["knobs"] if k["key"] == "target")
    assert (a["min"], a["max"]) == (0.03, 0.10)
    rest = next(k for k in d["knobs"] if k["key"] == "rest")
    assert rest["settable_by_voice"] is False
    with pytest.raises(ConfigError) as ei:
        rig.core.describe("agc.mik", scope="owner")
    assert ei.value.code == "unknown_plugin"
    assert rig.core.describe("core", scope="owner")["knobs"]


def test_queued_knobs_show_pending_queued(rig):
    owner(rig, [E("agc.mic.target", 0.06)])
    row = next(k for k in rig.core.describe("agc.mic", scope="owner")["knobs"] if k["key"] == "target")
    assert row["pending"] == "queued" and row["value"] == 0.06
    rig.sp()
    row = next(k for k in rig.core.describe("agc.mic", scope="owner")["knobs"] if k["key"] == "target")
    assert row["pending"] is None and row["source"] == "file"


def test_secret_knobs_show_only_whether_they_are_set(tmp_path, monkeypatch):
    j = []
    sl = [mk("brain.hermes", ["brain"], [K("key_file", "path", "", unit="", secret=True)], j)]
    core = Core.from_args(SimpleNamespace(), specs=sl, required_services=(), clock=FakeClock())
    core.boot()
    core.submit([E("brain.hermes.key_file", "/secret/path")], actor="owner", scope="owner")
    row = core.describe("brain.hermes", scope="owner")["knobs"][0]
    assert row["value"] == {"set": True} and row["default"] == {"set": False}
    cfg = core.config()
    assert cfg["plugins"]["brain.hermes"]["config"]["key_file"] == {"value": {"set": True}, "source": "file"}
    assert "/secret/path" not in json.dumps(cfg) and "/secret/path" not in json.dumps(core.describe("brain.hermes", scope="agent"))
    assert "/secret/path" not in json.dumps(core.changes())
    dry = core.submit([E("brain.hermes.key_file", "/secret/other")], actor="owner", scope="owner", dry_run=True)
    assert "/secret" not in json.dumps(dry.to_dict())
    (u,) = core.undo(actor="owner", scope="owner")
    assert "/secret" not in json.dumps(u.to_dict()) and core.store.value("brain.hermes.key_file") == ""
    assert "/secret/path" in (core.log.get(u.undo_of)["edits"][0]["new"])       # the log itself keeps the real value


def test_schema_lists_every_plugin_but_not_the_core_pseudo_plugin(rig):
    s = rig.core.schema()
    assert s["machine"] == "listener" and s["contract_version"] == 1 and s["revision"] == 1
    names = [p["name"] for p in s["plugins"]]
    assert "agc.mic" in names and "core" not in names
    agc = next(p for p in s["plugins"] if p["name"] == "agc.mic")
    assert agc["knobs"][0]["name"] == "target" and agc["knobs"][0]["agent_step"] == 0.01
    json.dumps(s)


def test_config_shapes(rig):
    owner(rig, [E("agc.mic.target", 0.06)])
    cfg = rig.core.config()
    assert cfg["revision"] == 2 and cfg["plugins"]["agc.mic"]["config"]["target"] == {"value": 0.06, "source": "file"}
    assert cfg["plugins"]["agc.mic"]["config"]["rest"] == {"value": 3.0, "source": "default"}
    assert cfg["plugins"]["agc.mic"]["enabled"] is True and cfg["core"]["engine"] == {"value": "legacy", "source": "default"}
    bare = rig.core.config(effective=False)
    assert bare["plugins"]["agc.mic"]["config"]["target"] == 0.06 and bare["core"]["engine"] == "legacy"
    assert bare["plugins"]["control.http"]["enabled"] is False


def test_changes_filters_and_order(rig):
    a = owner(rig, [E("agc.mic.target", 0.06)])
    rig.turn()
    b = agent(rig, [E("vad.silero.end_silence", 0.8)])
    c = owner(rig, [E("agc.mic.max_gain", 12)])
    ids = [x["id"] for x in rig.core.changes()]
    assert ids == [c.id, b.id, a.id]
    assert [x["id"] for x in rig.core.changes(limit=1)] == [c.id]
    assert [x["id"] for x in rig.core.changes(since=a.id)] == [c.id, b.id]
    assert [x["id"] for x in rig.core.changes(session="V-0001")] == [b.id]
    assert [x["id"] for x in rig.core.changes(actor="owner")] == [c.id, a.id]
    check = _validator()
    for ch in rig.core.changes():
        check("Change", ch)
    assert rig.core.changes(since="L-999999")[0]["id"] == c.id


def test_tuning_guide_from_the_local_schema(rig):
    check = _validator()
    g = rig.core.tuning_guide("hearing", scope="agent")
    check("TuningGuide", g)
    keys = [k["key"] for k in g["knobs"]]
    assert keys == ["agc.mic.target", "agc.mic.max_gain"]
    t = g["knobs"][0]
    assert t["ladder"] == [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1] and t["value"] == 0.05
    assert t["spoken"] == "microphone boost" and t["phrases"] == ["you can't hear me"]
    assert t["agent_min"] == 0.03 and t["agent_step"] == 0.01 and t["applies"] == "live"
    w = rig.core.tuning_guide("wake", scope="agent")
    assert w["knobs"] == [] and w["owner_only"][0]["key"] == "wake.kws.threshold"
    assert w["owner_only"][0]["owner_command"] == "mira set wake.kws.threshold=0.15 --trial 600"
    assert rig.core.tuning_guide("timing", scope="agent")["knobs"][1]["ladder"] == [4.0, 6.0, 8.0, 10.0, 12.0, 14.0]
    with pytest.raises(ConfigError):
        rig.core.tuning_guide("cooking", scope="agent")


def test_announce_text(rig):
    c = owner(rig, [E("vad.silero.end_silence", 0.8)], trial_s=600)
    assert c.announce == "I changed how long I wait for you to finish to 0.8 seconds, from 0.7 seconds, for 10 minutes."
    d = owner(rig, [E("loop.turns.preroll_chunks", 20)])
    assert d.announce.endswith("which takes effect after the next restart.")
    (u,) = rig.core.undo(c.id, actor="owner", scope="owner")
    assert u.announce == "I put how long I wait for you to finish back to 0.7 seconds."
    for text in (c.announce, d.announce, u.announce):
        assert chr(0x2014) not in text and chr(0x2013) not in text


# =============================================================================================
# the remote (speech machine) from the Pi side
# =============================================================================================

class FakeRemote:
    def __init__(self):
        self.calls = []
        self.schema = {"plugins": [{"name": "tts.engine", "knobs": [
            {"name": "speed", "type": "float", "default": 1.0, "description": "d", "unit": "x", "min": 0.5, "max": 1.6,
             "choices": None, "applies": "live", "agent": "bounded", "agent_min": 0.8, "agent_max": 1.3,
             "agent_step": 0.1, "secret": False, "arg": None, "env": None, "topic": "voice",
             "spoken": "how fast I talk", "effect": "talks faster", "phrases": ["you talk too fast"],
             "nullable": False},
            {"name": "threads", "type": "int", "default": 4, "description": "d", "unit": "threads", "min": 1, "max": 8,
             "choices": None, "applies": "rebuild", "agent": "deny", "agent_min": None, "agent_max": None,
             "agent_step": None, "secret": False, "arg": None, "env": None, "topic": "voice", "spoken": "",
             "effect": "", "phrases": [], "nullable": False}]}]}
        self.revision = 5
        self.up = True

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if not self.up:
            raise OSError("down")
        if path.startswith("/v1/config"):
            return {"revision": self.revision, "core": {}, "plugins": {"tts.engine": {"enabled": True, "config": {
                "speed": {"value": 1.1, "source": "file"}, "threads": {"value": 4, "source": "default"}}}}}
        if path == "/v1/schema":
            return self.schema
        if path == "/v1/status":
            return {"engine": "plugins", "speech": {"stt": "faster-whisper:tiny.en", "voice": "amy"}}
        if path in ("/v1/undo", "/v1/confirm"):
            return [{"id": body["change_id"], "ts": fmt_ts(0), "machine": "speech", "actor": "pi", "scope": "agent",
                     "session": None, "reason": "", "edits": [], "applies": "live", "status": "reverted",
                     "rev_before": 1, "rev_after": 2, "trial_expires": None, "undo_of": None, "error": None,
                     "announce": ""}]
        return {}


def test_tuning_guide_merges_the_speech_schema_and_caches_it_by_revision(rig):
    remote = FakeRemote()
    rig.core.register_remote("speech", remote)
    g = rig.core.tuning_guide("voice", scope="agent")
    assert [k["key"] for k in g["knobs"]] == ["tts.engine.speed"]
    assert g["knobs"][0]["value"] == 1.1 and g["knobs"][0]["ladder"] == [0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
    assert [o["key"] for o in g["owner_only"]] == ["tts.engine.threads"]
    schema_calls = lambda: [c for c in remote.calls if c[1] == "/v1/schema"]  # noqa: E731
    assert len(schema_calls()) == 1
    rig.core.tuning_guide("voice", scope="agent")
    assert len(schema_calls()) == 1, "the schema is fetched once per remote revision"
    remote.revision = 6
    rig.core.tuning_guide("voice", scope="agent")
    assert len(schema_calls()) == 2
    remote.up = False
    g2 = rig.core.tuning_guide("voice", scope="agent")
    assert g2["knobs"][0]["key"] == "tts.engine.speed"                       # served from the cache when the mini is down


def test_tuning_guide_without_a_remote_is_local_only(rig):
    assert rig.core.tuning_guide("voice", scope="agent") == {"topic": "voice", "knobs": [], "owner_only": []}


def test_status_reports_the_speech_machine_and_caches_for_a_few_seconds(rig):
    remote = FakeRemote()
    assert rig.core.status()["speech"] == {"reachable": False, "engine": "", "stt": "", "voice": ""}
    rig.core.register_remote("speech", remote)
    s = rig.core.status()["speech"]
    assert s == {"reachable": True, "engine": "plugins", "stt": "faster-whisper:tiny.en", "voice": "amy"}
    n = len(remote.calls)
    rig.core.status()
    assert len(remote.calls) == n
    rig.clock.advance(10)
    remote.up = False
    assert rig.core.status()["speech"]["reachable"] is False
    assert rig.core.remote("speech") is remote and rig.core.remotes["speech"] is remote


def test_session_keep_and_revert_forward_to_the_remote_in_newest_first_order(rig):
    remote = FakeRemote()
    rig.core.register_remote("speech", remote)
    rig.turn()
    a = agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.attach_remote_change({"id": "S-000045"})
    b = agent(rig, [E("vad.silero.end_silence", 0.8)])
    sess = rig.core.session("get", actor="x", scope="agent")
    assert sess["changes"] == [a.id, "S-000045", b.id]
    remote.calls.clear()
    kept = rig.core.session("keep", actor="hermes", scope="agent")
    assert kept["state"] == "kept"
    assert [(c[0], c[1], c[2]["change_id"]) for c in remote.calls] == [("POST", "/v1/confirm", "S-000045")]
    assert rig.last(a)["status"] == "confirmed"


def test_session_revert_forwards_undo_for_attached_remote_changes(rig):
    remote = FakeRemote()
    rig.core.register_remote("speech", remote)
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.attach_remote_change({"id": "S-000045"})
    agent(rig, [E("vad.silero.end_silence", 0.8)])
    rig.core.session("revert", actor="hermes", scope="agent")
    assert [c[2]["change_id"] for c in remote.calls if c[1] == "/v1/undo"] == ["S-000045"]
    assert rig.core.store.value("agc.mic.target") == 0.05 and rig.core.store.value("vad.silero.end_silence") == 0.7


def test_attach_remote_change_without_a_session_is_ignored(rig):
    rig.core.attach_remote_change({"id": "S-000001"})
    assert rig.core.session("get", actor="x", scope="agent") is None
    rig.core.attach_remote_change({"nope": 1})


def test_undo_of_a_remote_id_is_forwarded(rig):
    remote = FakeRemote()
    rig.core.register_remote("speech", remote)
    out = rig.core.undo("S-000045", actor="owner", scope="owner")
    assert out[0].id == "S-000045" and remote.calls[-1][1] == "/v1/undo"


# =============================================================================================
# the speech machine: no loop, one apply worker
# =============================================================================================

def speech_specs(j):
    return [
        mk("server.http", ["http"], [K("port", "int", 8765, min=1024, max=65535, applies="restart", unit="")], j,
           machine="speech"),
        mk("tts.engine", ["tts"], [
            K("voice", "str", "amy#0", min=1, max=100, applies="rebuild", agent="bounded", topic="voice"),
            K("speed", "float", 1.0, unit="x", min=0.5, max=1.6, agent="bounded", agent_min=0.8, agent_max=1.3,
              agent_step=0.1, topic="voice"),
        ], j, machine="speech"),
        mk("control.http", ["control"], [
            K("trial_default_s", "float", 600.0, min=60, max=3600), K("trial_max_s", "float", 1800.0, min=60, max=7200),
            K("agent_writes_per_10min", "int", 12, max=60, unit=""), K("busy_defer_s", "float", 10.0, min=0, max=120),
            K("history_keep", "int", 50, min=5, max=500, unit=""),
        ], j, required=False, machine="speech"),
    ]


def test_speech_worker_applies_live_and_rebuild_changes_on_its_own_thread(tmp_path, monkeypatch):
    j = []
    core = Core.from_args(SimpleNamespace(), specs=speech_specs(j), machine="speech", required_services=(),
                          clock=FakeClock())
    core.boot()
    try:
        c = core.submit([E("tts.engine.speed", 1.2), E("tts.engine.voice", "kokoro#af_heart")], actor="owner",
                        scope="owner", wait_s=5)
        assert c.status == "applied" and c.id == "S-000001" and c.machine == "speech"
        names = {e[-1] for e in j if e[0] in ("apply", "start") and e[1] == "tts.engine"}
        assert "mira-apply" in names
        assert core.service("tts").speed == 1.2 and core.service("tts").voice == "kokoro#af_heart"
        starts = [e for e in j if e[0] == "start" and e[1] == "tts.engine"]
        assert len(starts) == 2 and starts[-1][-1] == "mira-apply"            # rebuilt on the worker, not the caller
    finally:
        core.shutdown()
    assert not any(t.name == "mira-apply" and t.is_alive() for t in threading.enumerate())


def test_speech_agent_scope_trusts_the_pis_voice_turn_and_never_opens_a_session(tmp_path, monkeypatch):
    j = []
    core = Core.from_args(SimpleNamespace(), specs=speech_specs(j), machine="speech", required_services=(),
                          clock=FakeClock(), worker=False)
    core.boot()
    c = core.submit([E("tts.engine.speed", 1.1)], actor="pi", scope="agent", session="V-0003")
    assert c.session == "V-0003" and c.trial_expires and core.session("get", actor="pi", scope="agent") is None
    with pytest.raises(PolicyError) as ei:
        core.submit([E("tts.engine.speed", 2.0)], actor="pi", scope="agent")
    assert ei.value.code == "policy" and ei.value.allowed["max"] == 1.3
    with pytest.raises(PolicyError):
        core.submit([E("server.http.port", 9000)], actor="pi", scope="agent")


def test_speech_trial_expiry_swap_waits_for_the_busy_window(tmp_path, monkeypatch):
    j = []
    clock = FakeClock()
    core = Core.from_args(SimpleNamespace(), specs=speech_specs(j), machine="speech", required_services=(),
                          clock=clock, worker=False)
    core.boot()
    c = core.submit([E("tts.engine.voice", "kokoro#af_heart")], actor="owner", scope="owner", trial_s=60)
    core.tick()
    assert core.service("tts").voice == "kokoro#af_heart"
    clock.advance(61)
    core.note_busy()                                      # a /tts just finished
    core.tick()
    assert core.service("tts").voice == "kokoro#af_heart", "the swap waits until the server has been idle"
    clock.advance(5)
    core.note_busy()
    core.tick()
    assert core.service("tts").voice == "kokoro#af_heart"
    clock.advance(10.5)
    core.tick()
    assert core.service("tts").voice == "amy#0"
    assert next(x for x in core.changes() if x["id"] == c.id)["status"] == "reverted"


def test_speech_live_expiry_is_not_deferred_by_busy(tmp_path, monkeypatch):
    j = []
    clock = FakeClock()
    core = Core.from_args(SimpleNamespace(), specs=speech_specs(j), machine="speech", required_services=(),
                          clock=clock, worker=False)
    core.boot()
    core.submit([E("tts.engine.speed", 1.2)], actor="owner", scope="owner", trial_s=60)
    core.tick()
    clock.advance(61)
    core.note_busy()
    core.tick()
    assert core.service("tts").speed == 1.0


def test_speech_restart_signals_the_process_unless_a_hook_is_set(tmp_path, monkeypatch):
    j = []
    core = Core.from_args(SimpleNamespace(), specs=speech_specs(j), machine="speech", required_services=(),
                          clock=FakeClock(), worker=False)
    core.boot()
    hit = []
    core.on_restart = lambda: hit.append(1)
    core.request_restart(actor="owner", scope="owner")
    core.tick()
    core._safe_point_slow(True)
    assert hit == [1]


def test_the_worker_survives_a_failing_pass_and_reports_fatal_rebuilds(tmp_path, monkeypatch):
    j = []
    core = Core.from_args(SimpleNamespace(), specs=speech_specs(j), machine="speech", required_services=(),
                          clock=FakeClock())
    fatal = []
    core.on_fatal = fatal.append
    calls = {"n": 0}
    real = core.tick

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        real()

    core.tick = flaky
    core.boot()
    try:
        c = core.submit([E("tts.engine.speed", 1.2)], actor="owner", scope="owner", wait_s=5)
        assert c.status == "applied" and calls["n"] >= 2
    finally:
        core.shutdown()


# =============================================================================================
# from_file: production
# =============================================================================================

def file_doc(**plugins):
    return {"format": 1, "machine": "listener", "revision": 7, "core": {"legacy_env": "ignore"}, "plugins": plugins}


class FileRig:
    def __init__(self, tmp_path, monkeypatch, doc=None, *, argv=(), parser=None, mirror=False, changelog=True,
                 spec_list=None, env=None, **kw):
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(self.home))
        for k in ("MIRA_BOOT_GUARD",):
            monkeypatch.delenv(k, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        self.dir = tmp_path / "cfg"
        self.dir.mkdir(exist_ok=True)
        self.path = self.dir / "mira.json"
        if doc is not None:
            self.path.write_text(json.dumps(doc))
        self.j = []
        self.clock = kw.pop("clock", None) or FakeClock()
        self.specs = spec_list if spec_list is not None else specs(self.j)
        self.core = Core.from_file(self.path, argv=list(argv), machine="listener", specs=self.specs, parser=parser,
                                   mirror=mirror, changelog=changelog, required_services=(), clock=self.clock, **kw)

    def boot(self):
        self.core.boot()
        return self

    def disk(self):
        return json.loads(self.path.read_text())


def test_from_file_layers_file_over_env_over_default(tmp_path, monkeypatch):
    doc = file_doc(**{"agc.mic": {"enabled": True, "config": {"target": 0.06}}})
    doc["core"] = {}                                                     # legacy_env stays "fallback"
    r = FileRig(tmp_path, monkeypatch, doc, env={"FAKE_AGC_TARGET": "0.09"})
    assert r.core.cfg("agc.mic")["target"] == 0.06
    r.core.shutdown()
    doc2 = file_doc()
    doc2["core"] = {}
    r2 = FileRig(tmp_path / "b", monkeypatch, doc2, env={"FAKE_AGC_TARGET": "0.09"}) if (tmp_path / "b").mkdir() is None else None
    assert r2.core.cfg("agc.mic")["target"] == 0.09
    assert r2.core.config()["plugins"]["agc.mic"]["config"]["target"]["source"] == "env"


def test_from_file_ignores_env_once_legacy_env_is_ignore(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc(), env={"FAKE_AGC_TARGET": "0.09"})
    assert r.core.cfg("agc.mic")["target"] == 0.05


def test_from_file_argv_explicit_flags_beat_the_file(tmp_path, monkeypatch):
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--end-silence", type=float, default=0.7)
    p.add_argument("--kws-threshold", type=float, default=0.15)
    doc = file_doc(**{"vad.silero": {"enabled": True, "config": {"end_silence": 0.9}},
                      "wake.kws": {"enabled": True, "config": {"threshold": 0.05}}})
    r = FileRig(tmp_path, monkeypatch, doc, argv=["--end-silence", "1.1"], parser=p)
    assert r.core.cfg("vad.silero")["end_silence"] == 1.1          # explicit flag
    assert r.core.cfg("wake.kws")["threshold"] == 0.05             # the parser default 0.15 is NOT an explicit flag
    src = {k: v["source"] for k, v in r.core.config()["plugins"]["vad.silero"]["config"].items()}
    assert src["end_silence"] == "argv"


def test_persisted_change_is_logged_to_changes_jsonl_with_fsync_and_history(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc()).boot()
    c = owner(r, [E("agc.mic.target", 0.06)], reason="because")
    assert r.disk()["revision"] == 8 and r.disk()["plugins"]["agc.mic"]["config"]["target"] == 0.06
    log = r.dir / "changes.jsonl"
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert lines[0]["id"] == c.id and lines[0]["status"] == "queued" and lines[0]["reason"] == "because"
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600
    assert (r.dir / "history" / "listener.7.json").exists()
    r.core.safe_point(True)
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert lines[-1] == {"id": c.id, "status": "applied", "at": lines[-1]["at"], "error": None}
    r.core.shutdown()
    assert not list(r.home.rglob("*")), "nothing may be written under the real HOME"


def test_changelog_false_keeps_the_log_in_memory(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc(), changelog=False).boot()
    owner(r, [E("agc.mic.target", 0.06)])
    assert not (r.dir / "changes.jsonl").exists() and len(r.core.changes()) == 1


def test_ids_continue_after_a_restart(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc()).boot()
    owner(r, [E("agc.mic.target", 0.06)])
    owner(r, [E("agc.mic.max_gain", 12)])
    r.core.shutdown()
    r2 = FileRig(tmp_path, monkeypatch, None).boot()
    assert owner(r2, [E("vad.silero.end_silence", 0.9)]).id == "L-000003"


def test_file_changed_by_hand_is_refused_until_reload(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc()).boot()
    d = r.disk()
    d["plugins"] = {"agc.mic": {"enabled": True, "config": {"target": 0.08}}}
    r.path.write_text(json.dumps(d))
    with pytest.raises(ConflictError) as ei:
        owner(r, [E("agc.mic.max_gain", 12)])
    assert ei.value.code == "file_changed"
    assert r.core.changes() == []
    c = r.core.reload_file(actor="owner", scope="owner")
    assert c.actor == "file" and c.scope == "owner" and c.edits == [{"key": "agc.mic.target", "old": 0.05, "new": 0.08}]
    assert r.core.store.value("agc.mic.target") == 0.08
    r.core.safe_point(True)
    assert r.core.service("agc").target == 0.08 and r.core.status()["revision"] == 8
    owner(r, [E("agc.mic.max_gain", 12)])


def test_reload_file_with_an_invalid_file_changes_nothing(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc()).boot()
    d = r.disk()
    d["plugins"] = {"agc.mic": {"config": {"target": 99}}}
    r.path.write_text(json.dumps(d))
    with pytest.raises(ConfigError):
        r.core.reload_file(actor="owner", scope="owner")
    assert r.core.service("agc").target == 0.05


def test_reload_file_without_differences_is_an_empty_applied_change(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc()).boot()
    c = r.core.reload_file(actor="owner", scope="owner")
    assert c.edits == [] and c.status == "applied"


def test_invalid_file_at_boot_uses_lastgood_and_says_why(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc(**{"agc.mic": {"enabled": True, "config": {"target": 0.06}}})).boot()
    assert (r.dir / "mira.json.lastgood").exists()
    r.core.shutdown()
    r.path.write_text(json.dumps(file_doc(**{"agc.mic": {"config": {"targt": 0.1}}})))
    r2 = FileRig(tmp_path, monkeypatch, None).boot()
    assert r2.core.cfg("agc.mic")["target"] == 0.06
    st = r2.core.status()
    assert st["config_error"] and "agc.mic.target" in st["config_error"] and st["ok"] is False
    d = json.loads((r.dir / "mira.json.lastgood").read_text())
    assert d["plugins"]["agc.mic"]["config"]["target"] == 0.06


def test_an_unusable_environment_value_is_skipped_with_a_warning_not_fatal(tmp_path, monkeypatch, caplog):
    doc = file_doc()
    doc["core"] = {}
    r = FileRig(tmp_path, monkeypatch, doc, env={"FAKE_AGC_TARGET": "7"})
    assert r.core.cfg("agc.mic")["target"] == 0.05
    assert any("FAKE_AGC_TARGET" in rec.getMessage() for rec in caplog.records)


def test_an_unusable_command_line_flag_exits_78(tmp_path, monkeypatch):
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--agc-target", type=float)
    with pytest.raises(SystemExit) as ei:
        FileRig(tmp_path, monkeypatch, file_doc(), argv=["--agc-target", "7"], parser=p)
    assert ei.value.code == 78


def test_invalid_file_with_no_lastgood_exits_78(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        FileRig(tmp_path, monkeypatch, file_doc(**{"agc.mic": {"config": {"target": 99}}}))
    assert ei.value.code == 78


def test_a_cross_field_failure_after_load_also_falls_back_to_lastgood(tmp_path, monkeypatch):
    good = file_doc(**{"control.http": {"enabled": False, "config": {"trial_default_s": 300, "trial_max_s": 900}}})
    r = FileRig(tmp_path, monkeypatch, good).boot()
    r.core.shutdown()
    bad = file_doc(**{"control.http": {"enabled": True, "config": {"trial_default_s": 3000, "trial_max_s": 900}}})
    r.path.write_text(json.dumps(bad))
    r2 = FileRig(tmp_path, monkeypatch, None).boot()
    assert r2.core.cfg("control.http")["trial_max_s"] == 900.0 and r2.core.status()["config_error"]


def test_provider_conflict_in_the_file_falls_back_or_exits_78(tmp_path, monkeypatch):
    j = []

    def sl():
        return specs(j, extra=[mk("source.pipewire", ["source"], [], j), mk("source.wav", ["source"], [], j, enabled=False)])

    FileRig(tmp_path, monkeypatch, file_doc(), spec_list=sl()).boot().core.shutdown()
    r_path = tmp_path / "cfg" / "mira.json"
    bad = file_doc(**{"source.wav": {"enabled": True, "config": {}}})
    r_path.write_text(json.dumps(bad))
    r2 = FileRig(tmp_path, monkeypatch, None, spec_list=sl()).boot()
    assert "source.wav" in r2.core.status()["config_error"]
    r2.core.shutdown()
    (tmp_path / "cfg" / "mira.json.lastgood").unlink()
    with pytest.raises(SystemExit) as ei:
        FileRig(tmp_path, monkeypatch, None, spec_list=sl())
    assert ei.value.code == 78


def test_boot_writes_lastgood_after_a_clean_boot(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc())
    assert not (r.dir / "mira.json.lastgood").exists()
    r.boot()
    assert (r.dir / "mira.json.lastgood").exists()


def test_a_missing_file_is_an_empty_sparse_config(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, None).boot()
    assert r.core.cfg("agc.mic")["target"] == 0.05
    owner(r, [E("agc.mic.target", 0.06)])
    assert r.disk()["revision"] == 1 and not (r.dir / "history").exists()


# ---- boot replay
def test_boot_replay_reverts_an_expired_trial_in_the_file_before_plugins_start(tmp_path, monkeypatch):
    clock = FakeClock()
    r = FileRig(tmp_path, monkeypatch, file_doc(), clock=clock).boot()
    owner(r, [E("agc.mic.target", 0.06)], trial_s=100)
    r.core.safe_point(True)
    r.core.shutdown()
    assert r.disk()["plugins"]["agc.mic"]["config"]["target"] == 0.06
    clock.advance(500)                                                     # the process was down past the expiry
    r2 = FileRig(tmp_path, monkeypatch, None, clock=clock).boot()
    assert r2.disk()["plugins"]["agc.mic"]["config"]["target"] == 0.05
    assert r2.core.service("agc").target == 0.05, "plugins start from the already reverted file"
    ch = r2.core.changes()
    assert ch[0]["actor"] == "expiry" and ch[0]["scope"] == "system" and ch[0]["undo_of"] == "L-000001"
    assert ch[0]["status"] == "applied" and next(c for c in ch if c["id"] == "L-000001")["status"] == "reverted"


def test_boot_replay_keeps_the_remaining_time_of_an_unexpired_trial(tmp_path, monkeypatch):
    clock = FakeClock()
    r = FileRig(tmp_path, monkeypatch, file_doc(), clock=clock).boot()
    owner(r, [E("agc.mic.target", 0.06)], trial_s=600)
    r.core.safe_point(True)
    r.core.shutdown()
    clock.advance(200)
    r2 = FileRig(tmp_path, monkeypatch, None, clock=clock).boot()
    assert r2.core.service("agc").target == 0.06
    assert r2.core._trials["L-000001"]["due"] == pytest.approx(clock.t + 400, abs=1)
    clock.advance(399)
    r2.core.safe_point(True)
    assert r2.core.service("agc").target == 0.06
    clock.advance(3)
    r2.core.safe_point(True)
    assert r2.core.service("agc").target == 0.05


def test_boot_replay_skips_a_key_that_moved_on_without_crashing(tmp_path, monkeypatch):
    clock = FakeClock()
    r = FileRig(tmp_path, monkeypatch, file_doc(), clock=clock).boot()
    owner(r, [E("agc.mic.target", 0.06)], trial_s=100)
    r.core.shutdown()
    d = r.disk()
    d["plugins"]["agc.mic"]["config"]["target"] = 0.09               # the owner edited the file by hand meanwhile
    r.path.write_text(json.dumps(d))
    clock.advance(500)
    r2 = FileRig(tmp_path, monkeypatch, None, clock=clock).boot()
    assert r2.core.service("agc").target == 0.09


def test_boot_replay_settles_statuses_left_by_a_crash(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc()).boot()
    c = owner(r, [E("agc.mic.target", 0.06)])                         # queued, never applied: the process died
    r.core.shutdown()
    r2 = FileRig(tmp_path, monkeypatch, None).boot()
    assert next(x for x in r2.core.changes() if x["id"] == c.id)["status"] == "applied"
    assert r2.core.service("agc").target == 0.06


def test_boot_guard_reverts_recent_agent_changes_and_logs_boot_guard(tmp_path, monkeypatch):
    clock = FakeClock()
    r = FileRig(tmp_path, monkeypatch, file_doc(), clock=clock).boot()
    r.core.turn_begin()
    a = r.core.submit([E("agc.mic.target", 0.06)], actor="hermes", scope="agent")
    b = r.core.submit([E("vad.silero.end_silence", 0.8)], actor="hermes", scope="agent")
    r.core.confirm(a.id, actor="hermes", scope="agent")                 # kept: the guard still reverts it
    o = owner(r, [E("agc.mic.max_gain", 12)])
    r.core.shutdown()
    clock.advance(60)
    r2 = FileRig(tmp_path, monkeypatch, None, clock=clock, boot_guard=True).boot()
    assert r2.core.service("agc").target == 0.05 and r2.core.service("vad").end_silence == 0.7
    assert r2.core.service("agc").max_gain == 12.0                      # owner changes are not touched
    guards = [c for c in r2.core.changes() if c["actor"] == "boot_guard"]
    assert {g["undo_of"] for g in guards} == {a.id, b.id} and all(g["scope"] == "system" for g in guards)
    assert o.id not in {g["undo_of"] for g in guards}


def test_boot_guard_reads_the_environment_flag(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc()).boot()
    r.core.turn_begin()
    r.core.submit([E("agc.mic.target", 0.06)], actor="hermes", scope="agent")
    r.core.shutdown()
    r2 = FileRig(tmp_path, monkeypatch, None, env={"MIRA_BOOT_GUARD": "1"}).boot()
    assert r2.core.service("agc").target == 0.05
    r2.core.shutdown()
    r3 = FileRig(tmp_path, monkeypatch, None).boot()
    assert not [c for c in r3.core.changes() if c["actor"] == "boot_guard"][1:]


def test_boot_guard_leaves_old_agent_changes_alone(tmp_path, monkeypatch):
    clock = FakeClock()
    r = FileRig(tmp_path, monkeypatch, file_doc(), clock=clock).boot()
    r.core.turn_begin()
    a = r.core.submit([E("agc.mic.target", 0.06)], actor="hermes", scope="agent")
    r.core.confirm(a.id, actor="hermes", scope="agent")
    r.core.shutdown()
    clock.advance(3600)                                                  # older than crash_guard_revert_s (1800)
    r2 = FileRig(tmp_path, monkeypatch, None, clock=clock, boot_guard=True).boot()
    assert r2.core.service("agc").target == 0.06


# ---- the legacy mirror
def mirror_specs(j):
    sl = specs(j)
    sl[0] = mk("agc.mic", ["agc"], [
        K("on", "bool", True, arg="agc", env="FAKE_AGC", unit=""),
        K("target", "float", 0.05, unit="rms", min=0.005, max=0.3, agent="bounded", agent_min=0.03, agent_max=0.10,
          agent_step=0.01, arg="agc_target", env="FAKE_AGC_TARGET", topic="hearing", spoken="microphone boost"),
        K("max_gain", "float", 10.0, unit="x", min=1, max=40, agent="bounded", agent_min=4, agent_max=14,
          agent_step=2, topic="hearing"),
        K("rest", "float", 3.0, unit="x", min=1, max=40)], j)
    return sl


def test_mirror_writes_listen_env_and_legacy_args_after_boot_and_every_applied_change(tmp_path, monkeypatch):
    j = []
    r = FileRig(tmp_path, monkeypatch, file_doc(), mirror=True, spec_list=mirror_specs(j)).boot()
    env = (r.dir / "listen.env").read_text()
    assert env.splitlines()[0] == "# generated by mirakit from mira.json; edit with mira set"
    assert "FAKE_AGC=1" in env and "FAKE_AGC_TARGET=0.05" in env
    owner(r, [E("agc.mic.target", 0.06)])
    assert "FAKE_AGC_TARGET=0.05" in (r.dir / "listen.env").read_text()       # persisted, but not applied yet
    r.core.safe_point(True)
    assert "FAKE_AGC_TARGET=0.06" in (r.dir / "listen.env").read_text()       # the mirror follows every applied change
    args = (r.dir / "legacy.args").read_text().split()
    assert args[:2] == ["--end-silence", "0.7"] and "--kws-threshold" in args
    assert stat.S_IMODE(os.stat(r.dir / "listen.env").st_mode) == 0o600


def test_first_mirror_keeps_the_hand_written_listen_env(tmp_path, monkeypatch):
    j = []
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "listen.env").write_text("MIRA_AGC_TARGET=0.07\n")
    r = FileRig(tmp_path, monkeypatch, file_doc(), mirror=True, spec_list=mirror_specs(j)).boot()
    baks = list(r.dir.glob("listen.env.bak-adopt-*"))
    assert len(baks) == 1 and baks[0].read_text() == "MIRA_AGC_TARGET=0.07\n"
    assert (r.dir / "listen.env").read_text().startswith("# generated by mirakit")
    owner(r, [E("agc.mic.target", 0.06)])
    assert len(list(r.dir.glob("listen.env.bak-adopt-*"))) == 1           # a generated file is not backed up again


def test_mirror_off_writes_neither_file(tmp_path, monkeypatch):
    j = []
    r = FileRig(tmp_path, monkeypatch, file_doc(), mirror=False, spec_list=mirror_specs(j)).boot()
    owner(r, [E("agc.mic.target", 0.06)])
    assert not (r.dir / "listen.env").exists() and not (r.dir / "legacy.args").exists()


def test_legacy_mirror_report_lists_what_rollback_cannot_carry(tmp_path, monkeypatch):
    j = []
    sl = mirror_specs(j)
    sl.append(mk("mute.zero", [], [K("mute_s", "float", 10.0, min=2, max=60)], j, required=False))
    r = FileRig(tmp_path, monkeypatch, file_doc(**{"mute.zero": {"enabled": True, "config": {"mute_s": 20}}}),
                spec_list=sl)
    assert r.core.legacy_mirror_report() == [{"key": "mute.zero.mute_s", "value": 20.0, "default": 10.0}]


def test_request_restart_writes_the_planned_marker(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc()).boot()
    r.core.request_restart(actor="owner", scope="owner")
    marker = r.dir / "state" / "planned"
    assert marker.exists() and int(marker.read_text()) == int(r.clock.t)
    with pytest.raises(SystemExit):
        r.core.safe_point(True)


def test_from_file_never_creates_anything_outside_the_config_dir(tmp_path, monkeypatch):
    r = FileRig(tmp_path, monkeypatch, file_doc(), mirror=True).boot()
    owner(r, [E("agc.mic.target", 0.06)])
    r.core.shutdown()
    assert list(r.home.rglob("*")) == []
    created = {p.name for p in r.dir.iterdir()}
    assert created <= {"mira.json", "mira.json.lock", "mira.json.lastgood", "history", "changes.jsonl", "listen.env",
                       "legacy.args"}


# =============================================================================================
# repair: no remote HTTP call while the write lock is held; no secret value in an owner command
# =============================================================================================

class GatedRemote(FakeRemote):
    """Blocks inside undo and confirm until the test opens the gate, as a slow mini would."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.gate = threading.Event()

    def request(self, method, path, body=None):
        if path in ("/v1/undo", "/v1/confirm"):
            self.entered.set()
            assert self.gate.wait(10), "the test never opened the gate"
        return super().request(method, path, body)


def _loop_thread_stays_free(rig, remote, action):
    """Run `action` on a worker thread, wait until it is inside the remote call, then show that the loop
    thread's safe_point and a core call that takes the write lock both return while the call is still open."""
    t = threading.Thread(target=action, daemon=True)
    t.start()
    assert remote.entered.wait(5)
    rig.clock.advance(1)
    done = []
    probe = threading.Thread(target=lambda: (rig.sp(), rig.core.status(), done.append(1)), daemon=True)
    probe.start()
    probe.join(2)
    freed = bool(done)
    remote.gate.set()
    t.join(5)
    assert freed, "safe_point or a core call blocked behind a slow remote call (write lock held)"
    assert not t.is_alive()


def test_slow_remote_does_not_hold_the_lock_during_session_revert(rig):
    remote = GatedRemote()
    rig.core.register_remote("speech", remote)
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)], trial_s=60)
    rig.core.attach_remote_change({"id": "S-000045"})
    _loop_thread_stays_free(rig, remote, lambda: rig.core.session("revert", actor="hermes", scope="agent"))
    assert rig.core.store.value("agc.mic.target") == 0.05
    assert rig.core.session("get", actor="x", scope="agent") is None


def test_slow_remote_does_not_hold_the_lock_during_session_keep(rig):
    remote = GatedRemote()
    rig.core.register_remote("speech", remote)
    rig.turn()
    a = agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.attach_remote_change({"id": "S-000045"})
    _loop_thread_stays_free(rig, remote, lambda: rig.core.session("keep", actor="hermes", scope="agent"))
    assert rig.last(a)["status"] == "confirmed"


def test_slow_remote_does_not_hold_the_lock_during_remote_undo_and_confirm(rig):
    remote = GatedRemote()
    rig.core.register_remote("speech", remote)
    _loop_thread_stays_free(rig, remote, lambda: rig.core.undo("S-000045", actor="owner", scope="owner"))
    remote.entered.clear()
    remote.gate.clear()
    _loop_thread_stays_free(rig, remote, lambda: rig.core.confirm("S-000045", actor="owner", scope="owner"))


def test_session_revert_keeps_local_then_remote_results_in_order(rig):
    remote = FakeRemote()
    rig.core.register_remote("speech", remote)
    rig.turn()
    agent(rig, [E("agc.mic.target", 0.06)])
    rig.core.attach_remote_change({"id": "S-000045"})
    rig.core.session("revert", actor="hermes", scope="agent")
    assert rig.core.store.value("agc.mic.target") == 0.05
    assert [c[1] for c in remote.calls if c[1].startswith("/v1/undo")] == ["/v1/undo"]


def test_owner_command_for_a_secret_knob_never_carries_its_value(tmp_path):
    j = []
    sl = [mk("brain.hermes", ["brain"], [K("key_file", "path", "", unit="", secret=True)], j)]
    core = Core.from_args(SimpleNamespace(), specs=sl, required_services=(), clock=FakeClock())
    core.boot()
    core.submit([E("brain.hermes.key_file", "/secret/path")], actor="owner", scope="owner")
    for scope in ("agent", "owner"):
        g = core.tuning_guide("", scope=scope)
        assert g["owner_only"], "the secret knob should be listed as owner only"
        assert "/secret" not in json.dumps(g)
        assert g["owner_only"][0]["owner_command"] == "mira set brain.hermes.key_file=<value> --trial 600"
    with pytest.raises(PolicyError) as ei:
        core.submit([E("brain.hermes.key_file", "/secret/other")], actor="hermes", scope="agent")
    assert "/secret" not in json.dumps(ei.value.__dict__, default=str) and "/secret" not in str(ei.value)
    core.shutdown()
