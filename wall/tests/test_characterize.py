"""Behaviour of the wall listener per scenario S1 to S17 (spec 6.3), against v1 and against the current module.

Each assertion states behaviour that nothing else tests. Results come from the recording harness (fake speech,
Hermes, hook, music-api and mira-audio), shared with the parity test through record.cached_run.
"""
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from golden import record, scenarios

WALL = Path(__file__).resolve().parent.parent
MODULES = ["mira_listen_v1", "mira_listen"]
CAL = "what's on my calendar today?"
FAIL_LINE = "I cannot reach Hermes right now."


@pytest.fixture(params=MODULES)
def run(request):
    return lambda name: record.cached_run(request.param, name)


def states(res):
    out = []
    for b in res["hook"]:
        if not out or out[-1] != b["state"]:
            out.append(b["state"])
    return out


def kinds(res):
    return [e[0] for e in res["events"]]


def chat_messages(res):
    return [h["body"]["message"] for h in res["hermes_bodies"] if h["path"].endswith("/chat/stream")]


def sessions(res):
    return [h for h in res["hermes_bodies"] if h["path"] == "/api/sessions"]


def says(res):
    return [c for c in res["audio_calls"] if c.startswith("say ")]


def test_s01_calendar_round_trip(run):
    r = run("s01_calendar_roundtrip")
    assert states(r) == ["idle", "listening", "thinking", "speaking", "idle"]
    assert kinds(r).count("wake") == 1 and "followup" in kinds(r) and r["exception"] is None
    assert chat_messages(r) == [CAL]  # the wake word is stripped
    chat = [h for h in r["hermes_bodies"] if h["path"].endswith("/chat/stream")][0]["body"]
    assert chat["instructions"] == (WALL / "MIRA_WALL_IDENTITY.md").read_text()
    assert r["audio_calls"][0] == "duck" and r["audio_calls"][-1] == "unduck" and len(says(r)) == 1
    assert r["hook"][-1]["reply"].startswith("You have a physics review") and r["hook"][-1]["heard"] == CAL
    assert r["tts_bodies"] == [{"text": scenarios.CAL_REPLY}]
    assert r["music_calls"] == [] and len(r["stt_sha256"]) == 1


def test_s02_music_stays_local_with_volume_math(run):
    r = run("s02_music_local")
    assert r["hermes_bodies"] == []
    posts = [(c["path"], json.loads(c["body"])) for c in r["music_calls"] if c["method"] == "POST"]
    assert posts[0] == ("/x/echo/pause", {})
    assert [b for p, b in posts if p == "/x/echo/volume"] == [{"volume": 60}, {"volume": 100}, {"volume": 0}]
    assert all(c["headers"] == {"x-music-user": "arya", "x-music-token": "tok123", "x-music-salt": "salt456"}
               for c in r["music_calls"])
    assert [e[1] for e in r["events"] if e[0] == "music"] == ["pause", "volume_up", "volume_up", "volume_down"]
    assert [e[1] for e in r["events"] if e[0] == "reply"] == ["Paused.", "Volume 60.", "Volume 100.", "Volume 0."]


def test_s03_followup_reuses_session_without_wake_word(run):
    r = run("s03_followup_same_session")
    assert len(sessions(r)) == 1
    paths = [h["path"] for h in r["hermes_bodies"] if h["path"].endswith("/chat/stream")]
    assert paths == ["/api/sessions/sess_1/chat/stream"] * 2
    assert chat_messages(r) == [CAL, "And what about tomorrow?"]
    assert kinds(r).count("wake") == 1 and "followup_capture" in kinds(r)


def test_s04_stt_offline_chime_and_error_trail(run):
    r = run("s04_stt_offline")
    assert states(r) == ["idle", "listening", "thinking", "error", "idle"]
    off = [e for e in r["events"] if e[0] == "offline"]
    assert len(off) == 1 and off[0][1].startswith("stt:")
    chime = hashlib.sha256((WALL / "assets" / "offline.wav").read_bytes()).hexdigest()
    assert r["audio_calls"] == [f"say offline.wav {chime}"]  # no duck around the chime
    err = [b for b in r["hook"] if b["state"] == "error"][0]
    assert err["reply"] == "Speech is offline"
    assert r["hermes_bodies"] == [] and r["tts_bodies"] == [] and "followup" not in kinds(r)


def test_s05_stt_http500_counts_as_offline(run):
    r = run("s05_stt_http500")
    assert states(r) == ["idle", "listening", "thinking", "error", "idle"]
    off = [e for e in r["events"] if e[0] == "offline"]
    assert len(off) == 1 and "500" in off[0][1] and len(r["stt_sha256"]) == 1
    assert len(says(r)) == 1 and says(r)[0].startswith("say offline.wav ")


