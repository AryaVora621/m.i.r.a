"""Integration: previews (spec 2.6) on the real listener. An inline preview plays during the brain phase, before
Hermes' reply, and the next safe point flushes; an owner preview made outside a turn plays only when armed. The speech
server is the golden fake (it renders any /tts), playback is the recording fake mira-audio."""
import json
import threading

from test_int_facade import CAL_REPLY, CAL_TEXT, cal_audio, ml, rig_factory  # noqa: F401
from test_int_safe_point import spy_loop
from mirakit.core import RESET_STREAM


def audio_kinds(r):
    """The mira-audio call log with temp file names folded: duck / say / unduck."""
    return [ln.split()[0] for ln in r.audio.calls()]


def test_inline_preview_plays_before_the_reply_and_the_next_safe_point_flushes(rig_factory, monkeypatch):
    r = rig_factory(cal_audio(tail=4.0), stt=[CAL_TEXT], hermes=[CAL_REPLY])
    log = spy_loop(r, monkeypatch)
    out = {}

    def mid_turn(i):
        assert r.core.turn and r.core.turn.phase == "brain"
        out["before"] = list(r.audio.calls())
        out["result"] = r.core.preview(actor="owner", scope="owner", text="This is Amy.", voice="amy", wait_s=5)
        out["after"] = list(r.audio.calls())
        out["flush_pending"] = r.core.pending_flush
        out["t"] = r.src.clock()
    r.hermes.on_chat = mid_turn
    r.lis.run()
    assert out["before"] == []                                     # nothing played yet: Hermes had not answered
    assert out["result"]["played"] is True and out["result"]["voice"] == "amy"
    assert audio_kinds(r)[:3] == ["duck", "say", "unduck"]         # the sample, whole, before anything else
    assert out["after"] and len(out["after"]) == 3 and out["flush_pending"] is True
    assert audio_kinds(r) == ["duck", "say", "unduck", "duck", "say", "unduck"]   # then the reply, ducked as usual
    # the wall saw the preview as speaking with its own label, before the reply's speaking state
    posts = [b for _, b in r.hook.posts()]
    labels = [(b["state"], b.get("reply", "")) for b in posts]
    i_prev = next(i for i, (s, t) in enumerate(labels) if s == "speaking" and t.startswith("Voice preview"))
    # the reply's speaking state carries no caption of its own: its words are posted in groups as they are spoken (the fake
    # player here returns at once, so none are due)
    i_reply = next(i for i, (s, t) in enumerate(labels) if s == "speaking" and i > i_prev and not t.startswith("Voice preview"))
    assert i_prev < i_reply
    # the sample was rendered through the normal /tts with the overrides and the text, never stored anywhere
    tts_bodies = [b for b in r.speech.json_bodies("/tts")]
    assert tts_bodies[0]["text"] == "This is Amy." and tts_bodies[0].get("voice") == "amy"
    assert tts_bodies[1]["text"] == CAL_REPLY and "voice" not in tts_bodies[1]
    # the first safe point after the turn flushed the source and reset the stream
    flushed = [(t, a, f) for t, a, f in log.sp if f & RESET_STREAM]
    assert len(flushed) == 1 and flushed[0][0] >= out["t"]
    assert any(abs(t - flushed[0][0]) < 0.11 for t in log.flushes) and any(abs(t - flushed[0][0]) < 0.11 for t in log.clears)
    assert r.core.service("tts").render_preview  # no state kept: the reply voice is still the configured one
    assert r.core.service("speech").reply_voice == ""


def test_owner_preview_outside_a_turn_plays_only_when_armed(rig_factory, monkeypatch):
    r = rig_factory(cal_audio(tail=20.0), stt=[CAL_TEXT], hermes=[CAL_REPLY])
    log = spy_loop(r, monkeypatch)
    state = {}
    orig = r.lis._mark

    def mark(kind, detail=""):
        orig(kind, detail)
        if kind == "followup":                                    # the window is open now: she is not armed
            res = r.core.preview(actor="owner", scope="owner", text="Queued sample.", voice="amy", wait_s=0)
            state["queued"] = res
            state["t_ask"] = r.src.clock()
            state["until"] = r.lis.followup_until
    r.lis._mark = mark
    # count mira-audio calls around every safe point
    real_sp = r.core.safe_point
    seen = []

    def sp(armed):
        before = len(r.audio.calls())
        flags = real_sp(armed)
        seen.append((r.src.clock(), armed, flags, len(r.audio.calls()) - before))
        return flags
    r.core.safe_point = sp
    r.lis.run()
    assert state["queued"] == {"queued": True, "voice": "amy"}
    played = [(t, armed, flags, n) for t, armed, flags, n in seen if n and t >= state["t_ask"]]
    assert len(played) == 1
    t, armed, flags, n = played[0]
    assert armed and t >= state["until"] and flags & RESET_STREAM and n == 3   # duck, say, unduck at the first armed point
    assert not any(n for t2, a2, f2, n in seen if t2 >= state["t_ask"] and not a2)
    kinds = audio_kinds(r)
    assert kinds == ["duck", "say", "unduck"] * 2                 # the reply, then the queued sample


def test_an_agent_cannot_preview_outside_a_voice_turn(rig_factory):
    import pytest
    from mirakit.plugin import PolicyError
    r = rig_factory(cal_audio(1.0))
    with pytest.raises(PolicyError):
        r.core.preview(actor="hermes", scope="agent", text="x", voice="amy", wait_s=0)
    assert r.audio.calls() == []
