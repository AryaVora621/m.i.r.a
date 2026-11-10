"""Contract tests for mirakit.plugin, mirakit.services, mirakit.testkit and the JSON Schemas (P1).

A tiny stdlib JSON Schema validator lives here on purpose (type, required, properties, enum, items, $ref,
anyOf, additionalProperties, patternProperties): the project takes no jsonschema dependency.
"""
from __future__ import annotations

import copy
import inspect
import json
import pickle
import re
import subprocess
import sys
import threading
import time
from dataclasses import fields
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

import mirakit  # noqa: E402
from mirakit import plugin as P  # noqa: E402
from mirakit import services as SV  # noqa: E402
from mirakit import testkit as TK  # noqa: E402

CONTRACTS = WALL / "docs" / "contracts"


# ---------------------------------------------------------------- mini JSON Schema validator
_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _is_type(value, name):
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def validate(instance, schema, root, path="$"):
    """Return a list of error strings; empty means valid."""
    errs = []
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return validate(instance, node, root, path)
    if "anyOf" in schema:
        if not any(not validate(instance, s, root, path) for s in schema["anyOf"]):
            errs.append(f"{path}: matches none of anyOf")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(instance, t) for t in types):
            return errs + [f"{path}: {instance!r} is not of type {types}"]
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errs.append(f"{path}: missing required {req!r}")
        props = schema.get("properties", {})
        pats = schema.get("patternProperties", {})
        for key, val in instance.items():
            if key in props:
                errs += validate(val, props[key], root, f"{path}.{key}")
                continue
            hit = [s for p, s in pats.items() if re.search(p, key)]
            if hit:
                for s in hit:
                    errs += validate(val, s, root, f"{path}.{key}")
                continue
            extra = schema.get("additionalProperties", True)
            if extra is False:
                errs.append(f"{path}: unexpected key {key!r}")
            elif isinstance(extra, dict):
                errs += validate(val, extra, root, f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errs += validate(item, schema["items"], root, f"{path}[{i}]")
    return errs


@pytest.fixture(scope="module")
def api_schema():
    return json.loads((CONTRACTS / "control-api.schema.json").read_text())


@pytest.fixture(scope="module")
def cfg_schema():
    return json.loads((CONTRACTS / "config.schema.json").read_text())


def check_def(api_schema, name, instance):
    errs = validate(instance, {"$ref": f"#/$defs/{name}"}, api_schema)
    assert errs == [], errs


# ---------------------------------------------------------------- shared synthetic specs
def _noop_create(ctx, cfg):
    return P.PluginBase(ctx, cfg)


class _Tiny(P.PluginBase):
    def start(self):
        self.obj = {"gain": self.cfg.gain}
        self.ctx.provide("mute", self.obj)
        self.ctx.effect(lambda: setattr(self, "released", True), "tiny")

    def apply(self, changed, cfg):
        super().apply(changed, cfg)
        self.obj["gain"] = cfg.gain


def tiny_spec(**kw):
    knobs = (
        P.Knob("gain", "float", 1.0, "How loud the thing is; raising it makes it louder.", unit="x",
               min=0.5, max=2.0, agent="bounded", agent_min=0.8, agent_max=1.4, agent_step=0.2,
               topic="voice", spoken="loudness", effect="louder", phrases=("too quiet",)),
        P.Knob("on", "bool", True, "Switches the thing on.", applies="restart"),
        P.Knob("mode", "enum", "a", "Which mode runs.", choices=("a", "b")),
        P.Knob("secret_file", "path", "", "Token file.", secret=True),
    )
    base = dict(name="mute.tiny", kind="mute", version="1", summary="tiny", machine="listener",
                provides=("mute",), create=lambda c, g: _Tiny(c, g), knobs=knobs)
    base.update(kw)
    return P.PluginSpec(**base)


# ---------------------------------------------------------------- 2.1 and 2.3 names and signatures
def names(fn, skip_self=True):
    return [p for p in inspect.signature(fn).parameters if not (skip_self and p == "self")]


def test_versions_and_constants():
    assert mirakit.VERSION == "1" and mirakit.CONTRACT_VERSION == 1 == P.CONTRACT_VERSION
    assert P.RESET_STREAM == 1
    assert repr(P.RESET) == "RESET"
    assert copy.deepcopy(P.RESET) is P.RESET and copy.copy(P.RESET) is P.RESET
    assert pickle.loads(pickle.dumps(P.RESET)) is P.RESET
    for alias in ("Applies", "KnobType", "Topic", "Scope"):
        assert hasattr(P, alias)


def test_every_2_1_and_2_3_name_importable():
    for name in ("Knob PluginSpec State ConfigError PolicyError ConflictError PluginConfig PluginContext Paths "
                 "PluginBase Scope RESET RESET_STREAM Edit Change CoreAPI RemoteClient Applies KnobType Topic "
                 "CONTRACT_VERSION").split():
        assert hasattr(P, name), name


def test_knob_and_spec_fields_match_spec():
    assert [f.name for f in fields(P.Knob)] == [
        "name", "type", "default", "description", "unit", "min", "max", "choices", "applies", "agent",
        "agent_min", "agent_max", "agent_step", "secret", "arg", "env", "topic", "spoken", "effect", "phrases",
        "nullable"]
    assert [f.name for f in fields(P.PluginSpec)] == [
        "name", "kind", "version", "summary", "machine", "provides", "create", "knobs", "needs", "wants",
        "reads", "required", "core", "toggle", "default_enabled", "order", "validate", "docs", "tests"]
    assert [f.name for f in fields(P.Paths)] == ["here", "config_dir", "home"]
    assert [f.name for f in fields(P.Edit)] == ["key", "value"]
    assert [f.name for f in fields(P.Change)] == [
        "id", "ts", "machine", "actor", "scope", "session", "reason", "edits", "applies", "status",
        "rev_before", "rev_after", "trial_expires", "undo_of", "error", "announce"]
    with pytest.raises(Exception):
        P.Knob("a", "int", 1, "x").name = "b"          # frozen
    with pytest.raises(Exception):
        P.Edit("k", 1).key = "z"


def test_state_values():
    assert [s.value for s in P.State] == ["pending", "loading", "active", "failed", "disabled", "stopped"]
    assert P.State.ACTIVE == "active"


def test_error_types():
    e = P.ConfigError("bad", "agc.mic.target", "unknown_key")
    assert (e.message, e.field, e.code, str(e)) == ("bad", "agc.mic.target", "unknown_key", "bad")
    assert isinstance(e, ValueError) and P.ConfigError("x").code == "invalid" and P.ConfigError("x").field == ""
    p = P.PolicyError("policy", "no", field="k", allowed={"min": 1}, owner_command="mira set k=1")
    assert (p.code, p.message, p.field, p.allowed, p.owner_command, str(p)) == (
        "policy", "no", "k", {"min": 1}, "mira set k=1", "no")
    assert isinstance(p, PermissionError)
    c = P.ConflictError("stale_revision", "old", current=3, revision=4)
    assert (c.code, c.message, c.current, c.revision, str(c)) == ("stale_revision", "old", 3, 4, "old")
    assert isinstance(c, RuntimeError)
    assert names(P.PolicyError.__init__) == ["code", "message", "field", "allowed", "owner_command"]
    assert names(P.ConflictError.__init__) == ["code", "message", "current", "revision"]
    assert names(P.ConfigError.__init__) == ["message", "field", "code"]
    for fn, kwonly in ((P.PolicyError.__init__, ["field", "allowed", "owner_command"]),
                       (P.ConflictError.__init__, ["current", "revision"])):
        got = [n for n, p in inspect.signature(fn).parameters.items() if p.kind is p.KEYWORD_ONLY]
        assert got == kwonly


def test_plugin_base_and_context_signatures():
    assert names(P.PluginBase.__init__) == ["ctx", "cfg"]
    assert names(P.PluginBase.apply) == ["changed", "cfg"]
    assert names(P.PluginBase.rebuild) == ["cfg"]
    for m in ("start", "stop", "status"):
        assert names(getattr(P.PluginBase, m)) == []
    assert names(P.PluginConfig.__init__) == ["plugin", "values", "revision"]
    assert names(P.PluginConfig.replace) == ["changes"]
    expect = {"provide": ["service", "obj"], "get": ["service", "default"], "all": ["service"],
              "effect": ["release", "label"], "cfg_of": ["plugin"], "now": [], "on": ["event", "fn"],
              "emit": ["event", "payload"]}
    for m, args in expect.items():
        assert names(getattr(P.PluginContext, m)) == args, m
    ann = P.PluginContext.__annotations__
    assert {"machine", "log", "paths"} <= set(ann)


def test_plugin_base_defaults():
    ctx = TK.FakeCtx()
    cfg = P.PluginConfig("x.y", {"a": 1})
    pb = P.PluginBase(ctx, cfg)
    assert (pb.ctx, pb.cfg) == (ctx, cfg) and pb.status() == {}
    pb.apply({"a": 2}, cfg.replace(a=2))
    assert pb.cfg.a == 2
    order = []

    class Spy(P.PluginBase):
        def start(self): order.append(("start", self.cfg.a))
        def stop(self): order.append("stop")
    s = Spy(ctx, cfg)
    s.rebuild(cfg.replace(a=5))
    assert order == ["stop", ("start", 5)]


CORE_SIGS = {
    "status": ([], []), "inventory": ([], []), "describe": (["plugin"], ["scope"]), "schema": ([], []),
    "config": ([], ["effective"]), "tuning_guide": (["topic"], ["scope"]),
    "submit": (["edits"], ["actor", "scope", "reason", "trial_s", "expect_revision", "dry_run", "wait_s", "session"]),
    "undo": (["change_id"], ["actor", "scope", "session", "force"]),
    "confirm": (["change_id"], ["actor", "scope", "session"]),
    "changes": ([], ["limit", "since", "session", "actor"]),
    "session": (["action"], ["actor", "scope", "topic"]),
    "preview": ([], ["actor", "scope", "text", "voice", "speed", "peak", "wait_s"]),
    "set_enabled": (["plugin", "enabled"], ["actor", "scope"]),
    "reload_plugin": (["plugin"], ["actor", "scope"]), "reload_file": ([], ["actor", "scope"]),
    "request_restart": ([], ["actor", "scope"]), "register_remote": (["machine", "client"], []),
    "attach_remote_change": (["change"], []),
}


@pytest.mark.parametrize("cls", [P.CoreAPI, TK.FakeCore])
def test_core_api_signatures(cls):
    for name, (pos, kw) in CORE_SIGS.items():
        params = inspect.signature(getattr(cls, name)).parameters
        got_pos = [n for n, p in params.items() if n != "self" and p.kind is not p.KEYWORD_ONLY]
        got_kw = [n for n, p in params.items() if p.kind is p.KEYWORD_ONLY]
        assert (got_pos, got_kw) == (pos, kw), f"{cls.__name__}.{name}"
    sig = inspect.signature(cls.submit).parameters
    assert (sig["reason"].default, sig["trial_s"].default, sig["dry_run"].default, sig["wait_s"].default) == (
        "", None, False, 0.0)
    assert inspect.signature(cls.preview).parameters["wait_s"].default == 15.0
    assert inspect.signature(cls.changes).parameters["limit"].default == 20
    assert inspect.signature(cls.config).parameters["effective"].default is True
    assert names(P.RemoteClient.request) == ["method", "path", "body"]


# ---------------------------------------------------------------- Knob post-init
@pytest.mark.parametrize("name", ["a.b", "Gain", "1x", "", "x y", "x-y"])
def test_knob_rejects_bad_names(name):
    with pytest.raises(ValueError):
        P.Knob(name, "int", 1, "x")


def test_knob_accepts_good_names():
    for name in ("a", "gain_2", "x9"):
        P.Knob(name, "int", 1, "x")


def test_knob_bounded_numeric_needs_agent_fields():
    base = dict(min=0, max=10)
    for missing in ("agent_min", "agent_max", "agent_step"):
        kw = dict(agent_min=1, agent_max=5, agent_step=1)
        del kw[missing]
        with pytest.raises(ValueError):
            P.Knob("n", "float", 1.0, "x", agent="bounded", **base, **kw)
    with pytest.raises(ValueError):
        P.Knob("n", "int", 1, "x", agent="bounded", **base)
    P.Knob("n", "int", 1, "x", agent="bounded", agent_min=1, agent_max=5, agent_step=1, **base)
    P.Knob("v", "voice", "amy", "x", agent="bounded")      # non-numeric bounded needs no numbers


@pytest.mark.parametrize("kw", [dict(agent_min=-1, agent_max=5), dict(agent_min=1, agent_max=11),
                                dict(agent_min=6, agent_max=5), dict(agent_min=11, agent_max=12)])
def test_knob_agent_bounds_inside_hard_bounds(kw):
    with pytest.raises(ValueError):
        P.Knob("n", "float", 1.0, "x", min=0, max=10, agent="bounded", agent_step=1, **kw)


def test_knob_agent_step_positive():
    with pytest.raises(ValueError):
        P.Knob("n", "float", 1.0, "x", min=0, max=10, agent="bounded", agent_min=1, agent_max=5, agent_step=0)


# ---------------------------------------------------------------- PluginConfig
def test_plugin_config_mapping_and_attrs():
    cfg = P.PluginConfig("agc.mic", {"target": 0.05, "on": True}, revision=7)
    assert cfg.target == cfg["target"] == 0.05 and cfg.on is True
    assert (cfg.plugin, cfg.revision, len(cfg), sorted(cfg)) == ("agc.mic", 7, 2, ["on", "target"])
    assert dict(cfg) == {"target": 0.05, "on": True} and "target" in cfg and cfg.get("zzz", 9) == 9
    with pytest.raises(AttributeError):
        cfg.nope
    with pytest.raises(KeyError):
        cfg["nope"]


def test_plugin_config_immutable_and_replace():
    src = {"a": 1}
    cfg = P.PluginConfig("p.q", src, revision=3)
    src["a"] = 99                                   # the config copied its input
    assert cfg.a == 1
    for attempt in (lambda: setattr(cfg, "a", 2), lambda: setattr(cfg, "plugin", "z"),
                    lambda: cfg.__setitem__("a", 2), lambda: delattr(cfg, "a")):
        with pytest.raises((AttributeError, TypeError)):
            attempt()
    new = cfg.replace(a=2)
    assert new.a == 2 and cfg.a == 1 and new.plugin == "p.q" and new.revision == 3
    with pytest.raises(KeyError):
        cfg.replace(zz=1)


def test_plugin_config_equality_copy_pickle():
    a = P.PluginConfig("p.q", {"a": 1, "b": [1, 2]}, revision=1)
    b = P.PluginConfig("p.q", {"b": [1, 2], "a": 1}, revision=5)
    assert a == b and a == {"a": 1, "b": [1, 2]} and a != a.replace(a=2) and a != 5
    assert copy.deepcopy(a) == a and copy.copy(a).revision == 1
    assert pickle.loads(pickle.dumps(a)) == a
    with pytest.raises(TypeError):
        hash(a)


# ---------------------------------------------------------------- Change wire form and schemas
def sample_change(**over):
    d = {"id": "L-000124", "ts": "2026-11-10T13:42:19-0500", "machine": "listener", "actor": "hermes",
         "scope": "agent", "session": "V-0003", "reason": "owner said she cuts him off",
         "edits": [{"key": "vad.silero.end_silence", "old": 0.7, "new": 0.8}],
         "applies": "live", "status": "applied", "rev_before": 41, "rev_after": 42,
         "trial_expires": "2026-11-10T13:52:19-0500", "undo_of": None, "error": None,
         "announce": "I will wait a little longer for you to finish, 0.8 seconds instead of 0.7, for ten minutes."}
    d.update(over)
    return d


def test_change_roundtrip_and_schema(api_schema):
    wire = sample_change()
    ch = P.Change.from_dict(wire)
    assert ch.to_dict() == wire and list(ch.to_dict()) == list(wire)
    check_def(api_schema, "Change", ch.to_dict())
    ch.edits.append({"key": "x.y", "old": 1, "new": 2})
    assert len(wire["edits"]) == 1                       # to_dict and from_dict copy the edits
    assert P.Change.from_dict({**wire, "extra": 1}).id == "L-000124"
    bad = sample_change(status="nonsense")
    assert validate(bad, {"$ref": "#/$defs/Change"}, api_schema)
    assert validate({k: v for k, v in wire.items() if k != "announce"}, {"$ref": "#/$defs/Change"}, api_schema)


def test_shape_schemas_accept_fakecore_output(api_schema):
    core = TK.FakeCore([tiny_spec(), voice_spec()], machine="listener")
    core.voice_turn = True
    check_def(api_schema, "Status", core.status())
    for row in core.inventory():
        check_def(api_schema, "InventoryRow", row)
    for knob in core.describe("mute.tiny", scope="owner")["knobs"]:
        check_def(api_schema, "KnobRow", knob)
    for knob in core.describe("mute.tiny", scope="agent")["knobs"]:
        check_def(api_schema, "KnobRow", knob)
    check_def(api_schema, "TuningGuide", core.tuning_guide("voice", scope="agent"))
    ch = core.submit([P.Edit("mute.tiny.gain", 1.2)], actor="hermes", scope="agent", reason="r")
    check_def(api_schema, "Change", ch.to_dict())
    check_def(api_schema, "Session", core.session("get", actor="hermes", scope="agent"))
    check_def(api_schema, "Health", {"ok": True, "machine": "listener", "version": "1", "engine": "plugins",
                                      "uptime_s": 1.5, "revision": 2})
    check_def(api_schema, "Error", {"error": "policy", "message": "no", "revision": 1,
                                    "allowed": {"min": 1, "max": 2, "step": 1}, "owner_command": "mira set a=1"})
    check_def(api_schema, "Voices", {"current": {"id": "a#0", "alias": "amy", "label": "Amy"}, "families": [],
        "voices": [{"id": "a#0", "alias": "amy", "aliases": ["amy"], "label": "Amy", "lang": "en_US",
                    "gender": "", "engine": "piper", "usable": True, "loaded": True, "current": True,
                    "featured": True, "rtf": None}]})


def test_shape_schemas_reject_bad(api_schema):
    ref = lambda n: {"$ref": f"#/$defs/{n}"}
    assert validate({"error": "weird", "message": "m", "revision": 1}, ref("Error"), api_schema)
    assert validate({"error": "policy", "revision": 1}, ref("Error"), api_schema)
    assert validate({"ok": True}, ref("Health"), api_schema)
    assert validate({"id": "V-1"}, ref("Session"), api_schema)
    assert validate({"current": {}, "voices": [], "families": []}, ref("Voices"), api_schema)


def test_api_schema_has_required_defs(api_schema):
    assert api_schema["$schema"].endswith("2020-12/schema")
    assert set(api_schema["$defs"]) >= {"Change", "Error", "InventoryRow", "KnobRow", "Status", "Session",
                                         "TuningGuide", "VoiceEntry", "Voices", "Health"}


# ---------------------------------------------------------------- example config files (2.4.3, 2.4.4)
PI_EXAMPLE = r'''{
 "$comment": "Written by mirakit. Change with `mira set`; see wall/AGENTS.md. Bounds live in the plugin specs.",
 "format": 1, "machine": "listener", "revision": 1,
 "core": {"engine": "plugins", "legacy_env": "ignore",
  "unit_memory_max": "200M", "unit_cpu_quota": "40%", "unit_nice": 5, "unit_restart": "always", "unit_restart_sec": 3.0,
  "crash_window_s": 600, "crash_guard_after": 3, "crash_legacy_after": 4, "crash_guard_revert_s": 1800},
 "plugins": {
  "loop.turns": {"enabled": true, "config": {"wait_speech": 4.0, "max_s": 10.0, "followup_s": 8.0, "followup_wait_s": 1.0,
    "followup_seed_chunks": 5, "wake_seed_chunks": 3, "preroll_chunks": 15, "offline_text": "Speech is offline",
    "events_max": 2000, "log_text": true, "sigterm_idle": false}},
  "source.pipewire": {"enabled": true, "config": {"record_bin": "pw-record", "queue_chunks": 600}},
  "source.wav": {"enabled": false, "config": {"path": "", "tail_s": 1.0}},
  "mute.zero": {"enabled": true, "config": {"mute_s": 10.0, "mute_share": 0.99, "live_share": 0.90, "block_s": 1.0, "repeat_s": 60.0}},
  "agc.mic": {"enabled": true, "config": {"on": true, "target": 0.05, "max_gain": 10.0, "rest": 3.0, "gate": 1.8,
    "release": 0.25, "decay": 0.03, "min_rms": 0.001, "ceiling": 0.9, "floor_init": 0.003,
    "floor_rate_speech": 0.0005, "floor_rate_low": 0.2, "floor_rate_mid": 0.05}},
  "wake.kws": {"enabled": true, "config": {"kws_dir": "/home/mira/mira-listen/kws", "keywords": "keywords.txt",
    "threshold": 0.05, "score": 1.0, "threads": 1}},
  "vad.silero": {"enabled": true, "config": {"model": "/home/mira/mira-listen/models/silero_vad.onnx", "threshold": 0.5,
    "end_silence": 0.7, "min_speech": 0.15, "max_speech": 30.0, "threads": 1, "buffer_s": 30.0}},
  "speech.link": {"enabled": true, "config": {"url": "http://100.77.98.89:8765",
    "token_file": "/home/mira/.config/mira/speech.token", "stt_timeout_s": 30.0, "tts_timeout_s": 45.0}},
  "stt.remote": {"enabled": true, "config": {}},
  "tts.remote": {"enabled": true, "config": {"voice": "", "speed": 0.0, "peak": 0.0, "chunk_limit": 280, "max_chunks": 3,
    "pin_voice": true}},
  "gate.wake": {"enabled": true, "config": {"wake_word": "[a-z]{0,2}(?:i|ee|ea|e|y|ie|iu)r+(?:a|ah|ar|uh)h?",
    "wake_prefixes": ["hey", "hi", "ok", "okay"],
    "hallucinations": ["you", "thank you", "thanks", "thanks for watching", "bye", "okay", "uh", "um", "hmm", "the"]}},
  "intent.music": {"enabled": true, "config": {"priority": 100, "url": "http://100.77.98.89:4534",
    "auth_file": "/home/mira/.config/mira/music_auth.json", "timeout_s": 8.0, "volume_step": 10,
    "patterns": {
     "status": "(what'?s|what is|what song is) (playing|on)( right now| now)?|what am i listening to|what song is this",
     "volume_up": "(turn |make it |volume )?(the )?(volume )?(up|louder)( a (little|bit))?|volume up|turn (it|the music|the volume) up|louder",
     "volume_down": "volume down|turn (it|the music|the volume) down|(turn |make it )?(the )?(volume )?(down|quieter|softer)( a (little|bit))?|quieter",
     "pause": "(pause[ds]?|stop|mute)( the)?( music| song| playback)?|pause it",
     "next": "(next|skip(ped)?)( the)?( song| track)?|next song|skip it",
     "play": "(play(s|ed)?|resume|start)( the)?( music| song| playback)?|play it"},
    "replies": {"play": "Playing.", "pause": "Paused.", "next": "Next song.", "volume": "Volume {n}.",
     "echo_down": "The speaker is not connected.", "playing": "Music is playing.", "paused": "Music is paused.",
     "nothing": "Nothing is playing.", "done": "Done.", "unreachable": "I could not reach the music player."}}},
  "brain.hermes": {"enabled": true, "config": {"url": "http://127.0.0.1:8642", "key_file": "/home/mira/.config/mira/hermes_api_key",
    "session_ttl": 180.0, "session_timeout_s": 15.0, "stream_timeout_s": 150.0, "title_prefix": "Mira wall",
    "fail_reply": "I cannot reach Hermes right now.", "empty_reply": "I have nothing to add."}},
  "persona.file": {"enabled": true, "config": {"path": "/home/mira/mira-listen/MIRA_WALL_IDENTITY.md",
    "fallback": "You are Mira.", "reread": "start"}},
  "playback.miraaudio": {"enabled": true, "config": {"bin": "mira-audio", "call_timeout_s": 120.0, "level_lag": 0.15,
    "chime": "/home/mira/mira-listen/assets/offline.wav", "envelope_step_s": 0.1, "follow_join_s": 1.0}},
  "reporter.agentos": {"enabled": true, "config": {"url": "http://127.0.0.1:3456/api/hooks/voice",
    "token_file": "/home/mira/.config/mira/voice_token", "error_hold": 4.0, "min_gap": 0.5, "level_gap": 0.2,
    "level_step": 0.03, "post_timeout_s": 3.0, "close_wait_s": 20.0, "level_scale": 8.0}},
  "control.http": {"enabled": false, "config": {"bind": "127.0.0.1", "port": 8650,
    "owner_token_file": "/home/mira/.config/mira/control.owner.token", "agent_token_file": "/etc/mira/control.agent.token",
    "speech_url": "http://100.77.98.89:8765", "speech_token_file": "/home/mira/.config/mira/speech.control.token",
    "agents_locked": false, "agent_requires_voice_turn": true, "agent_writes_per_10min": 12, "agent_writes_per_knob_10min": 3,
    "agent_undo_window_s": 86400, "trial_default_s": 600, "trial_max_s": 1800, "session_max_s": 900,
    "session_max_changes": 12, "session_followup_s": 15.0, "session_idle_s": 120, "rebuild_quiet_s": 10.0,
    "previews_per_10min": 6, "preview_max_chars": 200, "preview_quiet": "",
    "preview_text": "This is {alias}. This is how I would sound.", "changelog_max_bytes": 5000000,
    "history_keep": 50, "wait_s_default": 5.0}}
 }
}'''
MINI_EXAMPLE = r'''{
 "$comment": "Written by mirakit. Change with `mira set` on the mini; see wall/AGENTS.md.",
 "format": 1, "machine": "speech", "revision": 1,
 "core": {"engine": "plugins", "legacy_env": "ignore", "unit_nice": 5, "unit_throttle_s": 15, "unit_keep_alive": true,
  "crash_window_s": 600, "crash_guard_after": 3, "crash_legacy_after": 4, "crash_guard_revert_s": 1800},
 "plugins": {
  "server.http": {"enabled": true, "config": {"bind": ["127.0.0.1", "100.77.98.89"], "port": 8765,
    "token_file": "/Users/aryavora/.config/mira/speech.token",
    "base_dir": "/Users/aryavora/Library/Application Support/mira-speech", "max_stt_s": 30.0, "max_tts_chars": 600,
    "tts_body_max": 4096}},
  "control.http": {"enabled": true, "config": {"owner_token_file": "/Users/aryavora/.config/mira/control.owner.token",
    "pi_token_file": "/Users/aryavora/.config/mira/control.pi.token", "trial_default_s": 600, "trial_max_s": 1800,
    "agent_writes_per_10min": 12, "busy_defer_s": 10.0, "history_keep": 50, "changelog_max_bytes": 5000000}},
  "stt.engine": {"enabled": true, "config": {"kind": "faster-whisper:tiny.en", "threads": 4, "warm": true}},
  "stt.faster_whisper": {"enabled": true, "config": {"compute": "int8", "beam": 1,
    "prompt": "Vocabulary: Mira, Hermes, 3D printers, Sovol SV08, Onshape, OrcaSlicer, slicer, vault, timer, calendar, YouTube Music.",
    "hotwords": "", "language": "en"}},
  "stt.sherpa_whisper": {"enabled": true, "config": {}},
  "stt.sherpa_moonshine": {"enabled": true, "config": {"retry_min_s": 1.0, "retry_rms": 0.01, "retry_offset_s": 0.2}},
  "stt.sherpa_moonshine_v2": {"enabled": true, "config": {}},
  "stt.sherpa_transducer": {"enabled": true, "config": {}},
  "tts.engine": {"enabled": true, "config": {"voice": "vits-piper-en_US-amy-medium#0", "speed": 1.0, "peak": 0.89,
    "threads": 4, "max_loaded": 2, "warm": true}},
  "tts.piper": {"enabled": true, "config": {"length_scale": 1.0}},
  "tts.kokoro": {"enabled": true, "config": {}},
  "voices.catalog": {"enabled": true, "config": {"rescan_s": 300, "featured": ["amy", "jenny", "lessac", "kristin", "hfc_female"],
    "agent_max_rtf": 0.5, "rtf_file": "/Users/aryavora/.config/mira/voices.rtf.json"}}
 }
}'''


@pytest.mark.parametrize("text,machine", [(PI_EXAMPLE, "listener"), (MINI_EXAMPLE, "speech")])
def test_example_files_validate(cfg_schema, text, machine):
    doc = json.loads(text)
    assert validate(doc, cfg_schema, cfg_schema) == []
    assert doc["machine"] == machine and doc["format"] == 1
    assert "$comment" in doc


def test_config_schema_rejects_bad_files(cfg_schema):
    good = json.loads(PI_EXAMPLE)
    for mutate in (lambda d: d.update(format=2), lambda d: d.update(machine="mini"),
                   lambda d: d.pop("plugins"), lambda d: d.update(revision="1"),
                   lambda d: d["plugins"]["stt.remote"].pop("enabled"),
                   lambda d: d["plugins"]["stt.remote"].update(enabled="yes"),
                   lambda d: d.update(stray=1), lambda d: d.update(core=[])):
        bad = copy.deepcopy(good)
        mutate(bad)
        assert validate(bad, cfg_schema, cfg_schema), mutate
    ok = copy.deepcopy(good)
    ok["$note"] = "ignored"
    ok["plugins"]["$why"] = "ignored"
    ok["plugins"]["stt.remote"]["$why"] = 1
    assert validate(ok, cfg_schema, cfg_schema) == []


def test_example_values_match_spec_table_for_agent_knobs():
    """The ten agent-settable knobs of 2.4.2 exist in the mock and the testkit specs with the same bounds."""
    pi = json.loads(PI_EXAMPLE)["plugins"]
    assert pi["vad.silero"]["config"]["end_silence"] == 0.7
    assert pi["brain.hermes"]["config"]["session_ttl"] == 180.0


# ---------------------------------------------------------------- services
def test_services_module():
    assert SV.MULTI_SERVICES == frozenset({"intent", "stt-backend", "tts-backend"})
    assert SV.REQUIRED_LISTENER == ("source", "mute", "wake", "vad", "speech", "stt", "tts", "gate", "brain",
                                    "persona", "playback", "reporter", "loop")
    assert SV.REQUIRED_SPEECH == ("http", "stt", "tts", "voices")
    for proto in ("Source Mute Agc Spotter VadFactory SpeechLink Stt Tts Gate Intent Persona Brain Playback "
                  "Reporter SttBackend SttModel TtsBackend TtsModel VoiceCatalog SttEngine TtsEngine").split():
        assert hasattr(SV, proto), proto
    assert names(SV.Source.read) == [] and names(SV.SpeechLink.tts) == ["text", "voice", "speed", "peak", "pin"]
    assert names(SV.VoiceCatalog.list) == ["lang", "include_unusable"]
    assert names(SV.TtsEngine.synth) == ["text", "voice", "speed", "peak"]


def test_services_and_plugin_import_without_numpy():
    code = ("import sys; sys.path.insert(0, %r); import mirakit, mirakit.plugin, mirakit.services, mirakit.testkit;"
            "assert 'numpy' not in sys.modules and 'sherpa_onnx' not in sys.modules, 'heavy import'") % str(WALL)
    r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- testkit: cfg_for, FakeCtx, FakeSource, FakeSpotter
def test_cfg_for():
    spec = tiny_spec()
    cfg = TK.cfg_for(spec)
    assert (cfg.plugin, cfg.revision, cfg.gain, cfg.on, cfg.mode) == ("mute.tiny", 0, 1.0, True, "a")
    cfg = TK.cfg_for(spec, 4, gain=1.5)
    assert cfg.gain == 1.5 and cfg.revision == 4
    with pytest.raises(KeyError):
        TK.cfg_for(spec, nope=1)
    assert TK.cfg_for(spec, gain=99).gain == 99          # no validation


class _Obj:
    def __init__(self, name, priority=None):
        self.name = name
        if priority is not None:
            self.priority = priority


def test_fake_ctx_services_effects_events():
    ctx = TK.FakeCtx("speech")
    assert ctx.machine == "speech" and ctx.log.name.startswith("mira") and ctx.paths.home.is_dir()
    assert ctx.paths.here.is_dir() and ctx.paths.config_dir.is_dir()
    with pytest.raises(KeyError):
        ctx.get("brain")
    assert ctx.get("brain", None) is None and ctx.all("brain") == []
    ctx.provide("brain", "B")
    assert ctx.get("brain") == "B" and ctx.all("brain") == ["B"] and ctx.has("brain")
    ctx.provide("agc", None)                             # the service exists, value None
    assert ctx.get("agc") is None and ctx.has("agc")
    a, b, c = _Obj("b", 100), _Obj("a", 100), _Obj("z", 300)
    for o in (a, b, c):
        ctx.provide("intent", o)
    assert ctx.all("intent") == [c, b, a] and ctx.get("intent") is c      # priority desc, then name
    order = []
    ctx.effect(lambda: order.append(1), "first")
    ctx.effect(lambda: order.append(2), "second")
    assert ctx.effect_labels == ["first", "second"]
    seen = []
    ctx.on("turn.heard", seen.append)
    ctx.on("turn.heard", lambda p: 1 / 0)                # a bad observer is swallowed
    ctx.emit("turn.heard", words=3)
    assert ctx.emitted[0]["event"] == "turn.heard" and ctx.emitted[0]["words"] == 3 and seen[0]["words"] == 3
    assert ctx.events("turn.heard") == ctx.emitted
    ctx.release_all()
    assert order == [2, 1]
    assert not ctx.has("brain") and ctx.all("intent") == [] and ctx.effect_labels == []


def test_fake_ctx_clock_and_cfg_of():
    ctx = TK.FakeCtx.with_specs(tiny_spec())
    assert ctx.now() == 0.0
    ctx.time = 12.5
    assert ctx.now() == 12.5 and ctx.advance(2) == 14.5
    assert ctx.cfg_of("mute.tiny").gain == 1.0
    with pytest.raises(KeyError):
        ctx.cfg_of("nope")


def test_fake_ctx_release_reports_failure_but_runs_all():
    ctx = TK.FakeCtx()
    ran = []
    ctx.effect(lambda: ran.append("a"))
    ctx.effect(lambda: 1 / 0)
    ctx.effect(lambda: ran.append("c"))
    with pytest.raises(ZeroDivisionError):
        ctx.release_all()
    assert ran == ["c", "a"]


def test_fake_source():
    import numpy as np
    chunks = [np.zeros(1600, dtype=np.float32), np.ones(800, dtype=np.float32)]
    src = TK.FakeSource(chunks)
    assert src.live is False and src.clock() == 0.0
    assert src.read() is chunks[0] and src.clock() == pytest.approx(0.1)
    assert src.read() is chunks[1] and src.clock() == pytest.approx(0.15)
    assert src.read() is None
    src.flush(); src.flush(); src.close()
    assert (src.flush_calls, src.close_calls) == (2, 1)
    assert TK.FakeSource.silence(3).read().shape == (1600,)


def test_fake_spotter():
    sp = TK.FakeSpotter(hits=[1, 3], text="mira")
    st = sp.create_stream()
    got = []
    for i in range(5):
        st.accept_waveform(16000, None)
        assert sp.is_ready(st)
        sp.decode_stream(st)
        got.append(sp.get_result(st))
        if got[-1]:
            sp.reset_stream(st)
            assert sp.get_result(st) == ""
    assert got == ["", "mira", "", "mira", ""]
    assert sp.streams_created == 1
    sp.create_stream()
    assert sp.streams_created == 2
    assert TK.FakeSpotter({0: "hey mira"}).hits == {0: "hey mira"}


# ---------------------------------------------------------------- FakeCore policy basics
def voice_spec():
    knobs = (
        P.Knob("voice", "voice", "amy", "Her voice.", agent="bounded", topic="voice", spoken="my voice"),
        P.Knob("speed", "float", 1.0, "How fast she talks.", unit="x", min=0.5, max=1.6, agent="bounded",
               agent_min=0.8, agent_max=1.3, agent_step=0.1, topic="voice", spoken="how fast I talk",
               effect="talks faster", phrases=("slow down",)),
        P.Knob("threads", "int", 4, "CPU threads.", unit="threads", min=1, max=8, applies="restart"),
        P.Knob("flag", "bool", False, "A flag."),
        P.Knob("names", "list[str]", ["a"], "Names.", min=0, max=3),
        P.Knob("label", "str", "x", "Label.", min=1, max=5, topic="wake", spoken="the label"),
        P.Knob("mode", "enum", "a", "Mode.", choices=("a", "b")),
    )
    return P.PluginSpec(name="tts.engine", kind="tts", version="1", summary="s", machine="speech",
                        provides=("tts",), create=_noop_create, knobs=knobs)


@pytest.fixture
def core():
    return TK.FakeCore([tiny_spec(), voice_spec()])


def E(key, value):
    return P.Edit(key, value)


def test_fakecore_owner_submit_coercion_and_bounds(core):
    ch = core.submit([E("tts.engine.speed", "1.5"), E("tts.engine.flag", "true"), E("tts.engine.threads", "6"),
                      E("tts.engine.names", '["x","y"]')], actor="arya", scope="owner", reason="r")
    assert ch.id == "L-000001" and ch.status == "pending_restart" and ch.applies == "restart"
    assert core.values["tts.engine.speed"] == 1.5 and core.values["tts.engine.flag"] is True
    assert core.values["tts.engine.threads"] == 6 and core.values["tts.engine.names"] == ["x", "y"]
    assert ch.trial_expires is None and ch.announce and ch.rev_after == ch.rev_before + 1
    assert [e["key"] for e in ch.edits] == ["tts.engine.speed", "tts.engine.flag", "tts.engine.threads",
                                            "tts.engine.names"]
    assert core.status()["pending_restart"] == ["tts.engine.threads"]
    for key, bad, code in [("tts.engine.speed", 2.0, "invalid"), ("tts.engine.speed", "abc", "invalid"),
                           ("tts.engine.flag", "maybe", "invalid"), ("tts.engine.threads", 2.5, "invalid"),
                           ("tts.engine.mode", "z", "invalid"), ("tts.engine.label", "toolong", "invalid"),
                           ("tts.engine.names", ["a"] * 4, "invalid"), ("tts.engine.speeed", 1, "unknown_key"),
                           ("tts.enginee.speed", 1, "unknown_plugin"), ("tts.engine.speed", True, "invalid")]:
        with pytest.raises(P.ConfigError) as ei:
            core.submit([E(key, bad)], actor="arya", scope="owner")
        assert ei.value.code == code, (key, bad)
    assert core.values["tts.engine.speed"] == 1.5          # failed submits changed nothing
    assert core.revision == 2


def test_fakecore_unknown_key_suggests_closest(core):
    with pytest.raises(P.ConfigError) as ei:
        core.submit([E("tts.engine.speeed", 1)], actor="a", scope="owner")
    assert "tts.engine.speed" in ei.value.message


def test_fakecore_reset_and_trial_and_dry_run_and_stale(core):
    core.submit([E("tts.engine.speed", 1.4)], actor="arya", scope="owner", trial_s=300)
    ch = core.changes()[0]
    assert ch["trial_expires"] is not None
    dry = core.submit([E("tts.engine.speed", P.RESET)], actor="arya", scope="owner", dry_run=True)
    assert dry.status == "dry_run" and dry.edits[0]["new"] == 1.0 and core.values["tts.engine.speed"] == 1.4
    assert len(core.changes()) == 1 and dry.id == "L-000002"
    ch2 = core.submit([E("tts.engine.speed", P.RESET)], actor="arya", scope="owner")
    assert ch2.id == "L-000002" and core.values["tts.engine.speed"] == 1.0
    with pytest.raises(P.ConflictError) as ei:
        core.submit([E("tts.engine.speed", 1.1)], actor="a", scope="owner", expect_revision=1)
    assert ei.value.code == "stale_revision" and ei.value.revision == core.revision
    core.submit([E("tts.engine.speed", 1.1)], actor="a", scope="owner", expect_revision=core.revision)


def test_fakecore_agent_policy(core):
    core.voice_turn = True
    ok = core.submit([E("tts.engine.speed", 0.9)], actor="hermes", scope="agent", reason="x" * 300)
    assert ok.status == "applied" and ok.trial_expires and ok.session and len(ok.reason) == 120
    assert "how fast I talk" in ok.announce and "0.9" in ok.announce and "10 minutes" in ok.announce
    with pytest.raises(P.PolicyError) as ei:                      # outside agent bounds
        core.submit([E("tts.engine.speed", 1.4)], actor="hermes", scope="agent")
    assert ei.value.code == "policy" and ei.value.allowed == {"min": 0.8, "max": 1.3, "step": 0.1}
    with pytest.raises(P.PolicyError) as ei:                      # more than one step
        core.submit([E("tts.engine.speed", 1.3)], actor="hermes", scope="agent")
    assert ei.value.code == "policy" and ei.value.allowed["step"] == 0.1
    with pytest.raises(P.PolicyError) as ei:                      # deny knob
        core.submit([E("tts.engine.threads", 2)], actor="hermes", scope="agent")
    assert ei.value.code == "policy" and ei.value.owner_command == "mira set tts.engine.threads=2 --trial 600"
    assert core.values["tts.engine.threads"] == 4 and core.values["tts.engine.speed"] == 0.9
    ok2 = core.submit([E("tts.engine.speed", 1.0), E("tts.engine.voice", "jenny")], actor="hermes", scope="agent",
                      trial_s=5)
    assert ok2.session == ok.session and core.session("get", actor="x", scope="agent")["changes"] == [ok.id, ok2.id]
    t0 = datetime_ts(ok2.ts), datetime_ts(ok2.trial_expires)
    assert t0[1] - t0[0] == 60                                    # trial clamped to at least 60 s
    core.voice_allowed = lambda v: v != "heart"
    with pytest.raises(P.PolicyError):
        core.submit([E("tts.engine.voice", "heart")], actor="hermes", scope="agent")


def datetime_ts(s):
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp()


def test_fakecore_voice_turn_lock_and_limits(core):
    core.voice_turn = False
    with pytest.raises(P.PolicyError) as ei:
        core.submit([E("tts.engine.speed", 0.9)], actor="hermes", scope="agent")
    assert ei.value.code == "not_in_voice_turn"
    core.submit([E("tts.engine.speed", 0.9)], actor="arya", scope="owner")   # owner never needs a turn
    core.voice_turn = True
    core.agents_locked = True
    with pytest.raises(P.PolicyError) as ei:
        core.submit([E("tts.engine.speed", 1.0)], actor="hermes", scope="agent")
    assert ei.value.code == "agents_locked"
    core.agents_locked = False
    core.agent_write_limit = 1
    core.submit([E("tts.engine.speed", 1.0)], actor="hermes", scope="agent")
    with pytest.raises(P.PolicyError) as ei:
        core.submit([E("tts.engine.speed", 0.9)], actor="hermes", scope="agent")
    assert ei.value.code == "rate_limited"
    core.agent_write_limit = 99
    core.session_max_changes = 1
    with pytest.raises(P.PolicyError) as ei:
        core.submit([E("tts.engine.speed", 0.9)], actor="hermes", scope="agent")
    assert ei.value.code == "session_cap"


def test_fakecore_undo_confirm(core):
    c1 = core.submit([E("tts.engine.speed", 1.2)], actor="arya", scope="owner")
    undone = core.undo(c1.id, actor="arya", scope="owner")
    assert len(undone) == 1 and undone[0].undo_of == c1.id and core.values["tts.engine.speed"] == 1.0
    assert core._find(c1.id).status == "reverted"
    with pytest.raises(P.ConflictError):                            # already reverted
        core.undo(c1.id, actor="arya", scope="owner")
    c2 = core.submit([E("tts.engine.speed", 1.3)], actor="arya", scope="owner")
    core.submit([E("tts.engine.speed", 1.4)], actor="arya", scope="owner")
    with pytest.raises(P.ConflictError) as ei:
        core.undo(c2.id, actor="arya", scope="owner")
    assert ei.value.code == "undo_conflict" and ei.value.current == 1.4
    core.undo(c2.id, actor="arya", scope="owner", force=True)
    assert core.values["tts.engine.speed"] == 1.0
    with pytest.raises(P.PolicyError):
        core.undo(c2.id, actor="hermes", scope="agent", force=True)
    # agents: undo only agent changes; undo needs no voice turn, confirm does
    owner_ch = core.submit([E("tts.engine.speed", 1.1)], actor="arya", scope="owner")
    with pytest.raises(P.PolicyError):
        core.undo(owner_ch.id, actor="hermes", scope="agent")
    core.voice_turn = True
    ag = core.submit([E("tts.engine.voice", "lessac")], actor="hermes", scope="agent")
    core.voice_turn = False
    with pytest.raises(P.PolicyError) as ei:
        core.confirm(ag.id, actor="hermes", scope="agent")
    assert ei.value.code == "not_in_voice_turn"
    core.voice_turn = True
    conf = core.confirm(ag.id, actor="hermes", scope="agent")
    assert conf[0].status == "confirmed" and conf[0].trial_expires is None
    assert core.undo(None, actor="x", scope="agent") == []         # nothing left for the agent
    assert core.undo(None, actor="arya", scope="owner")[0].undo_of == owner_ch.id


def test_fakecore_trial_expiry_helper(core):
    core.voice_turn = True
    core.submit([E("tts.engine.speed", 0.9)], actor="hermes", scope="agent", trial_s=60)
    core.session("end", actor="hermes", scope="agent")
    assert core.expire_due() == []
    core.advance(61)
    done = core.expire_due()
    assert len(done) == 1 and done[0].actor == "expiry" and done[0].scope == "system"
    assert core.values["tts.engine.speed"] == 1.0


def test_fakecore_session_flow(core):
    assert core.session("get", actor="a", scope="owner") is None
    core.voice_turn = False
    with pytest.raises(P.PolicyError):
        core.session("start", actor="hermes", scope="agent", topic="voice")
    core.voice_turn = True
    s = core.session("start", actor="hermes", scope="agent", topic="voice")
    assert s["id"] == "V-0001" and s["state"] == "open" and s["topic"] == "voice" and s["followup_s"] == 15.0
    assert core.session("start", actor="hermes", scope="agent")["id"] == "V-0001"      # one open session
    core.submit([E("tts.engine.speed", 0.9)], actor="hermes", scope="agent")
    core.submit([E("tts.engine.speed", 0.8)], actor="hermes", scope="agent")
    core.attach_remote_change({"id": "S-000045"})
    assert core.session("get", actor="a", scope="agent")["changes"][-1] == "S-000045"
    out = core.session("revert", actor="hermes", scope="agent")
    assert out["state"] == "reverted" and core.values["tts.engine.speed"] == 1.0
    assert core.session("get", actor="a", scope="agent") is None
    core.session("start", actor="hermes", scope="agent")
    c = core.submit([E("tts.engine.speed", 0.9)], actor="hermes", scope="agent")
    core.session("keep", actor="hermes", scope="agent")
    assert core._find(c.id).status == "confirmed" and core.values["tts.engine.speed"] == 0.9
    assert core.session("keep", actor="hermes", scope="agent") is None


def test_fakecore_preview_and_reads(core):
    core.voice_turn = True
    out = core.preview(actor="hermes", scope="agent", voice="jenny", text="hi")
    assert out["played"] is True and out["voice"] == "jenny" and core.previews[0]["text"] == "hi"
    core.voice_turn = False
    with pytest.raises(P.PolicyError):
        core.preview(actor="hermes", scope="agent")
    assert core.preview(actor="arya", scope="owner")["queued"] is True
    core.agents_locked = True
    core.voice_turn = True
    with pytest.raises(P.PolicyError) as ei:
        core.preview(actor="hermes", scope="agent")
    assert ei.value.code == "agents_locked"


def test_fakecore_describe_schema_config_guide_inventory(core):
    d = core.describe("tts.engine", scope="agent")
    row = {r["key"]: r for r in d["knobs"]}["speed"]
    assert (row["min"], row["max"], row["settable_by_voice"]) == (0.8, 1.3, True)
    owner_row = {r["key"]: r for r in core.describe("tts.engine", scope="owner")["knobs"]}["speed"]
    assert (owner_row["min"], owner_row["max"]) == (0.5, 1.6)
    with pytest.raises(P.ConfigError) as ei:
        core.describe("tts.enginee", scope="owner")
    assert ei.value.code == "unknown_plugin"
    sch = core.schema()
    assert sch["contract_version"] == 1 and {p["name"] for p in sch["plugins"]} == {"mute.tiny", "tts.engine"}
    json.dumps(sch)
    cfg = core.config(effective=True)
    assert cfg["plugins"]["tts.engine"]["config"]["speed"] == {"value": 1.0, "source": "default"}
    assert cfg["plugins"]["mute.tiny"]["config"]["secret_file"]["value"] == {"set": False}
    assert core.config(effective=False)["plugins"]["tts.engine"]["config"]["speed"] == 1.0
    g = core.tuning_guide("voice", scope="agent")
    knob = {k["key"]: k for k in g["knobs"]}
    assert knob["tts.engine.speed"]["ladder"] == [0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
    assert knob["tts.engine.speed"]["phrases"] == ["slow down"] and knob["tts.engine.voice"]["ladder"] == []
    assert core.tuning_guide("wake", scope="agent")["owner_only"][0]["owner_command"].startswith(
        "mira set tts.engine.label=")
    with pytest.raises(P.ConfigError):
        core.tuning_guide("nonsense", scope="agent")
    inv = {r["name"]: r for r in core.inventory()}
    assert inv["tts.engine"]["state"] == "active" and inv["tts.engine"]["machine"] == "speech"


def test_fakecore_owner_operations(core):
    ch = core.set_enabled("mute.tiny", False, actor="arya", scope="owner")
    assert ch.status == "pending_restart" and {r["name"]: r for r in core.inventory()}["mute.tiny"]["state"] == "disabled"
    with pytest.raises(P.PolicyError):
        core.set_enabled("mute.tiny", True, actor="hermes", scope="agent")
    with pytest.raises(P.ConfigError):
        core.reload_plugin("nope", actor="arya", scope="owner")
    assert core.reload_plugin("tts.engine", actor="arya", scope="owner").id.startswith("L-")
    assert core.reload_file(actor="arya", scope="owner").edits == []
    assert core.request_restart(actor="arya", scope="owner")["ok"] and core.restart_requested
    with pytest.raises(P.PolicyError):
        core.request_restart(actor="hermes", scope="agent")
    core.register_remote("speech", object())
    assert "speech" in core.remotes
    corespec = P.PluginSpec("loop.turns", "loop", "1", "s", "listener", ("loop",), _noop_create, core=True)
    c2 = TK.FakeCore([corespec])
    with pytest.raises(P.PolicyError):
        c2.set_enabled("loop.turns", False, actor="arya", scope="owner")


def test_fakecore_speech_ids_and_values():
    c = TK.FakeCore([voice_spec()], {"tts.engine.speed": 1.2}, machine="speech")
    assert c.values["tts.engine.speed"] == 1.2
    assert c.submit([E("tts.engine.flag", True)], actor="a", scope="owner").id == "S-000001"
    with pytest.raises(KeyError):
        TK.FakeCore([voice_spec()], {"nope.x": 1})
    seen = []
    spec = P.PluginSpec("x.y", "x", "1", "s", "listener", (), _noop_create,
                        knobs=(P.Knob("a", "int", 1, "A.", unit="u", min=0, max=9),
                               P.Knob("b", "int", 1, "B.", unit="u", min=0, max=9)),
                        validate=lambda v: seen.append(dict(v)) or (_ for _ in ()).throw(P.ConfigError("a above b", "a"))
                        if v["a"] > v["b"] else None)
    c3 = TK.FakeCore([spec])
    with pytest.raises(P.ConfigError):
        c3.submit([E("x.y.a", 5)], actor="a", scope="owner")
    assert seen and c3.values["x.y.a"] == 1


def test_fakecore_changes_filters(core):
    core.voice_turn = True
    a = core.submit([E("tts.engine.speed", 1.1)], actor="arya", scope="owner")
    b = core.submit([E("tts.engine.speed", 1.0)], actor="hermes", scope="agent")
    rows = core.changes()
    assert [r["id"] for r in rows] == [b.id, a.id]
    assert [r["id"] for r in core.changes(since=a.id)] == [b.id]
    assert [r["id"] for r in core.changes(actor="arya")] == [a.id]
    assert [r["id"] for r in core.changes(session=b.session)] == [b.id]
    assert len(core.changes(limit=1)) == 1


# ---------------------------------------------------------------- run_plugin_contract
def test_contract_passes_on_good_spec():
    TK.run_plugin_contract(tiny_spec())
    TK.run_plugin_contract(tiny_spec(), start=False)


def test_contract_uses_make_ctx():
    made = []
    def make():
        ctx = TK.FakeCtx("listener")
        made.append(ctx)
        return ctx
    TK.run_plugin_contract(tiny_spec(), make_ctx=make)
    assert len(made) == 1 and not made[0].has("mute")


def test_contract_applies_every_live_knob():
    applied = []

    class Rec(P.PluginBase):
        def start(self): self.ctx.provide("mute", object())
        def apply(self, changed, cfg):
            applied.append(dict(changed))
            super().apply(changed, cfg)
    TK.run_plugin_contract(tiny_spec(create=lambda c, g: Rec(c, g)))
    keys = {k for d in applied for k in d}
    assert keys == {"gain", "mode"}                        # "on" is restart, "secret_file" has no safe value


def broken(**kw):
    return tiny_spec(**kw)


@pytest.mark.parametrize("label,make,needle", [
    ("dup names", lambda: broken(knobs=(P.Knob("a", "int", 1, "A.", unit="u", min=0, max=3),
                                        P.Knob("a", "int", 1, "B.", unit="u", min=0, max=3))), "duplicate"),
    ("empty description", lambda: broken(knobs=(P.Knob("a", "int", 1, "  ", unit="u", min=0, max=3),)), "description"),
    ("long description", lambda: broken(knobs=(P.Knob("a", "int", 1, "x" * 161, unit="u", min=0, max=3),)), "161"),
    ("default above max", lambda: broken(knobs=(P.Knob("a", "int", 9, "A.", unit="u", min=0, max=3),)), "above max"),
    ("default below min", lambda: broken(knobs=(P.Knob("a", "float", -1.0, "A.", unit="u", min=0, max=3),)), "below min"),
    ("enum default", lambda: broken(knobs=(P.Knob("a", "enum", "z", "A.", choices=("x", "y")),)), "not in choices"),
    ("enum no choices", lambda: broken(knobs=(P.Knob("a", "enum", "z", "A."),)), "choices"),
    ("bool default", lambda: broken(knobs=(P.Knob("a", "bool", 1, "A."),)), "bool"),
    ("str too long", lambda: broken(knobs=(P.Knob("a", "str", "abcdef", "A.", min=1, max=3),)), "length"),
])
def test_contract_fails_on_broken_static(label, make, needle):
    with pytest.raises(AssertionError) as ei:
        TK.run_plugin_contract(make(), start=False)
    assert needle in str(ei.value) and "mute.tiny" in str(ei.value)


def test_contract_fails_when_service_not_provided():
    class Lazy(P.PluginBase):
        pass
    with pytest.raises(AssertionError, match="did not provide"):
        TK.run_plugin_contract(tiny_spec(create=lambda c, g: Lazy(c, g)))


def test_contract_fails_on_leaked_thread():
    stop = threading.Event()

    class Leaky(P.PluginBase):
        def start(self):
            self.ctx.provide("mute", object())
            self.t = threading.Thread(target=stop.wait, name="leaky", daemon=False)
            self.t.start()
    try:
        with pytest.raises(AssertionError, match="non-daemon threads"):
            TK.run_plugin_contract(tiny_spec(create=lambda c, g: Leaky(c, g)))
    finally:
        stop.set()
        time.sleep(0.05)


def test_contract_accepts_thread_that_stop_joins():
    class Tidy(P.PluginBase):
        def start(self):
            self.ctx.provide("mute", object())
            self.stop_ev = threading.Event()
            self.t = threading.Thread(target=self.stop_ev.wait, daemon=False)
            self.t.start()
        def stop(self):
            self.stop_ev.set()
            self.t.join()
    TK.run_plugin_contract(tiny_spec(create=lambda c, g: Tidy(c, g)))


def test_contract_fails_when_apply_raises():
    class Bad(P.PluginBase):
        def start(self): self.ctx.provide("mute", object())
        def apply(self, changed, cfg): raise RuntimeError("boom")
    with pytest.raises(AssertionError, match="apply"):
        TK.run_plugin_contract(tiny_spec(create=lambda c, g: Bad(c, g)))


def test_contract_fails_when_second_start_breaks():
    class Once(P.PluginBase):
        n = 0
        def start(self):
            Once.n += 1
            if Once.n > 1:
                raise RuntimeError("second start broke")
            self.ctx.provide("mute", object())
    Once.n = 0
    with pytest.raises(RuntimeError, match="second start"):
        TK.run_plugin_contract(tiny_spec(create=lambda c, g: Once(c, g)))


def test_contract_fails_on_wrong_create_type():
    with pytest.raises(AssertionError, match="PluginBase"):
        TK.run_plugin_contract(tiny_spec(create=lambda c, g: object()))


def test_tests_do_not_leave_stray_threads():
    assert [t for t in threading.enumerate() if t.name == "leaky" and t.is_alive()] == []


# ---- repairs found by the gate review of P1 (wave 1 check)
def test_preview_quiet_default_is_empty_everywhere():
    # owner decision 2026-11-10: no silent hours for previews; the knob and the quiet_hours code stay
    table = (CONTRACTS / "config-format.md").read_text(encoding="utf-8")
    row = next(r for r in table.splitlines() if r.startswith("| preview_quiet |"))
    assert row.split("|")[3].strip() == '""'
    assert json.loads(PI_EXAMPLE)["plugins"]["control.http"]["config"]["preview_quiet"] == ""


def test_night_knobs_are_listed_in_the_config_table():
    table = (CONTRACTS / "config-format.md").read_text(encoding="utf-8")
    rows = {r.split("|")[1].strip(): [c.strip() for c in r.split("|")[2:]] for r in table.splitlines()
            if r.startswith("| night_")}
    assert rows["night_window"][:2] == ["time_window", '""']
    assert rows["night_gain"][:4] == ["float", "0.5", "0.1..1.0 share", "L"]


@pytest.mark.parametrize("ktype,default,bad,good", [
    ("regex", "a+", "(", "b*"),
    ("url", "http://127.0.0.1:1", "ftp://x", "https://example.org"),
    ("time_window", "", "25:00-06:00", "23:00-06:00"),
    ("time_window", "", "23:00", ""),
])
def test_fakecore_checks_regex_url_and_time_window(ktype, default, bad, good):
    spec = tiny_spec(knobs=(P.Knob("v", ktype, default, "a value"),))
    core = TK.FakeCore([spec])
    key = f"{spec.name}.v"
    with pytest.raises(P.ConfigError) as exc:
        core.submit([P.Edit(key, bad)], actor="owner", scope="owner")
    assert exc.value.code == "invalid" and exc.value.field == key
    assert core.submit([P.Edit(key, good)], actor="owner", scope="owner").edits[0]["new"] == good
