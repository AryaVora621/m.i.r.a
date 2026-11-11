"""intent.weather: today's and tomorrow's weather from the Agent OS quick route (payload shape from pi/server/quick.py)."""
import copy

import pytest

import test_lp_common as H
import test_lp_quick_common as Q
from listener_plugins import intent_weather as iw
from listener_plugins import quick_common as qc

SPEC = iw.SPEC

# What GET /api/hooks/quick/weather returns (quick._weather over ambient._fetch_weather's block).
PAYLOAD = {"place": "Home", "temp_f": 49, "feels_f": 44, "summary": "Cloudy", "high_f": 55, "low_f": 41, "precip_pct": 20,
           "day_summary": "Rain", "date": "2026-11-10", "updated_at": "2026-11-10T23:30:00.000Z",
           "tomorrow": {"date": "2026-11-11", "summary": "Clear", "high_f": 60, "low_f": 40, "precip_pct": 5}}


def say(key, data=PAYLOAD, now=Q.NOW):
    return iw.compose(key, data, now, Q.cfg(iw))


@pytest.mark.parametrize("text,key", [
    ("what's the weather", "today"), ("What's the weather today?", "today"), ("what is the weather like", "today"),
    ("what's the weather like outside", "today"), ("how's the weather", "today"), ("how is the weather outside", "today"),
    ("weather", "today"), ("weather today", "today"), ("the weather", "today"), ("what's it like outside", "today"),
    ("what's today's forecast", "today"), ("what's the forecast", "today"), ("how cold is it", "today"),
    ("how warm is it outside", "today"), ("what's the temperature", "today"), ("what is the temperature outside", "today"),
    ("will it rain", "rain"), ("is it going to rain today", "rain"), ("will it snow tonight", "rain"), ("is it going to snow", "rain"),
    ("what's the weather tomorrow", "tomorrow"), ("what is the forecast for tomorrow", None), ("forecast for tomorrow", "tomorrow"),
    ("how's the weather tomorrow", "tomorrow"), ("weather tomorrow", "tomorrow"), ("what's tomorrow's weather", "tomorrow"),
    ("what's the weather going to be like tomorrow", "tomorrow"), ("will it rain tomorrow", "tomorrow"),
    ("what's the weather in boston", None), ("weather in tokyo", None), ("what's the weather this weekend", None),
    ("what's the weather on thursday", None), ("is it raining", None), ("do i need a coat", None), ("should i wear shorts", None),
    ("what's the weather like in new york tomorrow", None), ("how hot is the printer bed", None), ("how cold is the filament", None),
    ("what's the weather going to be this week", None), ("is it going to rain this weekend", None), ("is it sunny", None),
])
def test_phrasings(text, key):
    assert qc.phrase_match(text, iw.DEFAULT_PATTERNS) == key


def test_answers_for_the_realistic_payload():
    assert say("today") == "It is 49 degrees, cloudy. High of 55, low of 41, and a 20 percent chance of rain."
    assert say("rain") == "Today, a 20 percent chance of rain, with a high of 55."
    assert say("tomorrow") == "Tomorrow: clear, high of 60, low of 40, and no rain expected."
    for k in ("today", "rain", "tomorrow"):
        assert Q.words(say(k)) <= 25


def test_snow_and_odd_conditions():
    snow = dict(PAYLOAD, summary="Snow", day_summary="Snow", precip_pct=80, temp_f=28.4, high_f=31.6)
    assert say("today", snow) == "It is 28 degrees, snow. High of 32, low of 41, and an 80 percent chance of snow."
    assert say("rain", snow) == "Today, an 80 percent chance of snow, with a high of 32."
    unknown = dict(PAYLOAD, summary="Unknown")
    assert say("today", unknown).startswith("It is 49 degrees.")
    clear = dict(PAYLOAD, precip_pct=0)
    assert say("today", clear).endswith("and no rain expected.")


def test_every_condition_the_server_can_return_reads_naturally():
    """ambient._weather_summary returns nouns (Rain, Snow, Fog, Drizzle, Thunderstorm, Heavy rain) as well as adjectives."""
    for cond, lower in (("Clear", "clear"), ("Cloudy", "cloudy"), ("Fog", "fog"), ("Drizzle", "drizzle"), ("Rain", "rain"),
                        ("Heavy rain", "heavy rain"), ("Snow", "snow"), ("Thunderstorm", "thunderstorm")):
        assert say("today", dict(PAYLOAD, summary=cond)).startswith(f"It is 49 degrees, {lower}.")
        t = dict(PAYLOAD, tomorrow=dict(PAYLOAD["tomorrow"], summary=cond))
        assert say("tomorrow", t).startswith(f"Tomorrow: {lower}, high of 60")


