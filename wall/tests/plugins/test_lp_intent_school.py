"""intent.school: due today, tomorrow, this week and missing, from the Agent OS quick route (shape of ambient._school_read)."""
from datetime import datetime, timedelta, timezone

import pytest

import test_lp_common as H
import test_lp_quick_common as Q
from listener_plugins import intent_school as isc
from listener_plugins import quick_common as qc

SPEC = isc.SPEC
UPDATED = "2026-11-10T22:00:00.000Z"      # 5 pm in New York; the Classroom job posts at 7:00, 15:30 and 21:00


def item(title, due, state="assigned", course="Physics"):
    return {"title": title, "course": course, "due": due, "state": state}


# Stored as UTC "YYYY-MM-DDTHH:MM:SSZ", ordered missing first then by due, no-due last (ambient._school_read).
FEED = {"updated_at": UPDATED, "items": [
    item("Reading Quiz Chapter 9", "2026-10-01T04:59:00Z", "missing", "History"),
    item("Lab 4 Report", "2026-10-30T04:59:00Z", "missing"),
    item("Projectiles Review Sheets", "2026-11-11T04:59:00Z"),                       # today 11:59 pm
    item("Essay draft on the causes of the first world war", "2026-11-11T13:10:00Z", course="History"),  # tomorrow 8:10 am
    item("Calc set 4", "2026-11-12T20:00:00Z", course="Math"),                       # Thursday
    item("Spanish vocab", "2026-11-13T12:00:00Z", course="Spanish"),                 # Friday
    item("Poster", "2026-11-25T12:00:00Z"),                                          # beyond the week
    item("Open reading", None),
]}


def say(key, feed=FEED, now=Q.NOW, **over):
    return isc.compose(key, feed, now, Q.cfg(isc, **over))


@pytest.mark.parametrize("text,key", [
    ("what's due today", "today"), ("what is due today", "today"), ("what's due", "today"), ("what do i have due today", "today"),
    ("what do i have due", "today"), ("do i have anything due today", "today"), ("do i have anything due", "today"),
    ("anything due today", "today"), ("anything due", "today"), ("is there anything due today", "today"),
    ("what assignments are due today", "today"), ("what homework is due today", "today"), ("any homework due today", "today"),
    ("any assignments due", "today"), ("what homework do i have", "today"), ("what homework do i have today", "today"),
    ("what homework do i have tonight", "today"), ("what assignments do i have due today", "today"),
    ("do i have homework today", "today"), ("do i have any homework", "today"), ("do i have any assignments due today", "today"),
    ("what's due tonight", "today"), ("homework today", "today"), ("assignments due today", "today"),
    ("what's due tomorrow", "tomorrow"), ("what do i have due tomorrow", "tomorrow"), ("anything due tomorrow", "tomorrow"),
    ("do i have any homework tomorrow", "tomorrow"), ("what homework do i have tomorrow", "tomorrow"),
    ("what assignments are due tomorrow", "tomorrow"), ("homework tomorrow", "tomorrow"),
    ("what's due this week", "week"), ("what do i have due this week", "week"), ("anything due this week", "week"),
    ("what homework do i have this week", "week"), ("what's due in the next seven days", "week"),
    ("what's due over the next 7 days", "week"), ("what assignments are due for the week", "week"),
    ("what's missing", "missing"), ("what is missing", "missing"), ("what am i missing", "missing"),
    ("do i have anything missing", "missing"), ("is there anything missing", "missing"), ("any missing assignments", "missing"),
    ("do i have any missing work", "missing"), ("what assignments am i missing", "missing"), ("what homework is missing", "missing"),
    ("what's overdue", "missing"), ("anything overdue", "missing"), ("anything late", "missing"), ("what's missing from school", "missing"),
    ("how many assignments am i missing", "missing"),
    ("what's due on friday", None), ("what's due next week", None), ("what's due monday", None), ("what is due for chemistry", None),
    ("what's due in two days", None), ("when is the physics homework due", None), ("when is my chemistry test", None),
    ("what's my grade", None), ("what do i have today", None), ("what do i have tomorrow", None), ("do i have a test tomorrow", None),
    ("do i have school tomorrow", None), ("help me with my homework", None), ("is the homework hard", None),
    ("what's missing from the fridge", None), ("what am i missing from my bag", None), ("what's due for the printer", None),
    ("what's on my calendar today", None), ("what's playing", None), ("what's the weather", None),
])
def test_phrasings(text, key):
    assert qc.phrase_match(text, isc.DEFAULT_PATTERNS) == key


