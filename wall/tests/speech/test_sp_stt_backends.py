"""The five STT backends: the v1 branches, one module each, over fake faster_whisper and sherpa_onnx."""
import gc
import sys
import types

import numpy as np
import pytest

import sp_fakes as fk
from mirakit.testkit import FakeCtx, cfg_for
from speech_plugins import (stt_faster_whisper as fw, stt_sherpa_moonshine as sm, stt_sherpa_moonshine_v2 as sm2,
                            stt_sherpa_transducer as st, stt_sherpa_whisper as sw)

SILENCE = np.zeros(16000, np.float32)


class Seg:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def fake_fw(monkeypatch):
    calls = {}

    class FakeModel:
        def __init__(self, name, **kw):
            calls["init"] = (name, kw)

        def transcribe(self, samples, **kw):
            calls["transcribe"] = kw
            return [Seg(" Mira, "), Seg("open Onshape ")], None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return calls


class FakeRec:
    """One recogniser class for every sherpa from_* constructor; decode results are scripted."""
    results: list = []

    def __init__(self, kind, kw):
        self.kind, self.kw, self.decodes = kind, kw, []

    def create_stream(self):
        stream = types.SimpleNamespace(fed=None, result=types.SimpleNamespace(text=""))
        stream.accept_waveform = lambda rate, x: setattr(stream, "fed", x)
        return stream

    def decode_stream(self, s):
        self.decodes.append(s.fed)
        s.result.text = type(self).results.pop(0) if type(self).results else "hello "


@pytest.fixture
def sherpa(monkeypatch):
    made = []
    mod = types.ModuleType("sherpa_onnx")

    class OfflineRecognizer:
        pass

    for kind in ("whisper", "moonshine", "moonshine_v2", "transducer"):
        def make(kind=kind):
            def ctor(**kw):
                rec = FakeRec(kind, kw)
                made.append(rec)
                return rec
            return staticmethod(ctor)
        setattr(OfflineRecognizer, f"from_{kind}", make())
    mod.OfflineRecognizer = OfflineRecognizer
    monkeypatch.setitem(sys.modules, "sherpa_onnx", mod)
    FakeRec.results = []
    return made


def mkdir(tmp, name, files):
    d = tmp / name
    d.mkdir()
    for f in files:
        (d / f).write_text("")
    return d


def test_faster_whisper_forwards_everything_and_joins_segments(tmp_path, fake_fw):
    m = fw.load_model("faster-whisper:base.en", tmp_path, 3, {"compute": "float32", "prompt": "Vocabulary: Onshape.",
                                                              "hotwords": "Onshape", "beam_size": 3})
    assert m.transcribe(SILENCE) == "Mira, open Onshape"
    name, kw = fake_fw["init"]
    assert name == "base.en" and kw["compute_type"] == "float32" and kw["cpu_threads"] == 3 and kw["device"] == "cpu"
    assert kw["download_root"] == str(tmp_path / "fw")
    t = fake_fw["transcribe"]
    assert t["initial_prompt"] == "Vocabulary: Onshape." and t["hotwords"] == "Onshape" and t["beam_size"] == 3
    assert t["language"] == "en" and t["condition_on_previous_text"] is False


def test_faster_whisper_empty_strings_reach_the_library_as_none(tmp_path, fake_fw):
    m = fw.load_model("faster-whisper:tiny.en", tmp_path, 4, {"prompt": "", "hotwords": ""})
    m.transcribe(SILENCE)
    assert fake_fw["transcribe"]["initial_prompt"] is None and fake_fw["transcribe"]["hotwords"] is None


def test_faster_whisper_reads_its_attributes_at_every_transcribe(tmp_path, fake_fw):
    m = fw.load_model("faster-whisper:tiny.en", tmp_path, 4, {"prompt": "one"})
    m.transcribe(SILENCE)
    assert fake_fw["transcribe"]["initial_prompt"] == "one"
    m.prompt, m.beam_size = "two", 5
    m.transcribe(SILENCE)
    assert fake_fw["transcribe"]["initial_prompt"] == "two" and fake_fw["transcribe"]["beam_size"] == 5


