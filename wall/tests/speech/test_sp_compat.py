"""speech/server.py: the compat names the bench scripts and stt_eval use (F9), and the launcher (2.9.1, 2.5.6)."""
import importlib.util
import inspect
import json
import os
import signal
import sys
import types
from pathlib import Path

import numpy as np
import pytest

import sp_fakes as fk
from sp_fakes import real_config_dir_untouched  # noqa: F401  (autouse guard)

SERVER = fk.WALL / "speech" / "server.py"
SERVER_V1 = fk.WALL / "speech" / "server_v1.py"
NAMES = ("BASE", "MODELS", "TOKEN_FILE", "BIND", "PORT", "STT_KIND", "TTS_DIR", "TTS_VOICE", "THREADS", "TTS_THREADS",
         "TARGET_PEAK", "DEFAULT_VOCAB", "STT_PROMPT", "STT_HOTWORDS", "STT_COMPUTE", "STT_BEAM", "MAX_STT_BYTES",
         "MAX_TTS_CHARS", "STT_RATE")
ENV = ("MIRA_SPEECH_DIR", "MIRA_SPEECH_TOKEN_FILE", "MIRA_SPEECH_BIND", "MIRA_SPEECH_PORT", "MIRA_STT", "MIRA_TTS_DIR",
       "MIRA_TTS_VOICE", "MIRA_THREADS", "MIRA_TTS_THREADS", "MIRA_TTS_PEAK", "MIRA_STT_PROMPT", "MIRA_STT_HOTWORDS",
       "MIRA_STT_COMPUTE", "MIRA_STT_BEAM", "MIRA_TTS_LENGTH_SCALE", "MIRA_SPEECH_CONFIG")


def load(monkeypatch, path=SERVER, name="sp_server_under_test", home=None, **env):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    if home is not None:
        monkeypatch.setenv("HOME", str(home))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(monkeypatch, tmp_path):
    # mirakit.core is another package's module; the launcher only needs it to import, so a stub stands in for it.
    monkeypatch.setitem(sys.modules, "mirakit.core", types.ModuleType("mirakit.core"))
    return load(monkeypatch, home=tmp_path / "home")


@pytest.fixture
def v1(monkeypatch, tmp_path):
    return load(monkeypatch, SERVER_V1, "sp_server_v1_under_test", home=tmp_path / "home")


def test_every_2_9_1_name_exists_and_equals_the_first_versions_value(monkeypatch, tmp_path):
    env = {"MIRA_SPEECH_DIR": str(tmp_path / "b"), "MIRA_SPEECH_BIND": "127.0.0.1, 100.1.2.3", "MIRA_SPEECH_PORT": "9001",
           "MIRA_STT": "faster-whisper:small.en", "MIRA_TTS_DIR": "kokoro-en-v0_19", "MIRA_TTS_VOICE": "af",
           "MIRA_THREADS": "3", "MIRA_TTS_PEAK": "0.7", "MIRA_STT_PROMPT": "", "MIRA_STT_HOTWORDS": "x",
           "MIRA_STT_COMPUTE": "float32", "MIRA_STT_BEAM": "2"}
    for case in ({}, env):
        new = load(monkeypatch, name="sp_new", home=tmp_path / "h", **case)
        old = load(monkeypatch, SERVER_V1, "sp_old", home=tmp_path / "h", **case)
        for n in NAMES:
            assert getattr(new, n) == getattr(old, n), n
        assert new.TTS_THREADS == old.TTS_THREADS


def test_signatures_match_the_spec(server):
    def params(fn):
        return [(p.name, p.default) for p in inspect.signature(fn).parameters.values()]
    E = inspect.Parameter.empty
    assert params(server.Stt.__init__) == [("self", E), ("kind", E), ("prompt", None), ("hotwords", None),
                                           ("compute_type", None), ("beam_size", None)]
    assert params(server.Tts.__init__) == [("self", E), ("model_dir", E), ("voice", E)]
    assert params(server.read_wav16k) == [("data", E)]
    assert params(server.write_wav) == [("samples", E), ("rate", E)]
    assert params(server.normalise) == [("samples", E)]
    assert params(server.main) == []
    assert params(server.Tts.speak) == [("self", E), ("text", E), ("voice", E)]
    assert params(server.Stt.transcribe) == [("self", E), ("samples", E)]


