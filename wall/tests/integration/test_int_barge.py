"""Integration: barge-in, cancel words and the answer deadline inside the real facade and core (rig from test_int_facade).

The microphone is a paced live source: audio goes in when the test says so, while a turn is running on its worker thread.
Hermes is a server that stalls, mira-audio is a script whose `say` runs until it is terminated. Nothing plays and nothing
records; every port is an ephemeral one on 127.0.0.1.
"""
import json
import queue
import stat
import threading
import time

import numpy as np
import pytest

from golden import fakes, scenarios
import mira_listen as ml
from test_int_facade import CAL_TEXT, Rig, cal_audio, rig_factory  # noqa: F401  (the fixture is imported by name)

CHUNK, RATE = ml.CHUNK, ml.RATE


class PacedSource:
    """A live microphone stand-in: read() blocks until the test feeds audio; the clock is real time."""

    live = True

    def __init__(self):
        self.q = queue.Queue()

    def feed(self, x):
        x = np.asarray(x, dtype=np.float32)
        for i in range(0, len(x), CHUNK):
            c = x[i:i + CHUNK]
            if len(c) < CHUNK:
                c = np.concatenate([c, np.zeros(CHUNK - len(c), dtype=np.float32)])
            self.q.put(c)

    def eof(self):
        self.q.put(None)

    def read(self):
        return self.q.get()

    def clock(self):
        return time.monotonic()

    def flush(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def close(self):
        pass


def quiet(seconds, seed=1):
    return scenarios.silence(seconds, seed=seed)


class StallHermes(fakes.FakeHermes):
    """Answers like FakeHermes, except that the chat stream of the call indexes in `stall` opens and then goes quiet."""

    def __init__(self, replies=(), stall=(0,)):
        self.stall, self.release, self.opened = set(stall), threading.Event(), threading.Event()
        super().__init__(replies)

    def respond(self, method, path, body, headers, index):
        if method == "POST" and path.endswith("/chat/stream") and index in self.stall:
            self.opened.set()
            self.release.wait(30)
            return 200, fakes.sse([("assistant.completed", {"content": "too late"})]), "text/event-stream"
        return super().respond(method, path, body, headers, index)


def _slow_say(rig, seconds=30):
    """mira-audio whose `say` lasts `seconds` unless terminated, logging start and termination."""
    log = rig.audio.log
    rig.audio.bin.write_text(f"""#!/bin/sh
if [ "$1" = say ]; then
  echo say-start >> "{log}"
  trap 'echo say-term >> "{log}"; kill $! 2>/dev/null; exit 143' TERM
  sleep {seconds} &
  wait $!
  echo say-end >> "{log}"
else
  echo "$1" >> "{log}"
fi
exit 0
""")
    rig.audio.bin.chmod(rig.audio.bin.stat().st_mode | stat.S_IEXEC)


def wait_for(cond, timeout=15.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {what}")


class Live:
    """A listener running on a paced source in a thread."""

    def __init__(self, rig):
        self.rig, self.src = rig, PacedSource()
        self.lis = ml.Listener(rig.args, self.src, rig.core)
        self.lis.hermes.fail_reply = rig.lis.hermes.fail_reply
        self.thread = threading.Thread(target=self.lis.run, daemon=True)
        self.thread.start()

    def marks(self):
        return [k for k, _ in self.lis.events]

    def phase(self):
        t = self.lis._turn
        return t.phase if t is not None else None

    def finish(self):
        """End of file, repeated: a finished turn flushes the source queue, which would swallow a single marker."""
        for _ in range(40):
            self.src.eof()
            self.thread.join(0.5)
            if not self.thread.is_alive():
                return
        pytest.fail("the listener did not stop at end of file")


@pytest.fixture
def live(rig_factory):
    lives = []

    def make(**kw):
        r = rig_factory(quiet(1.0), **kw)
        l = Live(r)
        lives.append(l)
        return l
    yield make
    for l in lives:
        if hasattr(l.rig.hermes, "release"):
            l.rig.hermes.release.set()
        for _ in range(20):
            l.src.eof()
            l.thread.join(0.5)
            if not l.thread.is_alive():
                break


PAUSE = None


def pause_clip():
    return scenarios.pause(ml)


def test_barge_in_during_thinking_cancels_hermes_and_takes_the_new_request(live):
    l = live(stt=["Mira, what's on my calendar today?", "Mira, never mind"], hermes_cls=lambda replies: StallHermes(replies))
    l.src.feed(cal_audio(1.5))
    wait_for(lambda: l.phase() == "thinking" and l.rig.hermes.opened.is_set(), what="a thinking turn with Hermes stalled")
    first = l.lis._turn
    t0 = time.monotonic()
    l.src.feed(quiet(1.0, seed=3))
    l.src.feed(pause_clip())
    l.src.feed(quiet(1.5, seed=2))
    wait_for(lambda: "barge" in l.marks(), what="the barge mark")
    assert time.monotonic() - t0 < 4.0
    assert first.cancel.is_set()
    wait_for(lambda: "cancel" in l.marks(), what="the cancel word")
    marks = l.marks()
    assert "reply" not in marks and "followup" not in marks
    assert [c for c in l.rig.audio.calls() if c.startswith("say")] == []
    assert l.lis.followup_until == 0.0
    # Hermes was asked once, and the stalled request was not asked again
    assert len([c for c in l.rig.hermes.calls if c["path"].endswith("/chat/stream")]) == 1
    wait_for(lambda: l.lis._turn is None or l.lis._turn.done.is_set(), what="the cancel turn to end")
    l.rig.hermes.release.set()
    l.finish()


def test_barge_in_during_speaking_stops_the_sound_at_once_and_unducks(live):
    l = live(stt=["Mira, what's on my calendar today?", "Mira, stop"], hermes=["You have nothing today."])
    _slow_say(l.rig)
    l.src.feed(cal_audio(1.5))
    wait_for(lambda: "say-start" in l.rig.audio.calls(), what="speech to start")
    wait_for(lambda: l.phase() == "speaking" and l.src.clock() - l.lis._turn.speaking_since > 0.8, what="the ignore window")
    first = l.lis._turn
    t0 = time.monotonic()
    l.src.feed(quiet(1.0, seed=3))
    l.src.feed(pause_clip())
    l.src.feed(quiet(1.5, seed=2))
    wait_for(lambda: "say-term" in l.rig.audio.calls(), what="playback to be terminated")
    assert time.monotonic() - t0 < 4.0
    assert first.cancel.is_set() and "say-end" not in l.rig.audio.calls()
    wait_for(lambda: "cancel" in l.marks(), what="the cancel word")
    calls = l.rig.audio.calls()
    assert calls.count("unduck") == 1 and calls.index("unduck") > calls.index("say-term") - 1
    # a bare "stop" that interrupted her is a cancel, not the music command
    assert not any(c["path"].startswith("/x/echo/pause") for c in l.rig.music.calls)
    l.finish()


def test_the_wall_gets_the_state_after_a_barge(live):
    l = live(stt=["Mira, what's on my calendar today?", "Mira, never mind"], hermes_cls=lambda replies: StallHermes(replies))
    l.src.feed(cal_audio(1.5))
    wait_for(lambda: l.rig.hermes.opened.is_set())
    l.src.feed(quiet(1.0, seed=3))
    l.src.feed(pause_clip())
    l.src.feed(quiet(1.5, seed=2))
    wait_for(lambda: "cancel" in l.marks())
    wait_for(lambda: l.lis._turn is None or l.lis._turn.done.is_set())
    def settled():
        st = [p["state"] for _, p in l.rig.hook.posts()]
        return "thinking" in st and "listening" in st[st.index("thinking"):] and st[-1] == "idle" and len(st) > 3

    wait_for(settled, timeout=30, what="the wall to settle on idle")   # posts are spaced half a second apart
    states = [p["state"] for _, p in l.rig.hook.posts()]
    assert "thinking" in states and "listening" in states[states.index("thinking"):] and "speaking" not in states
    l.rig.hermes.release.set()
    l.finish()


def test_her_own_reply_with_a_name_like_word_cannot_cut_her_off(live):
    l = live(stt=["Mira, what's on my calendar today?"], hermes=["Mirror, mirror: nothing today."])
    _slow_say(l.rig, seconds=3)
    l.src.feed(cal_audio(1.5))
    wait_for(lambda: l.phase() == "speaking")
    assert l.lis._turn.guarded
    time.sleep(0.9)
    l.src.feed(quiet(1.0, seed=3))
    l.src.feed(pause_clip())     # what the spotter does with her own voice
    l.src.feed(quiet(1.0, seed=2))
    wait_for(lambda: "barge_ignored" in l.marks(), what="the hit to be ignored")
    assert "barge" not in l.marks() and "say-term" not in l.rig.audio.calls()
    wait_for(lambda: "say-end" in l.rig.audio.calls(), what="her to finish speaking")
    wait_for(lambda: "followup" in l.marks())
    l.finish()


def test_the_first_moments_of_speech_are_an_ignore_window(live):
    l = live(stt=["Mira, what's on my calendar today?"], hermes=["You have nothing today."])
    l.lis._lc.barge_ignore_s = 30.0
    _slow_say(l.rig, seconds=3)
    l.src.feed(cal_audio(1.5))
    wait_for(lambda: l.phase() == "speaking")
    l.src.feed(quiet(1.0, seed=3))
    l.src.feed(pause_clip())
    l.src.feed(quiet(1.0, seed=2))
    wait_for(lambda: "barge_ignored" in l.marks())
    assert "barge" not in l.marks()
    wait_for(lambda: "say-end" in l.rig.audio.calls())
    l.finish()


def test_barge_in_off_keeps_the_sequential_loop(live):
    l = live(stt=["Mira, what's on my calendar today?"], hermes=["You have nothing today."])
    l.lis._lc.barge_in = False
    assert l.lis._barge_on() is False
    l.src.feed(cal_audio(1.5))
    wait_for(lambda: "followup" in l.marks(), what="the turn to finish inline")
    assert l.lis._turn is None
    l.finish()


def test_a_stt_phase_turn_is_not_interruptible(live):
    """A hit while the clip is still being transcribed is ignored (the user is not talking over her yet)."""
    l = live(stt=["Mira, what's on my calendar today?"], hermes=["You have nothing today."])
    t = ml._Turn()
    l.lis._turn = None
    assert t.phase == "stt"
    assert l.lis._barge_refusal(t) is None  # the loop does not even feed the spotter in this phase
    l.finish()


# ---------------------------------------------------------------------------------- sequential (WAV) behaviour

def replies(r):
    return [d for k, d in r.lis.events if k == "reply"]


def test_a_stalled_hermes_ends_at_the_deadline_with_a_short_line_and_the_window_opens(rig_factory):
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes_cls=lambda replies: StallHermes(replies))
    r.lis.hermes.deadline_s = 1.0
    t0 = time.monotonic()
    try:
        r.run()
    finally:
        r.hermes.release.set()
    assert time.monotonic() - t0 < 6.0
    assert replies(r) == ["Hermes is taking too long."]
    assert any(c.startswith("say") for c in r.audio.calls())
    assert r.marks()[-1] == "eof" and "followup" in r.marks()
    states = [p["state"] for _, p in r.hook.posts()]
    assert "error" in states


def test_the_timeout_reply_is_a_knob_and_counts_as_a_failure_source(rig_factory):
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes_cls=lambda replies: StallHermes(replies))
    r.lis.hermes.deadline_s, r.lis.hermes.timeout_reply = 1.0, "Too slow."
    sources = []
    r.core.on("turn.replied", lambda p: sources.append(p["source"]))
    try:
        r.run()
    finally:
        r.hermes.release.set()
    assert replies(r) == ["Too slow."] and sources == ["fail"]