def test_s06_hermes_failure_error_posted_before_fail_line(run):
    r = run("s06_hermes_failure")
    st = states(r)
    assert st.index("error") < st.index("speaking") and st[-1] == "idle"
    err = [b for b in r["hook"] if b["state"] == "error"][0]
    assert err["reply"] == FAIL_LINE and err["heard"] == CAL
    assert r["tts_bodies"] == [{"text": FAIL_LINE}] and len(says(r)) == 1
    raw = r["_raw_hook"]
    t_err = [t for t, b in raw if b["state"] == "error"][0]
    t_say = [t for t, b in raw if b["state"] == "speaking"][0]
    assert t_say - t_err >= 0.9  # --error-hold 1.0: the speaking post waits behind the hold


def test_s07_sse_error_event_gives_the_fail_line(run):
    r = run("s07_hermes_sse_error")
    assert [e[1] for e in r["events"] if e[0] == "reply"] == [FAIL_LINE]
    assert r["tts_bodies"] == [{"text": FAIL_LINE}]
    st = states(r)
    assert st.index("error") < st.index("speaking")


def test_s08_long_reply_is_three_chunks_of_at_most_280(run):
    r = run("s08_long_reply_capped")
    assert len(r["tts_bodies"]) == 3 and all(0 < len(t["text"]) <= 280 for t in r["tts_bodies"])
    assert len(says(r)) == 3 and r["audio_calls"][0] == "duck" and r["audio_calls"][-1] == "unduck"


def test_s09_session_ttl_expiry_starts_new_session(run):
    r = run("s09_session_ttl")
    assert len(sessions(r)) == 2
    paths = [h["path"] for h in r["hermes_bodies"] if h["path"].endswith("/chat/stream")]
    assert paths == ["/api/sessions/sess_1/chat/stream", "/api/sessions/sess_2/chat/stream"]
    assert kinds(r).count("wake") == 2 and "followup_capture" not in kinds(r)


@pytest.mark.parametrize("name,detail", [("s10_empty_transcript", "heard nothing"),
                                         ("s11_lookalike_rejected", "heard 'Mirror on the wall'")])
def test_s10_s11_reject_opens_no_followup_window(run, name, detail):
    r = run(name)
    rej = [e for e in r["events"] if e[0] == "wake_rejected"]
    assert len(rej) == 1 and rej[0][1] == detail
    assert not {"heard", "followup", "followup_capture", "empty", "wake_accepted"} & set(kinds(r))
    assert len(r["stt_sha256"]) == 1  # the later speech was never captured
    assert r["hermes_bodies"] == [] and r["audio_calls"] == []
    assert states(r) == ["idle", "listening", "thinking", "idle"]


def test_s12_bare_wake_opens_followup_window(run):
    r = run("s12_bare_wake_opens_window")
    k = kinds(r)
    assert k.index("empty") < k.index("followup_capture") < k.index("heard")
    assert chat_messages(r) == ["What time is it?"] and len(r["stt_sha256"]) == 2
    assert [c[:2] for c in r["captures"]][0][0] < r["captures"][1][0]


def test_s13_silence_posts_nothing_beyond_idle(run):
    r = run("s13_silence_only")
    assert set(kinds(r)) <= {"mic", "eof"} and states(r) == ["idle"]
    assert r["stt_sha256"] == [] and r["audio_calls"] == [] and r["captures"] == []


def test_s14_digital_zeros_mute_then_live(run):
    r = run("s14_mic_mute")
    mic = [(e[1], e[2]) for e in r["events"] if e[0] == "mic"]
    assert mic == [("muted", 10.0), ("live", 13.0)]
    mics = [b["mic"] for b in r["hook"] if "mic" in b]
    assert mics[:2] == ["muted", "live"] and mics[-1] == "live"
    assert kinds(r).count("wake") == 1 and chat_messages(r) == [CAL]


def test_s15_no_agc_level_path(run):
    r = run("s15_no_agc")
    assert r["agc_digests"] == []
    wake = [e for e in r["events"] if e[0] == "wake"][0]
    assert "agc" not in wake[1]
    assert max(b["level"] for b in r["hook"] if b["state"] == "listening") > 0
    assert states(r) == ["idle", "listening", "thinking", "speaking", "idle"]


def test_s16_invalid_stt_json_propagates_value_error(run):
    r = run("s16_stt_invalid_json")
    assert r["exception"] == "JSONDecodeError" and issubclass(json.JSONDecodeError, ValueError)
    assert "eof" not in kinds(r)  # run() ended on the exception, not at the end of the audio
    assert r["hook"][-1]["state"] == "idle"  # the finally block still posts idle


def test_s17_tts_failure_on_chunk_two_ends_silently(run):
    r = run("s17_tts_fails_chunk2")
    assert len(r["tts_bodies"]) == 2  # chunk 3 is never requested
    assert r["audio_calls"][0] == "duck" and r["audio_calls"][-1] == "unduck" and len(says(r)) == 1
    assert "offline" not in kinds(r) and "error" not in states(r)
    assert states(r) == ["idle", "listening", "thinking", "speaking", "idle"]


# ---- gate additions: behaviour the scenarios cannot see (scripted transcripts skip the wake regex; the hook
# normalization hides the reporter's dedupe and gap; mute-before-AGC is invisible in output because gain >= 1
# never turns a zero into a non-zero sample or back). Found by perturbing mira_listen in a throwaway copy.

