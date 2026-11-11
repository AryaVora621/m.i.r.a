"""intent.printers: whether the printers are running and how long is left, from Agent OS's live printer status.

FIXED: states are Klipper's (printing, paused, standby, complete, cancelled, error) plus Agent OS's own offline; a
printer that is disabled or not configured is not mentioned. When no printer is readable at all the question goes to
Hermes. Early in a print Agent OS has no estimate (under 2 percent done), and the answer says so.
"""

from .quick_common import MAX_WORDS_KNOB, count_words, duration_words, make_spec, number_word, short_title

LEGACY_CLASSES = ()

_P = "(print|prints|printer|printers)"

DEFAULT_PATTERNS = {
    "status": r"are (the|my) printers (running|printing|busy|going|done|finished)|is (the|my) printer (running|printing|busy|done|finished)"
              r"|is the print (done|finished|going|running)|are (the|my) prints (done|finished|going)"
              r"|how('?s| is| are) (the|my) (print|prints)( going)?|(printer|print) status"
              r"|what (are|is) (the|my) (printers|printer) doing|what'?s the printer doing"
              r"|how (long|much time) (is )?(left|remaining) (on|for) (the |my )?" + _P +
              r"|how long (until|till) (the |my )?" + _P + r" (is |are )?(done|finished|finish|finishes)"
              r"|when (will|is) (the |my )?" + _P + r" (be )?(done|finished)",
}

CASES = [
    ("are the printers running", "status"), ("Are the printers running?", "status"), ("are my printers printing", "status"),
    ("is the printer running", "status"), ("is the printer done", "status"), ("is the print finished", "status"),
    ("how's the print going", "status"), ("how is the print", "status"), ("how long is left on the print", "status"),
    ("how much time is left on the print", "status"), ("how long until the print is done", "status"),
    ("how long till the printer finishes", "status"), ("when will the print be done", "status"),
    ("printer status", "status"), ("what are the printers doing", "status"), ("tell me how long is left on the print", "status"),
    ("start a print", None), ("cancel the print", None), ("pause the printer", None), ("how long does a benchy take to print", None),
    ("how do i print a file", None), ("is the printer out of filament", None), ("why did the print fail", None),
    ("what filament is loaded", None), ("what's the bed temperature", None), ("print the document", None),
    ("what's playing", None), ("what's on my calendar today", None), ("pause", None), ("stop", None),
]

_SAYS = {"paused": "paused", "error": "reporting an error", "complete": "done printing", "standby": "idle",
         "cancelled": "idle"}


def _name(p):
    return short_title(p.get("name") or p.get("id") or "A printer", 4) or "A printer"


def _readable(p):
    return isinstance(p, dict) and p.get("enabled", True) and p.get("state") not in (None, "not_configured")


def _one(p, with_pct):
    state = p.get("state")
    if not p.get("connected", True) or state == "offline":
        return f"{_name(p)} is offline"
    if state != "printing":
        return f"{_name(p)} is {_SAYS.get(state, 'idle')}"
    eta, prog = p.get("eta_seconds"), p.get("progress")
    parts = [f"{_name(p)} is printing"]
    if with_pct and isinstance(prog, (int, float)) and not isinstance(prog, bool) and prog > 0:
        parts.append(f"{int(round(prog * 100))} percent done")
    if isinstance(eta, (int, float)) and not isinstance(eta, bool):
        parts.append(f"about {duration_words(eta)} left")
    else:
        parts.append("no time estimate yet")
    return ", ".join(parts)


def compose(key, data, now, cfg):
    rows = [p for p in data.get("printers") or [] if _readable(p)]
    if not rows:
        return None
    printing = [p for p in rows if p.get("state") == "printing" and p.get("connected", True)]
    others = [p for p in rows if p not in printing]
    max_words = cfg.max_words
    if not printing:
        if len(rows) == 1:
            return _one(rows[0], False) + "."
        states = {(_SAYS.get(p.get("state"), "idle") if p.get("connected", True) and p.get("state") != "offline" else "offline")
                  for p in rows}
        if states == {"idle"}:
            return f"All {number_word(len(rows))} printers are idle." if len(rows) > 2 else "Both printers are idle."
        return "Nothing is printing right now. " + " ".join(_one(p, False) + "." for p in others)
    for with_pct in (True, False):
        text = " ".join(_one(p, with_pct) + "." for p in printing)
        if others:
            idle = [p for p in others if _one(p, False).endswith("idle")]
            if len(idle) == len(others):
                text += " The other printer is idle." if len(others) == 1 else " The others are idle."
            else:
                text += " " + " ".join(_one(p, False) + "." for p in others)
        if count_words(text) <= max_words:
            return text
    return _one(printing[0], False) + (f", plus {number_word(len(rows) - 1)} more." if len(rows) > 1 else ".")


SPEC = make_spec(
    name="intent.printers", topic="printers", summary="Local answers to whether the printers are running and the time left.",
    default_patterns=DEFAULT_PATTERNS, cases=CASES, compose=compose, timeout_s=4.0,
    test_file="wall/tests/plugins/test_lp_intent_printers.py", extra_knobs=(MAX_WORDS_KNOB,))
