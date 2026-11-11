"""intent.screens: put a wall screen up or take it down by voice, without Hermes ("bring up my 3D printers", "close it").

Why it exists: asking Hermes costs three model calls (5 to 11 s each) for what is one request to Agent OS. This intent
answers in about a second and falls through to Hermes whenever the phrase is not clearly a screen request.

Credential: Agent OS's screen hook (POST /api/hooks/screen, DELETE /api/hooks/screen/<id>) with its own bearer token,
read per call from a file only this user can read. It is not the agent route Hermes uses (that one is guarded by the
shared agent token, which also carries the owner's tasks) and it is never Hermes's key. The hook route accepts any
template, so the allowed set is fixed here to the eight the agent route allows for live screens. The hook route fills
nothing in: the live templates' sample data, the lifetime and the priority are sent explicitly, as the agent route would.

Clearing never uses the "everything" form of the hook (it would empty the owner's and the scheduled screens too) and never
deletes by template (that removes other sources' screens). It deletes by the ids this listener pushed. With nothing of its
own to remove, the request goes to Hermes, who can see the whole stack.

FIXED: the route paths, the source "mira", the priority "info", fullmatch semantics on the lower-cased text with
punctuation removed and apostrophes dropped, and a fuzzy match only when one screen is clearly closest.
"""

import difflib
import json
import re
import threading
import time
import urllib.error
import urllib.request

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

from .common import expand_path, host_is_loopback, read_secret

LEGACY_CLASSES = ()

LOOPBACK_URL = "http://127.0.0.1:3456"
DEFAULT_TOKEN_FILE = "~/.config/mira/screen_token"
MAX_ID_CHARS = 64

# The live templates the wall fills from its own feeds; the sample is what the agent route sends for them.
SAMPLES = {
    "print-studio": {"title": "Print Studio", "jobs": []},
    "school": {"title": "School", "items": []},
    "week": {"title": "Week", "days": []},
    "homelab": {"title": "Homelab", "services": []},
    "morning": {"note": ""},
    "recap": {"note": ""},
    "exams": {"title": "Exams", "items": []},
    "board": {"title": "Board", "columns": []},
}
TEMPLATE_KEYS = tuple(SAMPLES)

DEFAULT_ALIASES = {
    "print-studio": "printers|printer|3d printers|3d printer|my prints|prints|print|3d prints|3d print|print studio|printer status"
                    "|print status|the print studio|print farm",
    "school": "school|school board|assignments|assignment|homework|classroom|google classroom|classwork|school work"
              "|schoolwork|whats due|what is due|due|school stuff",
    "week": "week|my week|this week|calendar|my calendar|schedule|my schedule|week view|weekly|the week ahead",
    "homelab": "homelab|home lab|servers|server|home server|home servers|server status|homelab status|home lab status",
    "morning": "brief|morning brief|daily brief|briefing|morning briefing|morning|my morning brief",
    "recap": "recap|evening recap|daily recap|day recap|evening brief|my recap",
    "exams": "exams|exam|exam schedule|finals|upcoming exams|my exams",
    "board": "build board|board|robotics board|task board|project board|kanban",
}
DEFAULT_REPLIES = {"print-studio": "Printers are up.", "school": "School is up.", "week": "Week is up.",
                   "homelab": "Homelab is up.", "morning": "Brief is up.", "recap": "Recap is up.",
                   "exams": "Exams are up.", "board": "Board is up.", "cleared": "Cleared."}
REPLY_KEYS = tuple(DEFAULT_REPLIES)
MAX_REPLY_WORDS = 5  # the persona's rule for these confirmations

_LEAD = r"(?:(?:hey |hi |ok |okay )?(?:mira )?)?(?:(?:please|can you|could you|would you|will you|i want|i would like|lets|let us|go ahead and) )*"
_VERB = r"(?:bring up|bring|show me|show|open up|open|put up|put on|put|pull up|pull|throw up|display|get up|give me)"
_SHOW = re.compile(
    rf"{_LEAD}{_VERB} (?:me )?(?:up )?(?:on )?(?:the |my |our |a |an |some )?(?P<target>.+?)"
    r"(?: (?:on|to|onto) (?:the )?(?:wall|screen|display|tv|monitor|board|dashboard))?(?: up)?(?: for me| please| now| thanks| thank you)*")
