"""mirakit.envmap: argv-explicit detection, layers from args and env, adopt (listener and mini plist), legacy mirror."""
from __future__ import annotations

import argparse
import shutil
import types
from pathlib import Path

import pytest

from mirakit import envmap
from mirakit.plugin import ConfigError, Knob, Paths, PluginBase, PluginSpec

WALL = Path(__file__).resolve().parents[2]


def spec(name, *knobs, machine="listener", default_enabled=True):
    return PluginSpec(name=name, kind=name.split(".")[0], version="1", summary="s", machine=machine, provides=(),
                      create=lambda ctx, cfg: PluginBase(ctx, cfg), knobs=tuple(knobs),
                      default_enabled=default_enabled)


def listener_specs():
    return [
        spec("agc.mic",
             Knob("on", "bool", True, "d", arg="agc", env="MIRA_AGC"),
             Knob("target", "float", 0.05, "d", unit="rms", min=0.005, max=0.3, arg="agc_target",
                  env="MIRA_AGC_TARGET"),
             Knob("release", "float", 0.25, "d", unit="x", min=0.01, max=1.0, env="MIRA_AGC_RELEASE"),
             Knob("floor_rate_low", "float", 0.2, "d", unit="x", min=0, max=1)),
        spec("wake.kws",
             Knob("kws_dir", "path", "${HERE}/../voice/models/kws", "d", arg="kws_dir", env="MIRA_KWS_DIR"),
             Knob("threshold", "float", 0.15, "d", unit="score", min=0.01, max=0.5, arg="kws_threshold",
                  env="MIRA_KWS_THRESHOLD")),
        spec("vad.silero",
             Knob("model", "path", "${HERE}/models/silero_vad.onnx", "d", arg="vad_model"),
             Knob("end_silence", "float", 0.7, "d", unit="s", min=0.2, max=3.0, arg="end_silence"),
             Knob("min_speech", "float", 0.15, "d", unit="s", min=0.05, max=1.0)),
        spec("source.wav", Knob("path", "path", "", "d", arg="wav"), default_enabled=False),
        spec("source.pipewire", Knob("record_bin", "str", "pw-record", "d", min=1, max=200)),
        spec("speech.link", Knob("url", "url", "http://100.77.98.89:8765", "d", arg="speech_url",
                                 env="MIRA_SPEECH_URL"),
             Knob("token_file", "path", "", "d", arg="speech_token_file", env="MIRA_SPEECH_TOKEN_FILE",
                  secret=True)),
    ]


