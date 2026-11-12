"""playback.miraaudio: wav_envelope and the MiraAudio bodies copied from v1, plus the night window (addendum 1)."""
import io
import os
import time
import wave

import numpy as np
import pytest

import test_lp_common as H
from conftest import FakeServer  # noqa: F401
from listener_plugins import playback_miraaudio as pm
from golden.fakes import TTS_WAV

SPEC = pm.SPEC


def _fake_audio(tmp_path, sleep=0.0, code=0):
    """A `mira-audio` stand-in: logs argv, keeps a copy of every WAV it is asked to `say`."""
    log = tmp_path / "audio.log"
    keep = tmp_path / "said"
    keep.mkdir(exist_ok=True)
    script = tmp_path / "mira-audio"
    script.write_text(f"""#!/bin/sh
echo "$@" >> "{log}"
if [ "$1" = say ]; then
  n=$(ls "{keep}" | wc -l | tr -d ' ')
  cp "$2" "{keep}/say_$n.wav"
fi
sleep {sleep}
exit {code}
""")
    script.chmod(0o755)
    return str(script), log, keep


def _wav(peak=20000, seconds=0.5, rate=22050, freq=300.0, width=2):
    t = np.arange(int(seconds * rate)) / rate
    x = (np.sin(2 * np.pi * freq * t) * peak).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(x.tobytes() if width == 2 else (x >> 8).astype("i1").tobytes())
    return buf.getvalue()