def test_answers_for_the_realistic_feed():
    assert say("today") == "One thing due today: Projectiles Review Sheets at 11:59 pm."
    assert say("tomorrow") == "One thing due tomorrow: Essay draft on the causes at 8:10 am."
    assert say("week") == ("Four due this week, including Projectiles Review Sheets today, Essay draft on the causes "
                           "tomorrow, and Calc set 4 Thursday.")
    assert say("missing") == "Two missing: Reading Quiz Chapter 9 and Lab 4 Report."


def test_missing_lists_the_oldest_first_whatever_the_feed_order():
    assert isc.compose("missing", dict(FEED, items=list(reversed(FEED["items"]))), Q.NOW, Q.cfg(isc)) == say("missing")


def test_counts_in_words_and_the_including_form():
    many = {"updated_at": UPDATED, "items": [item(f"Worksheet {n}", f"2026-11-11T{n:02d}:00:00Z") for n in range(5, 12)]}
    # 7 items from 05:00Z to 11:00Z: midnight to 6 am on the 11th in New York, so all tomorrow
    tom = isc.compose("tomorrow", many, Q.NOW, Q.cfg(isc))
    assert tom.startswith("Seven due tomorrow, including Worksheet 5 at midnight")   # 05:00Z is 12 am in New York
    assert Q.words(tom) <= 25
    two = {"updated_at": UPDATED, "items": [item("A", "2026-11-11T04:00:00Z"), item("B", "2026-11-11T04:30:00Z")]}
    assert isc.compose("today", two, Q.NOW, Q.cfg(isc)) == "Two due today: A at 11 pm and B at 11:30 pm."
    three = {"updated_at": UPDATED, "items": two["items"] + [item("C", "2026-11-11T04:59:00Z")]}
    assert isc.compose("today", three, Q.NOW, Q.cfg(isc)) == "Three due today: A at 11 pm, B at 11:30 pm, and C at 11:59 pm."


def test_nothing_due_says_so_and_names_what_is_next():
    only_later = {"updated_at": UPDATED, "items": [item("Calc set 4", "2026-11-12T20:00:00Z")]}
    assert say("today", only_later) == "Nothing is due today. Next is Calc set 4, Thursday."
    assert say("today", {"updated_at": UPDATED, "items": [item("Soon", "2026-11-11T13:00:00Z")]}) == (
        "Nothing is due today. Next is Soon, tomorrow.")
    assert say("tomorrow", only_later) == "Nothing is due tomorrow. Next is Calc set 4, Thursday."
    assert say("week", {"updated_at": UPDATED, "items": []}) == "Nothing is due this week."
    assert say("today", {"updated_at": UPDATED, "items": []}) == "Nothing is due today."
    assert say("missing", {"updated_at": UPDATED, "items": []}) == "You have nothing missing."
    assert say("today", {"updated_at": UPDATED, "items": [item("Old", "2026-11-01T04:59:00Z")]}) == "Nothing is due today."


def test_a_past_due_assigned_item_is_neither_today_nor_next():
    assert say("today", {"updated_at": UPDATED, "items": [item("Yesterday", "2026-11-10T04:59:00Z")]}) == "Nothing is due today."


def test_local_day_arithmetic_not_utc():
    """Seven pm in New York is already tomorrow in UTC; 'today' must still mean the owner's day."""
    late = Q.NOW.replace(hour=22, minute=30)       # 03:30 UTC on the 11th
    feed = {"updated_at": "2026-11-11T02:00:00.000Z", "items": [item("Tonight", "2026-11-11T04:59:00Z")]}
    assert isc.compose("today", feed, late, Q.cfg(isc)) == "One thing due today: Tonight at 11:59 pm."
    assert isc.compose("tomorrow", feed, late, Q.cfg(isc)) == "Nothing is due tomorrow."


