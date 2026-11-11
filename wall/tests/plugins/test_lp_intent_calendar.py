"""intent.calendar: what is left today, what is on tomorrow, what is next, from the Agent OS quick route.

Payload shape: quick._calendar over the Google feed (pi/google-feed): events from the fetch to 36 hours later, at most 8,
all-day events carry bare dates with an exclusive end.
"""
from datetime import timedelta, timezone

import pytest

import test_lp_common as H
import test_lp_quick_common as Q
from listener_plugins import intent_calendar as ic
from listener_plugins import quick_common as qc

SPEC = ic.SPEC
FED = "2026-11-10T23:00:00+00:00"      # 6 pm New York; the window runs to 6 am on the 12th


def ev(title, start, end, all_day=False):
    return {"title": title, "start": start, "end": end, "all_day": all_day}


FEED = {"updated_at": FED, "events": [
    ev("Chemistry lab", "2026-11-11T00:30:00Z", "2026-11-11T01:30:00Z"),                 # tonight 7:30 to 8:30 pm
    ev("Robotics meeting with the whole team about the regional", "2026-11-11T14:00:00Z", "2026-11-11T15:00:00Z"),  # tomorrow 9 am
    ev("Teacher workday", "2026-11-11", "2026-11-12", True),                             # tomorrow, all day
    ev("Dentist", "2026-11-11T20:30:00-05:00", "2026-11-11T21:00:00-05:00"),              # offset form, 8:30 pm tomorrow
]}


def say(key, feed=FEED, now=Q.NOW, **over):
    return ic.compose(key, feed, now, Q.cfg(ic, **over))


@pytest.mark.parametrize("text,key", [
    ("what's on my calendar today", "today"), ("What's on my calendar today?", "today"), ("what's on my calendar", "today"),
    ("what is on my calendar", "today"), ("what do i have on my calendar", "today"), ("what do i have on my calendar today", "today"),
    ("do i have anything on my calendar", "today"), ("do i have anything on my calendar today", "today"),
    ("what meetings do i have today", "today"), ("what meetings do i have", "today"), ("do i have any meetings", "today"),
    ("do i have any events today", "today"), ("what appointments do i have today", "today"), ("what's on the calendar", "today"),
    ("what's on today's calendar", "today"), ("what's on my calendar for today", "today"),
    ("what's on my calendar tomorrow", "tomorrow"), ("what meetings do i have tomorrow", "tomorrow"),
    ("do i have any events tomorrow", "tomorrow"), ("what do i have on my calendar tomorrow", "tomorrow"),
    ("what's on tomorrow's calendar", "tomorrow"), ("what's on the calendar tomorrow", "tomorrow"),
    ("what's my next meeting", "next"), ("what's my next event", "next"), ("when is my next appointment", "next"),
    ("what's next on my calendar", "next"), ("what is my next meeting", "next"),
    ("what's on my calendar friday", None), ("what's on my calendar this week", None), ("what's on my calendar next week", None),
    ("what's on my calendar on friday", None), ("what's on my calendar the day after tomorrow", None),
    ("add a meeting to my calendar", None), ("add dentist to my calendar tomorrow", None), ("move my meeting to friday", None),
    ("cancel my meeting", None), ("what's my schedule", None), ("what do i have today", None), ("what's due today", None),
    ("when is my physics test", None), ("when is my next class", None), ("when is the meeting", None),
    ("who is in my next meeting", None), ("what's on my plate", None), ("what's playing", None),
])
def test_phrasings(text, key):
    assert qc.phrase_match(text, ic.DEFAULT_PATTERNS) == key


def test_answers_for_the_realistic_feed():
    assert say("today") == "One thing left today: Chemistry lab until 8:30 pm."
    assert say("tomorrow") == ("Three things tomorrow: Robotics meeting with the whole team at 9 am, "
                               "Dentist at 8:30 pm, and Teacher workday all day.")
    assert say("next") == "Next is Robotics meeting with the whole team tomorrow at 9 am."