def parser():
    p = argparse.ArgumentParser()
    p.add_argument("--wav")
    p.add_argument("--kws-dir", default="/legacy/kws")
    p.add_argument("--kws-threshold", type=float, default=0.15)
    p.add_argument("--agc", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--agc-target", type=float, default=None)
    p.add_argument("--vad-model", default="/legacy/vad.onnx")
    p.add_argument("--end-silence", type=float, default=0.7)
    p.add_argument("--speech-url", default="http://100.77.98.89:8765")
    p.add_argument("--speech-token-file", default="")
    return p


# ---- explicit_from_argv
def test_explicit_names_only_flags_on_the_command_line():
    ex = envmap.explicit_from_argv(parser(), ["--kws-threshold", "0.05", "--no-agc"])
    assert ex == {"kws_threshold": 0.05, "agc": False}


def test_explicit_empty_and_unknown_flags_tolerated():
    assert envmap.explicit_from_argv(parser(), []) == {}
    assert envmap.explicit_from_argv(parser(), ["--not-a-flag", "x", "--end-silence", "0.9"]) == {"end_silence": 0.9}


def test_explicit_does_not_mutate_the_parser():
    p = parser()
    envmap.explicit_from_argv(p, ["--kws-threshold", "0.05"])
    assert p.parse_args([]).kws_threshold == 0.15


def test_explicit_with_the_real_v1_parser():
    import mira_listen_v1 as v1
    ex = envmap.explicit_from_argv(v1.build_parser(), ["--agc-target", "0.06", "--wait-speech", "5"])
    assert ex == {"agc_target": 0.06, "wait_speech": 5.0}


def test_argv_layer_maps_dest_to_knob_keys():
    ex = envmap.explicit_from_argv(parser(), ["--kws-threshold", "0.05", "--vad-model", "/x.onnx"])
    assert envmap.argv_layer(ex, listener_specs()) == {"wake.kws.threshold": 0.05, "vad.silero.model": "/x.onnx"}


def test_argv_layer_rejects_a_bad_value():
    with pytest.raises(ConfigError):
        envmap.argv_layer({"kws_threshold": 9.0}, listener_specs())


# ---- env layer and v1 coercion
def test_env_layer_reads_only_knobs_with_env():
    out = envmap.env_layer({"MIRA_KWS_THRESHOLD": "0.07", "MIRA_AGC_RELEASE": "0.3", "OTHER": "1"}, listener_specs())
    assert out == {"wake.kws.threshold": 0.07, "agc.mic.release": 0.3}


def test_env_bool_is_false_only_for_zero_as_in_v1():
    sp = listener_specs()
    assert envmap.env_layer({"MIRA_AGC": "0"}, sp)["agc.mic.on"] is False
    assert envmap.env_layer({"MIRA_AGC": "1"}, sp)["agc.mic.on"] is True
    assert envmap.env_layer({"MIRA_AGC": "false"}, sp)["agc.mic.on"] is True     # v1: anything but "0" is on


def test_env_empty_number_is_ignored_but_empty_text_is_kept():
    sp = listener_specs()
    assert envmap.env_layer({"MIRA_AGC_TARGET": ""}, sp) == {}
    assert envmap.env_layer({"MIRA_SPEECH_TOKEN_FILE": ""}, sp) == {"speech.link.token_file": ""}


def test_env_out_of_bounds_is_an_error():
    with pytest.raises(ConfigError):
        envmap.env_layer({"MIRA_KWS_THRESHOLD": "7"}, listener_specs())


# ---- layer_from_args
def test_layer_from_args_arg_then_env_then_nothing():
    """MicAGC(target=None) read MIRA_AGC_TARGET in v1: arg None falls through to env."""
    sp = listener_specs()
    ns = types.SimpleNamespace(agc_target=None, kws_threshold=0.2, end_silence=0.7)
    out = envmap.layer_from_args(ns, sp, env={"MIRA_AGC_TARGET": "0.08", "MIRA_KWS_THRESHOLD": "0.33",
                                              "MIRA_AGC_RELEASE": "0.4"})
    assert out["agc.mic.target"] == 0.08            # arg None, env used
    assert out["wake.kws.threshold"] == 0.2         # arg present wins over env
    assert out["agc.mic.release"] == 0.4            # env only
    assert out["vad.silero.end_silence"] == 0.7
    assert "agc.mic.floor_rate_low" not in out      # neither: schema default applies later
    assert "vad.silero.min_speech" not in out


def test_layer_from_args_absent_attribute_uses_env_and_default_otherwise():
    out = envmap.layer_from_args(types.SimpleNamespace(), listener_specs(), env={"MIRA_AGC_TARGET": "0.09"})
    assert out == {"agc.mic.target": 0.09}
    assert envmap.layer_from_args(types.SimpleNamespace(), listener_specs(), env={}) == {}


def test_layer_from_args_accepts_a_mapping_and_uses_process_env_by_default(monkeypatch):
    monkeypatch.setenv("MIRA_AGC_TARGET", "0.07")
    assert envmap.layer_from_args({}, listener_specs())["agc.mic.target"] == 0.07


# ---- adopt_listener
def test_adopt_listener_writes_every_key_and_ignores_env_afterwards():
    sp = listener_specs()
    doc = envmap.adopt_listener(["--kws-threshold", "0.05"], {"MIRA_AGC_TARGET": "0.06", "MIRA_KWS_DIR": "/home/mira/kws"},
                                sp, parser())
    assert doc["format"] == 1 and doc["machine"] == "listener" and doc["core"]["legacy_env"] == "ignore"
    assert doc["core"]["engine"] == "legacy"
    cfg = doc["plugins"]
    assert cfg["wake.kws"]["config"]["threshold"] == 0.05            # flag
    assert cfg["wake.kws"]["config"]["kws_dir"] == "/home/mira/kws"  # env
    assert cfg["agc.mic"]["config"]["target"] == 0.06                # env on an arg knob whose arg is absent
    assert cfg["agc.mic"]["config"]["release"] == 0.25               # default
    assert set(cfg["agc.mic"]["config"]) == {"on", "target", "release", "floor_rate_low"}
    assert cfg["source.wav"]["enabled"] is False and cfg["source.pipewire"]["enabled"] is True


def test_adopt_listener_does_not_freeze_a_test_wav():
    doc = envmap.adopt_listener(["--wav", "/tmp/x.wav"], {}, listener_specs(), parser())
    assert doc["plugins"]["source.wav"]["config"]["path"] == ""


# ---- adopt_speech
def speech_specs():
    return [
        spec("server.http",
             Knob("bind", "list[str]", ["127.0.0.1", "100.77.98.89"], "d", min=1, max=4, env="MIRA_SPEECH_BIND"),
             Knob("port", "int", 8765, "d", min=1024, max=65535, env="MIRA_SPEECH_PORT"),
             Knob("token_file", "path", "~/.config/mira/speech.token", "d", env="MIRA_SPEECH_TOKEN_FILE"),
             machine="speech"),
        spec("stt.engine",
             Knob("kind", "str", "sherpa-moonshine:sherpa-onnx-moonshine-base-en-int8", "d", min=1, max=200,
                  env="MIRA_STT"),
             Knob("threads", "int", 4, "d", min=1, max=8, env="MIRA_THREADS"), machine="speech"),
        spec("tts.engine",
             Knob("voice", "voice", "vits-piper-en_US-amy-medium#0", "d"),
             Knob("peak", "float", 0.89, "d", min=0.3, max=0.98, env="MIRA_TTS_PEAK"),
             Knob("threads", "int", 4, "d", min=1, max=8, env="MIRA_TTS_THREADS"), machine="speech"),
    ]


def test_adopt_speech_from_a_copy_of_the_live_plist(tmp_path):
    plist = tmp_path / "org.aryavora.mira-speech.plist"
    shutil.copy(WALL / "speech" / "org.aryavora.mira-speech.plist", plist)
    doc = envmap.adopt_speech(plist, speech_specs())
    cfg = {n: e["config"] for n, e in doc["plugins"].items()}
    assert doc["machine"] == "speech" and doc["core"]["legacy_env"] == "ignore"
    assert cfg["stt.engine"]["kind"] == "faster-whisper:tiny.en"
    assert cfg["stt.engine"]["threads"] == 4
    assert cfg["server.http"]["bind"] == ["127.0.0.1", "100.77.98.89"]
    assert cfg["server.http"]["port"] == 8765
    assert cfg["tts.engine"]["voice"] == "vits-piper-en_US-amy-medium#0"     # no MIRA_TTS_DIR: the v1 default
    assert cfg["tts.engine"]["threads"] == 4                                   # falls back to MIRA_THREADS
    assert cfg["tts.engine"]["peak"] == 0.89
    assert doc["core"]["unit_nice"] == 5 and doc["core"]["unit_throttle_s"] == 15
    assert doc["core"]["unit_keep_alive"] is True


def test_adopt_speech_piper_and_kokoro_voices(tmp_path):
    import plistlib

    def adopt(env):
        p = tmp_path / "p.plist"
        p.write_bytes(plistlib.dumps({"EnvironmentVariables": env}))
        return envmap.adopt_speech(p, speech_specs())["plugins"]["tts.engine"]["config"]["voice"]

    assert adopt({"MIRA_TTS_DIR": "vits-piper-en_GB-jenny_dioco-medium", "MIRA_TTS_VOICE": "jenny"}) \
        == "vits-piper-en_GB-jenny_dioco-medium#0"
    assert adopt({"MIRA_TTS_DIR": "/Users/a/models/kokoro-en-v0_19", "MIRA_TTS_VOICE": "af_bella"}) \
        == "kokoro-en-v0_19#af_bella"
    assert adopt({"MIRA_TTS_DIR": "kokoro-en-v0_19", "MIRA_TTS_VOICE": "nicole"}) == "kokoro-en-v0_19#af_nicole"
    assert adopt({"MIRA_TTS_DIR": "kokoro-unknown-table", "MIRA_TTS_VOICE": "zed"}) == "kokoro-unknown-table#zed"


def test_adopt_speech_explicit_tts_threads_wins(tmp_path):
    import plistlib
    p = tmp_path / "p.plist"
    p.write_bytes(plistlib.dumps({"EnvironmentVariables": {"MIRA_THREADS": "4", "MIRA_TTS_THREADS": "2"}}))
    assert envmap.adopt_speech(p, speech_specs())["plugins"]["tts.engine"]["config"]["threads"] == 2


# ---- legacy mirror
def mirror_specs():
    return listener_specs() + [
        spec("loop.turns", Knob("preroll_chunks", "int", 15, "d", min=5, max=30, unit="chunks(100ms)"),
             Knob("wait_speech", "float", 4.0, "d", unit="s", min=1, max=15, arg="wait_speech")),
        spec("brain.hermes", Knob("key_file", "path", "", "d", arg="hermes_key_file", secret=True),
             Knob("session_timeout_s", "float", 15.0, "d", unit="s", min=3, max=60)),
        spec("core", Knob("engine", "enum", "legacy", "d", choices=("legacy", "plugins"), env="MIRA_ENGINE")),
    ]


def test_legacy_mirror_env_args_and_unrepresentable():
    sp = mirror_specs()
    eff = {
        "agc.mic.on": True, "agc.mic.target": 0.06, "agc.mic.release": 0.25, "agc.mic.floor_rate_low": 0.3,
        "wake.kws.kws_dir": "${HERE}/kws", "wake.kws.threshold": 0.05,
        "vad.silero.model": "/pi/silero.onnx", "vad.silero.end_silence": 0.9, "vad.silero.min_speech": 0.15,
        "source.wav.path": "", "source.pipewire.record_bin": "pw-record",
        "speech.link.url": "http://100.77.98.89:8765", "speech.link.token_file": "/pi/speech.token",
        "loop.turns.preroll_chunks": 20, "loop.turns.wait_speech": 6.0,
        "brain.hermes.key_file": "/pi/hermes_key", "brain.hermes.session_timeout_s": 15.0,
        "core.engine": "plugins",
    }
    paths = Paths(here=Path("/opt/mira/current"), config_dir=Path("/c"), home=Path("/home/mira"))
    env_text, args, lost = envmap.legacy_mirror(eff, sp, paths)
    lines = env_text.splitlines()
    assert lines[0] == "# generated by mirakit from mira.json; edit with mira set"
    assert "MIRA_AGC=1" in lines and "MIRA_AGC_TARGET=0.06" in lines and "MIRA_KWS_THRESHOLD=0.05" in lines
    assert "MIRA_KWS_DIR=/opt/mira/current/kws" in lines
    assert "MIRA_SPEECH_URL=http://100.77.98.89:8765" in lines
    assert "MIRA_SPEECH_TOKEN_FILE=/pi/speech.token" in lines
    assert not any(l.startswith("MIRA_ENGINE") for l in lines)             # core knobs are not mirrored
    pairs = dict(zip(args[::2], args[1::2]))
    assert pairs["--end-silence"] == "0.9" and pairs["--wait-speech"] == "6"      # flag-only knobs
    assert pairs["--kws-dir"] == "/opt/mira/current/kws"                           # path flags are absolute
    assert pairs["--vad-model"] == "/pi/silero.onnx"
    assert pairs["--speech-token-file"] == "/pi/speech.token"
    assert pairs["--hermes-key-file"] == "/pi/hermes_key"
    assert "--wav" not in args                                                     # empty path: no flag
    keys = {l["key"]: l for l in lost}
    assert set(keys) == {"agc.mic.floor_rate_low", "loop.turns.preroll_chunks"}    # changed literals only
    assert keys["loop.turns.preroll_chunks"] == {"key": "loop.turns.preroll_chunks", "value": 20, "default": 15}


def test_legacy_mirror_unchanged_literals_are_not_reported():
    sp = mirror_specs()
    eff = {f"{s.name}.{k.name}": k.default for s in sp for k in s.knobs}
    _env, _args, lost = envmap.legacy_mirror(eff, sp)
    assert lost == []


def test_legacy_mirror_bool_off_and_list_values():
    sp = [spec("agc.mic", Knob("on", "bool", True, "d", arg="agc", env="MIRA_AGC")),
          spec("x.y", Knob("bind", "list[str]", ["a"], "d", env="MIRA_B"))]
    env_text, _args, _lost = envmap.legacy_mirror({"agc.mic.on": False, "x.y.bind": ["a", "b"]}, sp)
    assert "MIRA_AGC=0" in env_text and "MIRA_B=a,b" in env_text


def test_arg_attr_uses_loop_turns_names():
    lt = spec("loop.turns", Knob("followup_wait_s", "float", 1.0, "d", unit="s", min=0.5, max=3))
    other = spec("vad.silero", Knob("min_speech", "float", 0.15, "d", unit="s", min=0.05, max=1))
    assert envmap.arg_attr(lt, lt.knobs[0]) == "followup_wait_s"
    assert envmap.arg_attr(other, other.knobs[0]) is None
    assert envmap.arg_attr(other, Knob("a", "float", 1.0, "d", arg="zed")) == "zed"