_CLEAR_LAST = re.compile(
    rf"{_LEAD}(?:close|dismiss|hide|remove|clear) (?:it|that|this)(?: up| away)?(?: please| now)?"
    rf"|{_LEAD}take (?:it|that|this) down|{_LEAD}put (?:it|that|this) away|{_LEAD}get rid of (?:it|that|this)")
_CLEAR_ALL = re.compile(
    rf"{_LEAD}(?:clear|wipe|empty|close|dismiss|hide) (?:the |my )?(?:wall|screens?|display|everything|all|it all|all screens|all of them"
    rf"|everything up)(?: please| now)?"
    rf"|{_LEAD}take (?:everything|them|it all) down|{_LEAD}close them(?: all)?")
_GENERIC_TAIL = re.compile(r"(?: (?:screen|page|view|display|dashboard|stuff|info|up))+$")
_THREE_D = re.compile(r"\b(?:3|three|free|tree|the) ?d\b")

CASES = [
    ("bring up my 3D printers", "show:print-studio"), ("Bring up my 3d printers.", "show:print-studio"),
    ("bring up my three d printers", "show:print-studio"), ("show me the printers", "show:print-studio"),
    ("show the printer", "show:print-studio"), ("put up my prints", "show:print-studio"),
    ("pull up the 3D printers on the wall", "show:print-studio"), ("open the printers please", "show:print-studio"),
    ("can you bring up the printers", "show:print-studio"), ("bring up my prints", "show:print-studio"),
    ("bring up school", "show:school"), ("show me my assignments", "show:school"), ("pull up homework", "show:school"),
    ("show the school board", "show:school"), ("put up the homework screen", "show:school"),
    ("bring up my week", "show:week"), ("show me this week", "show:week"), ("pull up my calendar", "show:week"),
    ("put up the schedule", "show:week"), ("open my week", "show:week"),
    ("bring up the homelab", "show:homelab"), ("bring up my home lab", "show:homelab"), ("show me the servers", "show:homelab"),
    ("pull up the servers", "show:homelab"), ("bring up the morning brief", "show:morning"), ("show me my brief", "show:morning"),
    ("bring up the recap", "show:recap"), ("show the evening recap", "show:recap"), ("bring up my exams", "show:exams"),
    ("show me the exams", "show:exams"), ("bring up the build board", "show:board"), ("show the board", "show:board"),
    ("bring up my printer's", "show:print-studio"), ("bring up the printerz", "show:print-studio"),
    ("bring up the scool board", "show:school"), ("bring up the home lab status", "show:homelab"),
    ("close it", "clear:last"), ("Close that.", "clear:last"), ("take it down", "clear:last"), ("dismiss it", "clear:last"),
    ("hide it", "clear:last"), ("put it away", "clear:last"), ("close it please", "clear:last"),
    ("clear the wall", "clear:all"), ("clear the screen", "clear:all"), ("clear everything", "clear:all"),
    ("take everything down", "clear:all"), ("close all", "clear:all"), ("wipe the wall", "clear:all"),
    # not clear: Hermes decides
    ("bring up my journey", None), ("bring up my journey, Hermes", None), ("show me the weather", None),
    ("bring up a timer for five minutes", None), ("show me how to print a benchy", None), ("open the pod bay doors", None),
    ("pull up a chair", None), ("show me my school grades", None), ("bring up", None), ("show me", None),
    ("put up a list of things to pack", None), ("bring up the alarm", None), ("show the printers menu options", None),
    ("what's on my calendar", None), ("turn it down", None), ("turn the volume down", None), ("take it down a notch", None),
    ("close the door", None), ("close", None), ("clear", None), ("clear my schedule", None), ("pause", None), ("stop", None),
    ("are the printers running", None), ("how long is left on the print", None), ("printer status", None),
    ("play it", None), ("what is playing", None), ("open a new session", None), ("put it on my calendar", None),
]

_FUZZY_MIN = 0.86       # similarity a heard word needs to count as a screen's name
_FUZZY_GAP = 0.08       # and how far it must lead the next screen, or the request goes to Hermes
_FUZZY_MAX_WORDS = 3
_FUZZY_MIN_CHARS = 4     # shorter words are too easy to hit by accident


def normalize(text):
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]", " ", t).replace("'", "")
    t = _THREE_D.sub("3d", " ".join(t.split()))
    return " ".join(t.split())


