"""tts.remote: clean_for_speech and speech_chunks copied from v1; the TtsRemote service."""
import json

import pytest

import test_lp_common as H
from listener_plugins import tts_remote as tr
from mirakit.plugin import ConfigError

SPEC = tr.SPEC

TEXTS = [
    "## Today\n- **Physics** at [3pm](http://x.y)\n- `code` done\nSee https://a.b/c",
    "```python\nprint(1)\n```\nHello there. How are you? Fine!",
    " ".join(f"This is sentence number {i} of a very long answer." for i in range(60)),
    "A" * 900,
    "Short.",
    "",
    "One sentence that is quite long " * 12 + ". And another one. And a third. And a fourth. And a fifth.",
]


@pytest.mark.parametrize("text", TEXTS)
def test_clean_and_chunks_equal_v1(text):
    assert tr.clean_for_speech(text) == H.v1.clean_for_speech(text)
    t = H.v1.clean_for_speech(text)
    assert tr.speech_chunks(t) == H.v1.speech_chunks(t)
    assert tr.speech_chunks(t, 120, 5) == H.v1.speech_chunks(t, 120, 5)


class FakeSpeech:
    voice = ""
    reply_voice = ""

    def __init__(self):
        self.calls = []

    def tts(self, text, voice="", speed=0.0, peak=0.0, pin=False):
        self.calls.append(("tts", text, voice, speed, peak, pin))
        return b"WAV"

    def render(self, text, voice="", speed=0.0, peak=0.0):
        self.calls.append(("render", text, voice, speed, peak))
        return b"PREVIEW"


def test_chunks_use_the_configured_limits_and_match_the_v1_loop_input():
    t = tr.TtsRemote(FakeSpeech())
    text = TEXTS[2]
    assert t.chunks(text) == H.v1.speech_chunks(H.v1.clean_for_speech(text))
    t.chunk_limit, t.max_chunks = 100, 2
    assert t.chunks(text) == H.v1.speech_chunks(H.v1.clean_for_speech(text), 100, 2)
    assert len(t.chunks(text)) == 2


def test_first_chunk_rule_makes_the_first_chunk_one_sentence():
    t = tr.TtsRemote(FakeSpeech(), first_chunk_chars=90)
    out = t.chunks("Printers are up on the wall (print studio, 5 minutes). I took duty printers to mean your 3D printers.")
    assert out[0] == "Printers are up on the wall (print studio, 5 minutes)." and len(out) == 2
    t.first_chunk_chars = 0
    assert len(t.chunks("Printers are up. I took duty printers to mean your 3D printers.")) == 1


def test_tts_passes_voice_speed_peak_and_gates_pin():
    fs = FakeSpeech()
    t = tr.TtsRemote(fs, voice="amy#0", speed=1.1, peak=0.8, pin_voice=True)
    t.tts("a")
    t.tts("b", pin=True)
    t.pin_voice = False
    t.tts("c", pin=True)
    assert fs.calls == [("tts", "a", "amy#0", 1.1, 0.8, False), ("tts", "b", "amy#0", 1.1, 0.8, True),
                        ("tts", "c", "amy#0", 1.1, 0.8, False)]


def test_render_preview_is_stateless_and_ignores_the_configured_voice():
    fs = FakeSpeech()
    t = tr.TtsRemote(fs, voice="amy#0", speed=1.0, peak=0.9)
    assert t.render_preview("Hi.", "jenny#0", 1.2, 0.7) == b"PREVIEW"
    assert fs.calls == [("render", "Hi.", "jenny#0", 1.2, 0.7)]


def test_end_to_end_bodies_with_the_real_speech_link(tmp_path):
    srv = H.Srv(lambda m, p, b, h: (200, b"WAVDATA", "audio/wav", {"X-Mira-Voice": "amy#0"}))
    try:
        tok = tmp_path / "t"
        tok.write_text("t")
        _, ctx, _ = H.started("speech.link", url=srv.url, token_file=str(tok))
        plugin, ctx, _ = H.started("tts.remote", ctx=ctx, voice="", speed=0.0, peak=0.0)
        tts = ctx.get("tts")
        chunks = tts.chunks("First sentence. " * 30)
        for i, ch in enumerate(chunks):
            tts.tts(ch, pin=i > 0)
        bodies = [json.loads(c["body"]) for c in srv.calls]
        assert "voice" not in bodies[0] and all(b["voice"] == "amy#0" for b in bodies[1:])
        assert tts.render_preview("Sample.", "kristin#0", 1.0, 0.9) == b"WAVDATA"
        assert json.loads(srv.calls[-1]["body"]) == {"text": "Sample.", "voice": "kristin#0", "speed": 1.0, "peak": 0.9}
    finally:
        srv.stop()


def test_live_knobs_apply_and_mirror_voice_to_speech():
    ctx = H.make_ctx()
    _, ctx, _ = H.started("speech.link", ctx=ctx)
    plugin, ctx, _ = H.started("tts.remote", ctx=ctx)
    t, speech = ctx.get("tts"), ctx.get("speech")
    H.assert_live_knobs(SPEC, plugin, {
        "voice": ("kristin#0", lambda p: t.voice), "speed": (1.2, lambda p: t.speed), "peak": (0.7, lambda p: t.peak),
        "chunk_limit": (200, lambda p: t.chunk_limit), "max_chunks": (5, lambda p: t.max_chunks),
        "pin_voice": (False, lambda p: t.pin_voice), "first_chunk_chars": (50, lambda p: t.first_chunk_chars)})
    plugin.apply({"voice": "x"}, plugin.cfg.replace(voice="x"))
    assert speech.voice == "x"


def test_validate_speed_and_peak_are_zero_or_in_range():
    values = {k.name: k.default for k in SPEC.knobs}
    SPEC.validate(values)
    SPEC.validate(dict(values, speed=0.5, peak=0.3))
    for bad in ({"speed": 0.3}, {"peak": 0.1}):
        with pytest.raises(ConfigError):
            SPEC.validate(dict(values, **bad))


def test_needs_speech():
    assert SPEC.needs == ("speech",) and tr.LEGACY_CLASSES == ()
