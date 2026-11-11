"""intent.time: "what time is it" and "what is the date", answered from the Pi's clock with no network at all.

FIXED: the answer wording; the clock is the system clock in the `timezone` knob.
"""

from .quick_common import clock_words, make_spec, ordinal_word

LEGACY_CLASSES = ()

DEFAULT_PATTERNS = {
    "time": r"(what'?s |what is )?the (current )?time( is it)?( now| right now)?|what time (is it|it is)( now| right now| currently)?"
            r"|current time|time now",
    "date": r"((what'?s|what is) (the |today'?s )?|the |today'?s )(date|day)( is it)?( today)?|what('?s| is) today"
            r"|what (day|date) is it( today)?|what day of the week is it( today)?|today'?s date",
}

# Phrasings that must hit, and near-misses that must go to Hermes (a pattern set that breaks a row is refused).
CASES = [
    ("what time is it", "time"), ("What time is it?", "time"), ("what's the time", "time"), ("what is the time", "time"),
    ("what is the time right now", "time"), ("tell me the time", "time"), ("what time is it right now", "time"),
    ("can you tell me what time it is", "time"), ("the time please", "time"), ("current time", "time"),
    ("what's the date", "date"), ("what is the date today", "date"), ("what day is it", "date"),
    ("What day is it today?", "date"), ("what's today's date", "date"), ("what's today", "date"),
    ("what day of the week is it", "date"), ("what date is it", "date"),
    ("what time is it in Tokyo", None), ("what time does school start", None), ("what time is the game", None),
    ("what time is my next class", None), ("what's the date of the exam", None), ("what day is the test", None),
    ("what's the time in london", None), ("set a timer for five minutes", None), ("what's today's schedule", None),
    ("what's playing", None), ("play the next song", None), ("what's on my calendar today", None), ("", None),
]


def compose(key, data, now, cfg):
    if key == "time":
        return f"It is {clock_words(now)}."
    return f"Today is {now.strftime('%A')}, {now.strftime('%B')} {ordinal_word(now.day)}."


SPEC = make_spec(
    name="intent.time", topic="", summary="Local answers to the time and the date, with no network.",
    default_patterns=DEFAULT_PATTERNS, cases=CASES, compose=compose, test_file="wall/tests/plugins/test_lp_intent_time.py")
