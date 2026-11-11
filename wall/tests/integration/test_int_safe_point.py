"""Integration: safe points (spec 2.3.2) on the real facade, core and plugins, in audio time.

A change is submitted from the fake Hermes' HTTP thread while the loop thread is blocked in Brain.ask. Live edits
land at turn_end; rebuilds wait for an ARMED safe point that is also quiet for rebuild_quiet_s."""
import collections
import json
import types

import pytest

from test_int_facade import CAL_REPLY, CAL_TEXT, WALL, Rig, cal_audio, ml, rig_factory  # noqa: F401
from mirakit.core import RESET_STREAM
from mirakit.plugin import Edit
import listener_plugins.wake_kws as wake_kws_mod


def spy_loop(r, monkeypatch):
    """Record every safe_point call, create_stream on the wake service, source flushes and pre-roll clears."""
    log = types.SimpleNamespace(sp=[], streams=[], flushes=[], clears=[], marks=[])
    real_sp, real_service = r.core.safe_point, r.core.service

    def safe_point(armed):
        flags = real_sp(armed)
        log.sp.append((r.src.clock(), armed, flags))
        return flags
    r.core.safe_point = safe_point

    class Wake:
        def __init__(self, real):
            self._real = real

        def create_stream(self, *a):
            log.streams.append((r.src.clock(), id(self._real)))
            return self._real.create_stream(*a)

        def __getattr__(self, n):
            return getattr(self._real, n)

    def service(name, *d):
        v = real_service(name, *d)
        return Wake(v) if name == "wake" and v is not None else v
    r.core.service = service

    real_flush = r.src.flush
    r.src.flush = lambda: (log.flushes.append(r.src.clock()), real_flush())[1]

    class SpyDeque(collections.deque):
        def clear(self):
            log.clears.append(r.src.clock())
            super().clear()
    monkeypatch.setattr(ml, "collections", types.SimpleNamespace(deque=SpyDeque))

    orig_mark = r.lis._mark

    def mark(kind, detail=""):
        log.marks.append((kind, r.src.clock()))
        orig_mark(kind, detail)
    r.lis._mark = mark
    return log


def test_live_change_mid_turn_applies_after_the_turn_and_the_next_chunk_uses_it(rig_factory):
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes=[CAL_REPLY])
    seen = {}
    calls = []
    orig = r.lis.agc.process
    r.lis.agc.process = lambda c: (calls.append((r.src.clock(), r.lis.agc.target)), orig(c))[1]

    def mid_turn(i):
        seen["phase"] = r.core.turn.phase
        seen["t"] = r.src.clock()
        ch = r.core.submit([Edit("agc.mic.target", 0.06)], actor="owner", scope="owner", reason="mid turn")
        seen["status"] = ch.status
        seen["target_now"] = r.lis.agc.target          # loop thread is blocked: nothing may have moved
        seen["arg_now"] = r.args.agc_target
    r.hermes.on_chat = mid_turn
    r.lis.run()
    assert seen["phase"] == "brain" and seen["status"] == "queued"
    assert seen["target_now"] == 0.05 and seen["arg_now"] in (None, 0.05)
    before = [t for c, t in calls if c <= seen["t"]]
    after = [t for c, t in calls if c > seen["t"]]
    assert before and set(before) == {0.05}
    assert after and set(after) == {0.06}              # the very next chunk after the turn runs with the new value
    assert r.args.agc_target == 0.06 and r.lis.agc.target == 0.06
    assert r.core.changes(limit=1)[0]["status"] == "applied"


@pytest.fixture
def rebuild_class_kws(monkeypatch):
    """The shipped wake.kws knobs are restart class (a Pi spotter build takes 5 to 13 s, pre-flight 2026-11-10), so the
    rebuild machinery of the core is exercised with a rig-local copy of the spec that applies them as a rebuild."""
    import dataclasses
    real = wake_kws_mod.SPEC
    assert {k.applies for k in real.knobs} == {"restart"}          # the real specs stay restart class
    monkeypatch.setattr(wake_kws_mod, "SPEC", dataclasses.replace(
        real, knobs=tuple(dataclasses.replace(k, applies="rebuild") if k.applies == "restart" and k.name != "threads" else k
                          for k in real.knobs)))


