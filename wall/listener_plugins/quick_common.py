"""Shared parts of the quick-answer intents (intent.time, .weather, .school, .calendar, .printers).

These intents answer the commonest spoken questions from data already on the Pi, so they skip the Hermes round trip
(5 to 11 s per model call). They follow the music intent's contract, with one difference that is on purpose: a hit in
the turn loop is final (a handle() error speaks fail_reply and Hermes is never asked), so the work happens inside
match(). match() returns an intent only when it holds a finished answer; a phrase it does not clearly match, a fetch
that failed or timed out, stale or empty data, anything it cannot say in one short breath returns None and the turn
goes to Hermes exactly as it does today. match() never raises (the loop calls it outside its try block).

FIXED: fullmatch semantics inside an optional polite lead-in and tail on the lower-cased text with punctuation
removed; the route GET <url>/api/hooks/quick/<topic> with a bearer token read per call; answers are built from
the spoken-word helpers below, never from free text of the feed except titles trimmed to a few words.
"""

import json
import logging
import re
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

from .common import expand_path, host_is_loopback, read_secret

MAX_BODY = 256 * 1024
LOOPBACK_URL = "http://127.0.0.1:3456"
DEFAULT_TOKEN_FILE = "~/.config/mira/quick_token"
DEFAULT_TZ = "America/New_York"  # the owner's zone, the same default the wall MCP uses for "today"
FALLBACK_REPLY = "I could not get that."

_LEAD_IN = re.compile(
    r"(?:(?:hey |hi |ok |okay )?(?:mira )?)?(?:please )?"
    r"(?:(?:(?:can|could|would) you (?:please )?(?:tell|give) me|tell me|give me|let me know|do you know|"
    r"i want to know|i need to know|i'd like to know) )?")

# Without a trailing "right now": patterns that take it say so themselves.
_TAIL = r"(?: please| for me| thanks| thank you)?"


def normalize(text):
    t = text.lower().replace("’", "'")
    return " ".join(re.sub(r"[^a-z0-9' ]", " ", t).split())


def phrase_match(text, patterns):
    """The name of the first pattern that matches the whole phrase, else None. Patterns are tried in order."""
    t = normalize(text)
    if not t:
        return None
    stripped = t[_LEAD_IN.match(t).end():]
    for name, p in (patterns.items() if hasattr(patterns, "items") else patterns):
        rx = re.compile(f"(?:{p}){_TAIL}")
        if (stripped and rx.fullmatch(stripped)) or rx.fullmatch(t):
            return name
    return None


# ---------------------------------------------------------------------------------------- spoken-word helpers

_UNITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
          "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty")
_ORDINALS = ("", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
             "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
             "nineteenth", "twentieth", "twenty first", "twenty second", "twenty third", "twenty fourth",
             "twenty fifth", "twenty sixth", "twenty seventh", "twenty eighth", "twenty ninth", "thirtieth",
             "thirty first")


def number_word(n):
    return _UNITS[n] if 0 <= n < len(_UNITS) else str(n)


def ordinal_word(day):
    return _ORDINALS[day]


def clock_words(dt):
    """12:00 is "noon", 0:00 is "midnight", 15:00 is "3 pm", 15:30 is "3:30 pm"."""
    if dt.minute == 0 and dt.hour == 12:
        return "noon"
    if dt.minute == 0 and dt.hour == 0:
        return "midnight"
    h = dt.hour % 12 or 12
    ap = "am" if dt.hour < 12 else "pm"
    return f"{h} {ap}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {ap}"


def duration_words(seconds):
    """About how long, in the words a person uses: "under a minute", "45 minutes", "1 hour and 10 minutes"."""
    mins = int(round(max(0, seconds) / 60.0))
    if mins < 1:
        return "under a minute"
    if mins > 9:    # past ten minutes a person says it in fives
        mins = int(round(mins / 5.0)) * 5
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''}"
    h, m = divmod(mins, 60)
    hours = f"{h} hour{'s' if h != 1 else ''}"
    return hours if m == 0 else f"{hours} and {m} minutes"


def count_words(text):
    return len(text.split())


def short_title(title, words=6):
    """A feed title as it is said: brackets and symbols gone, at most `words` words."""
    t = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", str(title))
    t = re.sub(r"[^A-Za-z0-9' .,&:+/-]", " ", t)
    parts = t.split()
    if len(parts) > words:  # a cut mid-phrase should not end on a word that wants a follower
        parts = parts[:words]
        while len(parts) > 1 and parts[-1].lower().strip(",.:") in _DANGLING:
            parts.pop()
    return " ".join(parts).strip(" -:,.") if parts else ""


_DANGLING = frozenset("a an the of on in to for with and or at by from about as is are".split())


