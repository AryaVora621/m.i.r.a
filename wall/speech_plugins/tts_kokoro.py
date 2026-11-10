"""TTS backend "kokoro": Kokoro voices run by sherpa-onnx.

handles() is true for a directory that carries a voices.bin. Voice names and their order come from the
committed table speech_plugins/voices/<model_dir>.json (written by `mira voices index` from the ONNX
metadata), never from a hand-typed list.

FIXED: provider "cpu", the model file is the first .onnx that does not start with "voices".
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from mirakit.plugin import PluginBase, PluginConfig, PluginSpec

ENGINE = "kokoro"
TABLES = Path(__file__).resolve().parent / "voices"


def voice_table(model_dir_name: str) -> dict | None:
    """The committed speaker table of a Kokoro directory, or None when there is none."""
    try:
        table = json.loads((TABLES / f"{model_dir_name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    names = table.get("speaker_names")
    return table if isinstance(names, list) and names else None


def voice_names(model_dir_name: str) -> dict | None:
    """name -> speaker id (the position in the model's own list), or None without a table."""
    table = voice_table(model_dir_name)
    return None if table is None else {n: i for i, n in enumerate(table["speaker_names"])}


class KokoroModel:
    def __init__(self, model_dir: Path, threads: int):
        import sherpa_onnx
        d = Path(model_dir)
        files = sorted(p.name for p in d.iterdir())
        model = next(f for f in files if f.endswith(".onnx") and not f.startswith("voices"))
        lex = ",".join(str(d / f) for f in files if f.startswith("lexicon"))
        kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=str(d / model), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
            data_dir=str(d / "espeak-ng-data"), lexicon=lex,
            dict_dir=str(d / "dict") if (d / "dict").is_dir() else "")
        mcfg = sherpa_onnx.OfflineTtsModelConfig(kokoro=kokoro, num_threads=threads, provider="cpu")
        self._tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=mcfg))
        self.model_dir = d.name
        self.rate = self._tts.sample_rate
        names = voice_names(d.name)
        self.num_speakers = len(names) if names else int(getattr(self._tts, "num_speakers", 1) or 1)
        self.lock = threading.Lock()

    def generate(self, text: str, sid: int, speed: float) -> tuple[np.ndarray, int]:
        with self.lock:
            audio = self._tts.generate(text, sid=sid, speed=speed)
        if not audio.samples:
            raise RuntimeError("tts produced no audio")
        return np.array(audio.samples, dtype=np.float32), audio.sample_rate


def load_model(model_dir: Path, threads: int, opts: Mapping[str, Any]) -> KokoroModel:
    return KokoroModel(Path(model_dir), threads)


def handles(model_dir: Path) -> bool:
    d = Path(model_dir)
    return d.is_dir() and (d / "voices.bin").exists()


class KokoroBackend(PluginBase):
    engine = ENGINE
    name = "tts.kokoro"
    priority = 0

    def start(self) -> None:
        self.ctx.provide("tts-backend", self)

    def handles(self, model_dir: Path) -> bool:
        return handles(model_dir)

    def load(self, model_dir: Path, threads: int, opts: Mapping[str, Any]) -> KokoroModel:
        return load_model(model_dir, threads, opts)


SPEC = PluginSpec(
    name="tts.kokoro", kind="tts", version="1", machine="speech",
    summary="Kokoro text to speech backend",
    provides=("tts-backend",), create=lambda ctx, cfg: KokoroBackend(ctx, cfg),
    tests=("wall/tests/speech/test_sp_tts_backends.py",),
)