def test_today_is_what_is_left_not_what_is_over():
    later = Q.NOW.replace(hour=21, minute=0)
    assert say("today", now=later) == "Nothing else on your calendar today."
    feed = {"updated_at": FED, "events": [ev("Early", "2026-11-10T23:30:00Z", "2026-11-11T00:00:00Z"),
                                          ev("Late", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z")]}
    assert say("today", feed) == "One thing left today: Late at 9 pm."       # Early (6:30 pm) already ended at 7 pm
    assert say("today", {"updated_at": FED, "events": []}) == "Nothing else on your calendar today."


def test_an_all_day_event_covers_its_whole_local_day_and_the_end_date_is_exclusive():
    feed = {"updated_at": FED, "events": [ev("Holiday", "2026-11-10", "2026-11-11", True)]}
    assert say("today", feed) == "One thing left today: Holiday all day."
    assert say("tomorrow", feed) == "Nothing on your calendar tomorrow."
    multi = {"updated_at": FED, "events": [ev("Trip", "2026-11-10", "2026-11-13", True)]}
    assert say("tomorrow", multi) == "One thing tomorrow: Trip all day."


def test_an_event_in_progress_is_said_with_until():
    feed = {"updated_at": FED, "events": [ev("Long lab", "2026-11-10T23:00:00Z", "2026-11-11T02:00:00Z")]}
    assert say("today", feed) == "One thing left today: Long lab until 9 pm."


def test_two_and_many_events_use_counts_and_including():
    two = {"updated_at": FED, "events": [ev("A", "2026-11-11T01:00:00Z", "2026-11-11T01:30:00Z"),
                                         ev("B", "2026-11-11T02:00:00Z", "2026-11-11T03:00:00Z")]}
    assert say("today", two) == "Two things left today: A at 8 pm and B at 9 pm."
    five = {"updated_at": FED, "events": [ev(f"Event {n}", f"2026-11-11T0{n}:00:00Z", f"2026-11-11T0{n}:30:00Z") for n in range(0, 5)]}
    text = say("today", {"updated_at": FED, "events": five["events"][:5]})
    assert text.startswith("Four things left today, including Event 1 at 8 pm")      # Event 0 ended at 7:30 pm
    assert Q.words(text) <= 25


def test_next_event_cases():
    assert say("next", {"updated_at": FED, "events": [ev("A", "2026-11-11T01:00:00Z", "2026-11-11T01:30:00Z")]}) == (
        "Next is A at 8 pm.")
    assert say("next", {"updated_at": FED, "events": [ev("Dentist", "2026-11-13T15:00:00Z", "2026-11-13T16:00:00Z")]}) == (
        "Next is Dentist Friday at 10 am.")
    assert say("next", {"updated_at": FED, "events": []}) == "Nothing on your calendar for the next day."
    only_all_day = {"updated_at": FED, "events": [ev("Holiday", "2026-11-11", "2026-11-12", True)]}
    assert say("next", only_all_day) is None                       # an all-day event is not a "next", ask Hermes
    assert say("next", {"updated_at": FED, "events": [ev("Past", "2026-11-10T23:30:00Z", "2026-11-11T00:00:00Z")]}) is None


def test_the_feed_must_cover_the_day_asked_about():
    morning = Q.NOW.replace(hour=8, minute=0)
    early_feed = {"updated_at": "2026-11-10T13:00:00+00:00", "events": []}      # fetched at 8 am: window ends 8 pm tomorrow
    assert say("today", early_feed, now=morning) == "Nothing else on your calendar today."
    assert say("tomorrow", early_feed, now=morning) is None                      # tomorrow runs past the window's end
    evening_feed = {"updated_at": "2026-11-11T01:00:00+00:00", "events": []}     # fetched at 8 pm: window ends 8 am the 12th
    assert say("tomorrow", evening_feed, now=Q.NOW.replace(hour=20)) is not None
    old = {"updated_at": "2026-11-10T11:00:00+00:00", "events": []}              # a 36 hour window from 6 am ends 6 pm tomorrow
    assert say("tomorrow", old, now=morning) is None


def test_the_eight_event_cap_cannot_hide_events_before_the_end_of_the_day():
    cut = {"updated_at": FED, "events": [ev(f"E{n}", f"2026-11-11T{10 + n}:00:00Z", f"2026-11-11T{10 + n}:30:00Z") for n in range(8)]}
    assert say("tomorrow", cut) is None                   # the 8th starts well before the end of tomorrow: more may exist
    assert say("today", cut) is not None                  # but today is fully inside what we hold... the 8th is past today
    full_today = {"updated_at": FED, "events": [ev(f"E{n}", f"2026-11-10T23:{10 + n}:00Z", f"2026-11-10T23:{11 + n}:00Z") for n in range(8)]}
    assert say("today", full_today, now=Q.NOW.replace(hour=18, minute=0)) is None
    assert say("next", full_today, now=Q.NOW.replace(hour=18, minute=0)) is not None   # the earliest is still the earliest


def test_bad_data_falls_through():
    assert say("today", {"events": []}) is None
    assert say("today", {"updated_at": "x", "events": []}) is None
    assert say("today", {"updated_at": FED, "events": None}) is None
    assert say("today", {"updated_at": FED, "events": [ev("Bad", "soon", "later")]}) is None
    assert say("today", {"updated_at": FED, "events": [ev("", "2026-11-11T00:30:00Z", "2026-11-11T01:00:00Z")]}) is None
    assert say("today", {"updated_at": FED, "events": ["junk"]}) == "Nothing else on your calendar today."


def test_word_budget_and_title_cleaning():
    feed = {"updated_at": FED, "events": [ev("Meeting [zoom] with the (whole) extended robotics leadership group", "2026-11-11T01:00:00Z", "2026-11-11T01:30:00Z")]}
    assert say("today", feed) == "One thing left today: Meeting with the extended robotics leadership at 8 pm."
    for key in ("today", "tomorrow"):
        assert Q.words(say(key, max_words=14)) <= 16


def _started(tmp_path, server, **over):
    return H.started("intent.calendar", url=server.url, token_file=Q.token_file(tmp_path), **over)


def test_service_end_to_end_and_fallthrough(tmp_path):
    s = Q.quick_server({"calendar": FEED})
    try:
        plugin, ctx, cfg = _started(tmp_path, s)
        i = ctx.get("intent")
        i.clock = lambda: Q.NOW.astimezone(timezone.utc)
        assert i.match("what's on my calendar today?") == "today"
        assert i.handle("today") == "One thing left today: Chemistry lab until 8:30 pm."
        assert i.match("what's on my calendar next friday") is None
        assert [c["path"] for c in s.calls] == ["/api/hooks/quick/calendar"]      # the unmatched phrase made no request
    finally:
        s.stop()
    for status in (503, 401):
        s = Q.quick_server({"calendar": FEED}, status=status)
        try:
            plugin, ctx, cfg = _started(tmp_path, s)
            assert ctx.get("intent").match("what's on my calendar today") is None
        finally:
            s.stop()


def test_live_knobs_apply():
    plugin, ctx, cfg = H.started("intent.calendar")
    i = ctx.get("intent")
    pats = dict(ic.DEFAULT_PATTERNS, today=r"gimme calendar")
    H.assert_live_knobs(SPEC, plugin, {
        "priority": (7, lambda p: i.priority), "url": ("http://127.0.0.1:1/", lambda p: i.client.url, "http://127.0.0.1:1"),
        "token_file": ("/t/quick", lambda p: i.client.token_file), "timeout_s": (1.5, lambda p: i.client.timeout_s),
        "timezone": ("Asia/Tokyo", lambda p: i.cfg.timezone), "patterns": (pats, lambda p: i.patterns),
        "max_words": (30, lambda p: i.cfg.max_words)})


def test_spec_shape_and_feed_constants():
    assert SPEC.name == "intent.calendar" and SPEC.default_enabled is False and ic.LEGACY_CLASSES == ()
    assert (ic.FEED_HOURS, ic.FEED_MAX) == (36, 8)