@pytest.mark.parametrize("pct,said", [(0, "no rain expected"), (5, "no rain expected"), (6, "a 6 percent chance of rain"),
                                      (8, "an 8 percent chance of rain"), (11, "an 11 percent chance of rain"),
                                      (18, "an 18 percent chance of rain"), (20, "a 20 percent chance of rain"),
                                      (80, "an 80 percent chance of rain"), (85, "an 85 percent chance of rain"),
                                      (89, "an 89 percent chance of rain"), (90, "a 90 percent chance of rain"),
                                      (100, "a 100 percent chance of rain"), (7.6, "an 8 percent chance of rain")])
def test_the_chance_reads_with_the_right_article(pct, said):
    assert said in say("today", dict(PAYLOAD, precip_pct=pct))


def test_missing_pieces_are_left_out_or_fall_through():
    assert say("today", dict(PAYLOAD, precip_pct=None)) == "It is 49 degrees, cloudy. High of 55, low of 41."
    assert say("today", dict(PAYLOAD, temp_f=None)) is None
    assert say("today", dict(PAYLOAD, high_f=None)) is None
    assert say("today", dict(PAYLOAD, low_f=True)) is None            # a bool is not a temperature
    assert say("rain", dict(PAYLOAD, precip_pct=None)) is None
    assert say("rain", dict(PAYLOAD, high_f=None)) is None
    assert say("tomorrow", dict(PAYLOAD, tomorrow=None)) is None
    assert say("tomorrow", dict(PAYLOAD, tomorrow={"summary": "Clear"})) is None
    t = copy.deepcopy(PAYLOAD)
    t["tomorrow"]["precip_pct"] = None
    t["tomorrow"]["summary"] = "Unknown"
    assert say("tomorrow", t) == "Tomorrow: high of 60, and low of 40."
    assert say("today", {}) is None


def test_a_reading_older_than_four_hours_falls_through():
    from datetime import timedelta
    assert say("today", now=Q.NOW + timedelta(hours=2)) is not None
    assert say("today", now=Q.NOW + timedelta(hours=5)) is None
    assert say("today", dict(PAYLOAD, updated_at="garbage")) is not None     # no age to judge: the server already limits it


def _started(tmp_path, server, **over):
    return H.started("intent.weather", url=server.url, token_file=Q.token_file(tmp_path), **over)


def test_service_fetches_the_weather_topic_and_speaks(tmp_path):
    s = Q.quick_server({"weather": PAYLOAD})
    try:
        plugin, ctx, cfg = _started(tmp_path, s)
        i = ctx.get("intent")
        i.clock = lambda: Q.NOW
        assert i.match("what's the weather?") == "today"
        assert i.handle("today").startswith("It is 49 degrees, cloudy")
        assert [c["path"] for c in s.calls] == ["/api/hooks/quick/weather"]
    finally:
        s.stop()


@pytest.mark.parametrize("status", [401, 404, 500, 503])
def test_fetch_errors_hand_the_question_to_hermes(tmp_path, status):
    s = Q.quick_server({"weather": PAYLOAD}, status=status)
    try:
        plugin, ctx, cfg = _started(tmp_path, s)
        i = ctx.get("intent")
        assert i.match("what's the weather") is None and i.handle("today") is None
        assert plugin.status()["to_hermes"] == 1
    finally:
        s.stop()


def test_a_slow_agent_os_costs_only_the_timeout(tmp_path):
    import time
    s = Q.quick_server({"weather": PAYLOAD}, delay=2.0)
    try:
        plugin, ctx, cfg = _started(tmp_path, s, timeout_s=0.5)
        t0 = time.monotonic()
        assert ctx.get("intent").match("what's the weather") is None
        assert time.monotonic() - t0 < 1.5
    finally:
        s.stop()


def test_refused_and_missing_token_fall_through(tmp_path):
    plugin, ctx, cfg = H.started("intent.weather", url=H.refused_url(), token_file=Q.token_file(tmp_path))
    assert ctx.get("intent").match("what's the weather") is None
    plugin, ctx, cfg = H.started("intent.weather", url=H.refused_url(), token_file=str(tmp_path / "none"))
    assert ctx.get("intent").match("what's the weather") is None


def test_live_knobs_apply(tmp_path):
    plugin, ctx, cfg = H.started("intent.weather")
    i = ctx.get("intent")
    pats = dict(iw.DEFAULT_PATTERNS, today=r"gimme weather")
    H.assert_live_knobs(SPEC, plugin, {
        "priority": (7, lambda p: i.priority), "url": ("http://127.0.0.1:1/", lambda p: i.client.url, "http://127.0.0.1:1"),
        "token_file": ("/t/quick", lambda p: i.client.token_file), "timeout_s": (1.5, lambda p: i.client.timeout_s),
        "timezone": ("Asia/Tokyo", lambda p: i.cfg.timezone), "patterns": (pats, lambda p: i.patterns)})
    plugin.apply({"patterns": pats}, cfg.replace(patterns=pats))
    assert i.patterns["today"] == "gimme weather"


def test_spec_shape():
    assert SPEC.name == "intent.weather" and SPEC.default_enabled is False and iw.LEGACY_CLASSES == ()
