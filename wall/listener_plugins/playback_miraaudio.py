"""playback.miraaudio: plays and ducks through the `mira-audio` command. wav_envelope is v1's (copied, with `step`
and `scale`); MiraAudio holds the bodies of v1's Listener._audio (as `_run`) and Listener._say_file (as `say_file`)
with their values read from the config.

Night mode (owner decision 2026-11-10): while the local time is inside `night_window`, every sound this plugin plays
for Mira (replies, previews, the offline chime) is scaled by `night_gain` before it reaches `mira-audio`. With the
window empty nothing changes: no file is written and the bytes are exactly what the caller gave.

FIXED: the sub-commands duck, unduck and say FILE, and a blocking `say` while a thread feeds the reply level.
"""

import os
import tempfile
import threading
import time
import subprocess
import wave

import numpy as np

from mirakit.plugin import Knob, PluginBase, PluginSpec

from .common import RATE, expand_path, in_window, local_minute, log, read_wav_float_bytes

LEGACY_CLASSES = ()


def wav_envelope(wav_bytes, step=0.1, scale=8.0):
    """[(start_s, level)] of a WAV's loudness every `step` seconds, scaled like the mic level (rms * 8, capped at 1)
    so the wall's rings read a spoken reply the same way they read a voice. Empty when the WAV is unreadable."""
    try:
        x = read_wav_float_bytes(wav_bytes)
    except (wave.Error, ValueError, EOFError):
        return []
    n = int(step * RATE)
    return [(i / RATE, min(1.0, float(np.sqrt(np.mean(x[i:i + n] ** 2))) * scale)) for i in range(0, len(x), n)]


def scale_wav(wav_bytes, gain):
    """The WAV with every 16-bit sample scaled by `gain` (header kept). None when it is not 16-bit PCM."""
    import io
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            params = w.getparams()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None
    if params.sampwidth != 2 or params.comptype != "NONE":
        return None
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) * gain
    out = np.clip(np.rint(x), -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(2)
        w.setframerate(params.framerate)
        w.writeframes(out.tobytes())
    return buf.getvalue()


class MiraAudio:
    """The `playback` service (spec 2.2)."""

    def __init__(self, bin="mira-audio", call_timeout_s=120.0, level_lag=0.15, chime="", envelope_step_s=0.1,
                 follow_join_s=1.0, level_scale=8.0, night_window="", night_gain=0.5):
        self.bin, self.call_timeout_s, self.level_lag, self.chime = bin, call_timeout_s, level_lag, chime
        self.envelope_step_s, self.follow_join_s, self.level_scale = envelope_step_s, follow_join_s, level_scale
        self.night_window, self.night_gain = night_window, night_gain
        self.minute_of_day = local_minute  # injectable so a test can pick the hour
        self.lock = threading.Lock()  # held by previews only (spec 2.6)

    def _run(self, *cmd):
        try:
            return subprocess.run([self.bin, *cmd], timeout=self.call_timeout_s, capture_output=True).returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"mira-audio {cmd[0]} failed: {type(e).__name__}")
            return 1

    def call(self, *cmd):
        if self.night_window and len(cmd) > 1 and cmd[0] == "say" and in_window(self.night_window, self.minute_of_day()):
            return self._night_say(cmd)
        return self._run(*cmd)

    def _night_say(self, cmd):
        """`say FILE` with FILE's samples scaled by night_gain, through a temp copy (the chime is a shared asset)."""
        try:
            with open(cmd[1], "rb") as f:
                scaled = scale_wav(f.read(), self.night_gain)
        except OSError:
            scaled = None
        if scaled is None:
            return self._run(*cmd)
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(scaled)
            return self._run("say", tmp, *cmd[2:])
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def say_file(self, path, wav, set_level):
        """mira-audio say FILE (blocking) while a thread feeds the reporter the reply's loudness in step with playback."""
        env = self.envelope(wav)
        done = threading.Event()

        def follow():
            t0 = time.monotonic() + self.level_lag  # pw-play needs a moment to start
            for at, lvl in env:
                wait = t0 + at - time.monotonic()
                if wait > 0 and done.wait(wait):
                    return
                set_level(lvl)
            set_level(0.0)

        th = threading.Thread(target=follow, daemon=True)
        th.start()
        try:
            self.call("say", path)
        finally:
            done.set()
            th.join(timeout=self.follow_join_s)
            set_level(0.0)

    def envelope(self, wav):
        return wav_envelope(wav, self.envelope_step_s, self.level_scale)


class PlaybackMiraaudio(PluginBase):
    def _level_scale(self):
        try:
            return float(self.ctx.cfg_of("reporter.agentos").level_scale)
        except KeyError:
            return 8.0

    def _sync(self, cfg):
        p = self.audio
        p.bin, p.call_timeout_s, p.level_lag = cfg.bin, cfg.call_timeout_s, cfg.level_lag
        p.chime = expand_path(cfg.chime, self.ctx.paths)
        p.envelope_step_s, p.follow_join_s, p.level_scale = cfg.envelope_step_s, cfg.follow_join_s, self._level_scale()
        p.night_window, p.night_gain = cfg.night_window, cfg.night_gain

    def start(self):
        self.audio = MiraAudio()
        self._sync(self.cfg)
        self.ctx.provide("playback", self.audio)

    def apply(self, changed, cfg):
        self.cfg = cfg
        self._sync(cfg)

    def status(self):
        a = self.audio
        return {"night": bool(a.night_window and in_window(a.night_window, a.minute_of_day()))}


SPEC = PluginSpec(
    name="playback.miraaudio", kind="playback", version="1", machine="listener", provides=("playback",),
    summary="Plays replies through mira-audio, ducking the music, quieter inside the night window.",
    create=lambda ctx, cfg: PlaybackMiraaudio(ctx, cfg), order=130, reads=("reporter.agentos.level_scale",),
    knobs=(
        Knob("bin", "str", "mira-audio", "Playback and duck command.", unit="chars", min=1, max=200, arg="mira_audio",
             env="MIRA_AUDIO_BIN"),
        Knob("call_timeout_s", "float", 120.0, "Seconds one mira-audio call may take.", unit="s", min=5, max=600),
        Knob("level_lag", "float", 0.15, "Delay from say start to sound, for the ring.", unit="s", min=0, max=1,
             arg="level_lag", env="MIRA_LEVEL_LAG"),
        Knob("chime", "path", "${HERE}/assets/offline.wav", "Sound played when speech is offline.", arg="chime",
             env="MIRA_CHIME"),
        Knob("envelope_step_s", "float", 0.1, "Step of the reply level.", unit="s", min=0.05, max=0.5),
        Knob("follow_join_s", "float", 1.0, "Seconds to wait for the level thread after a say.", unit="s", min=0.1,
             max=5),
        Knob("night_window", "time_window", "", "Local time window (HH:MM-HH:MM) in which she plays more quietly; empty is off."),
        Knob("night_gain", "float", 0.5, "How loud she is inside the night window, as a share of normal.",
             unit="share", min=0.1, max=1.0),
    ),
    tests=("wall/tests/plugins/test_lp_playback_miraaudio.py",),
)
