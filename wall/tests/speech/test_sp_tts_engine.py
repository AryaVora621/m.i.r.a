"""tts.engine: swaps, the candidate slot, previews, per-call settings (spec 2.9.2, 2.6 rule 6)."""
import gc
import io
import threading
import wave
import weakref

import numpy as np
import pytest

import sp_fakes as fk
from sp_fakes import real_config_dir_untouched  # noqa: F401  (autouse guard)
from mirakit.plugin import ConfigError
from mirakit.testkit import cfg_for
from speech_plugins import tts_engine as te

AMY = "vits-piper-en_US-amy-medium#0"
KRISTIN = "vits-piper-en_US-kristin-medium#0"
JENNY = "vits-piper-en_GB-jenny_dioco-medium#0"


@pytest.fixture
def stack(tmp_path):
    clock = fk.FakeClock()
    ctx, plugin, cat, backend = fk.build_tts_stack(tmp_path, clock=clock)
    yield types_ns(ctx=ctx, plugin=plugin, engine=plugin.engine, cat=cat, backend=backend, clock=clock, tmp=tmp_path)
    plugin.stop()
    ctx.cleanup()


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def wait_for(pred, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def peak_of(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return float(np.max(np.abs(x))) / 32767.0


def test_start_loads_warms_and_provides(stack):
    s = stack
    assert s.ctx.get("tts") is s.engine and s.engine.voice_id == AMY
    assert s.backend.loads == ["vits-piper-en_US-amy-medium"]
    assert s.backend.models["vits-piper-en_US-amy-medium"][0].calls[0][0] == "Ready."        # warmed
    assert s.engine.loaded() == [{"model_dir": "vits-piper-en_US-amy-medium", "role": "active", "voice": AMY}]


def test_synth_returns_wav_and_resolved_id_and_normalises_to_peak(stack):
    wav, vid = stack.engine.synth("hello")
    assert vid == AMY and peak_of(wav) == pytest.approx(0.89, abs=0.01)
    wav, vid = stack.engine.synth("hello", "", 1.3, 0.5)
    assert peak_of(wav) == pytest.approx(0.5, abs=0.01)
    calls = stack.backend.models["vits-piper-en_US-amy-medium"][0].calls
    assert calls[-1] == ("hello", 0, 1.3)
    wav, _ = stack.engine.synth("hello", "amy", 0.0, 9.0)                # peak clipped to the knob maximum
    assert peak_of(wav) == pytest.approx(0.98, abs=0.01)
    wav, _ = stack.engine.synth("hello", "", 0.01, 0.0)
    assert stack.backend.models["vits-piper-en_US-amy-medium"][0].calls[-1][2] == 0.5     # speed clipped to the minimum
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)


def test_settings_snapshot_speed_and_peak_apply_live(stack):
    s = stack
    cfg = s.plugin.cfg.replace(speed=1.2, peak=0.6)
    s.plugin.apply({"speed": 1.2, "peak": 0.6}, cfg)
    wav, _ = s.engine.synth("x")
    assert peak_of(wav) == pytest.approx(0.6, abs=0.01)
    assert s.backend.models["vits-piper-en_US-amy-medium"][0].calls[-1][2] == 1.2
    assert s.backend.loads == ["vits-piper-en_US-amy-medium"]            # nothing reloaded


def test_voice_change_to_another_model_builds_warms_and_swaps(stack):
    s = stack
    s.plugin.rebuild(s.plugin.cfg.replace(voice="kristin"))
    assert s.engine.voice_id == KRISTIN and s.backend.loads == ["vits-piper-en_US-amy-medium", "vits-piper-en_US-kristin-medium"]
    assert s.backend.models["vits-piper-en_US-kristin-medium"][0].calls[0][0] == "Ready."
    assert [r["model_dir"] for r in s.engine.loaded()] == ["vits-piper-en_US-kristin-medium"]     # old one dropped
    assert s.engine.synth("x")[1] == KRISTIN


def test_same_multi_speaker_model_only_changes_the_speaker_id(stack):
    s = stack
    s.plugin.rebuild(s.plugin.cfg.replace(voice="heart"))
    n = len(s.backend.loads)
    s.plugin.rebuild(s.plugin.cfg.replace(voice="bella"))
    assert len(s.backend.loads) == n                                     # no load
    assert s.engine.voice_id == "kokoro-int8-multi-lang-v1_0#af_bella"
    s.engine.synth("x")
    assert s.backend.models["kokoro-int8-multi-lang-v1_0"][0].calls[-1][1] == 2          # af_bella is index 2


def test_request_in_flight_finishes_on_the_old_model_while_the_swap_happens(tmp_path):
    clock = fk.FakeClock()
    backend = fk.FakeTtsBackend()
    gate = threading.Event()
    backend.gates["vits-piper-en_US-amy-medium"] = gate
    # warm off so the first model does not block at start
    ctx, plugin, cat, backend = fk.build_tts_stack(tmp_path, clock=clock, backend=backend, tts_cfg={"warm": False})
    engine = plugin.engine
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", engine.synth("long text")))
    t.start()
    old = backend.models["vits-piper-en_US-amy-medium"].pop(0)        # the fake backend must not keep it alive
    assert old.entered.wait(5)                                          # the request is inside generate(), blocked
    plugin.rebuild(plugin.cfg.replace(voice="kristin"))                 # the swap does not wait for it
    assert engine.voice_id == KRISTIN
    ref = weakref.ref(old)
    del old
    gc.collect()
    assert ref() is not None                                            # still referenced by the in-flight request
    gate.set()
    t.join(5)
    assert result["r"][1] == AMY                                        # finished on the old model and voice
    gc.collect()
    assert ref() is None                                                # then dropped by refcount
    assert engine.synth("next")[1] == KRISTIN
    plugin.stop()
    ctx.cleanup()


def test_failed_load_keeps_the_old_voice_and_raises(stack):
    s = stack
    s.backend.fail.add("vits-piper-en_US-kristin-medium")
    before = s.engine.voice_id
    with pytest.raises(RuntimeError) as e:
        s.plugin.rebuild(s.plugin.cfg.replace(voice="kristin"))
    assert "vits-piper-en_US-kristin-medium" in str(e.value) and "boom" not in str(e.value)
    assert s.engine.voice_id == before == AMY and s.engine.synth("x")[1] == AMY
    # the core's rollback rebuild(prev cfg) is cheap: no new load
    n = len(s.backend.loads)
    s.plugin.rebuild(cfg_for(te.SPEC))
    assert len(s.backend.loads) == n


def test_unknown_or_unusable_voice_in_a_rebuild_raises_and_changes_nothing(stack):
    s = stack
    with pytest.raises(ConfigError) as e:
        s.plugin.rebuild(s.plugin.cfg.replace(voice="nonesuch"))
    assert e.value.code == "unknown_voice"
    with pytest.raises(ConfigError):
        s.plugin.rebuild(s.plugin.cfg.replace(voice="ghost"))
    assert s.engine.voice_id == AMY


def test_threads_change_rebuilds_the_active_model(stack):
    s = stack
    s.plugin.rebuild(s.plugin.cfg.replace(threads=2))
    assert s.backend.loads.count("vits-piper-en_US-amy-medium") == 2


def test_backend_setting_change_rebuilds_the_active_model_once_and_the_rollback_is_free(stack):
    s = stack
    s.backend.sig = ("a",)
    s.engine.rebuild_for_backend(s.backend)                         # first sight of a signature: not stale yet
    first = s.backend.loads.count("vits-piper-en_US-amy-medium")
    s.engine.rebuild_for_backend(s.backend)
    assert s.backend.loads.count("vits-piper-en_US-amy-medium") == first
    s.plugin.rebuild(s.plugin.cfg)                                  # rebuild the model so it carries sig ("a",)
    n = s.backend.loads.count("vits-piper-en_US-amy-medium")
    s.backend.sig = ("b",)
    s.engine.rebuild_for_backend(s.backend)
    assert s.backend.loads.count("vits-piper-en_US-amy-medium") == n + 1 and s.engine.voice_id == AMY
    s.engine.rebuild_for_backend(s.backend)
    assert s.backend.loads.count("vits-piper-en_US-amy-medium") == n + 1       # nothing stale any more
    s.backend.fail.add("vits-piper-en_US-amy-medium")
    s.backend.sig = ("c",)
    with pytest.raises(RuntimeError):
        s.engine.rebuild_for_backend(s.backend)
    assert s.engine.voice_id == AMY
    m = s.backend.loads.count("vits-piper-en_US-amy-medium")
    s.backend.sig = ("b",)                                          # the core's rollback: the old setting again
    s.engine.rebuild_for_backend(s.backend)
    assert s.backend.loads.count("vits-piper-en_US-amy-medium") == m
    other = fk.FakeTtsBackend()
    other.sig = ("z",)
    s.engine.rebuild_for_backend(other)                            # a backend that built nothing here: ignored
    assert s.backend.loads.count("vits-piper-en_US-amy-medium") == m


def test_backend_setting_change_drops_that_backends_stale_candidates(stack):
    s = stack
    s.backend.sig = ("a",)
    s.plugin.rebuild(s.plugin.cfg)
    s.engine.synth("hi", "kristin")
    assert len(s.engine.loaded()) == 2
    s.backend.sig = ("b",)
    s.engine.rebuild_for_backend(s.backend)
    assert [r["role"] for r in s.engine.loaded()] == ["active"]


def test_the_real_piper_plugin_rebuild_reaches_the_engine_through_the_core_path(tmp_path, monkeypatch):
    import sys
    from speech_plugins import tts_piper
    sherpa = fk.FakeSherpa()
    monkeypatch.setitem(sys.modules, "sherpa_onnx", sherpa)
    ctx = fk.FakeCtx("speech")
    piper = tts_piper.SPEC.create(ctx, fk.cfg_for(tts_piper.SPEC))
    ctx.cfgs["tts.piper"] = piper.cfg
    piper.start()
    ctx2, plugin, cat, _ = fk.build_tts_stack(tmp_path, backend=piper)       # the engine sees the real backend
    piper.ctx = ctx2
    scales = lambda: [c["vits"].kw["length_scale"] for c in sherpa.configs if "vits" in c]
    assert scales() == [1.0]
    piper.rebuild(piper.cfg.replace(length_scale=1.5))
    assert scales() == [1.0, 1.5] and plugin.engine.voice_id == AMY
    piper.rebuild(piper.cfg)                                           # same setting again: no new model
    assert scales() == [1.0, 1.5]
    plugin.stop()
    ctx2.cleanup()
    ctx.cleanup()


def test_preview_loads_into_the_candidate_slot_and_changes_nothing(stack):
    s = stack
    wav, vid = s.engine.synth("hi", "kristin")
    assert vid == KRISTIN and s.engine.voice_id == AMY
    roles = {(r["model_dir"], r["role"]) for r in s.engine.loaded()}
    assert roles == {("vits-piper-en_US-amy-medium", "active"), ("vits-piper-en_US-kristin-medium", "candidate")}
    # a second preview of the same voice reuses the candidate
    s.engine.synth("hi", "kristin")
    assert s.backend.loads.count("vits-piper-en_US-kristin-medium") == 1
    # and the normal reply voice is untouched
    assert s.engine.synth("reply")[1] == AMY


def test_candidate_is_released_after_60_idle_seconds_with_an_injected_clock(stack):
    s = stack
    s.engine.synth("hi", "kristin")
    s.clock.advance(59)
    s.engine.sweep()
    assert len(s.engine.loaded()) == 2
    s.clock.advance(2)
    s.engine.sweep()
    assert [r["role"] for r in s.engine.loaded()] == ["active"]
    # using it again keeps it alive
    s.engine.synth("hi", "kristin")
    s.clock.advance(40)
    s.engine.synth("hi", "kristin")
    s.clock.advance(40)
    s.engine.sweep()
    assert len(s.engine.loaded()) == 2


def test_max_loaded_counts_the_active_model_and_evicts_the_candidate_first(stack):
    s = stack
    s.engine.synth("hi", "kristin")
    s.clock.advance(1)
    s.engine.synth("hi", "jenny")                                        # max_loaded 2: kristin makes way
    dirs = [(r["model_dir"], r["role"]) for r in s.engine.loaded()]
    assert ("vits-piper-en_US-kristin-medium", "candidate") not in dirs
    assert ("vits-piper-en_GB-jenny_dioco-medium", "candidate") in dirs and ("vits-piper-en_US-amy-medium", "active") in dirs


def test_max_loaded_one_refuses_a_voice_that_needs_another_model(tmp_path):
    ctx, plugin, cat, backend = fk.build_tts_stack(tmp_path, tts_cfg={"max_loaded": 1})
    with pytest.raises(ConfigError):
        plugin.engine.synth("hi", "kristin")
    assert backend.loads == ["vits-piper-en_US-amy-medium"]
    plugin.stop()
    ctx.cleanup()


def test_max_loaded_three_keeps_two_candidates_and_lowering_it_trims(tmp_path):
    ctx, plugin, cat, backend = fk.build_tts_stack(tmp_path, tts_cfg={"max_loaded": 3})
    e = plugin.engine
    e.synth("a", "kristin")
    e.synth("a", "jenny")
    assert len(e.loaded()) == 3
    plugin.apply({"max_loaded": 2}, plugin.cfg.replace(max_loaded=2))
    assert len(e.loaded()) == 2
    plugin.stop()
    ctx.cleanup()


def test_promoting_a_candidate_needs_no_new_load(stack):
    s = stack
    s.engine.synth("hi", "kristin")
    n = len(s.backend.loads)
    s.plugin.rebuild(s.plugin.cfg.replace(voice="kristin"))
    assert len(s.backend.loads) == n and s.engine.voice_id == KRISTIN
    assert [r["role"] for r in s.engine.loaded()] == ["active"]


def test_one_preview_render_at_a_time_second_gets_busy(tmp_path):
    backend = fk.FakeTtsBackend()
    gate = threading.Event()
    backend.gates["vits-piper-en_US-kristin-medium"] = gate
    ctx, plugin, cat, backend = fk.build_tts_stack(tmp_path, backend=backend, tts_cfg={"warm": False})
    e = plugin.engine
    t = threading.Thread(target=lambda: e.synth("hi", "kristin"))
    t.start()
    assert wait_for(lambda: backend.models.get("vits-piper-en_US-kristin-medium"))
    assert backend.models["vits-piper-en_US-kristin-medium"][0].entered.wait(5)
    with pytest.raises(te.BusyError):
        e.synth("hi", "jenny")
    assert e.synth("reply")[1] == AMY                                    # a normal reply is never blocked by a preview
    gate.set()
    t.join(5)
    assert e.synth("hi", "jenny")[1] == JENNY                            # slot free again
    plugin.stop()
    ctx.cleanup()


def test_unknown_voice_raises_with_near_and_does_not_touch_state(stack):
    s = stack
    with pytest.raises(ConfigError) as e:
        s.engine.synth("hi", "amee")
    assert e.value.code == "unknown_voice" and "amy" in e.value.near
    assert s.engine.voice_id == AMY


def test_synth_records_the_real_time_factor(stack):
    s = stack
    s.engine.synth("hi")
    assert s.cat.rtf(AMY) is not None and s.cat.rtf(AMY) > 0


def test_stop_releases_everything_and_a_second_start_works(stack):
    s = stack
    s.engine.synth("hi", "kristin")
    s.plugin.stop()
    assert s.engine.voice_id == "" and s.engine.loaded() == []
    with pytest.raises(RuntimeError):
        s.engine.synth("x")
    s.plugin.start()
    assert s.engine.voice_id == AMY


def _peak_tracker(backend):
    """Resident model count: loads minus models the engine has dropped (weakrefs after gc)."""
    refs = []
    orig = backend.load

    def load(model_dir, threads, opts):
        m = orig(model_dir, threads, opts)
        refs.append(weakref.ref(m))
        return m
    backend.load = load

    def resident():
        gc.collect()
        return sum(1 for r in refs if r() is not None)
    return resident


def test_persisted_voice_swap_never_holds_more_than_max_loaded_models(tmp_path):
    ctx, plugin, cat, backend = fk.build_tts_stack(tmp_path, clock=fk.FakeClock())
    resident = _peak_tracker(backend)
    engine = plugin.engine
    engine.synth("hi", "kristin")                       # active amy + candidate kristin: at the max of 2
    seen = []
    orig = backend.load

    def load(model_dir, threads, opts):
        seen.append(resident())                         # models alive before the new one is created
        return orig(model_dir, threads, opts)
    backend.load = load
    plugin.rebuild(plugin.cfg.replace(voice="jenny"))
    assert seen and max(seen) + 1 <= plugin.cfg.max_loaded, seen
    assert engine.voice_id == JENNY
    plugin.stop()
    ctx.cleanup()


def test_preview_arriving_during_a_swap_build_waits_instead_of_loading_beside_it(tmp_path):
    backend = fk.FakeTtsBackend()
    gate = threading.Event()
    backend.gates["vits-piper-en_US-kristin-medium"] = gate
    ctx, plugin, cat, backend = fk.build_tts_stack(tmp_path, clock=fk.FakeClock(), backend=backend)
    engine = plugin.engine
    swap = threading.Thread(target=lambda: plugin.rebuild(plugin.cfg.replace(voice="kristin")))
    swap.start()
    assert wait_for(lambda: "vits-piper-en_US-kristin-medium" in backend.loads)     # build is running, warm blocked
    out = {}
    prev = threading.Thread(target=lambda: out.setdefault("r", engine.synth("hi", "jenny")))
    prev.start()
    import time
    time.sleep(0.2)
    assert "vits-piper-en_GB-jenny_dioco-medium" not in backend.loads               # held back by the load lock
    gate.set()
    swap.join(5)
    prev.join(5)
    assert out["r"][1] == JENNY and engine.voice_id == KRISTIN
    roles = sorted(r["role"] for r in engine.loaded())
    assert roles == ["active", "candidate"]
    plugin.stop()
    ctx.cleanup()
