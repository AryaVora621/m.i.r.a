"""Contract and lint-style checks over the twelve speech specs (spec 2.1 rules, 2.4.2 tables, 2.11)."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

import sp_fakes as fk
import speech_plugins
from mirakit.plugin import ConfigError
from mirakit.services import MULTI_SERVICES, REQUIRED_SPEECH
from mirakit.testkit import FakeCtx, run_plugin_contract

SPECS = speech_plugins.specs()
BY_NAME = {s.name: s for s in SPECS}
REPO = fk.WALL.parent
START = {"server.http": False, "stt.engine": False, "tts.engine": False}    # these would bind 8765 or load real models


def ctx_with_http():
    from speech_plugins import server_http
    ctx = FakeCtx("speech")
    ctx.cfgs["server.http"] = fk.cfg_for(server_http.SPEC)
    return ctx


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_generic_plugin_contract(spec):
    run_plugin_contract(spec, make_ctx=ctx_with_http, start=START.get(spec.name, True))


def test_twelve_unique_speech_specs():
    assert len(SPECS) == 12 and len(BY_NAME) == 12
    assert all(s.machine == "speech" for s in SPECS)
    assert sorted(BY_NAME) == sorted(["server.http", "control.http", "stt.engine", "stt.faster_whisper", "stt.sherpa_whisper",
                                      "stt.sherpa_moonshine", "stt.sherpa_moonshine_v2", "stt.sherpa_transducer",
                                      "tts.engine", "tts.piper", "tts.kokoro", "voices.catalog"])


def test_required_services_have_a_default_enabled_provider_and_exclusive_ones_exactly_one():
    providers: dict[str, list[str]] = {}
    for s in SPECS:
        if s.default_enabled:
            for svc in s.provides:
                providers.setdefault(svc, []).append(s.name)
    for svc in REQUIRED_SPEECH:
        assert len(providers.get(svc, [])) == 1, svc
    for svc, names in providers.items():
        assert svc in MULTI_SERVICES or len(names) == 1
    assert len(providers["stt-backend"]) == 5 and len(providers["tts-backend"]) == 2


def test_needs_can_all_be_satisfied_in_some_start_order():
    provided = {"core"}
    pending = [s for s in SPECS if s.default_enabled]
    while pending:
        ready = [s for s in pending if all(n in provided for n in s.needs)]
        assert ready, f"cycle or unsatisfied needs: {[(s.name, s.needs) for s in pending]}"
        for s in ready:
            provided.update(s.provides)
            pending.remove(s)


def test_the_required_plugins_are_required_and_only_control_is_optional():
    for s in SPECS:
        assert s.required is (s.name != "control.http"), s.name
    assert not BY_NAME["control.http"].default_enabled and BY_NAME["control.http"].toggle == "live"


def test_every_knob_follows_the_rules_of_2_1():
    for s in SPECS:
        for k in s.knobs:
            tag = f"{s.name}.{k.name}"
            assert 0 < len(k.description) <= 160, tag
            assert k.applies in ("live", "rebuild", "restart"), tag
            if k.type in ("int", "float"):
                assert k.min is not None and k.max is not None and k.unit, tag
            if k.agent == "bounded" and k.type in ("int", "float"):
                assert k.agent_min is not None and k.agent_step, tag
            assert k.topic in ("", "voice", "hearing", "timing", "wake", "brain", "wall", "system"), tag


def test_only_the_three_voice_knobs_are_agent_settable_on_this_machine():
    bounded = sorted(f"{s.name}.{k.name}" for s in SPECS for k in s.knobs if k.agent == "bounded")
    assert bounded == ["tts.engine.peak", "tts.engine.speed", "tts.engine.voice"]
    k = {k.name: k for k in BY_NAME["tts.engine"].knobs}
    assert (k["speed"].agent_min, k["speed"].agent_max, k["speed"].agent_step) == (0.8, 1.3, 0.1)
    assert (k["peak"].agent_min, k["peak"].agent_max, k["peak"].agent_step) == (0.6, 0.95, 0.05)
    assert (k["speed"].min, k["speed"].max, k["peak"].min, k["peak"].max) == (0.5, 1.6, 0.3, 0.98)
    assert k["voice"].type == "voice" and k["voice"].applies == "rebuild" and k["speed"].applies == "live"
    assert k["voice"].topic == k["speed"].topic == k["peak"].topic == "voice"
    assert "slow down" in k["speed"].phrases and "speak up" in k["peak"].phrases


def test_agent_ranges_are_inside_the_hard_bounds():
    for s in SPECS:
        for k in s.knobs:
            if k.agent_min is not None:
                assert k.min <= k.agent_min <= k.agent_max <= k.max


def load_v1(monkeypatch, tmp_path):
    for k in [k for k in __import__("os").environ if k.startswith("MIRA_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location("sp_contract_v1", fk.WALL / "speech" / "server_v1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_knob_defaults_equal_the_v1_code_defaults(monkeypatch, tmp_path):
    v1 = load_v1(monkeypatch, tmp_path)
    d = {f"{s.name}.{k.name}": k.default for s in SPECS for k in s.knobs}
    home_rel = lambda p: "~/" + str(p.relative_to(tmp_path / "home"))
    assert d["server.http.bind"] == v1.BIND and d["server.http.port"] == v1.PORT
    assert d["server.http.token_file"] == home_rel(v1.TOKEN_FILE) and d["server.http.base_dir"] == home_rel(v1.BASE)
    assert d["server.http.max_tts_chars"] == v1.MAX_TTS_CHARS
    assert d["server.http.max_stt_s"] * 16000 * 2 + 4096 == v1.MAX_STT_BYTES
    assert d["stt.engine.kind"] == v1.STT_KIND and d["stt.engine.threads"] == v1.THREADS
    assert d["stt.faster_whisper.prompt"] == v1.STT_PROMPT and d["stt.faster_whisper.hotwords"] == v1.STT_HOTWORDS
    assert d["stt.faster_whisper.compute"] == v1.STT_COMPUTE and d["stt.faster_whisper.beam"] == v1.STT_BEAM
    assert d["tts.engine.voice"] == f"{v1.TTS_DIR}#0" and d["tts.engine.peak"] == v1.TARGET_PEAK
    assert d["tts.engine.threads"] == v1.TTS_THREADS and d["tts.engine.speed"] == 1.0
    assert d["tts.piper.length_scale"] == 1.0
    assert (d["stt.sherpa_moonshine.retry_min_s"], d["stt.sherpa_moonshine.retry_rms"],
            d["stt.sherpa_moonshine.retry_offset_s"]) == (1.0, 0.01, 0.2)


def test_every_knob_carries_its_legacy_env_name_where_v1_had_one():
    env = {f"{s.name}.{k.name}": k.env for s in SPECS for k in s.knobs if k.env}
    assert env == {"server.http.bind": "MIRA_SPEECH_BIND", "server.http.port": "MIRA_SPEECH_PORT",
                   "server.http.token_file": "MIRA_SPEECH_TOKEN_FILE", "server.http.base_dir": "MIRA_SPEECH_DIR",
                   "stt.engine.kind": "MIRA_STT", "stt.engine.threads": "MIRA_THREADS",
                   "stt.faster_whisper.compute": "MIRA_STT_COMPUTE", "stt.faster_whisper.beam": "MIRA_STT_BEAM",
                   "stt.faster_whisper.prompt": "MIRA_STT_PROMPT", "stt.faster_whisper.hotwords": "MIRA_STT_HOTWORDS",
                   "tts.engine.voice": "MIRA_TTS_DIR", "tts.engine.peak": "MIRA_TTS_PEAK",
                   "tts.engine.threads": "MIRA_TTS_THREADS", "tts.piper.length_scale": "MIRA_TTS_LENGTH_SCALE"}


def test_spec_test_files_exist():
    for s in SPECS:
        assert s.tests, s.name
        for t in s.tests:
            assert (REPO / t).exists(), (s.name, t)


def test_reads_name_real_knobs():
    for s in SPECS:
        for ref in s.reads:
            plugin, _, key = ref.rpartition(".")
            assert plugin in BY_NAME and key in {k.name for k in BY_NAME[plugin].knobs}, (s.name, ref)


def test_plugin_sources_obey_the_lint_rules():
    for p in sorted((fk.WALL / "speech_plugins").glob("*.py")):
        text = p.read_text(encoding="utf-8")
        assert "os.environ" not in text and "getenv" not in text, p.name
        assert not re.search(r"\bopen\(\s*[\"']", text), p.name
        assert chr(0x2013) not in text and chr(0x2014) not in text, p.name
    for p in sorted((fk.WALL / "speech_plugins" / "voices").glob("*.json")):
        text = p.read_text(encoding="utf-8")
        assert chr(0x2013) not in text and chr(0x2014) not in text


def test_the_example_file_is_valid_against_the_specs():
    data = json.loads((fk.WALL / "deploy" / "speech.example.json").read_text())
    assert data["format"] == 1 and data["machine"] == "speech"
    assert data["core"]["engine"] == "plugins" and data["core"]["legacy_env"] == "ignore"
    assert set(data["plugins"]) == set(BY_NAME)
    for name, entry in data["plugins"].items():
        spec = BY_NAME[name]
        knobs = {k.name: k for k in spec.knobs}
        assert entry["enabled"] is True
        for key, value in entry["config"].items():
            assert key in knobs, (name, key)
            k = knobs[key]
            if k.type in ("int", "float"):
                assert k.min <= value <= k.max, (name, key)
            if k.type == "enum":
                assert value in k.choices
        if spec.validate:
            full = {k.name: k.default for k in spec.knobs}
            full.update(entry["config"])
            spec.validate(full)
    cfg = data["plugins"]
    assert cfg["voices.catalog"]["config"]["agent_max_rtf"] == 0.5
    assert cfg["tts.engine"]["config"]["voice"] == "vits-piper-en_US-amy-medium#0"
    assert cfg["stt.engine"]["config"]["kind"] == "faster-whisper:tiny.en"
    assert cfg["server.http"]["config"]["bind"] == ["127.0.0.1", "100.77.98.89"]


def test_deployment_example_differs_from_schema_defaults_only_where_documented():
    data = json.loads((fk.WALL / "deploy" / "speech.example.json").read_text())["plugins"]
    allowed = {"server.http": {"token_file", "base_dir"}, "control.http": {"owner_token_file", "pi_token_file"},
               "stt.engine": {"kind"}, "voices.catalog": {"rtf_file"}}
    for name, entry in data.items():
        defaults = {k.name: k.default for k in BY_NAME[name].knobs}
        for key, value in entry["config"].items():
            if defaults[key] != value:
                assert key in allowed.get(name, set()), (name, key, value, defaults[key])


def test_validate_hooks_reject_bad_cross_field_values():
    with pytest.raises(ConfigError):
        BY_NAME["tts.engine"].validate({"voice": " "})
    BY_NAME["tts.engine"].validate({"voice": "amy"})