def test_faster_whisper_plugin_applies_live_knobs_to_models_it_built_and_holds_them_weakly(tmp_path, fake_fw):
    ctx = FakeCtx("speech")
    cfg = cfg_for(fw.SPEC)
    p = fw.SPEC.create(ctx, cfg)
    p.start()
    assert ctx.all("stt-backend") == [p] and p.backend_ids == ("faster-whisper",)
    m = p.load("faster-whisper:tiny.en", tmp_path, 4, {})
    assert fake_fw["init"][1]["compute_type"] == "int8"
    m.transcribe(SILENCE)
    assert fake_fw["transcribe"]["initial_prompt"].startswith("Vocabulary: Mira, Hermes")
    new = cfg.replace(prompt="fresh", hotwords="Orca", beam=4, language="de")
    p.apply({"prompt": "fresh"}, new)
    m.transcribe(SILENCE)
    t = fake_fw["transcribe"]
    assert (t["initial_prompt"], t["hotwords"], t["beam_size"], t["language"]) == ("fresh", "Orca", 4, "de")
    ref = __import__("weakref").ref(m)
    del m
    gc.collect()
    assert ref() is None and p.status() == {"models": 0}              # the plugin never keeps an old model alive


def test_faster_whisper_explicit_opts_beat_the_plugin_config(tmp_path, fake_fw):
    p = fw.SPEC.create(FakeCtx("speech"), cfg_for(fw.SPEC, compute="int16"))
    p.load("faster-whisper:tiny.en", tmp_path, 4, {"compute": "float32"})
    assert fake_fw["init"][1]["compute_type"] == "float32"


def test_sherpa_whisper_prefers_int8_and_the_fp32_suffix_flips_it(tmp_path, sherpa):
    mkdir(tmp_path, "w", ["base.en-encoder.onnx", "base.en-encoder.int8.onnx", "base.en-decoder.onnx",
                          "base.en-decoder.int8.onnx", "base.en-tokens.txt"])
    sw.load_model("sherpa-whisper:w", tmp_path, 2, {})
    kw = sherpa[-1].kw
    assert kw["encoder"].endswith("base.en-encoder.int8.onnx") and kw["decoder"].endswith("base.en-decoder.int8.onnx")
    assert kw["tokens"].endswith("base.en-tokens.txt") and kw["language"] == "en" and kw["task"] == "transcribe"
    assert kw["num_threads"] == 2 and kw["provider"] == "cpu"
    sw.load_model("sherpa-whisper-fp32:w", tmp_path, 2, {})
    assert sherpa[-1].kw["encoder"].endswith("base.en-encoder.onnx")
    sw.load_model("sherpa-whisper:w", tmp_path, 2, {"fp32": True})
    assert sherpa[-1].kw["decoder"].endswith("base.en-decoder.onnx")
    mkdir(tmp_path, "empty", [])
    with pytest.raises(FileNotFoundError):
        sw.load_model("sherpa-whisper:empty", tmp_path, 2, {})


def test_sherpa_whisper_transcribes(tmp_path, sherpa):
    mkdir(tmp_path, "w", ["x-encoder.int8.onnx", "x-decoder.int8.onnx", "x-tokens.txt"])
    m = sw.load_model("sherpa-whisper:w", tmp_path, 2, {})
    FakeRec.results = [" hi there "]
    assert m.transcribe(SILENCE) == "hi there"


def test_moonshine_v1_files_and_the_retry_rule(tmp_path, sherpa):
    mkdir(tmp_path, "m", ["preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx",
                          "cached_decode.int8.onnx", "tokens.txt"])
    m = sm.load_model("sherpa-moonshine:m", tmp_path, 2, {})
    kw = sherpa[-1].kw
    assert kw["preprocessor"].endswith("preprocess.onnx") and kw["encoder"].endswith("encode.int8.onnx")
    assert kw["uncached_decoder"].endswith("uncached_decode.int8.onnx") and kw["cached_decoder"].endswith("cached_decode.int8.onnx")
    loud = (np.ones(32000) * 0.1).astype(np.float32)
    FakeRec.results = ["", "second try"]
    assert m.transcribe(loud) == "second try"
    assert len(sherpa[-1].decodes) == 2 and len(sherpa[-1].decodes[1]) == 32000 - 3200     # 0.2 s offset
    FakeRec.results = [""]
    sherpa[-1].decodes.clear()
    assert m.transcribe(SILENCE) == "" and len(sherpa[-1].decodes) == 1                   # a 1 s clip is not longer than 1 s
    FakeRec.results = [""]
    sherpa[-1].decodes.clear()
    assert m.transcribe(np.zeros(32000, np.float32)) == "" and len(sherpa[-1].decodes) == 1     # quiet: no retry


