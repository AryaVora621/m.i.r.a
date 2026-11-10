"""tts.piper and tts.kokoro backends over a fake sherpa_onnx."""
import sys

import numpy as np
import pytest

import sp_fakes as fk
from mirakit.testkit import FakeCtx, cfg_for
from speech_plugins import tts_kokoro, tts_piper


@pytest.fixture
def sherpa(monkeypatch):
    fake = fk.FakeSherpa()
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return fake


@pytest.fixture
def models(tmp_path):
    return fk.make_models(tmp_path)


def test_piper_handles_by_metadata_not_by_name(models, tmp_path):
    b = tts_piper.SPEC.create(FakeCtx("speech"), cfg_for(tts_piper.SPEC))
    assert b.handles(models / "vits-piper-en_US-amy-medium")
    odd = tmp_path / "totally-unrelated-name"
    fk.make_piper(tmp_path, "totally-unrelated-name")
    assert b.handles(odd)                                          # a Piper json is enough
    assert not b.handles(models / "kokoro-en-v0_19")
    assert not b.handles(tmp_path / "missing")
    k = tts_kokoro.SPEC.create(FakeCtx("speech"), cfg_for(tts_kokoro.SPEC))
    assert k.handles(models / "kokoro-en-v0_19") and not k.handles(models / "vits-piper-en_US-amy-medium")
    # a Kokoro model in a directory with no "kokoro" in its name is still recognised
    fk.make_kokoro(tmp_path, "mystery-voices")
    assert k.handles(tmp_path / "mystery-voices") and not b.handles(tmp_path / "mystery-voices")


def test_piper_load_builds_the_vits_config_and_generates(models, sherpa):
    m = tts_piper.load_model(models / "vits-piper-en_US-amy-medium", 3, {"length_scale": 1.4})
    cfg = sherpa.configs[-1]["vits"].kw
    assert cfg["model"].endswith("en_US-amy-medium.onnx") and cfg["tokens"].endswith("tokens.txt")
    assert cfg["data_dir"].endswith("espeak-ng-data") and cfg["length_scale"] == 1.4
    assert sherpa.configs[-1]["num_threads"] == 3 and sherpa.configs[-1]["provider"] == "cpu"
    assert m.model_dir == "vits-piper-en_US-amy-medium" and m.rate == 22050 and m.num_speakers == 1
    samples, rate = m.generate("hi", 0, 1.25)
    assert samples.dtype == np.float32 and rate == 22050 and sherpa.generated[-1] == ("hi", 0, 1.25)


def test_piper_backend_merges_its_own_length_scale_and_tracks_models_weakly(models, sherpa):
    ctx = FakeCtx("speech")
    p = tts_piper.SPEC.create(ctx, cfg_for(tts_piper.SPEC, length_scale=1.7))
    p.start()
    assert ctx.all("tts-backend") == [p]
    m = p.load(models / "vits-piper-en_US-amy-medium", 2, {})
    assert sherpa.configs[-1]["vits"].kw["length_scale"] == 1.7
    p.load(models / "vits-piper-en_US-amy-medium", 2, {"length_scale": 0.9})
    assert sherpa.configs[-1]["vits"].kw["length_scale"] == 0.9
    assert m.num_speakers == 1


def test_piper_multi_speaker_count_comes_from_sherpa_or_the_json(models, sherpa):
    sherpa.num_speakers = 904
    m = tts_piper.load_model(models / "vits-piper-en_US-libritts_r-medium", 2, {})
    assert m.num_speakers == 904


def test_empty_audio_raises(models, sherpa):
    sherpa.empty = True
    m = tts_piper.load_model(models / "vits-piper-en_US-amy-medium", 2, {})
    with pytest.raises(RuntimeError):
        m.generate("hi", 0, 1.0)


def test_kokoro_load_builds_the_kokoro_config_with_lexicon_and_names(models, sherpa):
    d = models / "kokoro-int8-multi-lang-v1_0"
    (d / "dict").mkdir()
    (d / "lexicon-zh.txt").write_text("x y\n")
    m = tts_kokoro.load_model(d, 2, {})
    cfg = sherpa.configs[-1]["kokoro"].kw
    assert cfg["model"].endswith("model.int8.onnx") and cfg["voices"].endswith("voices.bin")
    assert cfg["lexicon"].count(",") == 1 and "lexicon-us-en.txt" in cfg["lexicon"] and "lexicon-zh.txt" in cfg["lexicon"]
    assert cfg["dict_dir"].endswith("dict") and cfg["data_dir"].endswith("espeak-ng-data")
    assert m.num_speakers == 54 and m.rate == 22050
    assert tts_kokoro.voice_names("kokoro-int8-multi-lang-v1_0")["af_heart"] == 3
    assert tts_kokoro.voice_names("kokoro-en-v0_19")["bm_lewis"] == 10
    assert tts_kokoro.voice_names("kokoro-nonexistent") is None


def test_kokoro_dir_without_dict_passes_an_empty_dict_dir(models, sherpa):
    tts_kokoro.load_model(models / "kokoro-en-v0_19", 2, {})
    assert sherpa.configs[-1]["kokoro"].kw["dict_dir"] == ""


def test_piper_signature_and_a_rebuild_without_an_engine_just_takes_the_config(models, sherpa):
    ctx = FakeCtx("speech")
    p = tts_piper.SPEC.create(ctx, cfg_for(tts_piper.SPEC))
    p.start()
    assert p.signature() == (1.0,)
    p.rebuild(p.cfg.replace(length_scale=1.5))                     # no "tts" service: nothing else to rebuild
    assert p.signature() == (1.5,) and ctx.all("tts-backend") == [p]
