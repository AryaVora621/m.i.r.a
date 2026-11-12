"""loop.turns: the turn loop's timing knobs. The loop itself lives in mira_listen.py (the facade); this plugin owns
the knobs and provides a small placeholder as the `loop` service so the facade and the core can find the values.

FIXED (the loop's order of work, spec 4.2): wake, capture, STT, gate, intent or Hermes, TTS, playback, follow-up.
Barge-in (2026-11-11): on a live microphone the request runs on a worker thread while the loop keeps reading the mic and
the wake spotter; a spotter hit cancels the turn (Hermes wait, playback, queued speech) and starts a normal wake capture.
"""

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

LEGACY_CLASSES = ()


class LoopSlot:
    """The `loop` service: the current values of the loop knobs, read by the facade through its args namespace."""

    def __init__(self, cfg):
        self.update(cfg)

    def update(self, cfg):
        self.wait_speech, self.max_s, self.followup_s = cfg.wait_speech, cfg.max_s, cfg.followup_s
        self.followup_wait_s, self.followup_seed_chunks = cfg.followup_wait_s, cfg.followup_seed_chunks
        self.wake_seed_chunks, self.preroll_chunks, self.offline_text = cfg.wake_seed_chunks, cfg.preroll_chunks, cfg.offline_text
        self.events_max, self.log_text, self.sigterm_idle = cfg.events_max, cfg.log_text, cfg.sigterm_idle
        self.barge_in, self.barge_ignore_s, self.barge_head_chunks = cfg.barge_in, cfg.barge_ignore_s, cfg.barge_head_chunks
        self.barge_guard_words = cfg.barge_guard_words
        self.cancel_words, self.cancel_after_barge = list(cfg.cancel_words), list(cfg.cancel_after_barge)
        self.cancel_reply = cfg.cancel_reply
        self.caption_sync = cfg.caption_sync


class LoopTurns(PluginBase):
    def start(self):
        self.slot = LoopSlot(self.cfg)
        self.ctx.provide("loop", self.slot)

    def apply(self, changed, cfg):
        self.cfg = cfg
        self.slot.update(cfg)

    def status(self):
        return {"wait_speech": self.slot.wait_speech, "followup_s": self.slot.followup_s}


def validate(values):
    pre, wake, follow = values["preroll_chunks"], values["wake_seed_chunks"], values["followup_seed_chunks"]
    if pre <= wake:
        raise ConfigError("preroll_chunks must exceed wake_seed_chunks", "preroll_chunks")
    if pre < follow:
        raise ConfigError("preroll_chunks must be at least followup_seed_chunks", "preroll_chunks")


SPEC = PluginSpec(
    name="loop.turns", kind="loop", version="1", machine="listener", provides=("loop",), core=True,
    summary="The turn loop's timing: waits, limits, pre-roll and follow-up.",
    create=lambda ctx, cfg: LoopTurns(ctx, cfg), order=140, validate=validate,
    knobs=(
        Knob("wait_speech", "float", 4.0, "How long she waits for you to start talking after her name.", unit="s",
             min=1, max=15, agent="bounded", agent_min=2, agent_max=8, agent_step=1, arg="wait_speech", topic="timing",
             spoken="how long I wait for you to start", effect="more time after my name",
             phrases=("you stop listening before I talk",)),
        Knob("max_s", "float", 10.0, "Longest request she records.", unit="s", min=3, max=30, agent="bounded",
             agent_min=6, agent_max=15, agent_step=2, arg="max_s", topic="timing", spoken="longest request",
             effect="longer requests fit", phrases=("you stop me mid sentence",)),
        Knob("followup_s", "float", 8.0, "How long she keeps listening without her name after answering.", unit="s",
             min=0, max=30, agent="bounded", agent_min=4, agent_max=15, agent_step=2, arg="followup_s", topic="timing",
             spoken="how long I keep listening after answering", effect="longer chance to reply without my name",
             phrases=("you stop listening too soon",)),
        Knob("followup_wait_s", "float", 1.0, "Wait for speech once the follow-up VAD fires.", unit="s", min=0.5,
             max=3, arg="followup_wait_s"),
        Knob("followup_seed_chunks", "int", 5, "Audio kept ahead of a follow-up.", unit="chunks(100ms)", min=1,
             max=15, applies="restart", arg="followup_seed_chunks"),
        Knob("wake_seed_chunks", "int", 3, "VAD seed after a wake; the rest of the pre-roll goes only to STT.",
             unit="chunks(100ms)", min=1, max=10, applies="restart", arg="wake_seed_chunks"),
        Knob("preroll_chunks", "int", 15, "Pre-roll length; must exceed wake_seed_chunks and cover followup_seed_chunks.",
             unit="chunks(100ms)", min=5, max=30, applies="restart", arg="preroll_chunks"),
        Knob("offline_text", "str", "Speech is offline", "Wall text when speech is offline.", unit="chars", min=1,
             max=80, arg="offline_text"),
        Knob("events_max", "int", 0, "In-memory event trail length; 0 is unbounded (legacy).", unit="events", min=0,
             max=100000, applies="restart"),
        Knob("log_text", "bool", True, "Heard text and replies in the journal."),
        Knob("barge_in", "bool", True, "Her name interrupts her while she thinks or speaks, on the live microphone."),
        Knob("barge_ignore_s", "float", 0.6, "After she starts speaking, how long her name is ignored, so the first "
             "syllables cannot wake her.", unit="s", min=0, max=5),
        Knob("barge_head_chunks", "int", 8, "Audio kept before her name when she is cut off mid-speech, so her own voice "
             "stays out of the request.", unit="chunks(100ms)", min=2, max=15),
        Knob("barge_guard_words", "regex", r"\b(?:mir\w*|mar[ia]\w*|m[iy]r\w*|me+r[ao]\w*|[vb][ei]+r[ao]h?)\b",
             "Words in a reply that sound like her name: while such a reply plays, her name does not interrupt it."),
        Knob("cancel_words", "list[str]", ["cancel", "cancel that", "never mind", "nevermind", "forget it", "forget that",
                                           "stop talking", "stop that", "stop it"],
             "Requests that only cancel what she is doing and are never sent to Hermes.", unit="items", min=0, max=30),
        Knob("cancel_after_barge", "list[str]", ["stop", "quiet", "enough", "that's enough"],
             "Extra cancel words, taken only when they interrupted a running turn; otherwise a bare stop stays a music command.",
             unit="items", min=0, max=30),
        Knob("cancel_reply", "str", "", "What she says after a cancel; empty is silence.", unit="chars", min=0, max=40),
        Knob("caption_sync", "bool", True, "The wall's caption shows each few words as they are spoken, posted on the "
             "playback clock; off posts the whole chunk and lets the page time it."),
        Knob("sigterm_idle", "bool", False, "SIGTERM posts a final idle and exits through the finally.",
             applies="restart"),
    ),
    tests=("wall/tests/plugins/test_lp_loop_turns.py",),
)
