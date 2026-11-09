"""End to end against the REAL mini speech server (STT and TTS); Hermes, the hook, music-api and
mira-audio are fakes. Each wav is silence + a noise floor, the utterance, then silence."""
import pytest

import mira_listen as ml
from conftest import compose, hook_states, run_listener, silence, write_wav

CAL = "Mira, what's on my calendar today?"


def test_calendar_roundtrip(speech, say_wake, secrets, fake_audio, hermes, hook, music, tmp_path):
    wav = write_wav(tmp_path / "cal.wav", compose(2.0, say_wake(CAL), 3.0))
    lis = run_listener(wav, secrets, fake_audio, hermes, hook, music)

    assert lis.wakes == 1
    heard = [d for k, d in lis.events if k == "heard"]
    assert len(heard) == 1 and "calendar" in heard[0].lower(), heard
    # Hermes got a session, then the text with the persona as the per-turn instructions.
    assert [c["path"] for c in hermes.calls][:2] == ["/api/sessions", "/api/sessions/sess_test/chat/stream"]
    msg = hermes.json_bodies("/api/sessions/sess_test/chat/stream")[0]
    assert "calendar" in msg["message"].lower() and "mira" not in msg["message"].lower()
    assert "Mira" in msg["instructions"] and "aloud" in msg["instructions"]
    assert hermes.calls[1]["headers"]["authorization"] == "Bearer hermes-test-key"
    # TTS audio came back and was handed to mira-audio, in order, ducked.
    calls = fake_audio.calls()
    assert calls[0] == "duck" and calls[-1] == "unduck" and "say" in calls, calls
    said = fake_audio.said()
    assert said and said[0].read_bytes()[:4] == b"RIFF" and said[0].stat().st_size > 20000
    # State trail for the wall.
    assert hook_states(hook) == ["idle", "listening", "thinking", "speaking", "idle"]
    last = hook.json_bodies("/api/hooks/voice")[-1]
    assert "calendar" in last["heard"].lower() and last["reply"].startswith("You have")
    assert hook.calls[0]["headers"]["authorization"] == "Bearer voice-test-token"
    assert not music.calls


@pytest.mark.parametrize("text", [
    "What's on my calendar today?",            # speech, no wake word
    "Mirror, what's on my calendar today?",    # lookalikes
    "Maria, what's on my calendar today?",
    "Hey Siri, what's on my calendar today?",
])
def test_no_wake_no_trigger(speech, say_wav, secrets, fake_audio, hermes, hook, tmp_path, text):
    wav = write_wav(tmp_path / "neg.wav", compose(2.0, say_wav(text), 3.0))
    lis = run_listener(wav, secrets, fake_audio, hermes, hook)
    # The spotter may fire on a look-alike; the transcript check must then stop it before any reaction.
    assert not hermes.calls and not fake_audio.calls() and not lis.heard_texts()
    assert set(hook_states(hook)) <= {"idle", "listening", "thinking"}


def test_music_intent_stays_local(speech, say_wake, say_wav, secrets, fake_audio, hermes, hook, music, tmp_path):
    wav = write_wav(tmp_path / "pause.wav", compose(2.0, say_wake("Mira, pause the music."), 3.0))
    lis = run_listener(wav, secrets, fake_audio, hermes, hook, music)
    assert lis.wakes == 1
    assert not hermes.calls
    pauses = [c for c in music.calls if c["path"] == "/x/echo/pause"]
    assert len(pauses) == 1 and pauses[0]["method"] == "POST"
    h = pauses[0]["headers"]
    assert (h["x-music-user"], h["x-music-token"], h["x-music-salt"]) == ("arya", "tok123", "salt456")
    assert fake_audio.calls()[0] == "duck" and "say" in fake_audio.calls()


def test_followup_window_needs_no_wake_word(speech, say_wake, say_wav, secrets, fake_audio, hermes, hook, music, tmp_path):
    wav = write_wav(tmp_path / "two.wav",
                    compose(2.0, say_wake(CAL), 3.0, say_wav("And what about tomorrow?"), 3.0))
    lis = run_listener(wav, secrets, fake_audio, hermes, hook, music)
    assert lis.wakes == 1
    msgs = [m["message"].lower() for m in hermes.json_bodies("/api/sessions/sess_test/chat/stream")]
    assert len(msgs) == 2 and "calendar" in msgs[0] and "morrow" in msgs[1]
    assert len([c for c in hermes.calls if c["path"] == "/api/sessions"]) == 1  # same conversation


def test_speech_offline_chime_and_error(speech, say_wake, secrets, fake_audio, hermes, hook, tmp_path):
    wav = write_wav(tmp_path / "off.wav", compose(2.0, say_wake(CAL), 3.0))
    lis = run_listener(wav, secrets, fake_audio, hermes, hook, speech_url="http://127.0.0.1:9")
    assert lis.wakes == 1 and not hermes.calls
    said = fake_audio.said()
    assert len(said) == 1 and said[0].read_bytes() == (ml.HERE / "assets" / "offline.wav").read_bytes()
    bodies = hook.json_bodies("/api/hooks/voice")
    errs = [b for b in bodies if b["state"] == "error"]
    assert errs and errs[0]["reply"] == "Speech is offline"
    assert hook_states(hook) == ["idle", "listening", "thinking", "error", "idle"]


def test_silence_only_does_nothing(secrets, fake_audio, hermes, hook, tmp_path):
    wav = write_wav(tmp_path / "sil.wav", silence(8.0))
    lis = run_listener(wav, secrets, fake_audio, hermes, hook, speech_url="http://127.0.0.1:9")
    assert lis.wakes == 0 and not fake_audio.calls()
