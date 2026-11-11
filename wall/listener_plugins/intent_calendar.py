"""intent.calendar: what is left on today's calendar, what is on tomorrow's, and what comes next.

The data is Agent OS's Google feed: events from the moment of the last fetch to 36 hours later, at most 8 of them,
dropped entirely when older than 30 minutes. So the answer is only given when the feed provably covers the day asked
about; otherwise the question goes to Hermes, which can read the calendar itself.

FIXED: FEED_HOURS and FEED_MAX are the feed helper's numbers (pi/google-feed/google_feed.py).
"""

from datetime import datetime, time as dtime, timedelta, timezone

from .quick_common import (MAX_WORDS_KNOB, clock_words, count_words, join_and, make_spec, number_word, parse_iso,
                           short_title)

LEGACY_CLASSES = ()
FEED_HOURS = 36
FEED_MAX = 8

def _ask(d, possessive):
    return (f"(what'?s|what is) on my calendar{d}|what do i have on my calendar{d}|do i have anything on my calendar{d}"
            f"|what (meetings|events|appointments) do i have{d}|do i have any (meetings|events|appointments){d}"
            f"|what'?s on the calendar{d}|what'?s on {possessive}'?s? calendar")


DEFAULT_PATTERNS = {
    "next": r"what'?s next on my calendar|what'?s my next (event|meeting|appointment)|when is my next (event|meeting|appointment)"
            r"|what is my next (event|meeting|appointment)",
    "tomorrow": _ask(" tomorrow", "tomorrow"),
    "today": _ask("( today| for today)?", "today"),
}

CASES = [
    ("what's on my calendar today", "today"), ("What's on my calendar today?", "today"), ("what's on my calendar", "today"),
    ("what is on my calendar today", "today"), ("what do i have on my calendar today", "today"),
    ("do i have anything on my calendar", "today"), ("what meetings do i have today", "today"),
    ("do i have any meetings today", "today"), ("what events do i have today", "today"), ("tell me what's on my calendar today", "today"),
    ("what's on my calendar tomorrow", "tomorrow"), ("what meetings do i have tomorrow", "tomorrow"),
    ("do i have any events tomorrow", "tomorrow"), ("what do i have on my calendar tomorrow", "tomorrow"),
    ("what's my next meeting", "next"), ("when is my next event", "next"), ("what's next on my calendar", "next"),
    ("what's on my calendar friday", None), ("what's on my calendar this week", None), ("what's on my calendar next week", None),
    ("add a meeting to my calendar", None), ("move my meeting to friday", None), ("what's my schedule", None),
    ("what do i have today", None), ("what's due today", None), ("when is my physics test", None),
    ("what's playing", None), ("what is on my calendar on friday", None), ("cancel my meeting", None),
]


def _local_range(day, tz):
    start = datetime.combine(day, dtime.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _event_span(e, tz):
    """(start, end, all_day) in local time, or None for an event that cannot be read. A bare date is an all-day event
    whose end date is exclusive."""
    s_raw, e_raw = e.get("start"), e.get("end") or e.get("start")
    start, end = parse_iso(s_raw), parse_iso(e_raw)
    if start is None or end is None:
        return None
    all_day = bool(e.get("all_day")) or (isinstance(s_raw, str) and len(s_raw) == 10)
    if all_day:  # a date has no zone: it belongs to the local day it names
        s_d = datetime.fromisoformat(str(s_raw)[:10]).date()
        e_d = datetime.fromisoformat(str(e_raw)[:10]).date()
        return (datetime.combine(s_d, dtime.min, tzinfo=tz), datetime.combine(max(e_d, s_d + timedelta(days=1)), dtime.min, tzinfo=tz), True)
    return start.astimezone(tz), end.astimezone(tz), False


def _covers(feed_start, events, day_end):
    """True when the feed can be trusted to hold everything that starts before `day_end`: the window reaches it and
    the 8-event cap did not cut the list short before it."""
    if feed_start + timedelta(hours=FEED_HOURS) < day_end:
        return False
    if len(events) >= FEED_MAX:
        last = max((e["_start"] for e in events), default=None)
        return last is not None and last >= day_end
    return True


def _item(e, now, title_words):
    title = short_title(e["title"], title_words)
    if e["_all_day"]:
        return f"{title} all day"
    start, end = e["_start"], e["_end"]
    return f"{title} until {clock_words(end)}" if start <= now else f"{title} at {clock_words(start)}"


def compose(key, feed, now, cfg):
    tz = now.tzinfo
    fed = parse_iso(feed.get("updated_at"))
    raw = feed.get("events")
    if fed is None or not isinstance(raw, list):
        return None
    events = []
    for e in raw:
        if not isinstance(e, dict) or not e.get("title"):
            if isinstance(e, dict):  # an untitled event still counts toward coverage, it just cannot be named
                span = _event_span(e, tz)
                if span:
                    events.append({"title": "", "_start": span[0], "_end": span[1], "_all_day": span[2]})
            continue
        span = _event_span(e, tz)
        if span is None:
            return None  # one event we cannot read means the list cannot be trusted
        events.append({"title": e["title"], "_start": span[0], "_end": span[1], "_all_day": span[2]})
    feed_start = fed.astimezone(tz)
    max_words = cfg.max_words
    if key == "next":
        upcoming = sorted((e for e in events if not e["_all_day"] and e["_start"] > now and e["title"]),
                          key=lambda e: e["_start"])
        if upcoming:
            e = upcoming[0]
            day = "" if e["_start"].date() == now.date() else " tomorrow" if (e["_start"].date() - now.date()).days == 1 else \
                f" {e['_start'].strftime('%A')}"
            return f"Next is {short_title(e['title'], 6)}{day} at {clock_words(e['_start'])}."
        if not events and feed_start + timedelta(hours=FEED_HOURS) >= now + timedelta(hours=24):
            return "Nothing on your calendar for the next day."
        return None
    day = now.date() + timedelta(days=1 if key == "tomorrow" else 0)
    lo, hi = _local_range(day, tz)
    if not _covers(feed_start, events, hi):
        return None
    mine = [e for e in events if e["_start"] < hi and e["_end"] > max(lo, now if key == "today" else lo)]
    if any(not e["title"] for e in mine):
        return None
    mine.sort(key=lambda e: (e["_all_day"], e["_start"]))
    word = "today" if key == "today" else "tomorrow"
    if not mine:
        return "Nothing else on your calendar today." if key == "today" else "Nothing on your calendar tomorrow."
    n = len(mine)
    where = "left today" if key == "today" else "tomorrow"
    lead = f"{number_word(n).capitalize()} things {where}" if n > 1 else f"One thing {where}"
    for k in (3, 2, 1):
        if k > n:
            continue
        for tw in (6, 4, 3):
            rows = [_item(e, now, tw) for e in mine[:k]]
            text = f"{lead}{', including' if k < n else ':'} {join_and(rows)}."
            if count_words(text) <= max_words:
                return text
    return text


SPEC = make_spec(
    name="intent.calendar", topic="calendar", summary="Local answers to today's and tomorrow's calendar from Agent OS.",
    default_patterns=DEFAULT_PATTERNS, cases=CASES, compose=compose,
    test_file="wall/tests/plugins/test_lp_intent_calendar.py", extra_knobs=(MAX_WORDS_KNOB,))