def test_rebuild_waits_for_an_armed_quiet_safe_point_then_resets_the_stream(rig_factory, monkeypatch, rebuild_class_kws):
    r = rig_factory(cal_audio(tail=30.0), stt=[CAL_TEXT], hermes=[CAL_REPLY])
    log = spy_loop(r, monkeypatch)
    old_kws = r.core.service("wake")._real
    seen = {}

    def mid_turn(i):
        seen["t"] = r.src.clock()
        ch = r.core.submit([Edit("wake.kws.threshold", 0.12)], actor="owner", scope="owner")
        seen["status"], seen["applies"] = ch.status, ch.applies
    r.hermes.on_chat = mid_turn
    r.lis.run()
    assert seen["applies"] == "rebuild" and seen["status"] == "queued"
    t_turn_end = dict(log.marks)["followup"]             # the last turn event the core noted
    flagged = [(t, armed) for t, armed, f in log.sp if f & RESET_STREAM]
    assert len(flagged) == 1
    t_reset, armed = flagged[0]
    assert armed and t_reset >= t_turn_end + 10.0 - 1e-6   # rebuild_quiet_s, and past the 8 s follow-up window
    assert t_reset >= t_turn_end + 8.0
    assert not any(f for t, a, f in log.sp if t < t_reset)  # nothing earlier touched the stream
    new_kws = r.core.service("wake")._real
    assert new_kws is not old_kws
    # a new stream from the NEW spotter right at the reset, the pre-roll cleared, the backlog flushed
    assert any(abs(t - t_reset) < 0.11 and sid == id(new_kws) for t, sid in log.streams)
    assert any(abs(t - t_reset) < 0.11 for t in log.clears)
    assert any(abs(t - t_reset) < 0.11 for t in log.flushes)
    ch = r.core.changes(limit=1)[0]
    assert ch["status"] == "applied" and ch["edits"][0]["new"] == 0.12
    assert r.args.kws_threshold == 0.12


def test_a_rebuild_never_runs_while_a_follow_up_window_is_open(rig_factory, monkeypatch, rebuild_class_kws):
    r = rig_factory(cal_audio(tail=6.0), stt=[CAL_TEXT], hermes=[CAL_REPLY])
    log = spy_loop(r, monkeypatch)
    r.hermes.on_chat = lambda i: r.core.submit([Edit("wake.kws.threshold", 0.12)], actor="owner", scope="owner")
    r.lis.run()                                              # EOF comes inside the 8 s window: the change stays queued
    assert not any(f for _, _, f in log.sp)
    assert r.core.changes(limit=1)[0]["status"] == "queued"
    assert r.core.service("wake")._real is not None


def test_a_failing_rebuild_reverts_the_value_and_the_file(tmp_path, monkeypatch, rebuild_class_kws):
    real_build = wake_kws_mod.build_kws

    def build(args):
        if args.kws_threshold == 0.12:
            raise RuntimeError("model would not load")
        return real_build(args)
    monkeypatch.setattr(wake_kws_mod, "build_kws", build)
    r = Rig(tmp_path, monkeypatch, cal_audio(3.0), mode="file")
    try:
        before = json.loads(r.cfg_path.read_text())
        ch = r.core.submit([Edit("wake.kws.threshold", 0.12)], actor="owner", scope="owner")
        assert ch.status == "queued"
        r.lis.run()                                      # first safe point: armed and quiet, so it rebuilds, fails, restores
        done = r.core.changes(limit=1)[0]
        assert done["status"] == "failed" and "model would not load" in (done["error"] or "")
        assert r.core.service("wake") is not None
        now = json.loads(r.cfg_path.read_text())
        cfg = now["plugins"]["wake.kws"]["config"]
        assert cfg.get("threshold", 0.15) == 0.15         # the file no longer carries the failed value
        assert now["revision"] > before["revision"]         # the revert was written as its own revision
        assert [c["status"] for c in r.core.changes(limit=5)][0] == "failed"
        assert r.args.kws_threshold == 0.15
    finally:
        r.close()
