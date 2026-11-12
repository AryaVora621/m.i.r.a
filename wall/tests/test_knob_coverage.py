"""Knob coverage (spec 2.4.6): every legacy flag, MIRA_* variable and marked literal of mira_listen_v1.py maps to
exactly one listener knob with an equal default under an empty environment, and deploy/mira.example.json equals the
schema defaults except the keys the spec lists (2.4.5, plus night_window from the 2026-11-10 addendum)."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WALL))

import listener_plugins as lp  # noqa: E402

V1_PATH = WALL / "mira_listen_v1.py"
V1_SRC = V1_PATH.read_text(encoding="utf-8")
V1_LINES = V1_SRC.split("\n")
KNOBS = {f"{s.name}.{k.name}": (s, k) for s in lp.specs() for k in s.knobs}

# Loop and reporter attributes that are new in the plugin engine: they have a knob `arg` but no v1 flag.
# Plugins that exist only in the plugin engine: v1 reads none of their keys (control.http, and the five quick-answer intents).
NEW_PLUGINS = ("control.http.", "intent.time.", "intent.weather.", "intent.school.", "intent.calendar.", "intent.printers.", "intent.screens.")
NEW_ARGS = {"followup_wait_s", "followup_seed_chunks", "wake_seed_chunks", "preroll_chunks", "offline_text",
            "level_scale"}


@pytest.fixture(scope="module")
def v1_clean():
    """A fresh copy of v1 imported with every MIRA_* variable removed (DEFAULTS is evaluated at import)."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("MIRA_")}
    for k in saved:
        del os.environ[k]
    try:
        spec = importlib.util.spec_from_file_location("mira_listen_v1_clean", V1_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        os.environ.update(saved)


def _expand(value):
    """Normalise a path default so v1's absolute path and the spec's ${HERE} form compare equal."""
    if isinstance(value, str) and ("${HERE}" in value or value.startswith(str(WALL))):
        return os.path.normpath(value.replace("${HERE}", str(WALL)))
    return value


def _same(knob_default, legacy):
    if isinstance(knob_default, (list, tuple)):
        return list(knob_default) == list(legacy)
    if isinstance(knob_default, bool) or isinstance(legacy, bool):
        return bool(knob_default) is bool(legacy)
    if isinstance(knob_default, (int, float)) and isinstance(legacy, (int, float)):
        return float(knob_default) == float(legacy)
    return _expand(knob_default) == _expand(legacy)


# ----------------------------------------------------------------------------------- the AST scan

def _scan_env_reads():
    """{name: default node} for every os.environ.get("MIRA_...") and _envf("MIRA_...") in the file."""
    found = {}
    for node in ast.walk(ast.parse(V1_SRC)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        is_get = (isinstance(f, ast.Attribute) and f.attr == "get" and isinstance(f.value, ast.Attribute)
                  and f.value.attr == "environ")
        is_envf = isinstance(f, ast.Name) and f.id == "_envf"
        first = node.args[0]
        if (is_get or is_envf) and isinstance(first, ast.Constant) and str(first.value).startswith("MIRA_"):
            found[first.value] = node.args[1] if len(node.args) > 1 else None
    return found


def _eval_default(node):
    return eval(compile(ast.Expression(node), "<v1>", "eval"), {"HERE": WALL, "str": str, "Path": Path})


def _scan_add_argument_dests():
    """Every add_argument dest in the file, the f-string loop `--agc-{knob}` resolved through its tuple."""
    dests = set()
    tree = ast.parse(V1_SRC)
    loops = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and isinstance(node.iter, ast.Tuple):
            loops[node.target.id] = [e.value for e in node.iter.elts if isinstance(e, ast.Constant)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            a = node.args[0]
            if isinstance(a, ast.Constant):
                flags = [a.value]
            else:  # JoinedStr over a loop variable
                var = next(v.value.id for v in a.values if isinstance(v, ast.FormattedValue))
                flags = [ast.unparse(a).strip("f'\"").replace("{" + var + "}", item) for item in loops[var]]
            dests.update(f.lstrip("-").replace("-", "_") for f in flags)
    return dests


ENV_READS = _scan_env_reads()


def test_the_scan_sees_what_the_spec_says_v1_reads():
    assert len(ENV_READS) >= 27, sorted(ENV_READS)
    assert {"MIRA_KWS_DIR", "MIRA_AGC", "MIRA_AGC_FLOOR", "MIRA_LEVEL_LAG", "MIRA_VOICE", "MIRA_CHIME"} <= set(ENV_READS)
    assert len(_scan_add_argument_dests()) == 32


# ------------------------------------------------------------------ flags: argparse dests and defaults

def test_every_add_argument_dest_maps_to_exactly_one_knob_with_an_equal_default(v1_clean):
    parser = v1_clean.build_parser()
    ns = parser.parse_args([])
    dests = {a.dest for a in parser._actions if a.dest != "help"}
    assert dests == _scan_add_argument_dests()
    by_arg = {}
    for key, (s, k) in KNOBS.items():
        if k.arg:
            by_arg.setdefault(k.arg, []).append(key)
    for dest in sorted(dests):
        owners = by_arg.get(dest, [])
        assert len(owners) == 1, f"flag --{dest.replace('_', '-')} maps to {owners}"
        s, k = KNOBS[owners[0]]
        default = getattr(ns, dest)
        if default is None:
            if dest == "wav":
                assert k.default == ""  # no file: the mic
            else:
                assert k.env, f"{owners[0]}: the flag default is None, so its value is the env default (needs env=)"
        else:
            assert _same(k.default, default), (owners[0], k.default, default)
    extra = set(by_arg) - dests
    assert extra == NEW_ARGS, extra


# ------------------------------------------------------------------------- env: every MIRA_* read

def test_every_env_read_maps_to_exactly_one_knob_with_an_equal_default(v1_clean):
    by_env = {}
    for key, (s, k) in KNOBS.items():
        if k.env:
            by_env.setdefault(k.env, []).append(key)
    for name, node in sorted(ENV_READS.items()):
        owners = by_env.get(name, [])
        assert len(owners) == 1, f"{name} maps to {owners}"
        s, k = KNOBS[owners[0]]
        raw = _eval_default(node)
        if k.type == "bool":
            legacy = str(raw) != "0"  # MIRA_AGC: os.environ.get("MIRA_AGC", "1") != "0"
        elif k.type == "float":
            legacy = float(raw)
        elif k.type == "int":
            legacy = int(raw)
        else:
            legacy = raw
        assert _same(k.default, legacy), (name, owners[0], k.default, legacy)
    assert set(by_env) == set(ENV_READS), set(by_env) ^ set(ENV_READS)  # no knob invents a variable v1 never read


def test_empty_environment_defaults_of_the_module_equal_the_knobs(v1_clean):
    d = v1_clean.DEFAULTS
    pairs = {"kws_dir": "wake.kws.kws_dir", "vad_model": "vad.silero.model", "speech_url": "speech.link.url",
             "speech_token_file": "speech.link.token_file", "hermes_url": "brain.hermes.url",
             "hermes_key_file": "brain.hermes.key_file", "voice_url": "reporter.agentos.url",
             "voice_token_file": "reporter.agentos.token_file", "music_url": "intent.music.url",
             "music_auth_file": "intent.music.auth_file", "mira_audio": "playback.miraaudio.bin",
             "identity": "persona.file.path", "chime": "playback.miraaudio.chime", "voice": "tts.remote.voice"}
    for name, key in pairs.items():
        assert _same(KNOBS[key][1].default, d[name]), (name, key)
    assert set(pairs) == set(d)
    # MicAGC's own env fallbacks (the class the plugin passes every value to) under an empty environment
    a = v1_clean.MicAGC()
    want = {"target": "agc.mic.target", "max_gain": "agc.mic.max_gain", "rest": "agc.mic.rest",
            "gate": "agc.mic.gate", "release": "agc.mic.release", "decay": "agc.mic.decay",
            "min_rms": "agc.mic.min_rms", "ceiling": "agc.mic.ceiling", "floor": "agc.mic.floor_init"}
    for attr, key in want.items():
        assert _same(KNOBS[key][1].default, getattr(a, attr)), (attr, key)


# ---------------------------------------------------------------------------------- marked literals

# (knob, v1 line, text that must be on that line, expected default). The line numbers are those of the frozen file.
LITERALS = [
    ("source.pipewire.record_bin", 137, '"pw-record"', "pw-record"),
    ("source.pipewire.queue_chunks", 139, "maxsize=600", 600),
    ("source.wav.tail_s", 108, "tail_s=1.0", 1.0),
    ("mute.zero.mute_s", 261, "mute_s=10.0", 10.0), ("mute.zero.mute_share", 261, "mute_share=0.99", 0.99),
    ("mute.zero.live_share", 261, "live_share=0.90", 0.90), ("mute.zero.block_s", 261, "block_s=1.0", 1.0),
    ("mute.zero.repeat_s", 261, "repeat_s=60.0", 60.0),
    ("agc.mic.floor_rate_speech", 225, "rate = 0.0005 if", 0.0005), ("agc.mic.floor_rate_low", 225, "(0.2 if rms", 0.2),
    ("agc.mic.floor_rate_mid", 225, "else 0.05)", 0.05),
    ("vad.silero.min_speech", 653, "min_speech_duration = 0.15", 0.15),
    ("vad.silero.max_speech", 654, "max_speech_duration = 30.0", 30.0), ("vad.silero.threads", 657, "num_threads = 1", 1),
    ("vad.silero.buffer_s", 659, "buffer_size_in_seconds=30", 30.0),
    ("speech.link.stt_timeout_s", 439, "timeout=30)", 30.0), ("speech.link.tts_timeout_s", 450, "timeout=45)", 45.0),
    ("tts.remote.chunk_limit", 603, "limit=280", 280), ("tts.remote.max_chunks", 603, "max_chunks=3", 3),
    ("gate.wake.wake_word", 578, "WAKE_WORD = r", None), ("gate.wake.wake_prefixes", 579, "(?:hey|hi|ok|okay)", None),
    ("gate.wake.hallucinations", 55, "HALLUCINATIONS = {", None),
    ("intent.music.timeout_s", 531, "timeout=8)", 8.0), ("intent.music.volume_step", 553, "(10 if intent", 10),
    ("brain.hermes.session_timeout_s", 473, "timeout=15)", 15.0), ("brain.hermes.stream_timeout_s", 491, "timeout=150)", 150.0),
    ("brain.hermes.title_prefix", 469, 'f"Mira wall ', "Mira wall"),
    ("brain.hermes.fail_reply", 844, '"I cannot reach Hermes right now."', "I cannot reach Hermes right now."),
    ("brain.hermes.empty_reply", 841, '"I have nothing to add."', "I have nothing to add."),
    ("persona.file.fallback", 660, '"You are Mira."', "You are Mira."),
    ("playback.miraaudio.call_timeout_s", 685, "timeout=120", 120.0),
    ("playback.miraaudio.envelope_step_s", 633, "step=0.1", 0.1), ("playback.miraaudio.follow_join_s", 791, "timeout=1)", 1.0),
    ("reporter.agentos.post_timeout_s", 410, "timeout=3.0)", 3.0), ("reporter.agentos.close_wait_s", 419, "timeout=20)", 20.0),
    ("reporter.agentos.min_gap", 322, "MIN_GAP = 0.5", 0.5), ("reporter.agentos.level_gap", 323, "LEVEL_GAP = 0.2", 0.2),
    ("reporter.agentos.level_step", 325, "LEVEL_STEP = 0.03", 0.03),
    ("reporter.agentos.level_scale", 241, "* 8)", 8.0), ("reporter.agentos.level_scale", 641, "* 8))", 8.0),
    ("reporter.agentos.level_scale", 704, "* 8))", 8.0),
    ("loop.turns.offline_text", 796, '"Speech is offline"', "Speech is offline"),
    ("loop.turns.followup_wait_s", 875, "wait_speech_s=1.0", 1.0), ("loop.turns.followup_seed_chunks", 875, "[-5:]", 5),
    ("loop.turns.preroll_chunks", 858, "maxlen=15", 15), ("loop.turns.wake_seed_chunks", 900, "before[-3:]", 3),
]


@pytest.mark.parametrize("key,line,snippet,expected", LITERALS, ids=[f"{k}@L{ln}" for k, ln, _, _ in LITERALS])
def test_marked_literal_is_in_the_v1_line_and_equals_the_knob_default(key, line, snippet, expected):
    assert snippet in V1_LINES[line - 1], f"v1 line {line} no longer holds {snippet!r}: {V1_LINES[line - 1]!r}"
    if expected is not None:
        assert _same(KNOBS[key][1].default, expected), (key, KNOBS[key][1].default, expected)


def test_structured_literals_equal_v1(v1_clean):
    from listener_plugins import gate_wake, intent_music
    assert KNOBS["gate.wake.wake_word"][1].default == v1_clean.WAKE_WORD
    assert set(KNOBS["gate.wake.hallucinations"][1].default) == v1_clean.HALLUCINATIONS
    assert len(KNOBS["gate.wake.hallucinations"][1].default) == len(v1_clean.HALLUCINATIONS)
    assert KNOBS["gate.wake.wake_prefixes"][1].default == ["hey", "hi", "ok", "okay"]
    pats = KNOBS["intent.music.patterns"][1].default
    assert list(pats) == ["status", "volume_up", "volume_down", "pause", "next", "play"]
    for name, pat in pats.items():
        assert f'("{name}", r"{pat}")' in V1_SRC, name
    replies = KNOBS["intent.music.replies"][1].default
    assert len(replies) == 10
    for key, text in replies.items():
        if key == "volume":
            assert 'f"Volume {vol}."' in V1_SRC and text == "Volume {n}."
        else:
            assert f'"{text}"' in V1_SRC, (key, text)


def test_every_knob_default_is_accounted_for():
    """A knob must come from a flag, a variable, a marked literal, or be listed here as new in the plugin engine."""
    from_flag_env = {k for k, (s, kn) in KNOBS.items() if kn.arg and kn.arg not in NEW_ARGS or kn.env}
    from_literal = {key for key, *_ in LITERALS}
    new = {"loop.turns.wait_speech", "loop.turns.max_s", "loop.turns.followup_s"}  # flags (arg): counted above
    new_in_plugin_engine = {
        "loop.turns.events_max", "loop.turns.log_text", "loop.turns.sigterm_idle",
        "agc.mic.on", "tts.remote.speed", "tts.remote.peak", "tts.remote.pin_voice",
        "persona.file.reread", "wake.kws.keywords",
        "intent.music.priority", "intent.music.patterns", "intent.music.replies",
        "playback.miraaudio.night_window", "playback.miraaudio.night_gain",
        # barge-in and cancel words (2026-11-11) and the answer deadline: nothing in v1 reads them
        "loop.turns.barge_in", "loop.turns.barge_ignore_s", "loop.turns.barge_head_chunks", "loop.turns.barge_guard_words",
        "loop.turns.cancel_words", "loop.turns.cancel_after_barge", "loop.turns.cancel_reply",
        "brain.hermes.deadline_s", "brain.hermes.timeout_reply",
        # speaking while she acts (2026-11-11): the first-chunk rule, streamed speech, the sentence guard, captions on the
        # playback clock and the gapless chunk player
        "brain.hermes.stream_speech", "brain.hermes.speak_max_sentences", "tts.remote.first_chunk_chars",
        "loop.turns.caption_sync", "playback.miraaudio.caption_shift_s", "playback.miraaudio.gapless_chunks",
        "playback.miraaudio.chunk_prestart_s",
    }
    unexplained = set(KNOBS) - from_flag_env - from_literal - new_in_plugin_engine
    unexplained = {k for k in unexplained if not k.startswith(NEW_PLUGINS)}
    assert not unexplained, sorted(unexplained)
    # control.http is new in its entirety: nothing in v1 reads any of its keys
    assert all(not kn.arg and not kn.env for k, (s, kn) in KNOBS.items() if k.startswith(NEW_PLUGINS))


# ------------------------------------------------------------------ the example file against the schema

CORE_DEFAULTS = {"engine": "legacy", "legacy_env": "fallback", "unit_memory_max": "200M", "unit_cpu_quota": "40%",
                 "unit_nice": 5, "unit_restart": "always", "unit_restart_sec": 3.0, "crash_window_s": 600,
                 "crash_guard_after": 3, "crash_legacy_after": 4, "crash_guard_revert_s": 1800}

# Spec 2.4.5 for the Pi, plus playback.miraaudio.night_window (owner decision 2026-11-10: the Pi runs 23:00-06:00), plus the splitter address.
EXPECTED_DIFFS = {
    "core.engine", "core.legacy_env", "wake.kws.kws_dir", "wake.kws.threshold", "vad.silero.model",
    "speech.link.token_file", "intent.music.url", "intent.music.auth_file", "brain.hermes.key_file",
    "persona.file.path", "playback.miraaudio.chime", "reporter.agentos.token_file", "loop.turns.events_max",
    "playback.miraaudio.night_window",
    "speech.link.url",  # the Pi speaks through the local splitter (127.0.0.1:8766), see deploy/README.md
}


def _expand_home(value):
    if isinstance(value, str):
        v = value.replace("${HOME}", "/home/mira")
        if v == "~" or v.startswith("~/"):
            v = "/home/mira" + v[1:]
        return v
    return value


def test_example_file_equals_schema_defaults_except_the_listed_keys():
    example = json.loads((WALL / "deploy" / "mira.example.json").read_text(encoding="utf-8"))
    assert example["format"] == 1 and example["machine"] == "listener"
    diffs = set()
    for k, v in example["core"].items():
        assert k in CORE_DEFAULTS, k
        if not _same(CORE_DEFAULTS[k], v):
            diffs.add(f"core.{k}")
    assert set(example["core"]) == set(CORE_DEFAULTS)
    assert set(example["plugins"]) == {s.name for s in lp.specs()}
    for spec in lp.specs():
        entry = example["plugins"][spec.name]
        assert entry["enabled"] is spec.default_enabled, spec.name
        cfg = entry["config"]
        assert set(cfg) == {k.name for k in spec.knobs}, (spec.name, set(cfg) ^ {k.name for k in spec.knobs})
        for k in spec.knobs:
            default = _expand_home(k.default)
            if isinstance(default, dict):
                same = list(default.items()) == list(cfg[k.name].items())  # order matters: patterns are tried in order
            else:
                same = _same(default, _expand_home(cfg[k.name]))
            if not same:
                diffs.add(f"{spec.name}.{k.name}")
    assert diffs == EXPECTED_DIFFS, (sorted(diffs - EXPECTED_DIFFS), sorted(EXPECTED_DIFFS - diffs))


def test_example_values_pass_every_validate_hook():
    example = json.loads((WALL / "deploy" / "mira.example.json").read_text(encoding="utf-8"))
    for spec in lp.specs():
        if spec.validate:
            spec.validate(example["plugins"][spec.name]["config"])


def test_example_night_and_quiet_values_follow_the_addendum():
    cfg = json.loads((WALL / "deploy" / "mira.example.json").read_text(encoding="utf-8"))["plugins"]
    assert cfg["playback.miraaudio"]["config"]["night_window"] == "23:00-06:00"
    assert cfg["playback.miraaudio"]["config"]["night_gain"] == 0.5
    assert cfg["control.http"]["config"]["preview_quiet"] == ""
    assert KNOBS["control.http.preview_quiet"][1].default == ""


def test_example_has_no_secrets_and_no_dash_characters():
    text = (WALL / "deploy" / "mira.example.json").read_text(encoding="utf-8")
    assert chr(0x2013) not in text and chr(0x2014) not in text
    for key in ("key", "token", "secret", "password"):
        for line in text.split("\n"):
            if key in line.lower() and "_file" not in line.lower() and "$comment" not in line:
                assert "Bearer" not in line, line
