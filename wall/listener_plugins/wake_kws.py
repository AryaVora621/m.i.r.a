"""wake.kws: the sherpa-onnx streaming keyword spotter. build_kws is v1's (copied, signature `build_kws(args)`
kept; the plugin hands it a namespace built from the config).

FIXED: the model file names inside kws_dir (tokens.txt, encoder.onnx, decoder.onnx, joiner.onnx), provider "cpu".
"""

import argparse
import gc
import time
from pathlib import Path

from mirakit.plugin import Knob, PluginBase, PluginSpec

from .common import expand_path

LEGACY_CLASSES = ()


def build_kws(args):
    """The keyword spotter exactly as the listener runs it (tests build theirs through this too)."""
    import sherpa_onnx
    kd = Path(args.kws_dir)
    return sherpa_onnx.KeywordSpotter(
        tokens=str(kd / "tokens.txt"), encoder=str(kd / "encoder.onnx"), decoder=str(kd / "decoder.onnx"),
        joiner=str(kd / "joiner.onnx"), keywords_file=str(kd / args.keywords),
        num_threads=args.kws_threads, provider="cpu",
        keywords_threshold=args.kws_threshold, keywords_score=args.kws_score)


class WakeKws(PluginBase):
    def _namespace(self, cfg):
        return argparse.Namespace(kws_dir=expand_path(cfg.kws_dir, self.ctx.paths), keywords=cfg.keywords,
                                  kws_threads=cfg.threads, kws_threshold=cfg.threshold, kws_score=cfg.score)

    def start(self):
        t0 = time.monotonic()
        self.kws = build_kws(self._namespace(self.cfg))
        self.last_build_ms = int((time.monotonic() - t0) * 1000)
        self.ctx.provide("wake", self.kws)

    def rebuild(self, cfg):
        """Dispose first: two spotters never coexist (MemoryMax=200M). The provided reference is dropped, the old
        spotter is released and collected, and only then is the new one built."""
        self.ctx.provide("wake", None)
        old, self.kws = self.kws, None
        del old
        gc.collect()
        self.cfg = cfg
        self.start()

    def status(self):
        return {"loaded": getattr(self, "kws", None) is not None, "last_build_ms": getattr(self, "last_build_ms", 0)}


SPEC = PluginSpec(
    name="wake.kws", kind="wake", version="1", machine="listener", provides=("wake",),
    summary="Streaming keyword spotter for the wake word.",
    create=lambda ctx, cfg: WakeKws(ctx, cfg), order=50,
    knobs=(
        Knob("kws_dir", "path", "${HERE}/../voice/models/kws", "Spotter model folder.", applies="rebuild",
             arg="kws_dir", env="MIRA_KWS_DIR"),
        Knob("keywords", "str", "keywords.txt", "Keyword file inside kws_dir.", min=1, max=120, unit="chars",
             applies="rebuild", arg="keywords"),
        Knob("threshold", "float", 0.15, "Spotter sensitivity; lower fires easier (the transcript gate decides).",
             unit="score", min=0.01, max=0.5, applies="rebuild", arg="kws_threshold", env="MIRA_KWS_THRESHOLD",
             topic="wake", spoken="wake sensitivity", effect="fires less often",
             phrases=("you wake up too much", "you don't wake up")),
        Knob("score", "float", 1.0, "Keyword boost; raising it hurt recall.", unit="score", min=0.5, max=3.0,
             applies="rebuild", arg="kws_score", env="MIRA_KWS_SCORE"),
        Knob("threads", "int", 1, "Spotter CPU threads.", unit="threads", min=1, max=2, applies="restart",
             arg="kws_threads", env="MIRA_KWS_THREADS"),
    ),
    tests=("wall/tests/plugins/test_lp_wake_kws.py",),
)
