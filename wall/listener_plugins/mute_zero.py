"""mute.zero: tells a muted USB mic from a live one by counting exact zeros on the raw chunks. The class is v1's
MuteDetector (copied, no edits).

FIXED: the verdicts are "muted", "live" and "repeat"; time is counted in samples at 16 kHz.
"""

import numpy as np

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

from .common import RATE

LEGACY_CLASSES = ("MuteDetector",)


class MuteDetector:
    """Tells a muted USB mic from a live one, on the raw chunks before the AGC.

    The JV603's touch button mutes in hardware: the stream is then digital zero apart from a few spikes
    when it starts. A live mic always carries hiss, so its samples are almost never exactly zero. The
    verdict therefore counts exactly-zero samples (rms would be inflated by the spikes). Time is counted in
    samples, not the wall clock, so a stalled reader cannot fake a mute.

    Samples are judged in blocks of `block_s` seconds. `muted` needs `mute_s` seconds in a row of blocks
    with at least `mute_share` zeros; one block under `live_share` zeros makes it `live` again (also the
    first verdict after start). `feed` returns "muted" or "live" on a change, "repeat" every `repeat_s`
    while muted (so a restarted Agent OS learns the state again), else None. `state` is None until known.
    """

    def __init__(self, mute_s=10.0, mute_share=0.99, live_share=0.90, block_s=1.0, repeat_s=60.0, rate=RATE):
        self.mute_s, self.mute_share, self.live_share = mute_s, mute_share, live_share
        self.block, self.repeat_s, self.rate = int(block_s * rate), repeat_s, rate
        self.state = None
        self._n = 0  # samples in the open block
        self._zeros = 0
        self._zero_run_s = 0.0
        self._since_post_s = 0.0

    def feed(self, chunk):
        self._n += len(chunk)
        self._zeros += int(len(chunk) - np.count_nonzero(chunk))
        if self._n < self.block:
            return None
        share, secs = self._zeros / self._n, self._n / self.rate
        self._n = self._zeros = 0
        self._zero_run_s = self._zero_run_s + secs if share >= self.mute_share else 0.0
        if self.state != "muted" and self._zero_run_s >= self.mute_s:
            self.state, self._since_post_s = "muted", 0.0
            return "muted"
        if self.state != "live" and share < self.live_share:
            self.state = "live"
            return "live"
        if self.state == "muted":
            self._since_post_s += secs
            if self._since_post_s >= self.repeat_s:
                self._since_post_s = 0.0
                return "repeat"
        return None


class MuteZero(PluginBase):
    def start(self):
        c = self.cfg
        self.mute = MuteDetector(mute_s=c.mute_s, mute_share=c.mute_share, live_share=c.live_share,
                                 block_s=c.block_s, repeat_s=c.repeat_s)
        self.ctx.provide("mute", self.mute)

    def apply(self, changed, cfg):
        self.cfg = cfg
        m = self.mute
        m.mute_s, m.mute_share, m.live_share, m.repeat_s = cfg.mute_s, cfg.mute_share, cfg.live_share, cfg.repeat_s

    def status(self):
        return {"state": self.mute.state}


def validate(values):
    if values["live_share"] > values["mute_share"]:
        raise ConfigError("live_share must not exceed mute_share", "live_share")


SPEC = PluginSpec(
    name="mute.zero", kind="mute", version="1", machine="listener", provides=("mute",),
    summary="Detects the hardware mute of the USB mic from runs of digital zeros.",
    create=lambda ctx, cfg: MuteZero(ctx, cfg), order=30, validate=validate,
    knobs=(
        Knob("mute_s", "float", 10.0, "Seconds of zero samples before the mic counts as muted.", unit="s", min=2,
             max=60),
        Knob("mute_share", "float", 0.99, "Share of exact zeros that makes a block muted.", unit="share", min=0.5,
             max=1.0),
        Knob("live_share", "float", 0.90, "A block under this share of zeros is live; must not exceed mute_share.",
             unit="share", min=0.5, max=1.0),
        Knob("block_s", "float", 1.0, "Length of one verdict block.", unit="s", min=0.5, max=5, applies="restart"),
        Knob("repeat_s", "float", 60.0, "Seconds between re-posts of muted so a restarted Agent OS learns it.",
             unit="s", min=10, max=600),
    ),
    tests=("wall/tests/plugins/test_lp_mute_zero.py",),
)
