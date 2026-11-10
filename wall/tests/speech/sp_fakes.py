"""Shared fakes for the speech plugin tests: fake model trees, fake sherpa_onnx, fake TTS backends.

Nothing here touches the real ~/.config/mira, a real model or a real port.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import types
from pathlib import Path

import numpy as np
import pytest

WALL = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures"
for _d in (WALL, WALL / "speech"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from mirakit.plugin import PluginBase  # noqa: E402
from mirakit.testkit import FakeCtx, cfg_for  # noqa: E402


def _touch_model(d: Path, onnx: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / onnx).write_bytes(b"onnx")
    (d / "tokens.txt").write_text("a 1\n")
    (d / "espeak-ng-data").mkdir(exist_ok=True)
    (d / "espeak-ng-data" / "en_dict").write_bytes(b"x")


def make_piper(models: Path, dir_name: str, json_fixture: str = "en_US-amy-medium.onnx.json", lang: str | None = None) -> Path:
    d = models / dir_name
    stem = dir_name.removeprefix("vits-piper-").removesuffix("-int8")
    _touch_model(d, f"{stem}.onnx")
    meta = json.loads((FIX / json_fixture).read_text())
    if lang:
        meta["language"]["code"] = lang
    (d / f"{stem}.onnx.json").write_text(json.dumps(meta))
    return d


def make_kokoro(models: Path, dir_name: str, table: bool = True) -> Path:
    d = models / dir_name
    _touch_model(d, "model.int8.onnx" if "int8" in dir_name else "model.onnx")
    (d / "voices.bin").write_bytes(b"v")
    (d / "lexicon-us-en.txt").write_text("a b\n")
    return d


def make_models(base: Path, *, with_broken: bool = True) -> Path:
    """The tree the mini has, in miniature. Returns the models dir (base/models)."""
    models = base / "models"
    models.mkdir(parents=True, exist_ok=True)
    make_piper(models, "vits-piper-en_US-amy-medium")
    make_piper(models, "vits-piper-en_US-amy-medium-int8")
    make_piper(models, "vits-piper-en_US-lessac-medium-int8")                  # int8 only
    make_piper(models, "vits-piper-en_US-kristin-medium")
    make_piper(models, "vits-piper-en_US-hfc_female-medium")
    make_piper(models, "vits-piper-en_GB-jenny_dioco-medium", lang="en_GB")
    make_piper(models, "vits-piper-en_GB-jenny_dioco-medium-int8", lang="en_GB")
    make_piper(models, "vits-piper-en_US-libritts_r-medium", "en_US-libritts_r-medium.onnx.json")
    make_kokoro(models, "kokoro-int8-multi-lang-v1_0")
    make_kokoro(models, "kokoro-en-v0_19")
    make_kokoro(models, "kokoro-int8-en-v0_19")
    if with_broken:
        d = make_piper(models, "vits-piper-en_US-ghost-medium")
        (d / "tokens.txt").unlink()
    return models


# ---- fake sherpa_onnx -----------------------------------------------------------------------

class _Cfg:
    def __init__(self, **kw):
        self.kw = kw


class FakeSherpa(types.ModuleType):
    """Records every config it is given; OfflineTts.generate returns a short ramp."""

    def __init__(self, num_speakers: int = 1, rate: int = 22050):
        super().__init__("sherpa_onnx")
        outer = self
        self.configs: list[dict] = []
        self.generated: list[tuple] = []
        self.rate, self.num_speakers = rate, num_speakers
        self.empty = False

        class OfflineTtsVitsModelConfig(_Cfg):
            pass

        class OfflineTtsKokoroModelConfig(_Cfg):
            pass

        class OfflineTtsModelConfig(_Cfg):
            pass

        class OfflineTtsConfig(_Cfg):
            pass

        class Audio:
            def __init__(self, samples, rate):
                self.samples, self.sample_rate = samples, rate

        class OfflineTts:
            def __init__(self, cfg):
                outer.configs.append(cfg.kw["model"].kw)
                self.sample_rate = outer.rate
                self.num_speakers = outer.num_speakers

            def generate(self, text, sid=0, speed=1.0):
                outer.generated.append((text, sid, speed))
                if outer.empty:
                    return Audio([], outer.rate)
                return Audio(list(np.linspace(0.0, 0.4, 100)), outer.rate)

        self.OfflineTtsVitsModelConfig = OfflineTtsVitsModelConfig
        self.OfflineTtsKokoroModelConfig = OfflineTtsKokoroModelConfig
        self.OfflineTtsModelConfig = OfflineTtsModelConfig
        self.OfflineTtsConfig = OfflineTtsConfig
        self.OfflineTts = OfflineTts


# ---- fake TTS backend and model -------------------------------------------------------------

class FakeTtsModel:
    def __init__(self, model_dir: str, rate: int = 22050):
        self.model_dir, self.rate, self.num_speakers = model_dir, rate, 1
        self.lock = threading.Lock()
        self.calls: list[tuple] = []
        self.gate: threading.Event | None = None      # when set, generate waits on it
        self.entered = threading.Event()
        self.amp = 0.5

    def generate(self, text, sid, speed):
        self.calls.append((text, sid, speed))
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never opened"
        return np.full(self.rate // 10, self.amp, dtype=np.float32), self.rate


class FakeTtsBackend(PluginBase):
    """handles() is true for every directory; load() hands out FakeTtsModels and remembers them."""
    engine = "fake"
    name = "tts.fake"
    priority = 0

    def __init__(self):
        self.loads: list[str] = []
        self.models: dict[str, list[FakeTtsModel]] = {}
        self.fail: set[str] = set()
        self.gates: dict[str, threading.Event] = {}
        self.sig = None

    def start(self):
        pass

    def signature(self):
        return self.sig

    def handles(self, model_dir):
        return True

    def load(self, model_dir, threads, opts):
        name = Path(model_dir).name
        self.loads.append(name)
        if name in self.fail:
            raise RuntimeError("boom")
        m = FakeTtsModel(name)
        if name in self.gates:
            m.gate = self.gates[name]
        self.models.setdefault(name, []).append(m)
        return m


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def build_tts_stack(tmp_path: Path, *, clock=None, tts_cfg: dict | None = None, catalog_cfg: dict | None = None,
                    with_models: bool = True, backend: FakeTtsBackend | None = None):
    """(ctx, tts plugin, catalog, backend) with the catalog scanning a fake tree and a started tts.engine."""
    from speech_plugins import server_http, tts_engine, voices_catalog
    base = tmp_path / "base"
    if with_models:
        make_models(base)
    ctx = FakeCtx("speech")
    ctx.cfgs["server.http"] = cfg_for(server_http.SPEC, base_dir=str(base))
    cat_cfg = cfg_for(voices_catalog.SPEC, rtf_file=str(tmp_path / "rtf.json"), **(catalog_cfg or {}))
    ctx.cfgs["voices.catalog"] = cat_cfg
    cat_plugin = voices_catalog.SPEC.create(ctx, cat_cfg)
    ctx.cfgs["tts.engine"] = cfg_for(tts_engine.SPEC, **(tts_cfg or {}))
    backend = backend or FakeTtsBackend()
    ctx.provide("tts-backend", backend)
    cat_plugin.start()
    plugin = tts_engine.TtsEnginePlugin(ctx, ctx.cfgs["tts.engine"], clock or FakeClock())
    plugin.start()
    return ctx, plugin, cat_plugin.catalog, backend


def token_file(path: Path, value: str) -> Path:
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)


def _real_config_snapshot():
    root = Path(os.path.expanduser("~/.config/mira"))
    if not root.exists():
        return None
    out = {}
    for p in sorted(root.rglob("*")):
        try:
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            pass
    return out


@pytest.fixture(scope="session", autouse=True)
def real_config_dir_untouched():
    """Imported into the test modules that write files: nothing may land under the real ~/.config/mira.
    (Not a conftest.py: a second conftest in tests/ would shadow tests/conftest.py for `from conftest import ...`.)"""
    before = _real_config_snapshot()
    yield
    assert _real_config_snapshot() == before, "a speech test wrote under the real ~/.config/mira"
