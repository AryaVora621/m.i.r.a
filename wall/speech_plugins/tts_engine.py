"""Plugin tts.engine: the speech engine of the mini (spec 2.9.2), provides "tts".

One active voice model, an optional candidate slot for a voice in another model, and an immutable settings
snapshot. A request takes one reference to (active model, settings) and never sees a half-applied change.

Swaps build the new model on the calling thread (in production the core's apply worker), outside the
reference lock, warm it, then replace the reference; requests in flight finish on the old model, which is
dropped by refcount. A failed build keeps the old model and raises, so the core marks the change failed and
writes the old value back. The reference lock is held only for assignments. A second lock serialises the
loading of candidate models so two requests cannot push the machine past max_loaded; it never blocks a
request on the active model.

FIXED: CANDIDATE_IDLE_S (a candidate model is released after this many idle seconds), SWEEP_S (how often the
idle sweeper looks), WARM_TEXT, the WAV contract (PCM_CHANNELS mono, PCM_WIDTH 2 bytes, at the model rate), the
peak normalisation floor.

A rebuild-class knob that lives in a backend plugin (tts.piper.length_scale) reaches this engine through the
backend: its rebuild() calls rebuild_for_backend(), because the core calls only the owning plugin's rebuild().
"""
from __future__ import annotations

import io
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginConfig, PluginSpec

CANDIDATE_IDLE_S = 60.0
SWEEP_S = 5.0
WARM_TEXT = "Ready."
PEAK_FLOOR = 1e-4
PCM_CHANNELS = 1
PCM_WIDTH = 2


class BusyError(RuntimeError):
    """A second preview render was asked for while one is running (HTTP 429 busy)."""


