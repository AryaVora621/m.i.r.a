"""Integration: she speaks while Hermes is still answering, speaks only the sentences she is allowed to, and does not talk
over a half-spoken answer with the timeout line. Hermes is replaced by a function that hands over its pieces on cue
(the golden fake answers a stream in one go); everything after it is the real facade, speaker and recording fakes."""
import time

from test_int_facade import CAL_TEXT, cal_audio, ml, rig_factory  # noqa: F401
from listener_plugins.brain_hermes import HermesTimeout

FULL = "Printers are up. I took it to mean 3D. Third sentence here."


def spoken(r):
    return [b["text"] for b in r.speech.json_bodies("/tts")]


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def says(r):
    return [ln for ln in r.audio.calls() if ln.startswith("say")]


def test_first_sentence_is_spoken_before_the_answer_is_complete(rig_factory, capsys):
    r = rig_factory(cal_audio(tail=4.0), stt=[CAL_TEXT], hermes=["unused"])
    seen = {}

    def ask(text, ttl, now, cancel=None, on_text=None):
        on_text("Printers are up. ")
        on_text("I took it ")
        seen["said_early"] = wait_for(lambda: bool(says(r)))
        seen["tts_early"] = spoken(r)[:]
        on_text("to mean 3D. Third sentence here.")
        return FULL

    r.core.service("brain").ask = ask
    r.lis.run()
    assert seen["said_early"] and seen["tts_early"] == ["Printers are up."]
    # the sentence cap (two by default) drops the third sentence from the speech, not from the wall's text
    assert spoken(r) == ["Printers are up.", "I took it to mean 3D."]
    assert [ln.split()[0] for ln in r.audio.calls()] == ["duck", "say", "say", "unduck"]
    posts = [b for _, b in r.hook.posts()]
    assert posts[-1]["state"] == "idle" and posts[-1]["reply"] == FULL
    assert "first_audio" in capsys.readouterr().err


def test_a_timeout_after_speech_has_begun_adds_no_apology(rig_factory):
    r = rig_factory(cal_audio(tail=4.0), stt=[CAL_TEXT], hermes=["unused"])

    def ask(text, ttl, now, cancel=None, on_text=None):
        on_text("Printers are up. And then")
        assert wait_for(lambda: bool(says(r)))
        raise HermesTimeout()

    r.core.service("brain").ask = ask
    r.lis.run()
    assert spoken(r) == ["Printers are up."]
    assert "Hermes is taking too long." not in " ".join(spoken(r))
    assert [b["state"] for _, b in r.hook.posts()].count("error") == 0


def test_a_timeout_before_any_speech_still_says_so(rig_factory):
    r = rig_factory(cal_audio(tail=4.0), stt=[CAL_TEXT], hermes=["unused"])

    def ask(text, ttl, now, cancel=None, on_text=None):
        on_text("Still thinking about")  # no sentence end: nothing was spoken
        raise HermesTimeout()

    r.core.service("brain").ask = ask
    r.lis.run()
    assert spoken(r) == ["Hermes is taking too long."]


def test_streaming_off_still_applies_the_sentence_cap(rig_factory):
    r = rig_factory(cal_audio(tail=4.0), stt=[CAL_TEXT], hermes=[FULL])
    r.core.service("brain").stream_speech = False
    r.lis.run()
    assert spoken(r) == ["Printers are up. I took it to mean 3D."]  # two short sentences pack into one chunk
    assert [b for _, b in r.hook.posts()][-1]["reply"] == FULL


def test_sentence_cap_zero_speaks_everything(rig_factory):
    r = rig_factory(cal_audio(tail=4.0), stt=[CAL_TEXT], hermes=[FULL])
    brain = r.core.service("brain")
    brain.stream_speech, brain.speak_max_sentences = False, 0
    r.lis.run()
    assert " ".join(spoken(r)) == FULL or spoken(r) == [FULL]


def test_a_cancelled_turn_speaks_nothing_more(rig_factory):
    from listener_plugins.brain_hermes import HermesCancelled
    r = rig_factory(cal_audio(tail=4.0), stt=[CAL_TEXT], hermes=["unused"])

    def ask(text, ttl, now, cancel=None, on_text=None):
        on_text("Printers are up. ")
        assert wait_for(lambda: bool(says(r)))
        cancel.set()
        on_text("Another sentence. ")
        raise HermesCancelled()

    r.core.service("brain").ask = ask
    r.lis.run()
    assert "Another sentence." not in spoken(r)


def test_idle_post_follows_the_speech(rig_factory):
    r = rig_factory(cal_audio(tail=4.0), stt=[CAL_TEXT], hermes=[FULL])
    r.lis.run()
    states = [b["state"] for _, b in r.hook.posts()]
    assert states.index("speaking") < len(states) - 1 and states[-1] == "idle"
