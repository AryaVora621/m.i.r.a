"""agc.mic: the gentle software AGC ahead of the spotter, the VAD and the clip sent to speech. The class is v1's
MicAGC (copied) with the four knob-fed edits of spec 4.4.

FIXED: attack is instant, release ramps across the chunk, digital zero stays zero, the wall level is rms times
`level_scale` capped at 1 (the scale itself is reporter.agentos.level_scale, read through `reads`).
"""

import os

import numpy as np

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

LEGACY_CLASSES = ("MicAGC",)
LEGACY_FUNCTIONS = ("_envf",)


def _envf(name, default):
    return float(os.environ.get(name, default))


class MicAGC:
    """Gentle software AGC on the mic chunks, ahead of the spotter, the VAD and the clip sent to speech.

    The JV603 delivers speech at rms 0.003 to 0.02 and a far or quiet voice barely clears its hiss (about
    0.0027), so the keyword spotter sees almost nothing from across the room. This lifts speech toward
    `target` rms with these properties:
      * the gain is capped at `max_gain`, and a chunk is never pushed past `ceiling` peak (no clipping);
      * attack is instant (a loud chunk drops the gain at once), release is slow (`release` per 100 ms chunk);
      * a noise floor is tracked (falls fast, rises slowly) and chunks under `gate` x floor are treated as
        silence: they never raise the gain, which relaxes slowly toward `rest`, so room hiss is not pumped;
      * the gain is ramped across each chunk so a change never steps the waveform.
    Digital zero stays zero. `speech` and `level` describe the last chunk for the wall's rings.
    """

    def __init__(self, target=None, max_gain=None, rest=None, release=None, decay=None, gate=None,
                 min_rms=None, ceiling=None, floor_rate_speech=0.0005, floor_rate_low=0.2, floor_rate_mid=0.05,
                 level_scale=8.0):
        self.target = _envf("MIRA_AGC_TARGET", 0.05) if target is None else target
        self.max_gain = _envf("MIRA_AGC_MAX_GAIN", 10.0) if max_gain is None else max_gain
        self.rest = _envf("MIRA_AGC_REST", 3.0) if rest is None else rest
        self.release = _envf("MIRA_AGC_RELEASE", 0.25) if release is None else release
        self.decay = _envf("MIRA_AGC_DECAY", 0.03) if decay is None else decay
        self.gate = _envf("MIRA_AGC_GATE", 1.8) if gate is None else gate
        self.min_rms = _envf("MIRA_AGC_MIN_RMS", 0.001) if min_rms is None else min_rms
        self.ceiling = _envf("MIRA_AGC_CEILING", 0.9) if ceiling is None else ceiling
        self.floor_rate_speech, self.floor_rate_low, self.floor_rate_mid = floor_rate_speech, floor_rate_low, floor_rate_mid
        self.level_scale = level_scale
        self.rest = min(max(self.rest, 1.0), self.max_gain)
        self.gain = self.rest
        self.floor = _envf("MIRA_AGC_FLOOR", 0.003)  # the JV603's hiss; a start in the middle of speech must not set the floor
        self.speech = False
        self.level = 0.0

    def process(self, c):
        rms = float(np.sqrt(np.mean(c * c))) if len(c) else 0.0
        if rms <= 0.0:
            self.speech, self.level = False, 0.0
            return c
        speechlike = rms > max(self.floor * self.gate, self.min_rms)
        # Hiss-like chunks pull the floor in quickly; speech-like chunks only nudge it up, so a long
        # utterance does not teach the gate that speech is noise, yet a louder room is learned in minutes.
        rate = self.floor_rate_speech if speechlike else (self.floor_rate_low if rms < self.floor else self.floor_rate_mid)
        self.floor += (rms - self.floor) * rate
        prev = self.gain
        if speechlike:
            want = min(self.max_gain, max(1.0, self.target / rms))
            self.gain = want if want < prev else prev + (want - prev) * self.release
        else:
            self.gain += (self.rest - self.gain) * self.decay
        peak = float(np.max(np.abs(c)))
        if peak * self.gain > self.ceiling:
            self.gain = max(1.0, self.ceiling / peak)  # loud input passes through untouched, as before the AGC existed
        # Attack is a step at the chunk start (a ramp from the old, higher gain could clip the first samples);
        # release ramps.
        g = np.linspace(prev, self.gain, len(c), dtype=np.float32) if self.gain > prev else self.gain
        out = np.clip(c * g, -1.0, 1.0).astype(np.float32, copy=False)
        self.speech = speechlike
        self.level = min(1.0, float(np.sqrt(np.mean(out * out))) * self.level_scale) if speechlike else 0.0
        return out


