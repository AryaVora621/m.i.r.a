"""speaking: the first-chunk rule, the sentence guard, the stream splitter, caption timing and the Speaker pipeline."""
import io
import re
import threading
import time
import wave

import pytest

from listener_plugins import speaking as sp
from listener_plugins.speech_link import SpeechOffline

REPLY = ('Printers are up on the wall (print studio, 5 minutes). I took "duty printers" to mean your 3D printers. '
         'If you meant a different screen, tell me which one.')


def wav(seconds, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def test_first_chunk_is_the_first_sentence():
    chunks = sp.split_for_speech(REPLY, 280, 3, 90)
    assert chunks[0] == "Printers are up on the wall (print studio, 5 minutes)."
    assert " ".join(chunks).startswith(chunks[0])
    assert len(chunks) == 2 and chunks[1].startswith("I took")


def test_first_chunk_off_packs_like_before():
    assert sp.split_for_speech(REPLY, 280, 3, 0) == [REPLY]


def test_a_long_first_sentence_is_cut_at_a_clause():
    text = "The forecast for the rest of the day is cloudy with a few light showers, and then it clears up tonight. Enjoy."
    head, rest = sp.cut_first(text, 90)
    assert head == "The forecast for the rest of the day is cloudy with a few light showers," and rest.startswith("and then")


def test_short_text_is_not_cut():
    assert sp.cut_first("Printers are up.", 90) == (None, "Printers are up.")
    assert sp.split_for_speech("Printers are up.", 280, 3, 90) == ["Printers are up."]


def test_decimals_and_times_are_not_sentence_ends():
    assert sp.split_sentences("It is 3.5 degrees at 5.30 today. Cold.") == ["It is 3.5 degrees at 5.30 today.", "Cold."]


def test_sentence_guard():
    assert sp.limit_sentences(REPLY, 2).endswith("3D printers.")
    assert sp.limit_sentences(REPLY, 0) == REPLY
    assert sp.limit_sentences("One. Two.", 5) == "One. Two."


def test_stream_splitter_speaks_the_first_sentence_before_the_answer_ends():
    s = sp.StreamSplitter(first_chars=90, max_sentences=0)
    out = []
    for piece in ["Printers are", " up on the wall", " (print studio, 5 minutes)."]:
        out += s.feed(piece)
    assert out == []  # a sentence is complete only once the space after its end has arrived
    out += s.feed(" I took")
    assert out == ["Printers are up on the wall (print studio, 5 minutes)."]
    s.feed(' "duty printers" to mean')
    rest = s.finish(REPLY)
    assert rest[0].startswith("I took") and rest[-1].endswith("which one.")


def test_stream_splitter_stops_at_the_sentence_cap_and_keeps_text_whole():
    s = sp.StreamSplitter(first_chars=90, max_sentences=2)
    out = s.feed(REPLY[:120])
    out += s.finish(REPLY)
    assert len(out) == 2 and out[1].endswith("3D printers.")


def test_stream_splitter_cuts_a_long_first_sentence_at_its_clause_while_streaming():
    s = sp.StreamSplitter(first_chars=60, max_sentences=0)
    out = s.feed("The forecast for the rest of the day is cloudy with a few light showers, and then it clears")
    assert out == ["The forecast for the rest of the day is cloudy with a few light showers,"]


def test_stream_finish_prefers_the_completed_text_when_it_extends_the_deltas():
    s = sp.StreamSplitter(first_chars=0)
    s.feed("Hello there. How")
    assert s.finish("Hello there. How are you?") == ["How are you?"]


def test_stream_finish_without_deltas_uses_the_final_text():
    assert sp.StreamSplitter().finish("Just this.") == ["Just this."]


def test_stream_splitter_total_length_cap():
    s = sp.StreamSplitter(first_chars=0, max_chars=30)
    out = s.feed("Aaaaaaaaaaaaaaaaaaaa. Bbbbbbbbbbbbbbbbbbbb. Cccccccccc. ")
    assert out == ["Aaaaaaaaaaaaaaaaaaaa.", "Bbbbbbbbbbbbbbbbbbbb."]


def test_caption_words_groups_of_two_to_four():
    groups = sp.caption_words("Printers are up on the wall (print studio, 5 minutes).")
    assert all(2 <= len(g.split()) <= 4 for g in groups)
    assert " ".join(groups) == "Printers are up on the wall (print studio, 5 minutes)."
    assert not any(g.split()[-1].lower() in {"the", "a", "of", "on"} for g in groups[:-1])


def test_caption_schedule_spans_the_audio_and_orders_groups():
    sched = sp.caption_schedule(REPLY, 7.8)
    offs = [o for o, _ in sched]
    assert offs[0] == 0.0 and offs == sorted(offs) and offs[-1] < 7.8
    assert all(b - a >= 0.45 for a, b in zip(offs, offs[1:]))  # none flashes past
    assert " ".join(g for _, g in sched) == REPLY


def test_caption_schedule_short_chunk_is_one_group():
    assert sp.caption_schedule("Printers are up.", 1.0) == [(0.0, "Printers are up.")]
    assert sp.caption_schedule("", 1.0) == []


def test_wav_seconds():
    assert sp.wav_seconds(wav(1.5)) == pytest.approx(1.5, abs=0.01)
    assert sp.wav_seconds(b"junk") == 0.0


# -- the pipeline ------------------------------------------------------------------------------------------------

class FakeTurn:
    def __init__(self):
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.ducked = False
        self.unducked = False
        self.phase = "thinking"
        self.guarded = False
        self.speaking_since = 0.0

    @property
    def live(self):
        return not self.cancel.is_set()


class FakeLis:
    """Just the parts of the facade a Speaker uses; every call is logged with its time."""

    def __init__(self, synth_s=0.05, play_s=0.3, fail_at=None):
        self.t0 = time.monotonic()
        self.log = []
        self.synth_s, self.play_s, self.fail_at = synth_s, play_s, fail_at
        self._heard = "bring up"
        self.src = self
        self.rep = self
        self.n = 0

    def now(self):
        return round(time.monotonic() - self.t0, 3)

    def clock(self):
        return time.monotonic()

    def _audio(self, *cmd):
        self.log.append((self.now(), "audio", cmd[0]))
        time.sleep(0.15 if cmd[0] == "duck" else 0)

    def _unduck(self, t):
        with t.lock:
            if not t.ducked or t.unducked:
                return
            t.unducked = True
        self.log.append((self.now(), "audio", "unduck"))

    def _guarded(self, text):
        return "mira" in text.lower()

    def _mark(self, kind, detail=""):
        self.log.append((self.now(), "mark", kind))

    def _next_wav(self, q, t):
        while True:
            try:
                return q.get(timeout=0.05)
            except Exception:
                if t.cancel.is_set():
                    return None

    def update(self, state, heard="", reply="", fast=False):
        self.log.append((self.now(), "post", state, reply))

    def _say_file(self, path, wav_bytes, text=None, turn=None):
        self.log.append((self.now(), "play", text))
        time.sleep(self.play_s)

    def make(self, text, pin=False):
        self.n += 1
        if self.fail_at is not None and self.n == self.fail_at:
            raise SpeechOffline("down")
        time.sleep(self.synth_s)
        self.log.append((self.now(), "tts", text))
        return wav(self.play_s)


def _speaker(lis, t, **kw):
    lis.tts = type("T", (), {"tts": staticmethod(lis.make)})()
    return sp.Speaker(lis, t, **kw)


def test_speaker_ducks_with_the_first_synthesis_and_plays_in_order():
    lis, t = FakeLis(), FakeTurn()
    s = _speaker(lis, t, overlap_s=0.0)
    s.feed("First one.")
    s.feed("Second one.")
    s.end()
    s.wait()
    kinds = [(e[1], e[2]) for e in lis.log]
    assert kinds.index(("audio", "duck")) < kinds.index(("play", "First one."))
    assert [e[2] for e in lis.log if e[1] == "play"] == ["First one.", "Second one."]
    assert ("audio", "unduck") in kinds and t.phase == "speaking" and s.spoken == ["First one.", "Second one."]
    # the speaking state is posted once the first audio is ready, with no caption yet (the playback posts those)
    post = next(e for e in lis.log if e[1] == "post")
    assert post[2:] == ("speaking", "")


def test_speaker_synthesises_the_next_chunk_while_the_current_one_plays():
    lis, t = FakeLis(synth_s=0.05, play_s=0.4), FakeTurn()
    s = _speaker(lis, t, overlap_s=0.0)
    for c in ("One.", "Two.", "Three."):
        s.feed(c)
    s.end()
    s.wait()
    tts_at = {e[2]: e[0] for e in lis.log if e[1] == "tts"}
    play_at = {e[2]: e[0] for e in lis.log if e[1] == "play"}
    assert tts_at["Three."] < play_at["Two."] + 0.4   # made before chunk two finished playing
    assert play_at["Two."] - play_at["One."] >= 0.38  # played one after the other


def test_speaker_overlap_starts_the_next_player_early():
    lis, t = FakeLis(play_s=0.5), FakeTurn()
    s = _speaker(lis, t, overlap_s=0.2)
    s.feed("One.")
    s.feed("Two.")
    s.end()
    s.wait()
    play_at = {e[2]: e[0] for e in lis.log if e[1] == "play"}
    assert 0.25 <= play_at["Two."] - play_at["One."] <= 0.4


def test_speaker_with_no_audio_raises_offline_and_unducks():
    lis, t = FakeLis(fail_at=1), FakeTurn()
    s = _speaker(lis, t)
    s.feed("Hello.")
    s.end()
    with pytest.raises(SpeechOffline):
        s.wait()
    assert ("audio", "unduck") in [(e[1], e[2]) for e in lis.log]


def test_speaker_keeps_what_it_already_said_when_a_later_chunk_fails():
    lis, t = FakeLis(fail_at=2), FakeTurn()
    s = _speaker(lis, t)
    s.feed("One.")
    s.feed("Two.")
    s.end()
    s.wait()  # no raise: the first chunk was spoken
    assert s.spoken == ["One."]


def test_speaker_cancel_stops_everything():
    lis, t = FakeLis(play_s=1.0), FakeTurn()
    s = _speaker(lis, t, overlap_s=0.0)
    s.feed("One.")
    s.feed("Two.")
    s.feed("Three.")
    time.sleep(0.3)
    t.cancel.set()
    start = time.monotonic()
    s.wait()
    assert time.monotonic() - start < 1.2
    assert [e[2] for e in lis.log if e[1] == "play"] == ["One."]


def test_speaker_journals_time_to_first_audio(capsys):
    lis, t = FakeLis(synth_s=0.1), FakeTurn()
    s = _speaker(lis, t)
    s.feed("Hello there.")
    s.end()
    s.wait()
    out = capsys.readouterr()
    assert re.search(r"first_audio \d+ ms after the text", out.err + out.out)


def test_speaker_guarded_chunk_sets_the_turn_flag():
    lis, t = FakeLis(), FakeTurn()
    s = _speaker(lis, t)
    s.feed("Ask Mira anything.")
    s.end()
    s.wait()
    assert t.guarded
