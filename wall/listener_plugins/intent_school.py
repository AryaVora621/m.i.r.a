"""intent.school: what is due today, tomorrow or this week, and what is missing, from Agent OS's Classroom list.

The wording logic is the wall MCP's school_view (pi/wall-mcp/server.py in agentic-os) re-expressed for one short
spoken line: the same scopes, the same rule that turned-in work is not in the feed, local-day arithmetic in the
`timezone` knob, and the same "stale after 10 hours" limit (the Classroom job posts at 7:00, 15:30 and 21:00).

FIXED: a list that was never posted or is older than `stale_hours` goes to Hermes, which says how old it is.
"""

from datetime import timezone

from mirakit.plugin import Knob

from .quick_common import (MAX_WORDS_KNOB, clock_words, count_words, join_and, make_spec, number_word, parse_iso,
                           short_title)

LEGACY_CLASSES = ()

_WHAT = ("(what'?s|what is|what do i have|do i have anything|do i have any (homework|assignments|work)|is there anything|"
         "anything|any (homework|assignments|work)|what (homework|assignments|work) (is|are))")
_NOUN = "(homework|assignments|work)"


def _scope(day, optional):
    d = f"( {day})?" if optional else f" {day}"
    return (f"{_WHAT} due{d}|what {_NOUN} do i have( due)?{d}|do i have (any )?{_NOUN}( due)?{d}"
            f"|(what )?(homework|assignments) (do i have |is |are )?(due )?{day}")


DEFAULT_PATTERNS = {
    "missing": r"what'?s missing|what is missing|what am i missing|what (assignments|work|homework) am i missing"
               r"|(do i have|is there) anything missing|(do i have )?(any )?(missing|overdue|late) (assignments|homework|work)"
               r"|what'?s overdue|anything overdue|anything late|what (assignments are|work is|homework is) missing"
               r"|what'?s missing (from|in) school|what do i have missing|how many (assignments|things) am i missing",
    "week": (f"{_WHAT} due (this week|in the next (7|seven) days|over the next (7|seven) days|for the week)"
             f"|what {_NOUN} do i have( due)? this week"),
    "tomorrow": _scope("tomorrow", optional=False),
    "today": _scope("(today|tonight)", optional=True),
}

CASES = [
    ("what's due today", "today"), ("What is due today?", "today"), ("what's due", "today"),
    ("what do i have due today", "today"), ("do i have anything due today", "today"), ("anything due today", "today"),
    ("what assignments are due today", "today"), ("what homework is due today", "today"), ("any homework due today", "today"),
    ("what homework do i have", "today"), ("what homework do i have tonight", "today"), ("do i have homework today", "today"),
    ("what's due tonight", "today"), ("tell me what's due today", "today"),
    ("what's due tomorrow", "tomorrow"), ("what do i have due tomorrow", "tomorrow"), ("do i have any homework tomorrow", "tomorrow"),
    ("anything due tomorrow", "tomorrow"), ("what homework do i have tomorrow", "tomorrow"),
    ("what's due this week", "week"), ("what do i have due this week", "week"), ("anything due this week", "week"),
    ("what homework do i have this week", "week"), ("what's due in the next 7 days", "week"),
    ("what's missing", "missing"), ("what am i missing", "missing"), ("do i have anything missing", "missing"),
    ("any missing assignments", "missing"), ("what assignments am i missing", "missing"), ("what's overdue", "missing"),
    ("what work is missing", "missing"), ("how many assignments am i missing", "missing"),
    ("what's due on friday", None), ("what's due next week", None), ("when is my physics test", None),
    ("when is the chemistry homework due", None), ("what's my grade in physics", None), ("what is due for chemistry", None),
    ("what's due in two days", None), ("what's missing from my backpack", None), ("play the next assignment", None),
    ("what do i have today", None), ("what's on my calendar today", None), ("what's playing", None),
    ("is the homework hard", None), ("help me with my homework", None), ("do i have a test tomorrow", None),
]

SCOPE_WORD = {"today": "today", "tomorrow": "tomorrow", "week": "this week"}


def _items(feed):
    return [i for i in feed.get("items") or [] if isinstance(i, dict) and i.get("title")]


def _when(due, now, scope):
    """Plain words for one due time in local time: the day is in the lead for today and tomorrow, so only the clock."""
    d = parse_iso(due)
    if d is None:
        return ""
    d = d.astimezone(now.tzinfo)
    off = (d.date() - now.date()).days
    if scope in ("today", "tomorrow"):
        return f"at {clock_words(d)}"
    if off == 0:
        return "today"
    if off == 1:
        return "tomorrow"
    return d.strftime("%A") if 2 <= off <= 6 else ""


def _offset(item, now):
    d = parse_iso(item.get("due"))
    return None if d is None else (d.astimezone(now.tzinfo).date() - now.date()).days


def _line(base, picked, now, scope, max_words):
    """Largest of three/two/one items, with titles from six words down to three, that fits `max_words`."""
    n = len(picked)
    for k in (3, 2, 1):
        if k > n:
            continue
        for tw in (6, 5, 4, 3):
            rows = []
            for i in picked[:k]:
                t = short_title(i["title"], tw)
                when = _when(i.get("due"), now, scope)
                rows.append(f"{t} {when}".strip())
            text = f"{base}{', including ' if k < n else ': '}{join_and(rows)}."
            if count_words(text) <= max_words:
                return text
    return text  # even the shortest form is over: still the best true thing to say


def compose(key, feed, now, cfg):
    updated = parse_iso(feed.get("updated_at"))
    if updated is None or (now.astimezone(timezone.utc) - updated).total_seconds() > cfg.stale_hours * 3600:
        return None
    items = _items(feed)
    max_words = cfg.max_words
    if key == "missing":
        picked = sorted((i for i in items if i.get("state") == "missing"), key=lambda i: (i.get("due") is None, i.get("due") or ""))
        if not picked:
            return "You have nothing missing."
        n = len(picked)
        return _line(f"{number_word(n).capitalize()} missing" if n > 1 else "One missing", picked, now, "missing",
                     max_words)
    scope = key
    live = [i for i in items if i.get("state") != "missing" and _offset(i, now) is not None]

    def keep(i):
        off = _offset(i, now)
        return off == 0 if scope == "today" else off == 1 if scope == "tomorrow" else 0 <= off <= 6

    picked = sorted((i for i in live if keep(i)), key=lambda i: i["due"])
    word = SCOPE_WORD[scope]
    if not picked:
        text = f"Nothing is due {word}."
        if scope != "week":
            after = 0 if scope == "today" else 1      # "next" is what comes after the day asked about
            ahead = sorted((i for i in live if _offset(i, now) > after), key=lambda i: i["due"])
            if ahead:
                nxt = ahead[0]
                when = _when(nxt["due"], now, "week") or "soon"
                alt = f"{text} Next is {short_title(nxt['title'], 4)}, {when}."
                if count_words(alt) <= max_words:
                    return alt
        return text
    n = len(picked)
    base = f"One thing due {word}" if n == 1 else f"{number_word(n).capitalize()} due {word}"
    return _line(base, picked, now, scope, max_words)


SPEC = make_spec(
    name="intent.school", topic="school", summary="Local answers to what is due and what is missing, from Agent OS.",
    default_patterns=DEFAULT_PATTERNS, cases=CASES, compose=compose,
    test_file="wall/tests/plugins/test_lp_intent_school.py",
    extra_knobs=(
        MAX_WORDS_KNOB,
        Knob("stale_hours", "float", 10.0, "A school list older than this goes to Hermes instead of being spoken.",
             unit="h", min=1, max=72),
    ))
