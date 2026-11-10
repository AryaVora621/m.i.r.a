"""Unit tests for the STT eval harness and the faster-whisper prompt plumbing in speech/server.py.

No models, no network, no audio hardware: faster_whisper is replaced by a recording fake.
"""
import importlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
for _d in (HERE.parent / "speech", HERE.parent, HERE):
    sys.path.insert(0, str(_d))

import stt_eval as ev  # noqa: E402


@pytest.fixture
def fake_fw(monkeypatch):
    calls = {}

    class Seg:
        def __init__(self, text):
            self.text = text

    class FakeModel:
        def __init__(self, name, **kw):
            calls["init"] = (name, kw)

        def transcribe(self, samples, **kw):
            calls["transcribe"] = kw
            return [Seg(" Mira, open Onshape ")], None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return calls


def load_server(monkeypatch, **env):
    for k in ("MIRA_STT_PROMPT", "MIRA_STT_HOTWORDS", "MIRA_STT_COMPUTE", "MIRA_STT_BEAM"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def test_wer_folds_spellings():
    assert ev.wer_counts("Mira, what's the SV08 doing", "Mira whats the S V zero eight doing") == (0, 5)
    assert ev.wer_counts("Mira, bring up my 3D printers", "Mira bring up my three D printers")[0] == 0
    assert ev.wer_counts("set a timer for ten minutes", "set a timer for 10 minutes")[0] == 0
    err, n = ev.wer_counts("Mira, open Onshape", "Mira, open the pen")
    assert (err, n) == (2, 3)


def test_intent_words_counted_from_reference_only():
    assert ev.intent_hits("Mira, show the vault", "mira show the vault") == (1, 1)
    assert ev.intent_hits("Mira, bring up my 3D printers", "Mira bring up my pen") == (0, 1)
    # "print" must not be credited by "printers" (whole words only)
    assert ev.intent_hits("Mira, how's the print going", "how is the printers going") == (0, 1)


def test_wake_check_uses_the_listener_rule():
    assert ev.wake_ok("Mira, what time is it")
    assert not ev.wake_ok("Mirror, play some music")
    assert not ev.wake_ok("")


def test_degrade_is_deterministic_and_has_requested_snr():
    rng = np.random.default_rng(3)
    speech = (rng.standard_normal(16000) * 0.1).astype(np.float32)
    noise = (rng.standard_normal(32000) * 0.003).astype(np.float32)
    a = ev.degrade(speech, noise, 6.0, seed=1)
    b = ev.degrade(speech, noise, 6.0, seed=1)
    assert np.array_equal(a, b)
    assert len(a) == len(speech) + 16000
    quiet = ev.degrade(np.zeros(16000, np.float32) + 1e-9, noise, 6.0, seed=1)
    assert float(np.sqrt(np.mean(quiet ** 2))) < 0.02


def test_phrases_fixture_covers_the_owner_vocabulary():
    shown = " ".join(s for s, _ in ev.load_phrases()).lower()
    for w in ("3d printers", "onshape", "slicer", "vault", "timer", "calendar", "sv08", "guest mode"):
        assert w in shown


def test_stt_forwards_prompt_hotwords_compute_and_beam(monkeypatch, fake_fw):
    server = load_server(monkeypatch)
    stt = server.Stt("faster-whisper:base.en", prompt="Vocabulary: Onshape.", hotwords="Onshape",
                     compute_type="float32", beam_size=3)
    assert stt.transcribe(np.zeros(16000, np.float32)) == "Mira, open Onshape"
    assert fake_fw["init"][1]["compute_type"] == "float32"
    kw = fake_fw["transcribe"]
    assert kw["initial_prompt"] == "Vocabulary: Onshape."
    assert kw["hotwords"] == "Onshape"
    assert kw["beam_size"] == 3
    assert kw["language"] == "en" and kw["condition_on_previous_text"] is False


def test_empty_prompt_reaches_faster_whisper_as_none(monkeypatch, fake_fw):
    server = load_server(monkeypatch)
    stt = server.Stt("faster-whisper:base.en", prompt="", hotwords="")
    stt.transcribe(np.zeros(16000, np.float32))
    assert fake_fw["transcribe"]["initial_prompt"] is None
    assert fake_fw["transcribe"]["hotwords"] is None


def test_env_sets_the_defaults_and_explicit_arguments_win(monkeypatch, fake_fw):
    server = load_server(monkeypatch, MIRA_STT_PROMPT="From env.", MIRA_STT_HOTWORDS="envword",
                         MIRA_STT_COMPUTE="float32", MIRA_STT_BEAM="2")
    stt = server.Stt("faster-whisper:tiny.en")
    stt.transcribe(np.zeros(16000, np.float32))
    assert fake_fw["init"][1]["compute_type"] == "float32"
    assert fake_fw["transcribe"]["initial_prompt"] == "From env."
    assert fake_fw["transcribe"]["hotwords"] == "envword"
    assert fake_fw["transcribe"]["beam_size"] == 2
    server.Stt("faster-whisper:tiny.en", compute_type="int8")
    assert fake_fw["init"][1]["compute_type"] == "int8"


def test_default_prompt_is_the_vocabulary(monkeypatch, fake_fw):
    server = load_server(monkeypatch)
    server.Stt("faster-whisper:tiny.en").transcribe(np.zeros(16000, np.float32))
    assert fake_fw["transcribe"]["initial_prompt"] == f"Vocabulary: {server.DEFAULT_VOCAB}."
    assert fake_fw["transcribe"]["hotwords"] is None
    server = load_server(monkeypatch, MIRA_STT_PROMPT="")
    server.Stt("faster-whisper:tiny.en").transcribe(np.zeros(16000, np.float32))
    assert fake_fw["transcribe"]["initial_prompt"] is None


def test_transducer_backends_pick_int8_files_and_model_type(monkeypatch, tmp_path):
    server = load_server(monkeypatch)
    monkeypatch.setattr(server, "MODELS", tmp_path)
    d = tmp_path / "m"
    d.mkdir()
    for f in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"):
        (d / f).write_text("")
    seen = {}

    class Rec:
        @staticmethod
        def from_transducer(**kw):
            seen.update(kw)
            return object()

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineRecognizer = Rec
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    server.Stt("sherpa-nemo-transducer:m")
    assert seen["model_type"] == "nemo_transducer" and seen["encoder"].endswith("encoder.int8.onnx")


def test_default_vocabulary_names_the_wall_words(monkeypatch):
    server = load_server(monkeypatch)
    for w in ("Mira", "Hermes", "3D printers", "Sovol SV08", "Onshape", "OrcaSlicer", "slicer", "vault", "timer",
              "calendar", "YouTube Music"):
        assert w in server.DEFAULT_VOCAB


def test_parse_config_substitutes_vocab_and_defaults_to_off(monkeypatch):
    server = load_server(monkeypatch)
    label, kind, kw = ev.parse_config("a=faster-whisper:base.en,compute=int8,prompt=@prompt", server)
    assert (label, kind) == ("a", "faster-whisper:base.en")
    assert kw["prompt"].startswith("Vocabulary: Mira") and kw["hotwords"] == "" and kw["compute_type"] == "int8"
    _, _, kw = ev.parse_config("m=sherpa-moonshine:x", server)
    assert kw == {"prompt": "", "hotwords": ""}
