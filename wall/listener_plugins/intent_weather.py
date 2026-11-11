"""intent.weather: today's and tomorrow's weather from Agent OS's cached forecast (the one the wall shows).

FIXED: the answer wording; "falling through" (to Hermes) when the reading is missing, malformed or older than 4 hours.
"""

from datetime import timezone

from .quick_common import make_spec, parse_iso

LEGACY_CLASSES = ()
STALE_S = 4 * 3600

DEFAULT_PATTERNS = {
    "today": r"((what'?s|what is) )?the (weather|forecast)( like)?( today| outside| right now| now| like today| like outside)?"
             r"|how'?s the weather( today| outside| right now| now)?|how is the weather( today| outside| right now| now)?"
             r"|(what'?s|what is) it like outside( today| right now)?|weather( today| right now| outside)?"
             r"|(what'?s|what is) today'?s (weather|forecast)|how (hot|cold|warm) is it( outside| right now| now)?"
             r"|(what'?s|what is) the temperature( outside| right now| now)?",
    "rain": r"(will|is) it (going to )?(rain|snow)( today| tonight)?",
    "tomorrow": r"(what'?s|what is) the (weather|forecast)( going to be)?( like)? tomorrow|how'?s the weather tomorrow"
                r"|weather tomorrow|forecast for tomorrow|(what'?s|what is) tomorrow'?s (weather|forecast)"
                r"|(will|is) it (going to )?(rain|snow) tomorrow",
}

CASES = [
    ("what's the weather", "today"), ("What's the weather like?", "today"), ("what's the weather today", "today"),
    ("how's the weather", "today"), ("weather", "today"), ("what is the weather like outside", "today"),
    ("how cold is it", "today"), ("how hot is it outside", "today"), ("what's the temperature", "today"),
    ("tell me the weather", "today"), ("will it rain", "rain"), ("is it going to rain today", "rain"),
    ("will it snow tonight", "rain"), ("what's the weather tomorrow", "tomorrow"), ("how's the weather tomorrow", "tomorrow"),
    ("what's tomorrow's forecast", "tomorrow"), ("will it rain tomorrow", "tomorrow"),
    ("what's the weather in London", None), ("what's the weather this weekend", None), ("what's the weather on friday", None),
    ("do i need a jacket", None), ("do i need an umbrella", None), ("is it raining", None), ("what's the weather going to be this week", None),
    ("how's the weather in new york", None), ("play weather report", None), ("what's playing", None),
    ("what's on my calendar today", None), ("what time is it", None),
]


def _cond(summary):
    s = str(summary or "").strip().lower()
    return "" if not s or s == "unknown" else s


def _chance(pct, snow):
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return ""
    what = "snow" if snow else "rain"
    n = int(round(pct))
    article = "an" if n in (8, 11, 18) or 80 <= n <= 89 else "a"    # "an 8", "an 11", "an 85"
    return f"no {what} expected" if n <= 5 else f"{article} {n} percent chance of {what}"


def _fresh(data, now):
    at = parse_iso(data.get("updated_at"))
    return at is None or (now - at).total_seconds() <= STALE_S


def compose(key, data, now, cfg):
    if not _fresh(data, now.astimezone(timezone.utc)):
        return None
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if key == "tomorrow":
        t = data.get("tomorrow")
        if not isinstance(t, dict) or num(t.get("high_f")) is None or num(t.get("low_f")) is None:
            return None
        cond = _cond(t.get("summary"))
        chance = _chance(t.get("precip_pct"), cond == "snow")
        parts = [f"high of {round(t['high_f'])}", f"low of {round(t['low_f'])}"] + ([chance] if chance else [])
        head = f"Tomorrow: {cond}, " if cond else "Tomorrow: "   # a noun (rain) and an adjective (clear) both fit here
        return head + ", ".join(parts[:-1]) + (f", and {parts[-1]}." if len(parts) > 1 else f"{parts[0]}.")
    hi, lo = num(data.get("high_f")), num(data.get("low_f"))
    if key == "rain":
        chance = _chance(data.get("precip_pct"), _cond(data.get("day_summary")) == "snow")
        if not chance or hi is None:
            return None
        return f"Today, {chance}, with a high of {round(hi)}."
    temp = num(data.get("temp_f"))
    if temp is None or hi is None or lo is None:
        return None
    cond = _cond(data.get("summary"))
    now_part = f"It is {round(temp)} degrees, {cond}." if cond else f"It is {round(temp)} degrees."
    chance = _chance(data.get("precip_pct"), _cond(data.get("day_summary")) == "snow")
    tail = f"High of {round(hi)}, low of {round(lo)}" + (f", and {chance}." if chance else ".")
    return f"{now_part} {tail}"


SPEC = make_spec(
    name="intent.weather", topic="weather", summary="Local answers to today's and tomorrow's weather from Agent OS.",
    default_patterns=DEFAULT_PATTERNS, cases=CASES, compose=compose,
    test_file="wall/tests/plugins/test_lp_intent_weather.py")