WAKE_TABLE = {
    "Mira": (True, ""), "mira, play": (True, "play"), "Hey Mira what time": (True, "what time"),
    "hi mira": (True, ""), "ok Mira": (True, ""), "okay, Mira!": (True, ""), "Okay Mira": (True, ""),
    "hey, mira": (True, ""), "Mirror on the wall": (False, "Mirror on the wall"), "Miracle": (False, "Miracle"),
    "Miranda": (False, "Miranda"), "miracles happen": (False, "miracles happen"), "Mirra": (True, ""),
    "Meera, stop": (True, "stop"), "Mia": (False, "Mia"), "Mara": (False, "Mara"), "Mirror": (False, "Mirror"),
    "hello Mira": (False, "hello Mira"), "I said Mira": (False, "I said Mira"), "Hey  Mira": (True, ""),
    "Heymira": (False, "Heymira"), "MIRA": (True, ""), "Myra": (True, ""), "Mirah": (True, ""),
    "Mirar": (True, ""), "Mir": (False, "Mir"), "Hey there Mira": (False, "Hey there Mira"), "": (False, ""),
    " Mira ": (True, ""), "Sierra": (True, ""), "Kira ": (True, ""), "Mirrah": (True, ""),
    "Hi, mirror": (False, "Hi, mirror"),
}  # "Sierra" and "Kira" are accepted by v1 too: recorded as-is, the refactor must keep them


@pytest.fixture(params=MODULES)
def mod(request):
    import importlib
    return importlib.import_module(request.param)


def test_wake_table_pinned(mod):
    for text, (ok, stripped) in WAKE_TABLE.items():
        assert (mod.has_wake(text), mod.strip_wake(text)) == (ok, stripped), repr(text)


def test_wake_patterns_equal_v1():
    import mira_listen
    import mira_listen_v1
    for name in ("WAKE_WORD", "WAKE_OK", "WAKE_PREFIX"):
        a, b = getattr(mira_listen, name), getattr(mira_listen_v1, name)
        assert getattr(a, "pattern", a) == getattr(b, "pattern", b), name
        assert getattr(a, "flags", 0) == getattr(b, "flags", 0), name


def test_reporter_dedupes_and_spaces_state_posts(mod, tmp_path):
    from golden import fakes
    hook = fakes.FakeHook()
    tokens = fakes.write_tokens(tmp_path)
    rep = mod.VoiceReporter(hook.url + "/api/hooks/voice", tokens["voice"], error_hold=0.3)
    rep.update("idle")
    time.sleep(0.3)  # let the first post go out: a still-queued twin would be coalesced even without the dedupe
    rep.update("idle")  # repeat of the last key: no post
    rep.update("listening")
    rep.update("thinking", "hi")
    time.sleep(1.3)  # the thinking post leaves after the 0.5 s gaps
    rep.update("thinking", "hi")  # repeat again
    rep.update("idle", "hi", "hello")
    rep.close(wait=True)
    posts = hook.posts()
    assert [(b["state"], b["heard"], b["reply"]) for _, b in posts] == [
        ("idle", "", ""), ("listening", "", ""), ("thinking", "hi", ""), ("idle", "hi", "hello")]
    gaps = [b[0] - a[0] for a, b in zip(posts, posts[1:])]
    assert all(g >= 0.45 for g in gaps), gaps  # at most 2 state posts per second (MIN_GAP 0.5)
    assert mod.VoiceReporter.MIN_GAP == 0.5 and mod.VoiceReporter.LEVEL_GAP == 0.2


def test_mute_detector_sees_the_raw_chunk_before_the_agc(mod, tmp_path, monkeypatch):
    import hashlib
    import numpy as np
    from golden import fakes, scenarios
    for k in [k for k in os.environ if k.startswith("MIRA_")]:
        monkeypatch.delenv(k)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wav = tmp_path / "in.wav"
    wav.write_bytes(mod.pcm16_wav_bytes(scenarios.compose(1.0)))
    tokens = fakes.write_tokens(tmp_path)
    audio = fakes.FakeAudio(tmp_path)
    dead = "http://127.0.0.1:%d" % fakes.free_port()
    urls = {"speech": dead, "hermes": dead, "voice": dead, "music": dead}
    args = mod.build_parser().parse_args(record.argv_for({}, tmp_path, urls, tokens, audio.bin))
    lis = mod.Listener(args, mod.WavSource(str(wav)))
    ref = mod.WavSource(str(wav))
    sha = lambda c: hashlib.sha256(np.ascontiguousarray(c, dtype=np.float32).tobytes()).hexdigest()
    log = []
    orig_feed, orig_proc = lis.mute.feed, lis.agc.process
    lis.mute.feed = lambda c: (log.append(("mute", sha(c))), orig_feed(c))[1]
    lis.agc.process = lambda c: (log.append(("agc", sha(c))), orig_proc(c))[1]
    try:
        for _ in range(5):
            del log[:]
            raw = ref.read()
            lis._read()
            assert log == [("mute", sha(raw)), ("agc", sha(raw))]
    finally:
        lis.rep.close(wait=False)
