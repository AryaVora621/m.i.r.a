"""mirakit.schema: coercion, bounds, host rules, time windows, JSON export, the core pseudo-plugin."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from mirakit import schema
from mirakit.plugin import ConfigError, Knob, Paths, PluginBase, PluginSpec


def knob(type_="float", default=1.0, **kw):
    return Knob("k", type_, default, "a knob", **kw)


def spec_of(*knobs, validate=None, name="demo.one"):
    return PluginSpec(name=name, kind="demo", version="1", summary="s", machine="listener", provides=(),
                      create=lambda ctx, cfg: PluginBase(ctx, cfg), knobs=tuple(knobs), validate=validate)


# ---- coercion
@pytest.mark.parametrize("raw,expected", [("true", True), ("False", False), ("1", True), ("0", False),
                                          ("yes", True), ("off", False), (True, True)])
def test_bool_coercion(raw, expected):
    assert schema.coerce(knob("bool", True), raw, "demo.one.k") is expected


@pytest.mark.parametrize("raw", ["maybe", 2, None, 1.0])
def test_bool_rejects(raw):
    with pytest.raises(ConfigError):
        schema.coerce(knob("bool", True), raw, "demo.one.k")


def test_numbers_from_text_and_whole_number_rule():
    assert schema.coerce(knob("float"), "0.06", "k") == 0.06
    assert schema.coerce(knob("int", 1), "7", "k") == 7
    assert schema.coerce(knob("int", 1), 7.0, "k") == 7
    with pytest.raises(ConfigError, match="whole"):
        schema.coerce(knob("int", 1), "0.5", "k")
    for bad in ("nan", "inf", "abc", True):
        with pytest.raises(ConfigError):
            schema.coerce(knob("float"), bad, "k")


def test_enum_and_text_types():
    k = knob("enum", "a", choices=("a", "b"))
    assert schema.coerce(k, "b") == "b"
    with pytest.raises(ConfigError, match="one of a, b"):
        schema.coerce(k, "c")
    with pytest.raises(ConfigError):
        schema.coerce(knob("str", ""), 5)


def test_json_text_for_list_and_map_and_comma_list_only_from_env():
    lst = knob("list[str]", ["a"])
    assert schema.coerce(lst, '["x", "y"]') == ["x", "y"]
    assert schema.coerce(lst, "a, b", text=True) == ["a", "b"]
    with pytest.raises(ConfigError):
        schema.coerce(lst, "a, b")
    with pytest.raises(ConfigError):
        schema.coerce(lst, [1, 2])
    mp = knob("map[str,str]", {})
    out = schema.coerce(mp, '{"z": "1", "a": "2"}')
    assert list(out) == ["z", "a"]          # insertion order is kept
    with pytest.raises(ConfigError):
        schema.coerce(mp, {"a": 1})


def test_url_regex_time_window_checks():
    assert schema.coerce(knob("url", ""), "http://127.0.0.1:8650") == "http://127.0.0.1:8650"
    for bad in ("ftp://x", "http://", "nonsense"):
        with pytest.raises(ConfigError):
            schema.coerce(knob("url", ""), bad)
    assert schema.coerce(knob("regex", ""), "a+b") == "a+b"
    with pytest.raises(ConfigError, match="pattern"):
        schema.coerce(knob("regex", ""), "(")
    assert schema.coerce(knob("time_window", ""), "") == ""
    assert schema.coerce(knob("time_window", ""), "23:00-06:00") == "23:00-06:00"
    for bad in ("23:00", "25:00-06:00", "6:00-7:00"):
        with pytest.raises(ConfigError):
            schema.coerce(knob("time_window", ""), bad)


def test_nullable():
    assert schema.coerce(knob("float", None, nullable=True), None) is None
    with pytest.raises(ConfigError):
        schema.coerce(knob("float"), None)


def test_voice_goes_through_the_resolver():
    seen = []

    def resolver(v):
        seen.append(v)
        return "vits-piper-en_US-amy-medium#0"

    k = knob("voice", "x#0")
    assert schema.coerce(k, "amy", voice_resolver=resolver) == "vits-piper-en_US-amy-medium#0"
    assert seen == ["amy"]
    assert schema.coerce(k, "amy") == "amy"          # no resolver (the listener): passed through


# ---- bounds
def test_bounds_numbers_and_lengths():
    k = knob("float", 1.0, min=0.5, max=2.0)
    schema.check_bounds(k, 0.5)
    schema.check_bounds(k, 2.0)
    with pytest.raises(ConfigError, match="below"):
        schema.check_bounds(k, 0.49, "demo.one.k")
    with pytest.raises(ConfigError, match="above"):
        schema.check_bounds(k, 2.01)
    s = knob("str", "ab", min=1, max=3)
    with pytest.raises(ConfigError):
        schema.check_bounds(s, "abcd")
    with pytest.raises(ConfigError):
        schema.check_bounds(s, "")
    schema.check_bounds(knob("list[str]", [], max=2), ["a", "b"])
    with pytest.raises(ConfigError):
        schema.check_bounds(knob("list[str]", [], max=2), ["a", "b", "c"])


def test_error_carries_the_field():
    with pytest.raises(ConfigError) as ei:
        schema.validate_knob(knob("float", 1.0, max=2), 3, "demo.one.k")
    assert ei.value.field == "demo.one.k" and ei.value.code == "invalid"


# ---- plugin level
def test_validate_plugin_fills_defaults_names_closest_and_runs_hook():
    calls = []
    sp = spec_of(Knob("alpha", "float", 1.0, "d"), Knob("beta", "int", 2, "d"),
                 validate=lambda v: calls.append(dict(v)))
    out = schema.validate_plugin(sp, {"alpha": "3"})
    assert out == {"alpha": 3.0, "beta": 2} and calls == [out]
    with pytest.raises(ConfigError) as ei:
        schema.validate_plugin(sp, {"alpah": 1})
    assert ei.value.code == "unknown_key" and "demo.one.alpha" in ei.value.message
    # keys starting with $ are comments
    assert schema.validate_plugin(sp, {"$comment": "x"})["alpha"] == 1.0


def test_hook_error_is_a_config_error():
    def hook(v):
        if v["a"] > v["b"]:
            raise ConfigError("a must not exceed b", "demo.one.a")

    sp = spec_of(Knob("a", "float", 1.0, "d"), Knob("b", "float", 2.0, "d"), validate=hook)
    with pytest.raises(ConfigError, match="exceed"):
        schema.validate_plugin(sp, {"a": 5})


def test_defaults_are_fresh_copies():
    sp = spec_of(Knob("l", "list[str]", ("x", "y"), "d"), Knob("m", "map[str,str]", {"a": "b"}, "d"))
    d1, d2 = schema.defaults(sp), schema.defaults(sp)
    d1["l"].append("z")
    d1["m"]["q"] = "r"
    assert d2 == {"l": ["x", "y"], "m": {"a": "b"}}


# ---- host rules
def test_host_rules():
    assert schema.host_is_loopback("http://127.0.0.1:8642")
    assert schema.host_is_loopback("http://localhost:1")
    assert schema.host_is_loopback("http://[::1]:1")
    assert not schema.host_is_loopback("http://100.77.98.89:8765")
    assert schema.host_is_tailnet("http://100.77.98.89:8765")
    assert schema.host_is_tailnet("http://100.64.0.1")
    assert schema.host_is_tailnet("http://100.127.255.254")
    assert not schema.host_is_tailnet("http://100.128.0.1")
    assert not schema.host_is_tailnet("http://100.63.255.255")
    assert not schema.host_is_tailnet("http://example.com")
    assert schema.host_is_loopback_or_tailnet("http://100.77.98.89")
    assert not schema.host_is_loopback_or_tailnet("http://192.168.1.152")
    assert schema.ip_is_loopback_or_tailnet("100.77.98.89") and not schema.ip_is_loopback_or_tailnet("0.0.0.0")


# ---- time windows
def at(h, m=0):
    return datetime(2026, 11, 10, h, m)


def test_time_window_plain_and_wrapping():
    assert schema.in_time_window("09:00-17:00", at(9)) and not schema.in_time_window("09:00-17:00", at(17))
    assert schema.in_time_window("23:00-06:00", at(23, 30))
    assert schema.in_time_window("23:00-06:00", at(2))
    assert not schema.in_time_window("23:00-06:00", at(6))
    assert not schema.in_time_window("23:00-06:00", at(12))
    assert schema.in_time_window("23:00-06:00", at(23, 0)) and not schema.in_time_window("23:00-06:00", at(22, 59))


def test_time_window_empty_means_off():
    assert schema.parse_time_window("") is None
    assert not schema.in_time_window("", at(3))
    assert not schema.in_time_window("10:00-10:00", at(10))
    with pytest.raises(ValueError):
        schema.parse_time_window("nope")


# ---- paths
def test_expand_path(tmp_path):
    p = Paths(here=Path("/opt/mira/current"), config_dir=tmp_path, home=Path("/home/mira"))
    assert schema.expand_path("~/x", p) == "/home/mira/x"
    assert schema.expand_path("${HOME}/y", p) == "/home/mira/y"
    assert schema.expand_path("${HERE}/models/a.onnx", p) == "/opt/mira/current/models/a.onnx"
    assert schema.expand_path("/abs/z", p) == "/abs/z"
    assert schema.expand_path("", p) == ""
    assert schema.expand_path("~", p) == "/home/mira"


# ---- export
def test_json_export_round_trips_through_json():
    k = Knob("t", "float", 0.05, "d", unit="rms", min=0.005, max=0.3, agent="bounded", agent_min=0.03,
             agent_max=0.1, agent_step=0.01, phrases=("a", "b"), choices=None)
    d = schema.knob_to_json(k)
    assert d["phrases"] == ["a", "b"] and d["choices"] is None and d["agent_step"] == 0.01
    sp = spec_of(k)
    blob = json.loads(json.dumps(schema.spec_to_json(sp)))
    assert blob["name"] == "demo.one" and blob["knobs"][0]["name"] == "t"
    rebuilt = Knob(**{**blob["knobs"][0], "phrases": tuple(blob["knobs"][0]["phrases"])})
    assert rebuilt == k


def test_ladder():
    k = Knob("t", "float", 0.05, "d", min=0.005, max=0.3, agent="bounded", agent_min=0.03, agent_max=0.1,
             agent_step=0.01)
    assert schema.ladder(k) == [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1]
    ik = Knob("n", "int", 4, "d", min=1, max=15, agent="bounded", agent_min=4, agent_max=15, agent_step=2)
    assert schema.ladder(ik) == [4, 6, 8, 10, 12, 14]          # 15 is not reachable in steps of 2
    fk = Knob("f", "float", 8.0, "d", min=0, max=30, agent="bounded", agent_min=4, agent_max=15, agent_step=2)
    assert schema.ladder(fk)[-1] == 14.0
    assert schema.ladder(Knob("n", "int", 4, "d")) == []


def test_spoken_name_and_formatting():
    assert schema.spoken_name(Knob("followup_wait_s", "float", 1.0, "d")) == "followup wait s"
    assert schema.spoken_name(Knob("x", "float", 1.0, "d", spoken="how fast")) == "how fast"
    assert schema.fmt_num(0.8) == "0.8" and schema.fmt_num(10.0) == "10" and schema.fmt_num(True) == "on"


# ---- the core pseudo-plugin
def test_core_spec_listener_and_speech():
    lst, spk = schema.core_spec("listener"), schema.core_spec("speech")
    assert {k.name for k in lst.knobs} >= {"engine", "legacy_env", "unit_memory_max", "unit_cpu_quota",
                                           "unit_restart_sec", "crash_guard_after"}
    assert {k.name for k in spk.knobs} >= {"engine", "unit_throttle_s", "unit_keep_alive"}
    assert all(k.agent == "deny" and k.applies == "restart" for k in lst.knobs)
    assert lst.core and not lst.required
    d = schema.defaults(lst)
    assert d["engine"] == "legacy" and d["unit_memory_max"] == "200M" and d["crash_legacy_after"] == 4
    schema.validate_plugin(lst, {})
    with pytest.raises(ConfigError, match="exceed"):
        schema.validate_plugin(lst, {"crash_guard_after": 5, "crash_legacy_after": 4})
    with pytest.raises(ConfigError):
        schema.validate_plugin(lst, {"unit_memory_max": "lots"})
    with pytest.raises(ConfigError):
        schema.validate_plugin(lst, {"unit_cpu_quota": "4000%"})
