"""The mic AGC (quiet speech up, silence left alone, no clipping) and the reporter's level-only posts."""
import json
import time

import numpy as np

import mira_listen as ml
from conftest import FakeServer

RATE = ml.RATE
CH = ml.CHUNK
RNG = np.random.default_rng(7)


def hiss(n_chunks, rms=0.0027):
    return [(RNG.standard_normal(CH) * rms).astype(np.float32) for _ in range(n_chunks)]


def voice(n_chunks, amp):
    """A voiced-ish burst: 180 Hz plus harmonics, with the hiss underneath."""
    t = np.arange(CH) / RATE
    out = []
    for i in range(n_chunks):
        ph = (i * CH) / RATE
        v = sum(np.sin(2 * np.pi * f * (t + ph)) / k for k, f in enumerate((180, 360, 540), 1))
        out.append((amp * v / 1.4 + RNG.standard_normal(CH) * 0.0027).astype(np.float32))
    return out


def rms(c):
    return float(np.sqrt(np.mean(np.square(c))))


def test_agc_boosts_quiet_speech():
    agc = ml.MicAGC()
    for c in hiss(30):
        agc.process(c)
    quiet = voice(12, 0.012)
    out = [agc.process(c) for c in quiet]
    gain = np.mean([rms(o) / rms(i) for o, i in zip(out[6:], quiet[6:])])
    assert gain >= 3.0, gain
    assert gain <= agc.max_gain + 0.01
    assert all(np.max(np.abs(o)) <= 1.0 for o in out)
    assert agc.speech and agc.level > 0.1


def test_agc_does_not_pump_silence():
    agc = ml.MicAGC()
    chunks = hiss(300)  # 30 s of room hiss
    out = [agc.process(c) for c in chunks]
    ratio = rms(np.concatenate(out)) / rms(np.concatenate(chunks))
    assert ratio <= agc.rest + 0.1, ratio  # never above the resting gain
    assert agc.gain <= agc.rest + 0.01
    assert agc.level == 0.0 and not agc.speech
    # a speech burst after hiss raises the gain, and the gain relaxes back toward rest once it ends
    for c in voice(10, 0.01):
        agc.process(c)
    peak_gain = agc.gain
    for c in hiss(200):
        agc.process(c)
    assert peak_gain > agc.rest + 1.0 and agc.gain < peak_gain and agc.gain <= agc.rest + 0.5


def test_agc_digital_zero_stays_zero():
    agc = ml.MicAGC()
    z = np.zeros(CH, dtype=np.float32)
    assert not np.any(agc.process(z))
    for c in hiss(5):
        agc.process(c)
    assert not np.any(agc.process(z))


def test_agc_never_clips_loud_input_or_a_loud_burst_after_quiet():
    agc = ml.MicAGC()
    for c in hiss(20) + voice(10, 0.01):  # gain climbs on quiet speech
        agc.process(c)
    assert agc.gain > 3.0
    loud = voice(6, 0.9) + [np.full(CH, 1.0, dtype=np.float32)]
    out = [agc.process(c) for c in loud]
    assert max(float(np.max(np.abs(o))) for o in out) <= 1.0
    # attack is instant: the first loud chunk is not boosted past what it already was
    assert float(np.max(np.abs(out[0]))) <= max(float(np.max(np.abs(loud[0]))), agc.ceiling) + 1e-4
    assert agc.gain == 1.0


def test_agc_loud_input_is_left_alone():
    agc = ml.MicAGC()
    loud = voice(20, 0.5)
    out = [agc.process(c) for c in loud]
    ratio = rms(np.concatenate(out[5:])) / rms(np.concatenate(loud[5:]))
    assert 0.999 <= ratio <= 1.001


def test_agc_off_by_flag():
    args = ml.build_parser().parse_args(["--no-agc"])
    assert args.agc is False
    assert ml.build_parser().parse_args([]).agc is True


def _reporter(tmp_path):
    s = FakeServer()
    s.routes[("POST", "/h")] = lambda b, h: (200, b"{}", "application/json")
    (tmp_path / "t").write_text("tok")
    return s, ml.VoiceReporter(s.url + "/h", str(tmp_path / "t"), error_hold=0.0)


def _posts(s):
    return [json.loads(c["body"]) for c in s.calls]


def test_level_only_posts_are_rate_limited_and_only_in_listening_or_speaking(tmp_path):
    s, rep = _reporter(tmp_path)
    rep.update("idle")
    time.sleep(0.7)
    t_end = time.monotonic() + 2.0
    n = 0
    while time.monotonic() < t_end:  # level changes every 20 ms while idle: no posts
        n += 1
        rep.set_level(0.1 + 0.8 * (n % 2))
        time.sleep(0.02)
    assert len(s.calls) == 1, "idle must not stream levels"
    rep.update("listening")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        n += 1
        rep.set_level(0.1 + 0.8 * (n % 2))
        time.sleep(0.02)
    rep.close()
    posts = _posts(s)
    lvl = [p for p in posts[2:] if p["state"] == "listening"]
    assert 2.0 * 2 <= len(lvl) <= 2.0 * 5 + 2, len(lvl)  # up to 5 per second (a loaded test box may manage fewer), never more
    assert all(p["state"] == "listening" for p in posts[1:])
    assert len({p["level"] for p in lvl}) > 1
    s.stop()