def test_moonshine_retry_knobs_are_live_and_default_to_the_v1_numbers(tmp_path, sherpa):
    mkdir(tmp_path, "m", ["preprocess.onnx", "encode.onnx", "uncached_decode.onnx", "cached_decode.onnx", "tokens.txt"])
    ctx = FakeCtx("speech")
    cfg = cfg_for(sm.SPEC)
    assert (cfg.retry_min_s, cfg.retry_rms, cfg.retry_offset_s) == (1.0, 0.01, 0.2)
    p = sm.SPEC.create(ctx, cfg)
    p.start()
    m = p.load("sherpa-moonshine:m", tmp_path, 2, {})
    assert (m.retry_min_s, m.retry_rms, m.retry_offset_s) == (1.0, 0.01, 0.2)
    p.apply({"retry_offset_s": 0.5}, cfg.replace(retry_offset_s=0.5, retry_min_s=0.5))
    assert (m.retry_offset_s, m.retry_min_s) == (0.5, 0.5)
    loud = (np.ones(16000) * 0.1).astype(np.float32)               # now longer than 0.5 s, so the retry runs
    FakeRec.results = ["", "x"]
    assert m.transcribe(loud) == "x" and len(sherpa[-1].decodes[1]) == 16000 - 8000


def test_moonshine_v2_globs_encoder_and_decoder(tmp_path, sherpa):
    mkdir(tmp_path, "v2", ["encoder_model.ort", "decoder_model_merged.ort", "tokens.txt"])
    sm2.load_model("sherpa-moonshine-v2:v2", tmp_path, 2, {})
    kw = sherpa[-1].kw
    assert kw["encoder"].endswith("encoder_model.ort") and kw["decoder"].endswith("decoder_model_merged.ort")
    assert kw["tokens"].endswith("tokens.txt") and kw["provider"] == "cpu"


def test_transducer_picks_int8_files_and_the_model_type(tmp_path, sherpa):
    mkdir(tmp_path, "t", ["encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "encoder.onnx",
                          "decoder.onnx", "joiner.onnx", "tokens.txt"])
    st.load_model("sherpa-nemo-transducer:t", tmp_path, 2, {})
    assert sherpa[-1].kw["model_type"] == "nemo_transducer" and sherpa[-1].kw["encoder"].endswith("encoder.int8.onnx")
    st.load_model("sherpa-transducer:t", tmp_path, 2, {})
    assert sherpa[-1].kw["model_type"] == "transducer"
    st.load_model("sherpa-nemo-transducer-fp32:t", tmp_path, 2, {})
    assert sherpa[-1].kw["model_type"] == "nemo_transducer" and sherpa[-1].kw["joiner"].endswith("joiner.onnx")
    assert st.IDS == ("sherpa-transducer", "sherpa-nemo-transducer")


def test_every_backend_registers_a_factory_and_loads_nothing_at_start():
    for mod in (fw, sw, sm, sm2, st):
        ctx = FakeCtx("speech")
        p = mod.SPEC.create(ctx, cfg_for(mod.SPEC))
        p.start()
        assert ctx.all("stt-backend") == [p] and set(p.backend_ids) == set(mod.IDS)


def test_faster_whisper_signature_is_the_rebuild_class_setting_only():
    ctx = FakeCtx("speech")
    cfg = cfg_for(fw.SPEC)
    p = fw.SPEC.create(ctx, cfg)
    assert p.signature() == ("int8",)
    p.apply({"prompt": "x"}, cfg.replace(prompt="x", beam=3))
    assert p.signature() == ("int8",)                               # live knobs never force a rebuild
