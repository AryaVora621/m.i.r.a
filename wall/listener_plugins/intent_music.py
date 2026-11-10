"""intent.music: local music commands against music-api. Music and music_intent are v1's (copied) with the knob-fed
edits of spec 4.4; MusicIntent is the `intent` service the loop's intent list calls.

FIXED: the music-api routes under /x/echo, the X-Music-* headers from the auth file (read per call), and fullmatch
semantics inside `(please )?(<pattern>)( please)?` on the lower-cased text with punctuation removed.
"""

import json
import re
import urllib.error
from pathlib import Path

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

from .common import expand_path, http

LEGACY_CLASSES = ("Music",)

REPLY_KEYS = ("play", "pause", "next", "volume", "echo_down", "playing", "paused", "nothing", "done", "unreachable")
DEFAULT_REPLIES = {"play": "Playing.", "pause": "Paused.", "next": "Next song.", "volume": "Volume {n}.",
                   "echo_down": "The speaker is not connected.", "playing": "Music is playing.",
                   "paused": "Music is paused.", "nothing": "Nothing is playing.", "done": "Done.",
                   "unreachable": "I could not reach the music player."}

# The v1 pattern list, same order and same regexes.
DEFAULT_PATTERNS = dict([
    ("status", r"(what'?s|what is|what song is) (playing|on)( right now| now)?|what am i listening to|what song is this"),
    ("volume_up", r"(turn |make it |volume )?(the )?(volume )?(up|louder)( a (little|bit))?|volume up|turn (it|the music|the volume) up|louder"),
    ("volume_down", r"volume down|turn (it|the music|the volume) down|(turn |make it )?(the )?(volume )?(down|quieter|softer)( a (little|bit))?|quieter"),
    ("pause", r"(pause[ds]?|stop|mute)( the)?( music| song| playback)?|pause it"),
    ("next", r"(next|skip(ped)?)( the)?( song| track)?|next song|skip it"),
    ("play", r"(play(s|ed)?|resume|start)( the)?( music| song| playback)?|play it"),
])
PATTERN_KEYS = tuple(DEFAULT_PATTERNS)

# The test_unit.py table; a candidate pattern set that breaks a row is refused.
MUSIC_CASES = [
    ("play", "play"), ("Play the music.", "play"), ("pause", "pause"), ("Pause the music", "pause"),
    ("next", "next"), ("skip this song", None), ("Skip the song", "next"), ("what's playing", "status"),
    ("What is playing right now?", "status"), ("volume up", "volume_up"), ("turn it down", "volume_down"),
    ("Turn the volume down", "volume_down"), ("play Taylor Swift", None), ("what's on my calendar today", None),
    ("play the next assignment", None),
]


class Music:
    """Local music intents against music-api (CONTRACT.md routes under /x/echo)."""

    def __init__(self, url, auth_file, timeout_s=8.0, volume_step=10, replies=DEFAULT_REPLIES):
        self.url = url.rstrip("/")
        self.auth_file = auth_file
        self.timeout_s, self.volume_step = timeout_s, volume_step
        self.replies = dict(replies)

    def _hdr(self):
        try:
            a = json.loads(Path(self.auth_file).read_text())
        except (OSError, ValueError):
            a = {}
        return {"X-Music-User": a.get("u", ""), "X-Music-Token": a.get("t", ""), "X-Music-Salt": a.get("s", ""),
                "Content-Type": "application/json"}

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        try:
            _, raw = http(method, f"{self.url}{path}", data, self._hdr(), timeout=self.timeout_s)
            return json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return {"error": "echo_down"}
            raise

    def handle(self, intent):
        r = self.replies
        if intent == "play":
            res = self.call("POST", "/x/echo/play")
            return r["echo_down"] if res.get("error") else r["play"]
        if intent == "pause":
            self.call("POST", "/x/echo/pause")
            return r["pause"]
        if intent == "next":
            res = self.call("POST", "/x/echo/next")
            return r["echo_down"] if res.get("error") else r["next"]
        if intent == "status":
            st = self.call("GET", "/x/echo/status").get("player", {})
            return {"playing": r["playing"], "paused": r["paused"]}.get(st.get("state"), r["nothing"])
        if intent in ("volume_up", "volume_down"):
            vol = int(self.call("GET", "/x/echo/status").get("player", {}).get("volume", 50))
            vol = max(0, min(100, vol + (self.volume_step if intent == "volume_up" else -self.volume_step)))
            self.call("POST", "/x/echo/volume", {"volume": vol})
            return r["volume"].format(n=vol)
        return None