def test_stale_never_posted_and_broken_lists_fall_through():
    assert say("today", dict(FEED, updated_at=None)) is None
    assert say("today", dict(FEED, updated_at="soon")) is None
    assert say("today", {"items": []}) is None
    assert say("today", FEED, now=Q.NOW + timedelta(hours=6)) is not None            # 11 h old at 1 am... check below
    assert say("today", FEED, now=Q.NOW + timedelta(hours=16)) is None               # older than 10 hours
    assert say("today", FEED, now=Q.NOW + timedelta(hours=16), stale_hours=24) is not None
    assert say("today", {"updated_at": UPDATED, "items": "junk"}) is not None        # junk items count as none
    assert say("today", {"updated_at": UPDATED, "items": [None, 5, {"title": ""}, {"nope": 1}]}) == "Nothing is due today."


def test_items_with_a_bad_due_are_skipped_not_fatal():
    feed = {"updated_at": UPDATED, "items": [item("Bad", "tomorrowish"), item("Good", "2026-11-11T04:59:00Z")]}
    assert isc.compose("today", feed, Q.NOW, Q.cfg(isc)) == "One thing due today: Good at 11:59 pm."


def test_long_titles_still_fit_the_word_budget():
    longs = {"updated_at": UPDATED, "items": [item("Extraordinarily long assignment title number " + str(n) + " with many more words",
                                                  f"2026-11-11T0{n}:00:00Z") for n in range(1, 6)]}
    for key in ("today", "tomorrow", "week", "missing"):
        feed = dict(longs, items=[dict(i, state="missing") if key == "missing" else i for i in longs["items"]])
        text = isc.compose(key, feed, Q.NOW, Q.cfg(isc))
        assert text and Q.words(text) <= 25, (key, text)
    tight = isc.compose("today", longs, Q.NOW, Q.cfg(isc, max_words=12))
    assert Q.words(tight) <= 14                   # the shortest form is the floor: never silent, never rambling


def test_titles_are_spoken_clean():
    feed = {"updated_at": UPDATED, "items": [item("[Honors] HW 3.2 *** Kinematics (due Friday)", "2026-11-11T04:59:00Z")]}
    assert isc.compose("today", feed, Q.NOW, Q.cfg(isc)) == "One thing due today: HW 3.2 Kinematics at 11:59 pm."


def _started(tmp_path, server, **over):
    return H.started("intent.school", url=server.url, token_file=Q.token_file(tmp_path), **over)


def test_service_end_to_end_with_the_quick_route(tmp_path):
    s = Q.quick_server({"school": FEED})
    try:
        plugin, ctx, cfg = _started(tmp_path, s)
        i = ctx.get("intent")
        i.clock = lambda: Q.NOW.astimezone(timezone.utc)
        assert i.match("What's due today?") == "today"
        assert i.handle("today") == "One thing due today: Projectiles Review Sheets at 11:59 pm."
        assert i.match("anything missing") is None            # not a phrasing: "anything missing" is left to Hermes
        assert i.match("what am i missing") == "missing" and i.handle("missing").startswith("Two missing")
        assert {c["path"] for c in s.calls} == {"/api/hooks/quick/school"}
    finally:
        s.stop()


@pytest.mark.parametrize("status", [401, 503, 500])
def test_unreachable_or_refused_goes_to_hermes(tmp_path, status):
    s = Q.quick_server({"school": FEED}, status=status)
    try:
        plugin, ctx, cfg = _started(tmp_path, s)
        assert ctx.get("intent").match("what's due today") is None
    finally:
        s.stop()
    plugin, ctx, cfg = H.started("intent.school", url=H.refused_url(), token_file=Q.token_file(tmp_path))
    assert ctx.get("intent").match("what's due today") is None


def test_live_knobs_apply():
    plugin, ctx, cfg = H.started("intent.school")
    i = ctx.get("intent")
    pats = dict(isc.DEFAULT_PATTERNS, today=r"gimme homework")
    H.assert_live_knobs(SPEC, plugin, {
        "priority": (7, lambda p: i.priority), "url": ("http://127.0.0.1:1/", lambda p: i.client.url, "http://127.0.0.1:1"),
        "token_file": ("/t/quick", lambda p: i.client.token_file), "timeout_s": (1.5, lambda p: i.client.timeout_s),
        "timezone": ("Asia/Tokyo", lambda p: i.cfg.timezone), "patterns": (pats, lambda p: i.patterns),
        "max_words": (30, lambda p: i.cfg.max_words), "stale_hours": (5.0, lambda p: i.cfg.stale_hours)})


def test_spec_shape():
    assert SPEC.name == "intent.school" and SPEC.default_enabled is False and isc.LEGACY_CLASSES == ()