@pytest.mark.parametrize("said", ["Mira, never mind.", "Mira, cancel", "Mira, forget it", "Mira, nevermind", "Mira, stop talking",
                                  "Mira, cancel that please"])
def test_cancel_words_say_nothing_and_never_reach_hermes(rig_factory, said):
    r = rig_factory(cal_audio(), stt=[said], hermes=["should not be asked"])
    r.run()
    assert "cancel" in r.marks() and "reply" not in r.marks() and "followup" not in r.marks()
    assert [c for c in r.hermes.calls if c["path"].endswith("/chat/stream")] == []
    assert not any(c.startswith("say") for c in r.audio.calls())
    assert r.lis.followup_until == 0.0


def test_the_cancel_reply_knob_can_say_okay(rig_factory):
    r = rig_factory(cal_audio(), stt=["Mira, never mind"], hermes=["x"])
    r.lis._lc.cancel_reply = "Okay."
    r.run()
    assert replies(r) == [] and any(c.startswith("say") for c in r.audio.calls())
    assert [c for c in r.hermes.calls if c["path"].endswith("/chat/stream")] == []


def test_a_bare_stop_with_nothing_interrupted_is_still_the_music_command(rig_factory):
    r = rig_factory(cal_audio(), stt=["Mira, stop"], hermes=["x"])
    r.run()
    assert "music" in r.marks() and "cancel" not in r.marks()
    assert any(c["path"] == "/x/echo/pause" for c in r.music.calls)
    assert replies(r) == ["Paused."]


