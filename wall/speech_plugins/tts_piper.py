"""TTS backend "piper": Piper (VITS) voices run by sherpa-onnx.

handles() looks at the model's own metadata (a Piper *.onnx.json with an "audio" section), never at the
directory name. generate() keeps v1's contract: float32 samples and the model rate.

FIXED: provider "cpu", the model file is the first .onnx that does not start with "voices".
"""
from __future__ import annotations

import json
import threading
import weakref
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from mirakit.plugin import Knob, PluginBase, PluginConfig, PluginSpec

ENGINE = "piper"


def model_file(d: Path) -> str:
    files = sorted(p.name for p in d.iterdir())
    return next(f for f in files if f.endswith(".onnx") and not f.startswith("voices"))


def read_meta(d: Path) -> dict | None:
    """The Piper json beside the model, or None when the directory carries none."""
    for p in sorted(d.glob("*.onnx.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) and isinstance(meta.get("audio"), dict) else None
    return None


class PiperModel:
    def __init__(self, model_dir: Path, threads: int, length_scale: float = 1.0):
        import sherpa_onnx
        d = Path(model_dir)
        model = model_file(d)
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=str(d / model), tokens=str(d / "tokens.txt"), data_dir=str(d / "espeak-ng-data"),
            length_scale=length_scale)
        mcfg = sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=threads, provider="cpu")
        self._tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=mcfg))
        self.model_dir = d.name
        self.rate = self._tts.sample_rate
        meta = read_meta(d) or {}
        self.num_speakers = int(getattr(self._tts, "num_speakers", 0) or meta.get("num_speakers", 1) or 1)
        self.lock = threading.Lock()

    def generate(self, text: str, sid: int, speed: float) -> tuple[np.ndarray, int]:
        with self.lock:
            audio = self._tts.generate(text, sid=sid, speed=speed)
        if not audio.samples:
            raise RuntimeError("tts produced no audio")
        return np.array(audio.samples, dtype=np.float32), audio.sample_rate


def load_model(model_dir: Path, threads: int, opts: Mapping[str, Any]) -> PiperModel:
    return PiperModel(Path(model_dir), threads, length_scale=float(opts.get("length_scale", 1.0)))


def handles(model_dir: Path) -> bool:
    d = Path(model_dir)
    return d.is_dir() and read_meta(d) is not None


class PiperBackend(PluginBase):
    engine = ENGINE
    name = "tts.piper"
    priority = 0

    def __init__(self, ctx, cfg: PluginConfig) -> None:
        super().__init__(ctx, cfg)
        self._models: "weakref.WeakSet[PiperModel]" = weakref.WeakSet()

    def start(self) -> None:
        self.ctx.provide("tts-backend", self)

    def handles(self, model_dir: Path) -> bool:
        return handles(model_dir)

    def signature(self) -> tuple:
        """The rebuild-class settings a model is built with; the engine compares it to know when to rebuild."""
        return (self.cfg.length_scale,)

    def rebuild(self, cfg: PluginConfig) -> None:
        """length_scale changed: Piper bakes it in at load, so the engine rebuilds the voice it holds."""
        self.cfg = cfg
        engine = self.ctx.get("tts", None)
        if engine is not None:
            engine.rebuild_for_backend(self)

    def load(self, model_dir: Path, threads: int, opts: Mapping[str, Any]) -> PiperModel:
        merged = {"length_scale": self.cfg.length_scale}
        merged.update(opts)
        model = load_model(model_dir, threads, merged)
        self._models.add(model)
        return model


SPEC = PluginSpec(
    name="tts.piper", kind="tts", version="1", machine="speech",
    summary="Piper (VITS) text to speech backend",
    provides=("tts-backend",), create=lambda ctx, cfg: PiperBackend(ctx, cfg),
    knobs=(
        Knob("length_scale", "float", 1.0, "Piper speaking length; higher is slower. Applies when a voice model is rebuilt.",
             unit="x", min=0.5, max=2.0, applies="rebuild", env="MIRA_TTS_LENGTH_SCALE", topic="voice"),
    ),
    tests=("wall/tests/speech/test_sp_tts_backends.py",),
)
