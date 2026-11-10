"""Plugin stt.engine: the speech-to-text engine of the mini (spec 2.9.2), provides "stt".

One active SttModel built by the backend plugin whose backend_ids contain the backend part of `kind`. A kind
or threads change builds the new model on the calling thread (in production the core's apply worker),
outside every lock, warms it with one second of silence, then swaps the reference; requests in flight finish
on the old model (each model keeps its own lock) and the old one is dropped by refcount. A failed build keeps
the old model and raises, so the core marks the change failed and writes the old value back.

A rebuild-class knob that lives in a backend plugin (stt.faster_whisper.compute) reaches this engine through
the backend: the core calls only the owning plugin's rebuild(), which calls rebuild_for_backend() here.

FIXED: BACKEND_IDS (the backends that exist), WARM_SECONDS and the 16 kHz clip rate.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

import numpy as np

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginConfig, PluginSpec

from . import expand_path

BACKEND_IDS = ("faster-whisper", "sherpa-whisper", "sherpa-moonshine", "sherpa-moonshine-v2",
               "sherpa-nemo-transducer", "sherpa-transducer")
WARM_SECONDS = 1
STT_RATE = 16000
_KIND = re.compile(r"[a-z0-9-]+:\S+\Z")


def split_kind(kind: str) -> tuple[str, str, bool]:
    """(backend id, model name, fp32) from "backend[-fp32]:name"."""
    backend, _, name = kind.partition(":")
    fp32 = backend.endswith("-fp32")
    return backend.removesuffix("-fp32"), name, fp32


class SttEngineImpl:
    """The "stt" service object."""

    def __init__(self, plugin: "SttEnginePlugin") -> None:
        self._p = plugin
        self._model: Optional[Any] = None
        self._provider: Optional[Any] = None     # the backend plugin that built the active model
        self._sig: Any = None                    # that backend's rebuild-class settings at build time
        self._lock = threading.Lock()
        self.priority = 0
        self.name = "stt.engine"

    @property
    def kind(self) -> str:
        m = self._model
        return m.kind if m is not None else ""

    def transcribe(self, samples: np.ndarray) -> tuple[str, int]:
        model = self._model          # one reference: a swap during the call cannot change the model under us
        if model is None:
            raise RuntimeError("stt engine is not running")
        t0 = time.perf_counter()
        text = model.transcribe(samples)
        return text, round((time.perf_counter() - t0) * 1000)

    def build(self, cfg: PluginConfig):
        """(model, provider, signature) for cfg.kind. Touches no engine state."""
        backend, name, fp32 = split_kind(cfg.kind)
        provider = next((b for b in self._p.ctx.all("stt-backend") if backend in b.backend_ids), None)
        if provider is None:
            raise RuntimeError(f"no STT backend for {backend}")
        models = expand_path(self._p.ctx.cfg_of("server.http")["base_dir"], self._p.ctx.paths) / "models"
        try:
            model = provider.load(cfg.kind, models, cfg.threads, {"fp32": fp32, "name": name, "backend": backend})
            sig = getattr(provider, "signature", lambda: None)()
            if cfg.warm:
                model.transcribe(np.zeros(WARM_SECONDS * STT_RATE, dtype=np.float32))
        except Exception as exc:
            raise RuntimeError(f"cannot load STT model {name} ({type(exc).__name__})") from exc
        return model, provider, sig

    def swap(self, cfg: PluginConfig) -> None:
        model, provider, sig = self.build(cfg)
        with self._lock:
            old, self._model, self._provider, self._sig = self._model, model, provider, sig
        del old

    def rebuild_for_backend(self, provider, cfg: Optional[PluginConfig] = None) -> None:
        """A rebuild-class setting of `provider` changed. Rebuild when it built the active model with other
        settings; otherwise do nothing, which also makes the core's rollback after a failed rebuild free."""
        if self._model is None or self._provider is not provider:
            return
        if getattr(provider, "signature", lambda: None)() == self._sig:
            return
        self.swap(cfg or self._p.cfg)

    def clear(self) -> None:
        with self._lock:
            self._model, self._provider, self._sig = None, None, None


class SttEnginePlugin(PluginBase):
    def __init__(self, ctx, cfg: PluginConfig) -> None:
        super().__init__(ctx, cfg)
        self.engine = SttEngineImpl(self)

    def start(self) -> None:
        self.engine.swap(self.cfg)
        self.ctx.provide("stt", self.engine)

    def stop(self) -> None:
        self.engine.clear()

    def rebuild(self, cfg: PluginConfig) -> None:
        # A rebuild with the model already in place (the core's rollback after a failed change) loads nothing.
        if self.engine._model is not None and cfg.kind == self.engine.kind and cfg.threads == self.cfg.threads:
            self.cfg = cfg
            return
        self.engine.swap(cfg)
        self.cfg = cfg

    def apply(self, changed, cfg: PluginConfig) -> None:
        # kind and threads normally arrive through rebuild(); handling them here too keeps either route correct.
        if "kind" in changed or "threads" in changed:
            self.engine.swap(cfg)
        self.cfg = cfg

    def status(self) -> dict:
        return {"kind": self.engine.kind}


def _validate(values) -> None:
    kind = str(values.get("kind", ""))
    if not _KIND.match(kind):
        raise ConfigError("kind must look like backend:name, for example faster-whisper:tiny.en", "kind")
    backend = split_kind(kind)[0]
    if backend not in BACKEND_IDS:
        raise ConfigError(f"unknown STT backend {backend}", "kind")


SPEC = PluginSpec(
    name="stt.engine", kind="stt", version="1", machine="speech",
    summary="speech to text engine; kind picks the backend and model",
    provides=("stt",), create=lambda ctx, cfg: SttEnginePlugin(ctx, cfg),
    knobs=(
        Knob("kind", "str", "sherpa-moonshine:sherpa-onnx-moonshine-base-en-int8",
             "Backend and model as backend[-fp32]:name; the backend must be one of the installed STT plugins.",
             min=3, max=200, applies="rebuild", env="MIRA_STT", topic="hearing"),
        Knob("threads", "int", 4, "CPU threads the speech to text model uses.", unit="threads", min=1, max=8,
             applies="rebuild", env="MIRA_THREADS", topic="hearing"),
        Knob("warm", "bool", True, "Warm a new model with one second of silence before it replaces the old one.",
             topic="hearing"),
    ),
    needs=("stt-backend",),
    validate=_validate,
    tests=("wall/tests/speech/test_sp_stt_engine.py",),
)