def parse_aliases(aliases):
    """{template: {alias, ...}} from the knob's pipe-separated text, each alias normalized."""
    return {k: {normalize(a) for a in str(v).split("|") if normalize(a)} for k, v in aliases.items()}


def resolve_target(target, table):
    """The template a spoken screen name means, or None when it is not clearly one."""
    t = normalize(target)
    t = re.sub(r"^(?:the|my|our|all|up) ", "", t)
    t = _GENERIC_TAIL.sub("", t).strip()
    if not t:
        return None
    exact = [k for k, names in table.items() if t in names]
    if len(exact) == 1:
        return exact[0]
    if exact or len(t.split()) > _FUZZY_MAX_WORDS or len(t) < _FUZZY_MIN_CHARS:
        return None
    flat = t.replace(" ", "")
    best = {}
    for k, names in table.items():
        for a in names:
            r = difflib.SequenceMatcher(None, flat, a.replace(" ", "")).ratio()
            best[k] = max(best.get(k, 0.0), r)
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    if ranked and ranked[0][1] >= _FUZZY_MIN and (len(ranked) < 2 or ranked[0][1] - ranked[1][1] >= _FUZZY_GAP):
        return ranked[0][0]
    return None


def screens_intent(text, table):
    """"show:<template>", "clear:last", "clear:all" or None."""
    t = normalize(text)
    if not t:
        return None
    if _CLEAR_LAST.fullmatch(t):
        return "clear:last"
    if _CLEAR_ALL.fullmatch(t):
        return "clear:all"
    m = _SHOW.fullmatch(t)
    if m:
        key = resolve_target(m.group("target"), table)
        if key:
            return f"show:{key}"
    return None


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback must never ride a proxy variable


class ScreenClient:
    """Agent OS's screen hook. Every failure raises; the intent turns that into a hand-over to Hermes."""

    def __init__(self, url, token_file, timeout_s):
        self.url, self.token_file, self.timeout_s = url.rstrip("/"), token_file, timeout_s

    def _call(self, method, path, body=None):
        token = read_secret(self.token_file)
        if not token:
            raise RuntimeError("no screen token")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(f"{self.url}{path}", data=data, method=method, headers=headers)
        with _opener.open(req, timeout=self.timeout_s) as r:
            raw = r.read(65536)
        out = json.loads(raw or b"{}")
        if not isinstance(out, dict):
            raise RuntimeError("unexpected answer")
        return out

    def push(self, template, minutes):
        out = self._call("POST", "/api/hooks/screen", {"template": template, "data": dict(SAMPLES[template]),
                                                       "ttl_s": int(minutes * 60), "priority": "info", "source": "mira"})
        sid = out.get("id")
        if not isinstance(sid, str) or not sid or len(sid) > MAX_ID_CHARS or not re.fullmatch(r"[0-9a-zA-Z_-]+", sid):
            raise RuntimeError("no screen id")
        return sid

    def remove(self, screen_id):
        return bool(self._call("DELETE", f"/api/hooks/screen/{screen_id}").get("ok"))


class ScreensIntent:
    """The `intent` service. A hit is final in the loop, so the work happens inside match(): it returns a key only when
    Agent OS has done what was asked, and None (to Hermes) for anything else, a failed call included."""

    name = "intent.screens"
    mark = "screens"
    log_name = "agent-os"

    def __init__(self, cfg, client, log):
        self.client, self.log = client, log
        self.hits = self.misses = 0
        self.shown = []      # [(id, template, expires_at monotonic)], oldest first: only what this listener pushed
        self.clock = time.monotonic
        self.lock = threading.Lock()
        self._ready = None
        self.update(cfg)

    def update(self, cfg):
        self.cfg = cfg
        self.priority, self.minutes = cfg.priority, cfg.minutes
        self.replies = dict(cfg.replies)
        self.table = parse_aliases(cfg.aliases)

    @property
    def done_reply(self):
        return self.replies["cleared"]

    @property
    def fail_reply(self):
        return "I could not do that."

    def _live(self):
        now = self.clock()
        self.shown = [s for s in self.shown if s[2] > now]
        return list(self.shown)

    def _show(self, template):
        # One of mine per template: the hook route stacks duplicates, the agent route would replace them.
        for sid, tpl, _ in [s for s in self._live() if s[1] == template]:
            try:
                self.client.remove(sid)
            except Exception:
                pass
            self.shown = [s for s in self.shown if s[0] != sid]
        sid = self.client.push(template, self.minutes)
        self.shown.append((sid, template, self.clock() + self.minutes * 60))
        return self.replies[template]

    def _clear(self, which):
        mine = self._live()
        if not mine:
            return None  # nothing of mine on the wall: Hermes can see everything else
        victims = mine[-1:] if which == "last" else mine
        removed = 0
        for sid, _, _ in victims:
            if self.client.remove(sid):
                removed += 1
            self.shown = [s for s in self.shown if s[0] != sid]
        return self.replies["cleared"] if removed else None

    def match(self, text):
        try:
            key = screens_intent(text, self.table)
            if key is None:
                return None
            with self.lock:
                kind, _, arg = key.partition(":")
                answer = self._show(arg) if kind == "show" else self._clear(arg)
        except Exception as e:  # a failure here must cost one fast answer, not the turn
            self.misses += 1
            self.log.warning("screens failed, asking Hermes: %s", type(e).__name__)
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


