"""stt.engine: build, warm, swap, in-flight requests, failed builds, kind validation."""
import gc
import threading
import time
import weakref

import numpy as np
import pytest

import sp_fakes as fk
from mirakit.plugin import ConfigError, PluginBase
from mirakit.testkit import FakeCtx, cfg_for
from speech_plugins import server_http, stt_engine as se

SIL = np.zeros(16000, np.float32)


class FakeSttModel:
    def __init__(self, kind):
        self.kind, self.lock = kind, threading.Lock()
        self.calls = 0
        self.gate = None
        self.entered = threading.Event()

    def transcribe(self, samples):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        return f"text from {self.kind}"


class FakeSttBackend(PluginBase):
    name, priority = "stt.fake", 0
    backend_ids = ("faster-whisper", "sherpa-moonshine")

    def __init__(self):
        self.loads, self.fail, self.gates, self.models = [], set(), {}, []
        self.sig = None

    def signature(self):
        return self.sig

    def load(self, kind, models, threads, opts):
        self.loads.append((kind, models, threads, dict(opts)))
        if kind in self.fail:
            raise RuntimeError("secret detail")
        m = FakeSttModel(kind)
        m.gate = self.gates.get(kind)
        self.models.append(m)
        return m


@pytest.fixture
def stack(tmp_path):
    ctx = FakeCtx("speech")
    ctx.cfgs["server.http"] = cfg_for(server_http.SPEC, base_dir=str(tmp_path / "base"))
    backend = FakeSttBackend()
    ctx.provide("stt-backend", backend)
    cfg = cfg_for(se.SPEC, kind="faster-whisper:tiny.en")
    ctx.cfgs["stt.engine"] = cfg
    plugin = se.SPEC.create(ctx, cfg)
    plugin.start()
    yield ctx, plugin, backend
    plugin.stop()
    ctx.cleanup()


def test_start_builds_warms_and_provides(stack, tmp_path):
    ctx, plugin, backend = stack
    assert ctx.get("stt") is plugin.engine and plugin.engine.kind == "faster-whisper:tiny.en"
    kind, models, threads, opts = backend.loads[0]
    assert models == tmp_path / "base" / "models" and threads == 4 and opts["fp32"] is False
    assert backend.models[0].calls == 1                                  # the warm-up decode
    text, ms = plugin.engine.transcribe(SIL)
    assert text == "text from faster-whisper:tiny.en" and isinstance(ms, int)


def test_fp32_suffix_is_passed_as_an_option_and_the_name_is_split(stack):
    ctx, plugin, backend = stack
    plugin.rebuild(plugin.cfg.replace(kind="sherpa-moonshine-fp32:m1"))
    kind, _m, _t, opts = backend.loads[-1]
    assert kind == "sherpa-moonshine-fp32:m1" and opts == {"fp32": True, "name": "m1", "backend": "sherpa-moonshine"}


def test_kind_change_swaps_and_the_old_model_is_freed(stack):
    ctx, plugin, backend = stack
    old = weakref.ref(backend.models[0])
    backend.models.clear()
    plugin.rebuild(plugin.cfg.replace(kind="sherpa-moonshine:m1"))
    gc.collect()
    assert plugin.engine.kind == "sherpa-moonshine:m1" and old() is None


def test_request_in_flight_finishes_on_the_old_model(stack):
    ctx, plugin, backend = stack
    gate = threading.Event()
    old = backend.models[0]
    old.gate = gate
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", plugin.engine.transcribe(SIL)))
    t.start()
    for _ in range(500):                                                 # call 1 was the warm-up, call 2 is ours
        if old.calls >= 2:
            break
        time.sleep(0.01)
    assert old.calls >= 2
    plugin.rebuild(plugin.cfg.replace(kind="sherpa-moonshine:m1"))       # does not wait for the blocked call
    assert plugin.engine.kind == "sherpa-moonshine:m1"
    gate.set()
    t.join(5)
    assert out["r"][0] == "text from faster-whisper:tiny.en"
    assert plugin.engine.transcribe(SIL)[0] == "text from sherpa-moonshine:m1"


def test_failed_build_keeps_the_old_model_and_names_the_model_not_the_cause(stack):
    ctx, plugin, backend = stack
    backend.fail.add("sherpa-moonshine:bad")
    with pytest.raises(RuntimeError) as e:
        plugin.rebuild(plugin.cfg.replace(kind="sherpa-moonshine:bad"))
    assert "bad" in str(e.value) and "secret detail" not in str(e.value)
    assert plugin.engine.kind == "faster-whisper:tiny.en" and plugin.cfg.kind == "faster-whisper:tiny.en"
    n = len(backend.loads)
    plugin.rebuild(cfg_for(se.SPEC, kind="faster-whisper:tiny.en"))        # the rollback is free
    assert len(backend.loads) == n


def test_unregistered_backend_raises_and_keeps_the_old_model(stack):
    ctx, plugin, backend = stack
    with pytest.raises(RuntimeError):
        plugin.rebuild(plugin.cfg.replace(kind="sherpa-whisper:x"))        # fake backend does not claim it
    assert plugin.engine.kind == "faster-whisper:tiny.en"