def test_helpers_behave_like_v1(server, v1):
    samples = np.linspace(-0.4, 0.3, 4000).astype(np.float32)
    assert server.write_wav(samples, 22050) == v1.write_wav(samples, 22050)
    assert np.array_equal(server.normalise(samples), v1.normalise(samples))
    assert server.normalise(np.zeros(5, np.float32)).tolist() == [0.0] * 5
    wav = v1.write_wav(samples, 16000)
    assert np.array_equal(server.read_wav16k(wav), v1.read_wav16k(wav))
    for bad in (v1.write_wav(samples, 8000), b"junk"):
        with pytest.raises(Exception) as a:
            server.read_wav16k(bad)
        with pytest.raises(Exception) as b:
            v1.read_wav16k(bad)
        assert type(a.value) is type(b.value) and str(a.value) == str(b.value)


@pytest.fixture
def fake_fw(monkeypatch):
    calls = {}

    class M:
        def __init__(self, name, **kw):
            calls["init"] = (name, kw)

        def transcribe(self, samples, **kw):
            calls["transcribe"] = kw
            return [types.SimpleNamespace(text=" hi ")], None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = M
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return calls


def test_stt_reads_the_module_globals_at_call_time(server, monkeypatch, tmp_path, fake_fw):
    monkeypatch.setattr(server, "MODELS", tmp_path / "m")
    monkeypatch.setattr(server, "THREADS", 7)
    monkeypatch.setattr(server, "STT_PROMPT", "late prompt")
    monkeypatch.setattr(server, "STT_COMPUTE", "int16")
    stt = server.Stt("faster-whisper:tiny.en")
    assert stt.kind == "faster-whisper:tiny.en" and stt.name == "tiny.en" and stt.lock is not None
    assert stt.transcribe(np.zeros(16000, np.float32)) == "hi"
    kw = fake_fw["init"][1]
    assert kw["cpu_threads"] == 7 and kw["compute_type"] == "int16" and kw["download_root"] == str(tmp_path / "m" / "fw")
    assert fake_fw["transcribe"]["initial_prompt"] == "late prompt"


def test_stt_unknown_backend_exits_like_v1(server):
    with pytest.raises(SystemExit) as e:
        server.Stt("nonesuch:x")
    assert "unknown STT backend nonesuch" in str(e.value)


def test_tts_piper_matches_v1_behaviour(server, monkeypatch, tmp_path):
    models = fk.make_models(tmp_path / "base")
    sherpa = fk.FakeSherpa()
    monkeypatch.setitem(sys.modules, "sherpa_onnx", sherpa)
    monkeypatch.setattr(server, "MODELS", models)
    monkeypatch.setattr(server, "TTS_THREADS", 5)
    monkeypatch.setenv("MIRA_TTS_LENGTH_SCALE", "1.3")            # read when the Tts is built, as in v1
    tts = server.Tts("vits-piper-en_US-amy-medium", "amy")
    assert (tts.name, tts.rate, tts.default_voice, tts.voices) == ("vits-piper-en_US-amy-medium", 22050, "amy", {})
    assert sherpa.configs[-1]["vits"].kw["length_scale"] == 1.3 and sherpa.configs[-1]["num_threads"] == 5
    samples, rate = tts.speak("hello", "whatever")
    assert rate == 22050 and float(np.max(np.abs(samples))) == pytest.approx(server.TARGET_PEAK, rel=1e-5)
    assert sherpa.generated[-1] == ("hello", 0, 1.0)              # a Piper model ignores the name and uses speed 1.0
    assert tts.lock is not None


def test_tts_kokoro_voice_names_come_from_the_tables_with_the_v1_lists_as_fallback(server, monkeypatch, tmp_path):
    models = fk.make_models(tmp_path / "base")
    fk.make_kokoro(models, "kokoro-multi-lang-v9_9")                # no committed table: the v1 28-name list
    fk.make_kokoro(models, "kokoro-en-v0_19-fp16")                  # no table, v0_19 in the name: the 11-name list
    sherpa = fk.FakeSherpa(rate=24000)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", sherpa)
    monkeypatch.setattr(server, "MODELS", models)
    tts = server.Tts("kokoro-int8-multi-lang-v1_0", "af_heart")
    assert tts.voices["af_heart"] == 3 and len(tts.voices) == 54
    tts.speak("hi", "bm_george")
    assert sherpa.generated[-1][1] == 26
    tts.speak("hi", None)
    assert sherpa.generated[-1][1] == 3                              # default voice
    tts.speak("hi", "unknown name")
    assert sherpa.generated[-1][1] == 3
    assert len(server.Tts("kokoro-multi-lang-v9_9", "af_heart").voices) == 28
    assert len(server.Tts("kokoro-en-v0_19-fp16", "af").voices) == 11


