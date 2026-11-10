"""STT backend "faster-whisper": CTranslate2 Whisper through the faster_whisper package.

It is the only backend that honours a vocabulary prompt, hotwords and a beam size; those three and the
language are read from the model's own attributes at every transcribe, so a live knob change reaches the
model the engine already holds (apply() sets them on every model this plugin built and still lives).

FIXED: condition_on_previous_text is False (a Whisper prompt carried between clips drifts).
LEGACY_CLASSES = ("FasterWhisperModel",)
"""
from __future__ import annotations

import threading
import weakref
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from mirakit.plugin import Knob, PluginBase, PluginConfig, PluginSpec

IDS = ("faster-whisper",)
DEFAULT_VOCAB = ("Mira, Hermes, 3D printers, Sovol SV08, Onshape, OrcaSlicer, slicer, vault, timer, "
                 "calendar, YouTube Music")


def _opt(value, fallback):
    """An explicit value wins; None falls back. "" is a real value (turns a prompt off)."""
    return fallback if value is None else value


class FasterWhisperModel:
    def __init__(self, kind: str, name: str, models: Path, threads: int, compute: str = "int8",
                 prompt: str = "", hotwords: str = "", beam_size: int = 1, language: str = "en"):
        from faster_whisper import WhisperModel
        self.kind = kind
        self.name = name
        self.lock = threading.Lock()
        self.prompt, self.hotwords, self.beam_size, self.language = prompt, hotwords, beam_size, language
        self._fw = WhisperModel(name, device="cpu", compute_type=compute, cpu_threads=threads,
                                download_root=str(models / "fw"))

    def transcribe(self, samples: np.ndarray) -> str:
        with self.lock:
            # An empty string must reach faster-whisper as None: "" would still be tokenised as a prompt.
            segs, _ = self._fw.transcribe(samples, beam_size=self.beam_size, language=self.language,
                                          condition_on_previous_text=False,
                                          initial_prompt=self.prompt or None,
                                          hotwords=self.hotwords or None)
            return " ".join(s.text.strip() for s in segs).strip()


def load_model(kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> FasterWhisperModel:
    """opts: compute, prompt, hotwords, beam_size, language (all optional; missing ones use the backend defaults)."""
    name = kind.partition(":")[2]
    return FasterWhisperModel(kind, name, Path(models), threads,
                              compute=opts.get("compute", "int8"), prompt=opts.get("prompt", ""),
                              hotwords=opts.get("hotwords", ""), beam_size=opts.get("beam_size", 1),
                              language=opts.get("language", "en"))


class FasterWhisperBackend(PluginBase):
    backend_ids = IDS
    name = "stt.faster_whisper"
    priority = 0

    def __init__(self, ctx, cfg: PluginConfig) -> None:
        super().__init__(ctx, cfg)
        self._models: "weakref.WeakSet[FasterWhisperModel]" = weakref.WeakSet()

    def start(self) -> None:
        self.ctx.provide("stt-backend", self)

    def load(self, kind: str, models: Path, threads: int, opts: Mapping[str, Any]) -> FasterWhisperModel:
        cfg = self.cfg
        merged = {"compute": cfg.compute, "prompt": cfg.prompt, "hotwords": cfg.hotwords,
                  "beam_size": cfg.beam, "language": cfg.language}
        merged.update(opts)
        model = load_model(kind, models, threads, merged)
        self._models.add(model)
        return model

    def signature(self) -> tuple:
        """The rebuild-class settings a model is built with; the engine compares it to know when to rebuild."""
        return (self.cfg.compute,)

    def rebuild(self, cfg: PluginConfig) -> None:
        """compute changed: the model the engine holds was built with the old one, so the engine rebuilds it.
        The service stays provided throughout (the core checks that after a rebuild)."""
        self.cfg = cfg
        engine = self.ctx.get("stt", None)
        if engine is not None:
            engine.rebuild_for_backend(self)

    def apply(self, changed: Mapping[str, Any], cfg: PluginConfig) -> None:
        self.cfg = cfg
        for model in list(self._models):
            model.prompt, model.hotwords = cfg.prompt, cfg.hotwords
            model.beam_size, model.language = cfg.beam, cfg.language

    def status(self) -> dict:
        return {"models": len(self._models)}


SPEC = PluginSpec(
    name="stt.faster_whisper", kind="stt", version="1", machine="speech",
    summary="faster-whisper speech to text backend (prompt, hotwords, beam)",
    provides=("stt-backend",), create=lambda ctx, cfg: FasterWhisperBackend(ctx, cfg),
    knobs=(
        Knob("compute", "enum", "int8", "CTranslate2 compute type of the Whisper model; takes effect when the model is rebuilt.",
             choices=("int8", "int8_float32", "int16", "float32"), applies="rebuild",
             env="MIRA_STT_COMPUTE", topic="hearing"),
        Knob("beam", "int", 1, "Beam size of the decoder; higher can be a little more accurate and slower.",
             unit="beams", min=1, max=5, env="MIRA_STT_BEAM", topic="hearing"),
        Knob("prompt", "str", f"Vocabulary: {DEFAULT_VOCAB}.",
             "Vocabulary hint that biases Whisper toward the words said to the wall; an empty text turns it off.",
             min=0, max=1000, env="MIRA_STT_PROMPT", topic="hearing"),
        Knob("hotwords", "str", "", "Extra words Whisper is nudged toward; empty for none.",
             min=0, max=400, env="MIRA_STT_HOTWORDS", topic="hearing"),
        Knob("language", "str", "en", "Language code handed to Whisper.", min=2, max=8, topic="hearing"),
    ),
    tests=("wall/tests/speech/test_sp_stt_backends.py",),
)