class IntentScreens(PluginBase):
    def _client(self, cfg):
        return ScreenClient(cfg.url, expand_path(cfg.token_file, self.ctx.paths), cfg.timeout_s)

    def start(self):
        self.intent = ScreensIntent(self.cfg, self._client(self.cfg), self.ctx.log)
        self.ctx.provide("intent", self.intent)

    def apply(self, changed, cfg):
        self.cfg = cfg
        i = self.intent
        i.update(cfg)
        c = i.client
        c.url, c.timeout_s, c.token_file = cfg.url.rstrip("/"), cfg.timeout_s, expand_path(cfg.token_file, self.ctx.paths)

    def status(self):
        return {"priority": self.intent.priority, "answered": self.intent.hits, "to_hermes": self.intent.misses,
                "on_wall": len(self.intent.shown)}


def validate(values):
    aliases, replies = values["aliases"], values["replies"]
    if set(aliases) != set(TEMPLATE_KEYS):
        raise ConfigError(f"aliases must have exactly the keys {', '.join(TEMPLATE_KEYS)}", "aliases")
    if set(replies) != set(REPLY_KEYS):
        raise ConfigError(f"replies must have exactly the keys {', '.join(REPLY_KEYS)}", "replies")
    for k, v in replies.items():
        if not v.strip() or len(v.split()) > MAX_REPLY_WORDS:
            raise ConfigError(f"reply {k} must be one to {MAX_REPLY_WORDS} words", "replies")
    table = parse_aliases(aliases)
    if any(not names for names in table.values()):
        raise ConfigError("every screen needs at least one alias", "aliases")
    for text, want in CASES:
        got = screens_intent(text, table)
        if got != want:
            raise ConfigError(f"the aliases must map {text!r} to {want!r}, not {got!r}", "aliases")
    if not host_is_loopback(values["url"]):
        raise ConfigError("url must point at this machine (127.0.0.1 or localhost)", "url")


SPEC = PluginSpec(
    name="intent.screens", kind="intent", version="1", machine="listener", provides=("intent",),
    summary="Bring up and close wall screens by voice in about a second, without Hermes.",
    create=lambda ctx, cfg: IntentScreens(ctx, cfg), order=102, required=False, default_enabled=True, toggle="live",
    validate=validate, knobs=(
        Knob("priority", "int", 95, "Higher runs first among intents; music is 100.", unit="rank", min=0, max=1000),
        Knob("url", "url", LOOPBACK_URL, "Agent OS address; loopback only."),
        Knob("token_file", "path", DEFAULT_TOKEN_FILE, "Bearer token file for the screen hook, read per call.",
             secret=True),
        Knob("timeout_s", "float", 4.0, "Seconds a screen call may take before the request goes to Hermes.", unit="s",
             min=0.5, max=8),
        Knob("minutes", "float", 5.0, "How long a screen stays up.", unit="min", min=1, max=30),
        Knob("aliases", "map[str,str]", dict(DEFAULT_ALIASES),
             "Spoken names of each screen, separated by |; a close match to one name also counts.", unit=""),
        Knob("replies", "map[str,str]", dict(DEFAULT_REPLIES), "Spoken confirmations, five words or fewer.", unit=""),
    ),
    tests=("wall/tests/plugins/test_lp_intent_screens.py",),
)
