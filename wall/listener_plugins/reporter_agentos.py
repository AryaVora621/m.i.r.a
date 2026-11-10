"""reporter.agentos: posts the voice state, level and text to Agent OS's /api/hooks/voice. The class is v1's
VoiceReporter (copied) with the five knob-fed constructor parameters of spec 4.4.

FIXED: the post body {state, level, heard, reply, at, mic}, LEVEL_STATES = ("listening", "speaking"), the queue and
coalescing rules, and the error hold that delays the next state post.
"""

import json
import threading
import time

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

from .common import http, host_is_loopback, log, read_secret

LEGACY_CLASSES = ("VoiceReporter",)


class VoiceReporter:
    """Posts {state, level, heard, reply, at} on transitions and text changes (state posts at most 2 per
    second), plus level-only posts (same state, new `level`) at up to 5 per second while the last posted state
    is `listening` or `speaking`, so the wall's rings can follow the voice.

    Every state change is kept in order (a quick listening -> thinking must not collapse); a text-only
    change replaces a still-queued item with the same state. A hold keeps `error` on the wall for a while
    before the follow-up idle goes out. A level-only post is only ever made when no state item is queued and
    does not count toward the state gap, so it can neither delay nor reorder a state change.
    """

    MIN_GAP = 0.5
    LEVEL_GAP = 0.2  # 5 level posts per second
    LEVEL_STATES = ("listening", "speaking")
    LEVEL_STEP = 0.03  # smaller moves than this are not worth a post

    def __init__(self, url, token_file, error_hold=4.0, min_gap=0.5, level_gap=0.2, level_step=0.03,
                 post_timeout_s=3.0, close_wait_s=20.0):
        self.url, self.token_file, self.error_hold = url, token_file, error_hold
        # Instance attributes shadow the class constants, which stay as the v1 defaults.
        self.MIN_GAP, self.LEVEL_GAP, self.LEVEL_STEP = min_gap, level_gap, level_step
        self.post_timeout_s, self.close_wait_s = post_timeout_s, close_wait_s
        self.q = []
        self.cv = threading.Condition()
        self.last = None  # (state, heard, reply) of the last item enqueued
        self.mic = None  # "muted" / "live" once the mute detector knows; every post carries it from then on
        self.level = 0.0
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_level(self, v):
        self.level = float(v)

    def update(self, state, heard="", reply=""):
        key = (state, heard, reply)
        with self.cv:
            if key == self.last:
                return
            item = {"state": state, "level": round(self.level, 3), "heard": heard, "reply": reply}
            if self.mic:
                item["mic"] = self.mic
            if self.q and self.q[-1]["state"] == state and self.last and self.last[0] == state:
                self.q[-1].update(item)  # text change on a queued item: coalesce
            else:
                self.q.append(item)
            self.last = key
            self.cv.notify()

    def set_mic(self, mic):
        """Record the mic state and post the current voice state with it (also the minute repeat while muted)."""
        with self.cv:
            self.mic = mic
            state, heard, reply = self.last or ("idle", "", "")
            # Always its own queue item: a coalesced text change must not swallow the mic report.
            self.q.append({"state": state, "level": round(self.level, 3), "heard": heard, "reply": reply, "mic": mic})
            self.cv.notify()

    def _next(self, posted, last_level_at):
        """Block until there is something to post: a queued state item first, else a due level-only item."""
        with self.cv:
            while True:
                if self.q:
                    return self.q.pop(0), False
                if self.closed:
                    return None, False
                if posted is None or posted["state"] not in self.LEVEL_STATES:
                    self.cv.wait()
                    continue
                due = last_level_at + self.LEVEL_GAP - time.monotonic()
                lvl = round(self.level, 3)
                if due <= 0 and abs(lvl - posted["level"]) >= self.LEVEL_STEP:
                    return dict(posted, level=lvl), True
                self.cv.wait(timeout=max(due, 0.05))

    def _run(self):
        last_sent = 0.0  # last STATE post: the 2 per second limit and the error hold are about states
        last_level_at = 0.0
        hold_until = 0.0
        posted = None
        while True:
            item, level_only = self._next(posted, last_level_at)
            if item is None:
                return
            if not level_only:
                wait = max(last_sent + self.MIN_GAP, hold_until) - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            item["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self._post(item)
            now = time.monotonic()
            last_level_at = now
            posted = item
            if not level_only:
                last_sent = now
                hold_until = last_sent + self.error_hold if item["state"] == "error" else 0.0

    def _post(self, item):
        token = read_secret(self.token_file)
        if not self.url:
            return
        try:
            http("POST", self.url, json.dumps(item).encode(),
                 {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, timeout=self.post_timeout_s)
        except Exception as e:  # the wall display is never worth stopping the assistant for
            log(f"voice hook failed: {type(e).__name__}")

    def close(self, wait=True):
        with self.cv:
            self.closed = True
            self.cv.notify()
        if wait:
            self.thread.join(timeout=self.close_wait_s)


class ReporterAgentos(PluginBase):
    def start(self):
        c = self.cfg
        self.rep = VoiceReporter(c.url, c.token_file, error_hold=c.error_hold, min_gap=c.min_gap,
                                 level_gap=c.level_gap, level_step=c.level_step, post_timeout_s=c.post_timeout_s,
                                 close_wait_s=c.close_wait_s)
        # close() is safe to call twice: the loop's own finally normally closes it first.
        self.rep.level_scale = c.level_scale  # read by agc.mic and playback.miraaudio through their `reads`
        self.ctx.effect(lambda: self.rep.close(wait=True), "voice reporter")
        self.ctx.provide("reporter", self.rep)

    def apply(self, changed, cfg):
        self.cfg = cfg
        r = self.rep
        r.url, r.token_file, r.error_hold = cfg.url, cfg.token_file, cfg.error_hold
        r.MIN_GAP, r.LEVEL_GAP, r.LEVEL_STEP = cfg.min_gap, cfg.level_gap, cfg.level_step
        r.post_timeout_s, r.level_scale = cfg.post_timeout_s, cfg.level_scale
        # close_wait_s is a restart knob in the table but only read at close(), so keeping it current is harmless.

    def status(self):
        return {"queued": len(self.rep.q), "mic": self.rep.mic}


def validate(values):
    url = values["url"]
    if url and not host_is_loopback(url):
        raise ConfigError("url must point at this machine (127.0.0.1 or localhost), or be empty to stop posting", "url")


SPEC = PluginSpec(
    name="reporter.agentos", kind="reporter", version="1", machine="listener", provides=("reporter",),
    summary="Posts the voice state to Agent OS so the wall can draw it.",
    create=lambda ctx, cfg: ReporterAgentos(ctx, cfg), order=10, validate=validate,
    knobs=(
        Knob("url", "url", "http://127.0.0.1:3456/api/hooks/voice", "Agent OS voice hook; empty stops the posts.",
             arg="voice_url", env="MIRA_VOICE_URL"),
        Knob("token_file", "path", "", "Bearer token file, read on every post.", secret=True,
             arg="voice_token_file", env="MIRA_VOICE_TOKEN_FILE"),
        Knob("error_hold", "float", 4.0, "How long the error state stays on the wall.", unit="s", min=0, max=30,
             arg="error_hold"),
        Knob("min_gap", "float", 0.5, "Spacing between state posts.", unit="s", min=0.1, max=5),
        Knob("level_gap", "float", 0.2, "Spacing between level posts.", unit="s", min=0.05, max=2),
        Knob("level_step", "float", 0.03, "Smallest level move worth a post.", unit="level", min=0.005, max=0.5),
        Knob("post_timeout_s", "float", 3.0, "Seconds one hook post may take.", unit="s", min=0.5, max=30),
        Knob("close_wait_s", "float", 20.0, "Seconds close() waits for the queue to drain.", unit="s", min=1,
             max=120, applies="restart"),
        Knob("level_scale", "float", 8.0, "Ring level is rms times this, capped at 1; the one copy.", unit="x",
             min=1, max=32, arg="level_scale"),
    ),
    tests=("wall/tests/plugins/test_lp_reporter_agentos.py",),
)
