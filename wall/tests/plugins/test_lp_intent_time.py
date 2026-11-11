"""intent.time: the time and the date from the Pi's clock, no network."""
import socket
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import test_lp_common as H
import test_lp_quick_common as Q
from listener_plugins import intent_time as it
from listener_plugins import quick_common as qc

SPEC = it.SPEC


def at(y, mo, d, h, mi, tz="America/New_York"):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


@pytest.mark.parametrize("text,key", [
    ("what time is it", "time"), ("What time is it?", "time"), ("what's the time", "time"), ("what is the time", "time"),
    ("what is the time right now", "time"), ("what time is it right now", "time"), ("what time is it now", "time"),
    ("what time is it currently", "time"), ("the time", "time"), ("current time", "time"), ("time now", "time"),
    ("hey tell me the time please", "time"), ("could you tell me what time it is", "time"), ("what the time", None),
    ("what's the date", "date"), ("what is the date", "date"), ("what's the date today", "date"), ("what is today's date", "date"),
    ("what day is it", "date"), ("what day is it today", "date"), ("what date is it", "date"), ("what's today", "date"),
    ("what is today", "date"), ("what's the day", "date"), ("what day of the week is it", "date"), ("today's date", "date"),
    ("tell me the date", "date"), ("what is the day today", "date"),
    ("what time is it in paris", None), ("what time does the store close", None), ("what time is lunch", None),
    ("what time is my appointment", None), ("what's the time zone", None), ("how much time is left on the print", None),
    ("what's the date of my physics test", None), ("what's the date on friday", None), ("what day is my next exam", None),
    ("what day is it in japan", None), ("what time is sunset", None), ("time for bed", None), ("what is time", None),
])
def test_phrasings(text, key):
    assert qc.phrase_match(text, it.DEFAULT_PATTERNS) == key


@pytest.mark.parametrize("when,time_ans,date_ans", [
    (at(2026, 11, 10, 19, 42), "It is 7:42 pm.", "Today is Tuesday, November tenth."),
    (at(2026, 11, 11, 12, 0), "It is noon.", "Today is Wednesday, November eleventh."),
    (at(2026, 11, 12, 0, 0), "It is midnight.", "Today is Thursday, November twelfth."),
    (at(2026, 12, 1, 9, 5), "It is 9:05 am.", "Today is Tuesday, December first."),
    (at(2026, 12, 31, 23, 59), "It is 11:59 pm.", "Today is Thursday, December thirty first."),
    (at(2027, 1, 22, 8, 0), "It is 8 am.", "Today is Friday, January twenty second."),
])
def test_answers_for_realistic_clock_readings(when, time_ans, date_ans):
    plugin, ctx, cfg = H.started("intent.time")
    i = ctx.get("intent")
    i.clock = lambda: when
    assert i.match("what time is it") == "time" and i.handle("time") == time_ans
    assert i.match("what day is it") == "date" and i.handle("date") == date_ans


def test_the_day_follows_the_timezone_knob_across_midnight():
    when = at(2026, 11, 11, 3, 30, "UTC")        # 10:30 pm on the 10th in New York, already the 11th in Tokyo
    for tz, day in (("America/New_York", "Tuesday, November tenth"), ("Asia/Tokyo", "Wednesday, November eleventh")):
        plugin, ctx, cfg = H.started("intent.time", timezone=tz)
        i = ctx.get("intent")
        i.clock = lambda: when
        i.match("what day is it")
        assert i.handle("date") == f"Today is {day}."


def test_summer_time_is_followed():
    plugin, ctx, cfg = H.started("intent.time")
    i = ctx.get("intent")
    i.clock = lambda: at(2026, 7, 4, 15, 30)
    i.match("what's the time")
    assert i.handle("time") == "It is 3:30 pm."


def test_answers_are_short():
    for when in (at(2026, 11, 10, 19, 42), at(2026, 12, 31, 23, 59)):
        for key in ("time", "date"):
            assert Q.words(it.compose(key, None, when.astimezone(Q.TZ), Q.cfg(it))) <= 8


def test_no_socket_is_opened_and_no_token_is_needed(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("the time intent touched the network")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    plugin, ctx, cfg = H.started("intent.time")
    i = ctx.get("intent")
    assert i.client is None and i.topic == ""
    assert i.match("what time is it") == "time" and i.handle("time").startswith("It is ")


def test_other_phrases_return_none_so_hermes_gets_them():
    plugin, ctx, cfg = H.started("intent.time")
    i = ctx.get("intent")
    assert i.match("what time is it in tokyo") is None and i.match("play music") is None and i.match("") is None
    assert i.handle("time") is None


def test_live_knobs_apply():
    plugin, ctx, cfg = H.started("intent.time")
    i = ctx.get("intent")
    pats = dict(it.DEFAULT_PATTERNS, time=r"gimme the time")
    H.assert_live_knobs(SPEC, plugin, {
        "priority": (7, lambda p: i.priority), "timezone": ("Asia/Tokyo", lambda p: i.cfg.timezone),
        "patterns": (pats, lambda p: i.patterns)})
    plugin.apply({"patterns": pats}, cfg.replace(patterns=pats))
    assert i.match("gimme the time") == "time" and i.match("what time is it") is None


def test_spec_shape_and_status():
    assert SPEC.name == "intent.time" and SPEC.provides == ("intent",) and it.LEGACY_CLASSES == ()
    plugin, ctx, cfg = H.started("intent.time")
    ctx.get("intent").match("what time is it")
    assert plugin.status() == {"priority": 90, "answered": 1, "to_hermes": 0}