def test_a_bare_stop_that_interrupted_a_turn_is_a_cancel(rig_factory):
    r = rig_factory(quiet(1.0), stt=["Mira, stop"], hermes=["x"])
    r.lis.handle_clip(scenarios.pause(ml), verify=True, interrupted=True)
    assert "cancel" in r.marks() and "music" not in r.marks()
    assert not any(c["path"] == "/x/echo/pause" for c in r.music.calls)
    assert not any(c.startswith("say") for c in r.audio.calls())


def test_cancel_words_do_not_catch_real_requests(rig_factory):
    for said in ("Mira, can you cancel my 3 pm meeting", "Mira, stop the print", "Mira, what does never mind mean"):
        r = rig_factory(quiet(1.0), stt=[said], hermes=["Okay."])
        r.lis.handle_clip(scenarios.pause(ml), verify=True)
        assert "cancel" not in r.marks(), said


def test_barge_trim_keeps_a_real_request_after_her_own_last_words(rig_factory):
    r = rig_factory(quiet(1.0))
    assert r.lis._barge_trim("of the week. Mira, never mind") == "Mira, never mind"
    assert r.lis._barge_trim("Mira, stop") == "Mira, stop"
    assert r.lis._barge_trim("nothing at all here friends, today ok then") == "nothing at all here friends, today ok then"
    assert r.lis._barge_trim("") == ""