def write_wav(samples: np.ndarray, rate: int) -> bytes:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(PCM_CHANNELS)
        w.setsampwidth(PCM_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def normalise(samples: np.ndarray, peak: float) -> np.ndarray:
    """Peak-normalise to `peak` so every reply plays at the same level whatever the voice or text."""
    top = float(np.max(np.abs(samples))) if samples.size else 0.0
    if top < PEAK_FLOOR:
        return samples
    return samples * (peak / top)


@dataclass(frozen=True)
class Settings:
    voice_id: str
    model_dir: str
    sid: int
    speed: float
    peak: float


@dataclass
class Slot:
    model: Any
    model_dir: str
    last_used: float = 0.0
    backend: Any = None
    sig: Any = None          # the backend's rebuild-class settings this model was built with


@dataclass(frozen=True)
class _State:
    active: Slot
    settings: Settings


class TtsEngineImpl:
    """The "tts" service object."""

    def __init__(self, plugin: "TtsEnginePlugin", clock: Callable[[], float] = time.monotonic) -> None:
        self._p = plugin
        self._clock = clock
        self._state: Optional[_State] = None
        self._cands: dict[str, Slot] = {}
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self.preview_lock = threading.Lock()
        self._threads = 0
        self.priority = 0
        self.name = "tts.engine"

    # ---- service surface -----------------------------------------------------------------

    @property
    def voice_id(self) -> str:
        st = self._state
        return st.settings.voice_id if st else ""

    @property
    def model_dir(self) -> str:
        st = self._state
        return st.active.model_dir if st else ""

    def loaded(self) -> list[dict]:
        with self._lock:
            st = self._state
            rows = []
            if st is not None:
                rows.append({"model_dir": st.active.model_dir, "role": "active", "voice": st.settings.voice_id})
            now = self._clock()
            for slot in self._cands.values():
                rows.append({"model_dir": slot.model_dir, "role": "candidate",
                             "idle_s": round(now - slot.last_used, 1)})
            return rows

    def synth(self, text: str, voice: str = "", speed: float = 0.0, peak: float = 0.0) -> tuple[bytes, str]:
        """(wav bytes, resolved voice id). Raises ConfigError for an unknown or unusable voice, BusyError for a
        second preview, RuntimeError when the model produces nothing."""
        state = self._state
        if state is None:
            raise RuntimeError("tts engine is not running")
        self.sweep()
        st = state.settings
        cat = self._p.ctx.get("voices")
        entry = cat.resolve(voice) if voice else None
        if entry is not None and not entry["usable"]:
            raise ConfigError(f"The voice {entry['alias']} cannot be used: {entry['reason']}.", "voice")
        spd = self._clip("speed", speed or st.speed)
        pk = self._clip("peak", peak or st.peak)
        target = entry["id"] if entry is not None else st.voice_id
        override = target.lower() != st.voice_id.lower()
        if override and not self.preview_lock.acquire(blocking=False):
            raise BusyError("a voice preview is already being rendered")
        try:
            if not override:
                model, sid, vid = state.active.model, st.sid, st.voice_id
            elif entry["model_dir"] == state.active.model_dir:
                model, sid, vid = state.active.model, cat.locate(entry)[1], entry["id"]
            else:
                slot = self._candidate(entry, cat)
                model, sid, vid = slot.model, cat.locate(entry)[1], entry["id"]
            t0 = time.perf_counter()
            samples, rate = model.generate(text, sid, spd)
            took = time.perf_counter() - t0
        finally:
            if override:
                self.preview_lock.release()
        duration = len(samples) / rate
        if duration > 0 and hasattr(cat, "record_rtf"):
            cat.record_rtf(vid, took / duration)
        return write_wav(normalise(samples, pk), rate), vid

    # ---- candidate slot -------------------------------------------------------------------

    def _capacity(self, cfg: Optional[PluginConfig] = None) -> int:
        return max(int((cfg or self._p.cfg).max_loaded) - 1, 0)

    def _candidate(self, entry: dict, cat) -> Slot:
        key = entry["model_dir"]
        cfg = self._p.cfg
        with self._lock:
            slot = self._cands.get(key)
            if slot is not None:
                slot.last_used = self._clock()
                return slot
        if self._capacity(cfg) < 1:
            raise ConfigError("Only one voice model may be loaded at a time, so I cannot use that voice now.", "voice")
        with self._load_lock:
            with self._lock:
                slot = self._cands.get(key)
                if slot is not None:
                    slot.last_used = self._clock()
                    return slot
                while self._cands and len(self._cands) >= self._capacity(cfg):
                    oldest = min(self._cands, key=lambda k: self._cands[k].last_used)
                    del self._cands[oldest]
            # A swap may have finished while this request waited for the load lock; if the model it wants is now
            # the active one, use that instead of loading a second copy.
            current = self._state
            if current is not None and current.active.model_dir == key:
                return current.active
            slot = self._build(entry, cat, cfg)
            with self._lock:
                self._cands[key] = slot
            return slot

    def sweep(self) -> None:
        """Release candidate models that have been idle for CANDIDATE_IDLE_S."""
        now = self._clock()
        with self._lock:
            for key in [k for k, s in self._cands.items() if now - s.last_used >= CANDIDATE_IDLE_S]:
                del self._cands[key]

    # ---- building and swapping ------------------------------------------------------------

    def _backend_for(self, path):
        for b in self._p.ctx.all("tts-backend"):
            if b.handles(path):
                return b
        raise RuntimeError(f"no TTS backend handles {path.name}")

    def _build(self, entry: dict, cat, cfg: PluginConfig) -> Slot:
        """Load and warm a model for `entry` with the settings of `cfg`. Touches no engine state."""
        path, sid = cat.locate(entry)
        try:
            backend = self._backend_for(path)
            model = backend.load(path, cfg.threads, {})
            sig = getattr(backend, "signature", lambda: None)()
            if cfg.warm:
                model.generate(WARM_TEXT, sid, cfg.speed)
        except Exception as exc:
            raise RuntimeError(f"cannot load voice model {path.name} ({type(exc).__name__})") from exc
        return Slot(model, entry["model_dir"], self._clock(), backend, sig)

    def _clip(self, name: str, value: float) -> float:
        knob = next(k for k in SPEC.knobs if k.name == name)
        return min(max(float(value), knob.min), knob.max)

    def _entry_for(self, cfg: PluginConfig, cat) -> dict:
        entry = cat.resolve(cfg.voice)
        if not entry["usable"]:
            raise ConfigError(f"The voice {entry['alias']} cannot be used: {entry['reason']}.", "voice")
        return entry

    def boot(self, cfg: PluginConfig) -> None:
        cat = self._p.ctx.get("voices")
        entry = self._entry_for(cfg, cat)
        slot = self._build(entry, cat, cfg)
        settings = Settings(entry["id"], entry["model_dir"], cat.locate(entry)[1], cfg.speed, cfg.peak)
        with self._lock:
            self._state = _State(slot, settings)
            self._threads = cfg.threads

    def reconcile(self, cfg: PluginConfig, force: bool = False) -> None:
        """Make the engine match cfg. Same model: swap the settings only. A candidate holds the model: promote
        it. Otherwise build, warm and swap. Raises and changes nothing when the build fails."""
        cat = self._p.ctx.get("voices")
        entry = self._entry_for(cfg, cat)
        settings = Settings(entry["id"], entry["model_dir"], cat.locate(entry)[1], cfg.speed, cfg.peak)
        old = self._state
        if old is None:
            self.boot(cfg)
            return
        same_threads = cfg.threads == self._threads
        if same_threads and not force and entry["model_dir"] == old.active.model_dir:
            with self._lock:
                self._state = _State(old.active, settings)
                self._trim(cfg)
            return
        # The swap build holds the load lock and first evicts every candidate except the one being promoted, so
        # active + candidates + the new model never exceed max_loaded (the swap itself needs one spare slot, so
        # with max_loaded 1 the old and new active model coexist briefly). A preview that arrives mid-build waits
        # on the same lock instead of loading a candidate beside it.
        with self._load_lock:
            promoted = None
            with self._lock:
                if same_threads and not force:
                    promoted = self._cands.pop(entry["model_dir"], None)
                self._cands.clear()
            slot = promoted if promoted is not None else self._build(entry, cat, cfg)
            with self._lock:
                self._state = _State(slot, settings)
                self._threads = cfg.threads
                self._trim(cfg)
        del old, promoted      # the old model is freed once the last request on it finishes

    def rebuild_for_backend(self, backend) -> None:
        """A rebuild-class setting of `backend` changed (called from that plugin's rebuild()). Rebuild the active
        model when this backend built it with other settings, and drop its stale candidates. With nothing
        stale this does nothing, which makes the core's rollback after a failed rebuild free."""
        sig = getattr(backend, "signature", lambda: None)()
        state = self._state
        if state is None:
            return
        with self._lock:
            for key in [k for k, c in self._cands.items() if c.backend is backend and c.sig != sig]:
                del self._cands[key]
        if state.active.backend is backend and state.active.sig != sig:
            self.reconcile(self._p.cfg, force=True)

    def _trim(self, cfg: Optional[PluginConfig] = None) -> None:
        """Called with the lock held: drop candidates beyond the capacity, least recently used first."""
        while len(self._cands) > self._capacity(cfg):
            del self._cands[min(self._cands, key=lambda k: self._cands[k].last_used)]

    def clear(self) -> None:
        with self._lock:
            self._state = None
            self._cands.clear()


class TtsEnginePlugin(PluginBase):
    def __init__(self, ctx, cfg: PluginConfig, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(ctx, cfg)
        self.engine = TtsEngineImpl(self, clock)
        self._stop = threading.Event()
        self._sweeper: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        self.engine.boot(self.cfg)
        self.ctx.provide("tts", self.engine)
        self._sweeper = threading.Thread(target=self._sweep_loop, name="tts-candidate-sweeper", daemon=True)
        self._sweeper.start()

    def _sweep_loop(self) -> None:
        while not self._stop.wait(SWEEP_S):
            self.engine.sweep()

    def stop(self) -> None:
        self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=SWEEP_S)
        self.engine.clear()

    def rebuild(self, cfg: PluginConfig) -> None:
        # The new config is kept only when the swap worked, so a failed change leaves the old one in place.
        self.engine.reconcile(cfg)
        self.cfg = cfg

    def apply(self, changed, cfg: PluginConfig) -> None:
        self.engine.reconcile(cfg)
        self.cfg = cfg

    def status(self) -> dict:
        return {"voice": self.engine.voice_id, "loaded": len(self.engine.loaded())}


def _validate(values) -> None:
    if not str(values.get("voice", "")).strip():
        raise ConfigError("voice needs a voice id or alias", "voice")


SPEC = PluginSpec(
    name="tts.engine", kind="tts", version="1", machine="speech",
    summary="text to speech engine with runtime voice, speed and loudness",
    provides=("tts",), create=lambda ctx, cfg: TtsEnginePlugin(ctx, cfg),
    knobs=(
        Knob("voice", "voice", "vits-piper-en_US-amy-medium#0", "Her voice: a catalog voice id or alias.",
             applies="rebuild", agent="bounded", env="MIRA_TTS_DIR", topic="voice", spoken="my voice",
             phrases=("change your voice", "tune your voice model", "sound different")),
        Knob("speed", "float", 1.0, "How fast she talks; higher is faster.", unit="x", min=0.5, max=1.6,
             agent="bounded", agent_min=0.8, agent_max=1.3, agent_step=0.1, topic="voice",
             spoken="how fast I talk", effect="talks faster",
             phrases=("you talk too fast", "slow down", "speak faster")),
        Knob("peak", "float", 0.89, "How loud her replies are, as the peak level the audio is normalised to.",
             unit="peak", min=0.3, max=0.98, agent="bounded", agent_min=0.6, agent_max=0.95, agent_step=0.05,
             env="MIRA_TTS_PEAK", topic="voice", spoken="how loud I talk", effect="louder replies",
             phrases=("too quiet", "too loud", "speak up")),
        Knob("threads", "int", 4, "CPU threads the voice model uses.", unit="threads", min=1, max=8,
             applies="rebuild", env="MIRA_TTS_THREADS", topic="voice"),
        Knob("max_loaded", "int", 2, "Voice models kept in memory at once: the active one plus one candidate.",
             unit="models", min=1, max=3, topic="voice"),
        Knob("warm", "bool", True, "Warm a new voice model with one short phrase before it replaces the old one.",
             topic="voice"),
    ),
    needs=("voices", "tts-backend"),
    validate=_validate,
    tests=("wall/tests/speech/test_sp_tts_engine.py",),
)