def music_intent(text, pats=DEFAULT_PATTERNS):
    t = re.sub(r"[^a-z0-9' ]", " ", text.lower())
    t = " ".join(t.split())
    for name, p in (pats.items() if hasattr(pats, "items") else pats):
        if re.fullmatch(f"(please )?({p})( please)?", t):
            return name
    return None


class MusicIntent:
    """The `intent` service (spec 2.2): `priority` orders it among intents, the rest is read by the loop on a hit."""

    name = "intent.music"
    mark = "music"
    log_name = "music-api"

    def __init__(self, music, patterns, priority=100):
        self.music, self.patterns, self.priority = music, dict(patterns), priority

    @property
    def done_reply(self):
        return self.music.replies["done"]

    @property
    def fail_reply(self):
        return self.music.replies["unreachable"]

    def match(self, text):
        return music_intent(text, self.patterns)

    def handle(self, intent):
        return self.music.handle(intent)


class IntentMusic(PluginBase):
    def start(self):
        c = self.cfg
        music = Music(c.url, expand_path(c.auth_file, self.ctx.paths), timeout_s=c.timeout_s,
                      volume_step=c.volume_step, replies=c.replies)
        self.intent = MusicIntent(music, c.patterns, c.priority)
        self.ctx.provide("intent", self.intent)

    def apply(self, changed, cfg):
        self.cfg = cfg
        i = self.intent
        m = i.music
        m.url, m.auth_file = cfg.url.rstrip("/"), expand_path(cfg.auth_file, self.ctx.paths)
        m.timeout_s, m.volume_step, m.replies = cfg.timeout_s, cfg.volume_step, dict(cfg.replies)
        i.patterns, i.priority = dict(cfg.patterns), cfg.priority

    def status(self):
        return {"priority": self.intent.priority}


def validate(values):
    pats, replies = values["patterns"], values["replies"]
    if set(pats) != set(PATTERN_KEYS):
        raise ConfigError(f"patterns must have exactly the keys {', '.join(PATTERN_KEYS)}", "patterns")
    for k, p in pats.items():
        try:
            re.compile(p)
        except re.error as e:
            raise ConfigError(f"pattern {k} is not valid: {e}", "patterns") from None
    if set(replies) != set(REPLY_KEYS):
        raise ConfigError(f"replies must have exactly the keys {', '.join(REPLY_KEYS)}", "replies")
    if "{n}" not in replies["volume"]:
        raise ConfigError("the volume reply needs {n} for the new level", "replies")
    for text, want in MUSIC_CASES:
        got = music_intent(text, pats)
        if got != want:
            raise ConfigError(f"the patterns must map {text!r} to {want!r}, not {got!r}", "patterns")


SPEC = PluginSpec(
    name="intent.music", kind="intent", version="1", machine="listener", provides=("intent",),
    summary="Local music commands (play, pause, next, volume, status) answered without Hermes.",
    create=lambda ctx, cfg: IntentMusic(ctx, cfg), order=100, toggle="live", validate=validate,
    knobs=(
        Knob("priority", "int", 100, "Higher runs first among intents.", unit="rank", min=0, max=1000),
        Knob("url", "url", "http://127.0.0.1:4534", "music-api address.", arg="music_url", env="MIRA_MUSIC_URL"),
        Knob("auth_file", "path", "", "JSON {u,t,s} for music-api, read per call.", secret=True,
             arg="music_auth_file", env="MIRA_MUSIC_AUTH_FILE"),
        Knob("timeout_s", "float", 8.0, "Seconds a music-api call may take.", unit="s", min=1, max=30),
        Knob("volume_step", "int", 10, "Volume change for up or down.", unit="percent", min=1, max=50),
        Knob("patterns", "map[str,str]", dict(DEFAULT_PATTERNS),
             "Command patterns tried in order, matched whole inside an optional please.", unit=""),
        Knob("replies", "map[str,str]", dict(DEFAULT_REPLIES), "Spoken confirmations.", unit=""),
    ),
    tests=("wall/tests/plugins/test_lp_intent_music.py",),
)