class AgcMic(PluginBase):
    def _level_scale(self):
        try:
            return float(self.ctx.cfg_of("reporter.agentos").level_scale)
        except KeyError:
            return 8.0

    def start(self):
        c = self.cfg
        if not c.on:
            self.agc = None
            self.ctx.provide("agc", None)
            return
        # Every parameter is passed, so MicAGC's MIRA_AGC_* env fallback never runs inside the plugin engine.
        agc = MicAGC(target=c.target, max_gain=c.max_gain, rest=c.rest, release=c.release, decay=c.decay, gate=c.gate,
                     min_rms=c.min_rms, ceiling=c.ceiling, floor_rate_speech=c.floor_rate_speech,
                     floor_rate_low=c.floor_rate_low, floor_rate_mid=c.floor_rate_mid, level_scale=self._level_scale())
        # MicAGC reads MIRA_AGC_FLOOR itself and spec 4.4 gives it no parameter, so the configured start is set here.
        agc.floor = c.floor_init
        self.agc = agc
        self.ctx.provide("agc", agc)

    def apply(self, changed, cfg):
        self.cfg = cfg
        a = self.agc
        if a is None:
            return
        a.target, a.max_gain, a.gate, a.release, a.decay = cfg.target, cfg.max_gain, cfg.gate, cfg.release, cfg.decay
        a.min_rms, a.ceiling = cfg.min_rms, cfg.ceiling
        a.floor_rate_speech, a.floor_rate_low, a.floor_rate_mid = cfg.floor_rate_speech, cfg.floor_rate_low, cfg.floor_rate_mid
        a.level_scale = self._level_scale()
        # The same clamp as MicAGC.__init__, from the configured rest so a later rise of max_gain restores it.
        a.rest = min(max(cfg.rest, 1.0), cfg.max_gain)

    def status(self):
        a = self.agc
        return {"on": a is not None, "gain": round(a.gain, 2) if a is not None else None}


def validate(values):
    if values["rest"] > values["max_gain"]:
        raise ConfigError("rest must not exceed max_gain", "rest")


SPEC = PluginSpec(
    name="agc.mic", kind="agc", version="1", machine="listener", provides=("agc",),
    summary="Software gain stage on the mic chunks.",
    create=lambda ctx, cfg: AgcMic(ctx, cfg), order=40, validate=validate,
    reads=("reporter.agentos.level_scale",),
    knobs=(
        Knob("on", "bool", True, "Software gain stage on the mic.", applies="restart", arg="agc", env="MIRA_AGC"),
        Knob("target", "float", 0.05, "Speech level the gain aims for.", unit="rms", min=0.005, max=0.3,
             agent="bounded", agent_min=0.03, agent_max=0.10, agent_step=0.01, arg="agc_target",
             env="MIRA_AGC_TARGET", topic="hearing", spoken="microphone boost",
             effect="hears quiet speech from farther away, and more room noise",
             phrases=("you can't hear me", "you don't hear me from my desk")),
        Knob("max_gain", "float", 10.0, "Most the gain may rise.", unit="x", min=1, max=40, agent="bounded",
             agent_min=4, agent_max=14, agent_step=2, arg="agc_max_gain", env="MIRA_AGC_MAX_GAIN", topic="hearing",
             spoken="most microphone boost", effect="quiet rooms get boosted more",
             phrases=("you miss me when I talk softly",)),
        Knob("rest", "float", 3.0, "Gain in a quiet room; re-clamped to 1..max_gain on apply.", unit="x", min=1,
             max=40, arg="agc_rest", env="MIRA_AGC_REST"),
        Knob("gate", "float", 1.8, "How far above the noise floor counts as speech.", unit="x floor", min=1, max=6,
             arg="agc_gate", env="MIRA_AGC_GATE"),
        Knob("release", "float", 0.25, "How fast the gain rises.", unit="per chunk", min=0.01, max=1,
             env="MIRA_AGC_RELEASE"),
        Knob("decay", "float", 0.03, "How fast the gain relaxes to rest in silence.", unit="per chunk", min=0.001,
             max=0.5, env="MIRA_AGC_DECAY"),
        Knob("min_rms", "float", 0.001, "Absolute floor of the speech test.", unit="rms", min=0, max=0.05,
             env="MIRA_AGC_MIN_RMS"),
        Knob("ceiling", "float", 0.9, "No-clip limit.", unit="peak", min=0.1, max=1.0, env="MIRA_AGC_CEILING"),
        Knob("floor_init", "float", 0.003, "Starting noise floor (the JV603 hiss).", unit="rms", min=0, max=0.1,
             applies="restart", env="MIRA_AGC_FLOOR"),
        Knob("floor_rate_speech", "float", 0.0005, "Noise floor tracking rate during speech.", unit="rate", min=0,
             max=0.1),
        Knob("floor_rate_low", "float", 0.2, "Noise floor tracking rate when the chunk is below the floor.",
             unit="rate", min=0, max=1),
        Knob("floor_rate_mid", "float", 0.05, "Noise floor tracking rate for any other chunk.", unit="rate", min=0,
             max=1),
    ),
    tests=("wall/tests/plugins/test_lp_agc_mic.py",),
)