def test_tts_picks_the_backend_from_files_and_falls_back_to_the_directory_name(server, monkeypatch, tmp_path):
    sherpa = fk.FakeSherpa()
    monkeypatch.setitem(sys.modules, "sherpa_onnx", sherpa)
    models = tmp_path / "models"
    fk.make_kokoro(models, "mystery-voices")                         # voices.bin, no "kokoro" in the name
    fk.make_piper(models, "weird-name")
    d = models / "kokoro-bare"
    fk.make_kokoro(models, "kokoro-bare")
    (d / "voices.bin").unlink()                                      # nothing recognises it: the name rule says Kokoro
    monkeypatch.setattr(server, "MODELS", models)
    server.Tts("mystery-voices", "af")
    assert "kokoro" in sherpa.configs[-1]
    server.Tts("weird-name", "x")
    assert "vits" in sherpa.configs[-1]
    server.Tts("kokoro-bare", "af")
    assert "kokoro" in sherpa.configs[-1]


# ---- launcher ----------------------------------------------------------------------------------

def write_cfg(path: Path, engine="plugins", **core):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": 1, "machine": "speech", "revision": 1, "core": {"engine": engine, **core},
                                "plugins": {}}))
    return path


def test_default_config_path_is_under_home(monkeypatch, tmp_path):
    s = load(monkeypatch, home=tmp_path / "h")
    assert s.config_path() == tmp_path / "h" / ".config/mira/speech.json"
    monkeypatch.setenv("MIRA_SPEECH_CONFIG", str(tmp_path / "x.json"))
    assert s.config_path() == tmp_path / "x.json"


def test_no_config_file_means_legacy(server, tmp_path):
    assert server.choose_engine(tmp_path / "home" / ".config/mira/speech.json") == ("legacy", "config file missing or unreadable")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert server.choose_engine(bad)[0] == "legacy"
    bad.write_text("[1]")
    assert server.choose_engine(bad)[0] == "legacy"
    assert not (tmp_path / "home" / ".config").exists()               # deciding wrote nothing


def test_engine_key_decides_and_a_sparse_file_is_legacy(server, tmp_path):
    assert server.choose_engine(write_cfg(tmp_path / "a.json", "legacy"))[0] == "legacy"
    (tmp_path / "b.json").write_text(json.dumps({"format": 1, "machine": "speech"}))
    assert server.choose_engine(tmp_path / "b.json")[0] == "legacy"
    assert server.choose_engine(write_cfg(tmp_path / "c.json", "plugins")) == ("plugins", "core.engine is plugins")


