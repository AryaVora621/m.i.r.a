"""STT backend "sherpa-whisper": Whisper exported to ONNX, run by sherpa-onnx.

FIXED: language "en", task "transcribe", provider "cpu", the 16 kHz sample rate.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from mirakit.plugin import PluginBase, PluginConfig, PluginSpec

IDS = ("sherpa-whisper",)
STT_RATE = 16000


def _prefix(files):
    for f in files:
        if f.endswith("-encoder.onnx") or f.endswith("-encoder.int8.onnx"):
            return f.split("-encoder")[0]
    raise FileNotFoundError("no whisper encoder in model dir")


class SherpaWhisperModel:
    def __init__(self, kind: str, name: str, models: Path, threads: int, fp32: bool):
        import sherpa_onnx
        self.kind, self.name = kind, name
        self.lock = threading.Lock()
        d = Path(models) / name
        files = sorted(p.name for p in d.iterdir())
        pre = _prefix(files)

        # Prefer the int8 files (smaller, faster on a CPU with only AVX).
        def pick(part):
            order = (f"{pre}-{part}.onnx", f"{pre}-{part}.int8.onnx") if fp32 else \
                    (f"{pre}-{part}.int8.onnx", f"{pre}-{part}.onnx")
            for suffix in order:
                if suffix in files:
                    return str(d / suffix)
            raise FileNotFoundError(part)
        self._rec = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=pick("encoder"), decoder=pick("decoder"),
            tokens=str(d / f"{pre}-tokens.txt"),
            language="en", task="transcribe", num_threads=threads, provider="cpu")

    def transcribe(self, samples: np.ndarray) -> str:
        with self.lock:
            s = self._rec.create_stream()
            s.accept_waveform(STT_RATE, samples)
            self._rec.decode_stream(s)
            return s.result.text.strip()


def load_model(kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> SherpaWhisperModel:
    backend, _, name = kind.partition(":")
    fp32 = opts.get("fp32", backend.endswith("-fp32"))
    return SherpaWhisperModel(kind, name, models, threads, fp32)


class SherpaWhisperBackend(PluginBase):
    backend_ids = IDS
    name = "stt.sherpa_whisper"
    priority = 0

    def start(self) -> None:
        self.ctx.provide("stt-backend", self)

    def load(self, kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> SherpaWhisperModel:
        return load_model(kind, models, threads, opts)


SPEC = PluginSpec(
    name="stt.sherpa_whisper", kind="stt", version="1", machine="speech",
    summary="sherpa-onnx Whisper speech to text backend",
    provides=("stt-backend",), create=lambda ctx, cfg: SherpaWhisperBackend(ctx, cfg),
    tests=("wall/tests/speech/test_sp_stt_backends.py",),
)