def join_and(parts):
    if len(parts) <= 1:
        return "".join(parts)
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def parse_iso(value):
    """A feed timestamp as an aware datetime; a bare date is midnight UTC. None when it is not a timestamp."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------------- Agent OS client

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback must never ride a proxy variable


class QuickClient:
    """GET /api/hooks/quick/<topic>. Every failure is None: the caller hands the question to Hermes."""

    def __init__(self, url, token_file, timeout_s):
        self.url, self.token_file, self.timeout_s = url.rstrip("/"), token_file, timeout_s

    def get(self, topic):
        token = read_secret(self.token_file)
        if not token:
            return None
        req = urllib.request.Request(f"{self.url}/api/hooks/quick/{topic}", method="GET",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            with _opener.open(req, timeout=self.timeout_s) as r:
                if r.status != 200:
                    return None
                raw = r.read(MAX_BODY + 1)
            data = json.loads(raw) if len(raw) <= MAX_BODY else None
        except Exception:
            return None
        return data if isinstance(data, dict) else None


# ------------------------------------------------------------------------------------------------ the service

class QuickIntent:
    """The `intent` service for one quick topic. `compose(key, data, now, cfg)` returns the spoken text or None."""

    done_reply = FALLBACK_REPLY
    fail_reply = FALLBACK_REPLY
    log_name = "agent-os"

    def __init__(self, name, mark, topic, compose, cfg, client, log):
        self.name, self.mark, self.topic, self.compose, self.client, self.log = name, mark, topic, compose, client, log
        self.hits = self.misses = 0
        self.clock = lambda: datetime.now(timezone.utc)
        self._ready = None
        self.update(cfg)

    def update(self, cfg):
        self.cfg = cfg
        self.priority, self.patterns = cfg.priority, dict(cfg.patterns)

    def match(self, text):
        try:
            key = phrase_match(text, self.patterns)
            if key is None:
                return None
            data = None
            if self.topic:
                data = self.client.get(self.topic)
                if data is None:
                    self.misses += 1
                    self.log.info("quick %s unavailable, asking Hermes", self.topic)
                    return None
            now = self.clock().astimezone(ZoneInfo(self.cfg.timezone))
            answer = self.compose(key, data, now, self.cfg)
        except Exception as e:  # a bug here must cost one fast answer, not the turn
            self.log.warning("quick %s failed: %s", self.topic or self.name, type(e).__name__)
            return None
        if not answer:
            self.misses += 1
            return None
        self.hits += 1
        self._ready = (key, answer)
        return key

    def handle(self, intent):
        ready, self._ready = self._ready, None
        return ready[1] if ready and ready[0] == intent else None


class QuickPlugin(PluginBase):
    def __init__(self, ctx, cfg, spec_name, mark, topic, compose):
        super().__init__(ctx, cfg)
        self._args = (spec_name, mark, topic, compose)

    def _client(self, cfg):
        if "url" not in cfg:
            return None
        return QuickClient(cfg.url, expand_path(cfg.token_file, self.ctx.paths), cfg.timeout_s)

    def start(self):
        name, mark, topic, compose = self._args
        self.intent = QuickIntent(name, mark, topic, compose, self.cfg, self._client(self.cfg), self.ctx.log)
        self.ctx.provide("intent", self.intent)

    def apply(self, changed, cfg):
        self.cfg = cfg
        i = self.intent
        i.update(cfg)
        if i.client is not None:
            i.client.url, i.client.timeout_s = cfg.url.rstrip("/"), cfg.timeout_s
            i.client.token_file = expand_path(cfg.token_file, self.ctx.paths)

    def status(self):
        return {"priority": self.intent.priority, "answered": self.intent.hits, "to_hermes": self.intent.misses}


# ----------------------------------------------------------------------------------------------- spec builder

def make_validate(default_patterns, cases, has_url):
    def validate(values):
        pats = values["patterns"]
        if set(pats) != set(default_patterns):
            raise ConfigError(f"patterns must have exactly the keys {', '.join(default_patterns)}", "patterns")
        for k, p in pats.items():
            try:
                re.compile(p)
            except re.error as e:
                raise ConfigError(f"pattern {k} is not valid: {e}", "patterns") from None
        for text, want in cases:
            got = phrase_match(text, pats)
            if got != want:
                raise ConfigError(f"the patterns must map {text!r} to {want!r}, not {got!r}", "patterns")
        try:
            ZoneInfo(values["timezone"])
        except Exception:
            raise ConfigError("timezone must be an IANA zone name such as America/New_York", "timezone") from None
        if has_url and not host_is_loopback(values["url"]):
            raise ConfigError("url must point at this machine (127.0.0.1 or localhost)", "url")
    return validate


def make_spec(*, name, summary, topic, default_patterns, cases, compose, test_file, timeout_s=3.0, extra_knobs=(),
              order=101):
    short = name.split(".", 1)[1]
    knobs = [Knob("priority", "int", 90, "Higher runs first among intents; music is 100.", unit="rank", min=0, max=1000)]
    if topic:
        knobs += [
            Knob("url", "url", LOOPBACK_URL, "Agent OS address; loopback only."),
            Knob("token_file", "path", DEFAULT_TOKEN_FILE, "Bearer token file for the quick route, read per call.",
                 secret=True),
            Knob("timeout_s", "float", timeout_s, "Seconds the fetch may take before the question goes to Hermes.",
                 unit="s", min=0.5, max=8),
        ]
    knobs += [
        Knob("timezone", "str", DEFAULT_TZ, "Zone that decides what today and tomorrow mean.", unit="zone", min=3,
             max=40),
        Knob("patterns", "map[str,str]", dict(default_patterns),
             "Phrasings tried in order, matched whole inside a polite lead-in and tail.", unit=""),
    ]
    knobs += list(extra_knobs)
    return PluginSpec(
        name=name, kind="intent", version="1", machine="listener", provides=("intent",), summary=summary,
        create=lambda ctx, cfg: QuickPlugin(ctx, cfg, name, f"quick_{short}", topic, compose),
        order=order, required=False, default_enabled=False, toggle="live",
        validate=make_validate(default_patterns, cases, bool(topic)), knobs=tuple(knobs), tests=(test_file,))


MAX_WORDS_KNOB = Knob("max_words", "int", 25, "Longest spoken answer; titles and items are dropped to fit.",
                      unit="words", min=12, max=60)