def test_level_posts_never_delay_or_reorder_a_state_change(tmp_path):
    s, rep = _reporter(tmp_path)
    rep.update("idle")
    rep.update("listening")
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < 1.5:  # a busy mic
        n += 1
        rep.set_level(0.1 + 0.8 * (n % 2))
        time.sleep(0.01)
    t_change = time.monotonic()
    for st in ("thinking", "speaking", "idle"):
        rep.update(st, "q", "a")
    while not any(json.loads(c["body"])["state"] == "thinking" for c in s.calls):
        assert time.monotonic() - t_change < 3.0, "thinking was never posted"
        time.sleep(0.01)
    gap_think = time.monotonic() - t_change
    # keep the level moving through the changes
    t1 = time.monotonic()
    while time.monotonic() - t1 < 1.5:
        n += 1
        rep.set_level(0.1 + 0.8 * (n % 2))
        time.sleep(0.01)
    rep.close()
    posts = _posts(s)
    seq = [p["state"] for p in posts]
    # collapse level repeats: the state order is exactly the order of the updates
    order = [st for i, st in enumerate(seq) if i == 0 or st != seq[i - 1]]
    assert order == ["idle", "listening", "thinking", "speaking", "idle"], order
    # thinking went out within the 0.5 s state gap (plus slack) of the update, not behind a level backlog
    assert seq.index("thinking") > 0
    assert gap_think < 0.9, gap_think
    # no level-only post after the speaking state ended (idle carries no stream)
    assert seq[-1] == "idle" and seq.count("idle") == 2
    s.stop()


def test_state_gap_still_applies_between_state_posts(tmp_path):
    s, rep = _reporter(tmp_path)
    t0 = time.monotonic()
    for st in ("idle", "listening", "thinking", "speaking", "idle"):
        rep.update(st)
    rep.close()
    states = [p["state"] for p in _posts(s)]
    assert [x for i, x in enumerate(states) if i == 0 or x != states[i - 1]] == ["idle", "listening", "thinking", "speaking", "idle"]
    assert time.monotonic() - t0 >= 1.9
    s.stop()


def test_wav_envelope_follows_loudness():
    t = np.arange(RATE) / RATE
    x = np.concatenate([np.zeros(RATE // 2), 0.3 * np.sin(2 * np.pi * 200 * t[: RATE // 2])]).astype(np.float32)
    env = ml.wav_envelope(ml.pcm16_wav_bytes(x))
    assert len(env) == 10 and env[0][0] == 0.0
    assert all(l == 0 for _, l in env[:5]) and all(l > 0.5 for _, l in env[5:])
    assert ml.wav_envelope(b"nope") == []


# ------------------------------------------------------------------------------------ mic mute detector

def _muted_chunks(n_chunks, spikes=True):
    """What pw-record gives a muted JV603: digital zero, a few big spikes where the stream starts."""
    out = [np.zeros(CH, dtype=np.float32) for _ in range(n_chunks)]
    if spikes and out:
        out[0][:6] = 0.86
    return out


def _run(det, chunks):
    return [det.feed(c) for c in chunks]


def test_mute_detector_goes_muted_after_ten_seconds_of_zeros_and_not_before():
    det = ml.MuteDetector()
    ev = _run(det, _muted_chunks(99))  # 9.9 s: not yet
    assert "muted" not in ev and det.state != "muted"
    ev = _run(det, _muted_chunks(10, spikes=False))
    assert ev.count("muted") == 1 and det.state == "muted"


def test_mute_detector_hiss_stays_live_and_reports_live_once():
    det = ml.MuteDetector()
    ev = _run(det, hiss(300))  # 30 s of a live mic
    assert det.state == "live"
    assert [e for e in ev if e] == ["live"]


def test_mute_detector_returns_to_live_within_about_a_second_of_hiss():
    det = ml.MuteDetector()
    _run(det, _muted_chunks(120))
    assert det.state == "muted"
    ev = _run(det, hiss(10))
    assert "live" in ev and det.state == "live"


def test_mute_detector_repeats_once_a_minute_while_muted():
    det = ml.MuteDetector()
    ev = _run(det, _muted_chunks(10 * 10 + 60 * 10 * 2 + 5, spikes=False))
    assert ev.count("muted") == 1
    assert ev.count("repeat") == 2


def test_voice_posts_carry_the_last_known_mic_state(tmp_path):
    s, rep = _reporter(tmp_path)
    rep.update("idle")
    rep.set_mic("muted")
    rep.update("listening")
    rep.set_mic("live")
    rep.update("idle", "a", "b")
    rep.close()
    posts = _posts(s)
    assert "mic" not in posts[0]
    assert [p.get("mic") for p in posts[1:]] == ["muted", "muted", "live", "live"]
    assert posts[1]["state"] == "idle"  # the mic report repeats the current state
    s.stop()