def _peak(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return int(np.max(np.abs(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"))))


def _say(audio, tmp_path, wav, name="reply.wav"):
    p = tmp_path / name
    p.write_bytes(wav)
    levels = []
    audio.say_file(str(p), wav, levels.append)
    return p, levels


# ------------------------------------------------------------------------------ v1 equivalence

def test_wav_envelope_equals_v1():
    for wav in (TTS_WAV, _wav(), H.CALENDAR_WAV.read_bytes(), b"not a wav", b""):
        assert pm.wav_envelope(wav) == H.v1.wav_envelope(wav)
        assert pm.wav_envelope(wav, 0.1, 8.0) == H.v1.wav_envelope(wav, 0.1)
    assert pm.wav_envelope(TTS_WAV, 0.25, 4.0) != pm.wav_envelope(TTS_WAV)


def test_call_return_codes_and_failures_match_v1(tmp_path):
    script, log, _ = _fake_audio(tmp_path)
    audio = pm.MiraAudio(bin=script)
    assert audio.call("duck") == 0 and audio.call("unduck") == 0
    assert log.read_text().split() == ["duck", "unduck"]
    # a missing binary and a timeout are both rc 1, never an exception
    assert pm.MiraAudio(bin=str(tmp_path / "nope")).call("duck") == 1
    (tmp_path / "slow").mkdir()
    slow, _, _ = _fake_audio(tmp_path / "slow", sleep=5)
    t0 = time.monotonic()
    assert pm.MiraAudio(bin=slow, call_timeout_s=0.3).call("say", "x") == 1
    assert time.monotonic() - t0 < 4
    (tmp_path / "f").mkdir()
    failing, _, _ = _fake_audio(tmp_path / "f", code=3)
    assert pm.MiraAudio(bin=failing).call("duck") == 3 == H.v1.Listener._audio(
        type("L", (), {"a": type("A", (), {"mira_audio": failing})()})(), "duck")


def test_say_file_feeds_the_envelope_and_ends_at_zero(tmp_path):
    script, log, keep = _fake_audio(tmp_path, sleep=0.6)
    audio = pm.MiraAudio(bin=script, level_lag=0.0)
    p, levels = _say(audio, tmp_path, TTS_WAV)
    want = H.v1.wav_envelope(TTS_WAV)
    assert [round(v, 6) for v in levels[:len(want)]] == [round(v, 6) for _, v in want]
    assert levels[len(want):] and set(levels[len(want):]) == {0.0}  # the follower's end, then the finally
    assert (keep / "say_0.wav").read_bytes() == TTS_WAV  # window empty: the bytes are untouched
    assert log.read_text().split() == ["say", str(p)]


def test_a_failing_say_still_zeroes_the_level(tmp_path):
    audio = pm.MiraAudio(bin=str(tmp_path / "nope"), level_lag=0.0)
    p, levels = _say(audio, tmp_path, TTS_WAV)
    assert levels[-1] == 0.0


def test_lock_is_a_plain_lock_for_previews():
    audio = pm.MiraAudio()
    assert audio.lock.acquire(False) and not audio.lock.acquire(False)
    audio.lock.release()


# ---------------------------------------------------------------------------------- night window

class Clock:
    def __init__(self, hh, mm=0):
        self.t = hh * 60 + mm

    def __call__(self):
        return self.t


def _night(tmp_path, window="23:00-06:00", gain=0.5, hh=23, mm=30):
    script, log, keep = _fake_audio(tmp_path)
    audio = pm.MiraAudio(bin=script, level_lag=0.0, night_window=window, night_gain=gain)
    audio.minute_of_day = Clock(hh, mm)
    return audio, log, keep


def test_outside_the_window_the_bytes_handed_over_equal_the_input(tmp_path):
    audio, log, keep = _night(tmp_path, hh=12)
    wav = _wav()
    p, _ = _say(audio, tmp_path, wav)
    assert (keep / "say_0.wav").read_bytes() == wav
    assert log.read_text().split() == ["say", str(p)]  # the very same path, no temp copy


def test_inside_the_window_the_peak_is_scaled_and_the_header_stays_valid(tmp_path):
    audio, log, keep = _night(tmp_path, gain=0.5, hh=23, mm=30)
    wav = _wav(peak=20000)
    _say(audio, tmp_path, wav)
    got = (keep / "say_0.wav").read_bytes()
    assert abs(_peak(got) - _peak(wav) * 0.5) <= _peak(wav) * 0.5 * 0.01  # within 1 percent
    with wave.open(io.BytesIO(got), "rb") as a, wave.open(io.BytesIO(wav), "rb") as b:
        assert a.getparams() == b.getparams()
        assert a.getnframes() == b.getnframes()
    assert log.read_text().split()[0] == "say" and log.read_text().split()[1] != str(tmp_path / "reply.wav")
    assert not list(tmp_path.glob("tmp*.wav"))  # the temp copy is removed


@pytest.mark.parametrize("gain", [0.1, 0.3, 0.75, 1.0])
def test_scale_tracks_night_gain(tmp_path, gain):
    audio, _, keep = _night(tmp_path, gain=gain)
    wav = _wav(peak=24000)
    _say(audio, tmp_path, wav)
    want = _peak(wav) * gain
    assert abs(_peak((keep / "say_0.wav").read_bytes()) - want) <= want * 0.01 + 1


@pytest.mark.parametrize("hh,mm,inside", [(23, 0, True), (23, 59, True), (0, 0, True), (3, 15, True), (5, 59, True),
                                          (6, 0, False), (12, 0, False), (22, 59, False)])
def test_a_window_that_wraps_midnight(tmp_path, hh, mm, inside):
    audio, _, keep = _night(tmp_path, window="23:00-06:00", hh=hh, mm=mm)
    wav = _wav(peak=20000)
    _say(audio, tmp_path, wav)
    scaled = abs(_peak((keep / "say_0.wav").read_bytes()) - _peak(wav) / 2) <= 100
    assert scaled is inside


def test_an_empty_window_is_off_no_resampling_no_extra_writes(tmp_path, monkeypatch):
    audio, log, keep = _night(tmp_path, window="", hh=23, mm=30)
    monkeypatch.setattr(pm.tempfile, "mkstemp", lambda *a, **k: pytest.fail("a temp file was written with the window off"))
    monkeypatch.setattr(pm, "scale_wav", lambda *a, **k: pytest.fail("audio was resampled with the window off"))
    wav = _wav()
    p, _ = _say(audio, tmp_path, wav)
    assert (keep / "say_0.wav").read_bytes() == wav and log.read_text().split() == ["say", str(p)]


def test_the_chime_is_scaled_through_a_copy_and_the_asset_is_left_alone(tmp_path):
    audio, log, keep = _night(tmp_path, gain=0.4)
    chime = tmp_path / "offline.wav"
    chime.write_bytes(_wav(peak=30000))
    before = chime.read_bytes()
    assert audio.call("say", str(chime)) == 0
    assert abs(_peak((keep / "say_0.wav").read_bytes()) - _peak(before) * 0.4) <= _peak(before) * 0.4 * 0.01
    assert chime.read_bytes() == before


def test_duck_and_unduck_are_never_touched(tmp_path):
    audio, log, _ = _night(tmp_path)
    audio.call("duck")
    audio.call("unduck")
    assert log.read_text().split() == ["duck", "unduck"]


def test_unscalable_audio_plays_unchanged(tmp_path):
    audio, log, keep = _night(tmp_path)
    p = tmp_path / "x.wav"
    p.write_bytes(b"this is not a wav file")
    assert audio.call("say", str(p)) == 0
    assert (keep / "say_0.wav").read_bytes() == b"this is not a wav file"
    audio.call("say", str(tmp_path / "missing.wav"))  # a missing file goes straight to mira-audio, like before
    assert log.read_text().split()[-2:] == ["say", str(tmp_path / "missing.wav")]
    eight = tmp_path / "w8.wav"
    eight.write_bytes(_wav(width=1))
    assert audio.call("say", str(eight)) == 0
    assert (keep / "say_1.wav").read_bytes() == eight.read_bytes()


def test_scale_wav_clips_and_keeps_sample_count():
    wav = _wav(peak=32767)
    out = pm.scale_wav(wav, 1.0)
    assert out == wav or (out is not None and _peak(out) == _peak(wav))
    louder = pm.scale_wav(wav, 2.0)
    assert _peak(louder) == 32767 and len(louder) == len(wav)
    assert pm.scale_wav(b"junk", 0.5) is None


def test_the_window_is_read_at_play_time_so_a_live_change_applies_at_once(tmp_path):
    audio, _, keep = _night(tmp_path, window="", hh=23, mm=30)
    wav = _wav(peak=20000)
    _say(audio, tmp_path, wav)
    audio.night_window = "23:00-06:00"
    _say(audio, tmp_path, wav, "second.wav")
    assert (keep / "say_0.wav").read_bytes() == wav
    assert abs(_peak((keep / "say_1.wav").read_bytes()) - _peak(wav) / 2) <= 100


# --------------------------------------------------------------------------------------- plugin

def test_plugin_provides_playback_with_defaults_equal_to_v1():
    plugin, ctx, cfg = H.started("playback.miraaudio")
    a = ctx.get("playback")
    assert (a.bin, a.call_timeout_s, a.level_lag, a.envelope_step_s, a.follow_join_s, a.level_scale) == (
        "mira-audio", 120.0, 0.15, 0.1, 1.0, 8.0)
    assert a.chime == str(ctx.paths.here / "assets" / "offline.wav")
    assert a.night_window == "" and a.night_gain == 0.5 and a.envelope(TTS_WAV) == H.v1.wav_envelope(TTS_WAV)


def test_level_scale_is_read_from_the_reporter_config():
    ctx = H.make_ctx()
    ctx.cfgs["reporter.agentos"] = ctx.cfgs["reporter.agentos"].replace(level_scale=4.0)
    plugin, ctx, _ = H.started("playback.miraaudio", ctx=ctx)
    a = ctx.get("playback")
    assert a.level_scale == 4.0
    assert a.envelope(TTS_WAV) == pm.wav_envelope(TTS_WAV, 0.1, 4.0)
    ctx.cfgs["reporter.agentos"] = ctx.cfgs["reporter.agentos"].replace(level_scale=16.0)
    plugin.apply({"reporter.agentos.level_scale": 16.0}, plugin.cfg)
    assert a.level_scale == 16.0 and "reporter.agentos.level_scale" in SPEC.reads


def test_live_knobs_apply():
    plugin, ctx, _ = H.started("playback.miraaudio")
    a = ctx.get("playback")
    H.assert_live_knobs(SPEC, plugin, {
        "bin": ("other-audio", lambda p: a.bin), "call_timeout_s": (30.0, lambda p: a.call_timeout_s),
        "level_lag": (0.3, lambda p: a.level_lag), "chime": ("/x/c.wav", lambda p: a.chime),
        "envelope_step_s": (0.2, lambda p: a.envelope_step_s), "follow_join_s": (2.0, lambda p: a.follow_join_s),
        "night_window": ("22:00-07:00", lambda p: a.night_window), "night_gain": (0.25, lambda p: a.night_gain),
        "caption_shift_s": (0.4, lambda p: a.caption_shift_s), "chunk_overlap_s": (0.1, lambda p: a.chunk_overlap_s)})
    assert not [k for k in SPEC.knobs if k.applies != "live"]


def test_night_knob_definitions_follow_the_addendum():
    k = {x.name: x for x in SPEC.knobs}
    assert (k["night_window"].type, k["night_window"].default, k["night_window"].applies, k["night_window"].agent) == (
        "time_window", "", "live", "deny")
    g = k["night_gain"]
    assert (g.type, g.default, g.min, g.max, g.applies, g.agent) == ("float", 0.5, 0.1, 1.0, "live", "deny")
    assert g.description == "How loud she is inside the night window, as a share of normal."
    assert "owner" not in g.description.lower()


def test_status_reports_night_without_text(tmp_path):
    plugin, ctx, _ = H.started("playback.miraaudio", night_window="23:00-06:00")
    a = ctx.get("playback")
    a.minute_of_day = Clock(1)
    assert plugin.status() == {"night": True}
    a.minute_of_day = Clock(12)
    assert plugin.status() == {"night": False}


# ------------------------------------------------------------------------- stop() for barge-in (2026-11-11)

def _slow_say_bin(tmp_path):
    """A mira-audio whose `say` starts a long-running child of its own, like the real helper's pw-play, and records the
    child's pid; duck and unduck return at once."""
    import stat
    log, pidfile = tmp_path / "log", tmp_path / "child.pid"
    b = tmp_path / "mira-audio"
    b.write_text(f"""#!/bin/sh
echo "$1" >> "{log}"
if [ "$1" = say ]; then
  sleep 30 &
  echo $! > "{pidfile}"
  wait
fi
exit 0
""")
    b.chmod(b.stat().st_mode | stat.S_IEXEC)
    return b, log, pidfile


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_stop_ends_a_running_say_and_its_player_child_at_once(tmp_path):
    import threading
    import time
    b, log, pidfile = _slow_say_bin(tmp_path)
    a = pm.MiraAudio(bin=str(b))
    rc = []
    th = threading.Thread(target=lambda: rc.append(a.call("say", "x.wav")))
    th.start()
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    child = int(pidfile.read_text())
    assert _alive(child)
    t0 = time.monotonic()
    assert a.stop() == 1
    th.join(5)
    assert not th.is_alive() and time.monotonic() - t0 < 1.5
    time.sleep(0.2)
    assert not _alive(child), "the player child kept playing after stop()"
    assert rc and rc[0] != 0


def test_stop_never_touches_duck_or_unduck_and_is_a_noop_when_idle(tmp_path):
    import threading
    import time
    b, log, pidfile = _slow_say_bin(tmp_path)
    a = pm.MiraAudio(bin=str(b))
    assert a.stop() == 0
    assert a.call("duck") == 0 and a.call("unduck") == 0
    assert a._say_procs == set()
    # a duck running while a say is stopped is left alone
    th = threading.Thread(target=lambda: a.call("say", "x.wav"))
    th.start()
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    a.stop()
    th.join(5)
    assert a.call("unduck") == 0 and log.read_text().split() == ["duck", "unduck", "say", "unduck"]


def test_a_say_that_outlives_call_timeout_is_killed_with_its_child(tmp_path):
    import time
    b, log, pidfile = _slow_say_bin(tmp_path)
    a = pm.MiraAudio(bin=str(b), call_timeout_s=0.6)
    assert a.call("say", "x.wav") == 1
    time.sleep(0.2)
    assert not _alive(int(pidfile.read_text()))
