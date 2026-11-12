"""tts.remote: text-to-speech through the speech.link object. clean_for_speech and speech_chunks are v1's (copied);
TtsRemote adds the per-request voice, speed and peak, the chunk limits as knobs and the pinned voice of one reply.

FIXED: markdown is stripped by clean_for_speech and replies are split on sentence ends.
"""

import re

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

LEGACY_CLASSES = ()


def clean_for_speech(text):
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"(?m)^\s{0,3}(#{1,6}|[-*+>]|\d+[.)])\s+", "", t)
    t = re.sub(r"[*_~#|]+", "", t)
    return " ".join(t.split())


def speech_chunks(text, limit=280, max_chunks=3):
    """Whole sentences up to ~280 chars each, at most three: long replies are cut, not read for a minute."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        if len(s) > limit:
            s = s[:limit].rsplit(" ", 1)[0]
        if cur and len(cur) + 1 + len(s) > limit:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return chunks[:max_chunks]


class TtsRemote:
    """The `tts` service (spec 2.2). Chunking reads its limits per call, so a live knob needs no rebuild."""

    def __init__(self, speech, voice="", speed=0.0, peak=0.0, chunk_limit=280, max_chunks=3, pin_voice=True,
                 first_chunk_chars=0):
        self.speech, self.voice, self.speed, self.peak = speech, voice, speed, peak
        self.chunk_limit, self.max_chunks, self.pin_voice = chunk_limit, max_chunks, pin_voice
        self.first_chunk_chars = first_chunk_chars

    def chunks(self, text):
        from .speaking import split_for_speech  # speaking imports this module's helpers: the import waits for first use
        return split_for_speech(text, self.chunk_limit, self.max_chunks, self.first_chunk_chars)

    def tts(self, text, pin=False):
        """Chunk 1 of a reply goes out unpinned and records the voice the server chose; later chunks (pin=True)
        carry that voice, so one reply never changes voice half way."""
        return self.speech.tts(text, voice=self.voice, speed=self.speed, peak=self.peak,
                               pin=bool(pin and self.pin_voice))

    def render_preview(self, text, voice, speed, peak):
        """One sample for a preview: no pin and no reply_voice."""
        return self.speech.render(text, voice=voice, speed=speed, peak=peak)


class TtsRemotePlugin(PluginBase):
    def start(self):
        c = self.cfg
        speech = self.ctx.get("speech")
        self.tts = TtsRemote(speech, c.voice, c.speed, c.peak, c.chunk_limit, c.max_chunks, c.pin_voice,
                              c.first_chunk_chars)
        speech.voice = c.voice  # v1 kept the configured voice on the Speech object; the tts calls pass it too
        self.ctx.provide("tts", self.tts)

    def apply(self, changed, cfg):
        self.cfg = cfg
        t = self.tts
        t.voice, t.speed, t.peak = cfg.voice, cfg.speed, cfg.peak
        t.chunk_limit, t.max_chunks, t.pin_voice = cfg.chunk_limit, cfg.max_chunks, cfg.pin_voice
        t.first_chunk_chars = cfg.first_chunk_chars
        t.speech.voice = cfg.voice

    def status(self):
        return {"pinned_voice": self.tts.speech.reply_voice}


def validate(values):
    if values["speed"] and values["speed"] < 0.5:
        raise ConfigError("speed is 0 (the server setting) or 0.5 to 1.6", "speed")
    if values["peak"] and values["peak"] < 0.3:
        raise ConfigError("peak is 0 (the server setting) or 0.3 to 0.98", "peak")


SPEC = PluginSpec(
    name="tts.remote", kind="tts", version="1", machine="listener", provides=("tts",), needs=("speech",),
    summary="Text-to-speech through the speech server, with chunking and a pinned voice per reply.",
    create=lambda ctx, cfg: TtsRemotePlugin(ctx, cfg), order=85, validate=validate,
    knobs=(
        Knob("voice", "str", "", "Voice sent per request; empty uses the mini's current voice.", unit="chars", min=0,
             max=200, arg="voice", env="MIRA_VOICE"),
        Knob("speed", "float", 0.0, "Per-request speed override, 0 for the server setting.", unit="x", min=0,
             max=1.6),
        Knob("peak", "float", 0.0, "Per-request loudness override, 0 for the server setting.", unit="peak", min=0,
             max=0.98),
        Knob("chunk_limit", "int", 280, "Longest spoken chunk.", unit="chars", min=80, max=600),
        Knob("max_chunks", "int", 3, "Chunks spoken per reply; the rest is dropped.", unit="chunks", min=1, max=10),
        Knob("pin_voice", "bool", True, "Later chunks of one reply use the voice id the first chunk got."),
        Knob("first_chunk_chars", "int", 90, "The first spoken chunk is one sentence, or its first clause when longer than "
             "this, so the voice starts sooner; 0 packs it like the rest.",
             unit="chars", min=0, max=300),
    ),
    tests=("wall/tests/plugins/test_lp_tts_remote.py",),
)
