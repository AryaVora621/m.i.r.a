"""source.wav: a WAV file standing in for the mic (tests, shadow runs and `--wav`). The class is v1's WavSource (copied).

FIXED: 16 kHz mono float32 chunks of 1600 samples; the clock is audio position.
"""

import numpy as np

from mirakit.plugin import Knob, PluginBase, PluginSpec

from .common import CHUNK, RATE, expand_path, read_wav_float

LEGACY_CLASSES = ("WavSource",)


class WavSource:
    """Feeds a file as fast as the pipeline asks. The clock is audio time, so windows still work."""

    live = False

    def __init__(self, path, tail_s=1.0):
        x = read_wav_float(path)
        # A little trailing silence lets the keyword spotter flush its last tokens at EOF.
        self.x = np.concatenate([x, np.zeros(int(tail_s * RATE), dtype=np.float32)])
        self.pos = 0

    def read(self):
        if self.pos >= len(self.x):
            return None
        c = self.x[self.pos:self.pos + CHUNK]
        self.pos += len(c)
        return c

    def clock(self):
        return self.pos / RATE

    def flush(self):
        pass

    def close(self):
        pass


class SourceWav(PluginBase):
    def start(self):
        src = WavSource(expand_path(self.cfg.path, self.ctx.paths), tail_s=self.cfg.tail_s)
        self.src = src
        self.ctx.effect(src.close, "wav source")
        self.ctx.provide("source", src)

    def status(self):
        src = getattr(self, "src", None)
        return {"position_s": round(src.clock(), 1) if src is not None else 0.0}


SPEC = PluginSpec(
    name="source.wav", kind="source", version="1", machine="listener", provides=("source",),
    summary="A WAV file in place of the microphone.",
    create=lambda ctx, cfg: SourceWav(ctx, cfg), default_enabled=False, order=20,
    knobs=(
        Knob("path", "path", "", "WAV file that replaces the mic.", applies="restart", arg="wav"),
        Knob("tail_s", "float", 1.0, "Zeros appended so the spotter flushes at the end of the file.", unit="s",
             min=0, max=5, applies="restart"),
    ),
    tests=("wall/tests/plugins/test_lp_source_wav.py",),
)
