import time

import pytest

import mira_listen as ml
from conftest import FakeServer


@pytest.mark.parametrize("text,intent", [
    ("play", "play"), ("Play the music.", "play"), ("pause", "pause"), ("Pause the music", "pause"),
    ("next", "next"), ("skip this song", None), ("Skip the song", "next"), ("what's playing", "status"),
    ("What is playing right now?", "status"), ("volume up", "volume_up"), ("turn it down", "volume_down"),
    ("Turn the volume down", "volume_down"), ("play Taylor Swift", None), ("what's on my calendar today", None),
    ("play the next assignment", None),
])
def test_music_intent(text, intent):
    assert ml.music_intent(text) == intent


@pytest.mark.parametrize("raw,out", [
    ("Mira, what's on my calendar?", "what's on my calendar?"),
    ("Hey Mira what time is it", "what time is it"),
    ("What did Mira say", "What did Mira say"),
])
def test_strip_wake(raw, out):
    assert ml.strip_wake(raw) == out


def test_clean_for_speech_drops_markdown():
    t = ml.clean_for_speech("## Today\n- **Physics** at [3pm](http://x.y)\n- `code` done\nSee https://a.b/c")
    assert "*" not in t and "#" not in t and "http" not in t and "`" not in t
    assert "Physics at 3pm" in t


def test_speech_chunks_cap():
    text = " ".join(f"This is sentence number {i} of a very long answer." for i in range(60))
    ch = ml.speech_chunks(text)
    assert 1 <= len(ch) <= 3 and all(len(c) <= 280 for c in ch)


def test_reporter_rate_order_and_coalescing(tmp_path):
    s = FakeServer()
    s.routes[("POST", "/h")] = lambda b, h: (200, b"{}", "application/json")
    (tmp_path / "t").write_text("tok")
    rep = ml.VoiceReporter(s.url + "/h", str(tmp_path / "t"), error_hold=0.0)
    t0 = time.monotonic()
    for st in ("idle", "listening", "thinking", "speaking", "idle"):
        rep.update(st)
    rep.update("speaking", "", "a")  # text change: separate post only if the state was already sent
    rep.close()
    import json
    states = [json.loads(c["body"])["state"] for c in s.calls]
    assert states[:5] == ["idle", "listening", "thinking", "speaking", "idle"]
    # at most 2 per second: 5 posts need at least 4 gaps of 0.5 s
    assert time.monotonic() - t0 >= 1.9
    assert s.calls[0]["headers"]["authorization"] == "Bearer tok"
    s.stop()


@pytest.mark.parametrize("text,ok", [
    ("Mira, what's on my calendar?", True), ("Hey Mira pause the music", True), ("Beerah paused the music.", True),
    ("Meera what time is it", True), ("Miura, what's on my calendar today?", True), ("Myra, play", True), ("Viera paused the music", True), ("Vera paused the music.", True),
    ("Mirror, what's on my calendar?", False), ("Maria, what's on my calendar", False), ("Mara, hi", False),
    ("Miranda is coming at six", False), ("The miracle store", False), ("what's on my calendar", False),
    ("Hey Siri", False), ("Mia", False),
])
def test_has_wake(text, ok):
    assert ml.has_wake(text) is ok
