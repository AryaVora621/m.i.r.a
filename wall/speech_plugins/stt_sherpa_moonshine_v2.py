"""STT backend "sherpa-moonshine-v2": the 2026 Moonshine packages (one encoder, one merged decoder, ORT format).

FIXED: provider "cpu", the 16 kHz sample rate.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from mirakit.plugin import PluginBase, PluginConfig, PluginSpec

IDS = ("sherpa-moonshine-v2",)
STT_RATE = 16000


class SherpaMoonshineV2Model:
    def __init__(self, kind: str, name: str, models: Path, threads: int):
        import sherpa_onnx
        self.kind, self.name = kind, name
        self.lock = threading.Lock()
        d = Path(models) / name
        self._rec = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
            encoder=str(next(d.glob("encoder_model*"))), decoder=str(next(d.glob("decoder_model*"))),
            tokens=str(d / "tokens.txt"), num_threads=threads, provider="cpu")

    def transcribe(self, samples: np.ndarray) -> str:
        with self.lock:
            s = self._rec.create_stream()
            s.accept_waveform(STT_RATE, samples)
            self._rec.decode_stream(s)
            return s.result.text.strip()


def load_model(kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> SherpaMoonshineV2Model:
    return SherpaMoonshineV2Model(kind, kind.partition(":")[2], models, threads)


class SherpaMoonshineV2Backend(PluginBase):
    backend_ids = IDS
    name = "stt.sherpa_moonshine_v2"
    priority = 0

    def start(self) -> None:
        self.ctx.provide("stt-backend", self)

    def load(self, kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> SherpaMoonshineV2Model:
        return load_model(kind, models, threads, opts)


SPEC = PluginSpec(
    name="stt.sherpa_moonshine_v2", kind="stt", version="1", machine="speech",
    summary="sherpa-onnx Moonshine v2 speech to text backend",
    provides=("stt-backend",), create=lambda ctx, cfg: SherpaMoonshineV2Backend(ctx, cfg),
    tests=("wall/tests/speech/test_sp_stt_backends.py",),
)
