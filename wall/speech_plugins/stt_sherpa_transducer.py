"""STT backend "sherpa-transducer" and "sherpa-nemo-transducer": k2 zipformer or NeMo (Parakeet) transducers.

encoder, decoder and joiner (.int8 first, fp32 first with the -fp32 suffix) plus tokens.txt.

FIXED: provider "cpu", the 16 kHz sample rate.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from mirakit.plugin import PluginBase, PluginConfig, PluginSpec

IDS = ("sherpa-transducer", "sherpa-nemo-transducer")
STT_RATE = 16000


class SherpaTransducerModel:
    def __init__(self, kind: str, name: str, models: Path, threads: int, fp32: bool, nemo: bool):
        import sherpa_onnx
        self.kind, self.name = kind, name
        self.lock = threading.Lock()
        d = Path(models) / name
        files = sorted(p.name for p in d.iterdir())

        def pick(part):
            order = (f"{part}.onnx", f"{part}.int8.onnx") if fp32 else (f"{part}.int8.onnx", f"{part}.onnx")
            for suffix in order:
                if suffix in files:
                    return str(d / suffix)
            raise FileNotFoundError(part)
        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=pick("encoder"), decoder=pick("decoder"), joiner=pick("joiner"),
            tokens=str(d / "tokens.txt"), num_threads=threads, provider="cpu",
            model_type="nemo_transducer" if nemo else "transducer")

    def transcribe(self, samples: np.ndarray) -> str:
        with self.lock:
            s = self._rec.create_stream()
            s.accept_waveform(STT_RATE, samples)
            self._rec.decode_stream(s)
            return s.result.text.strip()


def load_model(kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> SherpaTransducerModel:
    backend, _, name = kind.partition(":")
    fp32 = opts.get("fp32", backend.endswith("-fp32"))
    return SherpaTransducerModel(kind, name, models, threads, fp32,
                                 nemo=backend.removesuffix("-fp32") == "sherpa-nemo-transducer")


class SherpaTransducerBackend(PluginBase):
    backend_ids = IDS
    name = "stt.sherpa_transducer"
    priority = 0

    def start(self) -> None:
        self.ctx.provide("stt-backend", self)

    def load(self, kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> SherpaTransducerModel:
        return load_model(kind, models, threads, opts)


SPEC = PluginSpec(
    name="stt.sherpa_transducer", kind="stt", version="1", machine="speech",
    summary="sherpa-onnx transducer (zipformer, NeMo Parakeet) speech to text backend",
    provides=("stt-backend",), create=lambda ctx, cfg: SherpaTransducerBackend(ctx, cfg),
    tests=("wall/tests/speech/test_sp_stt_backends.py",),
)