def test_an_unimportable_mirakit_means_legacy(server, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mirakit", None)
    engine, why = server.choose_engine(write_cfg(tmp_path / "c.json"))
    assert engine == "legacy" and "not importable" in why


def test_an_unimportable_speech_plugins_package_means_legacy(server, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "speech_plugins", None)
    assert server.choose_engine(write_cfg(tmp_path / "c.json"))[0] == "legacy"


def test_a_missing_core_or_one_broken_plugin_module_means_legacy(server, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mirakit.core", None)
    assert server.choose_engine(write_cfg(tmp_path / "c.json"))[0] == "legacy"
    monkeypatch.setitem(sys.modules, "mirakit.core", types.ModuleType("mirakit.core"))
    monkeypatch.setitem(sys.modules, "speech_plugins.tts_piper", None)
    engine, why = server.choose_engine(write_cfg(tmp_path / "c.json"))
    assert engine == "legacy" and "not importable" in why


def test_crash_rule_counts_unplanned_starts_inside_the_window(server, tmp_path):
    cfg = write_cfg(tmp_path / "cfg" / "speech.json", crash_window_s=600, crash_legacy_after=4)
    starts = tmp_path / "cfg" / "state" / "starts"
    starts.parent.mkdir()
    now = 100000.0
    starts.write_text("\n".join([f"{now - 50:.0f} unplanned", f"{now - 40:.0f} unplanned", f"{now - 30:.0f} planned",
                                 f"{now - 5000:.0f} unplanned", "garbage line"]) + "\n")
    assert server.choose_engine(cfg, now)[0] == "plugins"            # 2 unplanned in the window, plus this start = 3
    starts.write_text(starts.read_text() + f"{now - 20:.0f} unplanned\n")
    engine, why = server.choose_engine(cfg, now)                     # 3 in the window, plus this start = 4
    assert engine == "legacy" and "crash loop" in why
    assert server.choose_engine(cfg, now + 700)[0] == "plugins"      # the window moved on


def test_record_start_appends_and_consumes_a_fresh_planned_marker(server, tmp_path):
    cfg = tmp_path / "cfg" / "speech.json"
    cfg.parent.mkdir()
    server.record_start(cfg, 1000.0)
    state = cfg.parent / "state"
    (state / "planned").write_text("")
    os.utime(state / "planned", (990.0, 990.0))
    server.record_start(cfg, 1000.0)
    assert not (state / "planned").exists()
    (state / "planned").write_text("")
    os.utime(state / "planned", (500.0, 500.0))                      # stale: more than 60 s old
    server.record_start(cfg, 1000.0)
    assert (state / "starts").read_text().splitlines() == ["1000 unplanned", "1000 planned", "1000 unplanned"]
    assert not (state / "planned").exists()


def test_main_runs_legacy_without_recording_a_start(server, monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(server, "_run_legacy", lambda: ran.append("legacy"))
    monkeypatch.setattr(server, "_run_plugins", lambda path: ran.append("plugins"))
    monkeypatch.setenv("MIRA_SPEECH_CONFIG", str(write_cfg(tmp_path / "cfg" / "speech.json", "legacy")))
    server.main()
    assert ran == ["legacy"] and not (tmp_path / "cfg" / "state").exists()


def test_main_runs_plugins_and_records_the_start(server, monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(server, "_run_legacy", lambda: ran.append("legacy"))
    monkeypatch.setattr(server, "_run_plugins", lambda path: ran.append(path))
    cfg = write_cfg(tmp_path / "cfg" / "speech.json", "plugins")
    monkeypatch.setenv("MIRA_SPEECH_CONFIG", str(cfg))
    server.main()
    assert ran == [cfg] and (tmp_path / "cfg" / "state" / "starts").read_text().endswith("unplanned\n")


def test_main_with_no_config_runs_legacy_and_touches_nothing(server, monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(server, "_run_legacy", lambda: ran.append("legacy"))
    monkeypatch.delenv("MIRA_SPEECH_CONFIG", raising=False)
    server.main()
    assert ran == ["legacy"] and not (tmp_path / "home").exists()


def test_run_legacy_calls_server_v1_main(server, monkeypatch):
    stub = types.ModuleType("server_v1")
    stub.main = lambda: stub.__dict__.setdefault("ran", True)
    monkeypatch.setitem(sys.modules, "server_v1", stub)
    server._run_legacy()
    assert stub.ran is True


def test_run_plugins_builds_a_speech_core_with_the_spec_arguments(server, monkeypatch, tmp_path):
    seen = {}

    class FakeCore:
        restart_requested = True

        @classmethod
        def from_file(cls, path, **kw):
            seen["path"], seen["kw"] = path, kw
            return cls()

        def boot(self):
            seen["booted"] = True

        def shutdown(self):
            seen["shutdown"] = True

    mod = types.ModuleType("mirakit.core")
    mod.Core = FakeCore
    monkeypatch.setitem(sys.modules, "mirakit.core", mod)
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        server._run_plugins(tmp_path / "speech.json")
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    kw = seen["kw"]
    assert kw["machine"] == "speech" and kw["mirror"] is False and kw["changelog"] is True
    assert len(kw["specs"]) == 12 and isinstance(kw["argv"], list)
    assert seen["booted"] and seen["shutdown"]


def test_importing_the_module_needs_only_stdlib_and_numpy(monkeypatch, tmp_path):
    for k in [k for k in sys.modules if k.startswith(("speech_plugins", "mirakit", "sherpa_onnx", "faster_whisper"))]:
        monkeypatch.delitem(sys.modules, k)
    before = set(sys.modules)
    load(monkeypatch, name="sp_import_probe", home=tmp_path / "h")
    new = {m.split(".")[0] for m in set(sys.modules) - before}
    assert not new & {"speech_plugins", "mirakit", "sherpa_onnx", "faster_whisper", "onnxruntime"}
