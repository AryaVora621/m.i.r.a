"""STT backend "sherpa-moonshine": Moonshine v1 packaging (preprocess, encode, uncached and cached decode).

The empty-result retry rule keeps its v1 numbers as knob defaults and the model reads them per call.

FIXED: provider "cpu", the 16 kHz sample rate.
LEGACY_CLASSES = ("SherpaMoonshineModel",)
"""
from __future__ import annotations

import threading
import weakref
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from mirakit.plugin import Knob, PluginBase, PluginConfig, PluginSpec

IDS = ("sherpa-moonshine",)
STT_RATE = 16000


class SherpaMoonshineModel:
    def __init__(self, kind: str, name: str, models: Path, threads: int,
                 retry_min_s: float = 1.0, retry_rms: float = 0.01, retry_offset_s: float = 0.2):
        import sherpa_onnx
        self.kind, self.name = kind, name
        self.lock = threading.Lock()
        self.retry_min_s, self.retry_rms, self.retry_offset_s = retry_min_s, retry_rms, retry_offset_s
        d = Path(models) / name
        files = sorted(p.name for p in d.iterdir())
        f = lambda part: str(d / next(x for x in files if x.startswith(part) and x.endswith(".onnx")))
        self._rec = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=f("preprocess"), encoder=f("encode"),
            uncached_decoder=f("uncached_decode"), cached_decoder=f("cached_decode"),
            tokens=str(d / "tokens.txt"), num_threads=threads, provider="cpu")

    def transcribe(self, samples: np.ndarray) -> str:
        with self.lock:
            text = self._decode(samples)
            if not text and len(samples) > self.retry_min_s * STT_RATE \
                    and float(np.sqrt(np.mean(samples ** 2))) > self.retry_rms:
                # Moonshine sometimes returns nothing for a clip that plainly has speech in it, and a
                # different start offset fixes it. Costs one extra decode only in that case.
                text = self._decode(samples[int(self.retry_offset_s * STT_RATE):])
            return text

    def _decode(self, samples):
        s = self._rec.create_stream()
        s.accept_waveform(STT_RATE, samples)
        self._rec.decode_stream(s)
        return s.result.text.strip()


def load_model(kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> SherpaMoonshineModel:
    name = kind.partition(":")[2]
    keys = ("retry_min_s", "retry_rms", "retry_offset_s")
    return SherpaMoonshineModel(kind, name, models, threads, **{k: opts[k] for k in keys if k in opts})


class SherpaMoonshineBackend(PluginBase):
    backend_ids = IDS
    name = "stt.sherpa_moonshine"
    priority = 0

    def __init__(self, ctx, cfg: PluginConfig) -> None:
        super().__init__(ctx, cfg)
        self._models: "weakref.WeakSet[SherpaMoonshineModel]" = weakref.WeakSet()

    def start(self) -> None:
        self.ctx.provide("stt-backend", self)

    def load(self, kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> SherpaMoonshineModel:
        cfg = self.cfg
        merged = {"retry_min_s": cfg.retry_min_s, "retry_rms": cfg.retry_rms, "retry_offset_s": cfg.retry_offset_s}
        merged.update(opts)
        model = load_model(kind, models, threads, merged)
        self._models.add(model)
        return model

    def apply(self, changed: Mapping[str, Any], cfg: PluginConfig) -> None:
        self.cfg = cfg
        for model in list(self._models):
            model.retry_min_s, model.retry_rms, model.retry_offset_s = cfg.retry_min_s, cfg.retry_rms, cfg.retry_offset_s


SPEC = PluginSpec(
    name="stt.sherpa_moonshine", kind="stt", version="1", machine="speech",
    summary="sherpa-onnx Moonshine v1 speech to text backend",
    provides=("stt-backend",), create=lambda ctx, cfg: SherpaMoonshineBackend(ctx, cfg),
    knobs=(
        Knob("retry_min_s", "float", 1.0, "A clip longer than this gets one more decode when the first comes back empty.",
             unit="s", min=0, max=5, topic="hearing"),
        Knob("retry_rms", "float", 0.01, "Loudness a clip must exceed before an empty result is retried.",
             unit="rms", min=0, max=0.1, topic="hearing"),
        Knob("retry_offset_s", "float", 0.2, "Where the retry decode starts inside the clip.",
             unit="s", min=0, max=1, topic="hearing"),
    ),
    tests=("wall/tests/speech/test_sp_stt_backends.py",),
)