def test_threads_change_rebuilds_and_warm_off_skips_the_warmup(stack):
    ctx, plugin, backend = stack
    plugin.rebuild(plugin.cfg.replace(threads=2, warm=False))
    assert backend.loads[-1][2] == 2 and backend.models[-1].calls == 0
    plugin.apply({"warm": True}, plugin.cfg.replace(warm=True))
    assert plugin.cfg.warm is True


def test_backend_setting_change_rebuilds_only_the_model_that_backend_built(stack):
    ctx, plugin, backend = stack
    backend.sig = ("int8",)
    plugin.engine.rebuild_for_backend(backend)                        # the active model was built with sig None: stale
    n = len(backend.loads)
    plugin.engine.rebuild_for_backend(backend)
    assert len(backend.loads) == n                                      # same setting: nothing to do
    backend.sig = ("float32",)
    plugin.engine.rebuild_for_backend(backend)
    assert len(backend.loads) == n + 1 and plugin.engine.kind == "faster-whisper:tiny.en"
    other = FakeSttBackend()
    other.sig = ("x",)
    plugin.engine.rebuild_for_backend(other)                          # a backend that built nothing here
    assert len(backend.loads) == n + 1 and other.loads == []


def test_failed_backend_rebuild_keeps_the_model_and_the_rollback_is_free(stack):
    ctx, plugin, backend = stack
    backend.sig = ("int8",)
    plugin.engine.rebuild_for_backend(backend)
    n = len(backend.loads)
    backend.fail.add("faster-whisper:tiny.en")
    backend.sig = ("int16",)
    with pytest.raises(RuntimeError):
        plugin.engine.rebuild_for_backend(backend)
    assert plugin.engine.transcribe(SIL)[0] == "text from faster-whisper:tiny.en"
    backend.sig = ("int8",)                                              # the core rebuilds with the previous config
    plugin.engine.rebuild_for_backend(backend)
    assert len(backend.loads) == n + 1                                   # only the failed attempt


def test_the_real_faster_whisper_plugin_rebuild_reaches_the_engine(tmp_path, monkeypatch):
    import sys
    import types
    from speech_plugins import stt_faster_whisper as fw
    inits = []

    class WM:
        def __init__(self, name, **kw):
            inits.append(kw["compute_type"])
            if kw["compute_type"] == "int16":
                raise RuntimeError("unsupported")

        def transcribe(self, samples, **kw):
            return [types.SimpleNamespace(text="ok")], None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = WM
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    ctx = FakeCtx("speech")
    ctx.cfgs["server.http"] = cfg_for(server_http.SPEC, base_dir=str(tmp_path / "base"))
    backend = fw.SPEC.create(ctx, cfg_for(fw.SPEC))
    ctx.cfgs[fw.SPEC.name] = backend.cfg
    backend.start()
    cfg = cfg_for(se.SPEC, kind="faster-whisper:tiny.en", warm=False)
    ctx.cfgs["stt.engine"] = cfg
    plugin = se.SPEC.create(ctx, cfg)
    plugin.start()
    assert inits == ["int8"]
    backend.rebuild(backend.cfg.replace(compute="float32"))
    assert inits == ["int8", "float32"] and plugin.engine.transcribe(SIL)[0] == "ok"
    backend.rebuild(backend.cfg)                                        # the same compute again: nothing
    assert inits == ["int8", "float32"]
    with pytest.raises(RuntimeError):
        backend.rebuild(backend.cfg.replace(compute="int16"))
    assert plugin.engine.transcribe(SIL)[0] == "ok" and plugin.engine.kind == "faster-whisper:tiny.en"
    backend.rebuild(backend.cfg.replace(compute="float32"))             # what the registry does after the failure
    assert inits == ["int8", "float32", "int16"]
    assert ctx.has("stt-backend")                                       # still provided: the registry checks it
    plugin.stop()
    ctx.cleanup()


def test_the_engine_keeps_its_old_config_when_a_rebuild_fails(stack):
    ctx, plugin, backend = stack
    backend.fail.add("sherpa-moonshine:bad")
    old = plugin.cfg
    with pytest.raises(RuntimeError):
        plugin.rebuild(old.replace(kind="sherpa-moonshine:bad"))
    assert plugin.cfg is old


def test_kind_validation():
    se.SPEC.validate({"kind": "faster-whisper:tiny.en"})
    se.SPEC.validate({"kind": "sherpa-nemo-transducer-fp32:parakeet"})
    for bad in ("tiny.en", "faster-whisper:", "nonesuch:x", ":x", "faster whisper:x"):
        with pytest.raises(ConfigError) as e:
            se.SPEC.validate({"kind": bad})
        assert e.value.field == "kind"


def test_backend_id_list_equals_what_the_backend_modules_declare():
    from speech_plugins import (stt_faster_whisper, stt_sherpa_moonshine, stt_sherpa_moonshine_v2,
                                stt_sherpa_transducer, stt_sherpa_whisper)
    ids = set()
    for m in (stt_faster_whisper, stt_sherpa_moonshine, stt_sherpa_moonshine_v2, stt_sherpa_transducer, stt_sherpa_whisper):
        ids |= set(m.IDS)
    assert ids == set(se.BACKEND_IDS)


def test_stop_drops_the_model(stack):
    ctx, plugin, backend = stack
    plugin.stop()
    assert plugin.engine.kind == ""
    with pytest.raises(RuntimeError):
        plugin.engine.transcribe(SIL)
    plugin.start()
    assert plugin.engine.kind == "faster-whisper:tiny.en"
