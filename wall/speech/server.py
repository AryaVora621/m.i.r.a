#!/usr/bin/env python3
"""mira-speech: STT and TTS over HTTP for the wall, run on the Mac mini.

This file is the LaunchAgent's entry point and has two jobs.

1. Compat module. bench_stt.py, bench_tts.py, tests/stt_eval.py and tests/stt_eval_test.py import it and use
   the module-level names below, computed from env at import exactly as the first version did, plus the
   Stt and Tts classes. Stt and Tts are thin dispatchers: they read MODELS, THREADS, TTS_THREADS and the
   STT_* defaults from THIS module's globals at call time and hand them to the backend plugin modules in
   speech_plugins, so monkeypatching a global here still works.
2. Launcher. main() runs the frozen server_v1.py (legacy) or boots the plugin engine (mirakit Core with the
   speech_plugins). It falls back to legacy when the config file is missing or unreadable, when
   core.engine is "legacy", when mirakit or speech_plugins cannot be imported, or when the crash rule trips.

Only stdlib and numpy are imported at the top: the legacy fallback must still work when a plugin module is
broken, so speech_plugins and mirakit are imported lazily.

Config comes from env (see below) and a bearer token file, never from the repo.
"""

import io
import json
import os
import signal
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
# In the repo speech_plugins and mirakit sit one level above this file; on the mini they may sit beside it.
for _d in (HERE.parent, HERE):
    if str(_d) not in sys.path:
        sys.path.append(str(_d))

HOME = Path.home()
BASE = Path(os.environ.get("MIRA_SPEECH_DIR", HOME / "Library/Application Support/mira-speech"))
MODELS = BASE / "models"
TOKEN_FILE = Path(os.environ.get("MIRA_SPEECH_TOKEN_FILE", HOME / ".config/mira/speech.token"))
# Never 0.0.0.0: the bearer token travels over plain HTTP, so the server answers only on loopback and on
# the mini's Tailscale address (encrypted and authenticated by WireGuard). Comma-separated.
BIND = [a.strip() for a in os.environ.get("MIRA_SPEECH_BIND", "127.0.0.1,100.77.98.89").split(",") if a.strip()]
PORT = int(os.environ.get("MIRA_SPEECH_PORT", "8765"))
STT_KIND = os.environ.get("MIRA_STT", "sherpa-moonshine:sherpa-onnx-moonshine-base-en-int8")
TTS_DIR = os.environ.get("MIRA_TTS_DIR", "vits-piper-en_US-amy-medium")
TTS_VOICE = os.environ.get("MIRA_TTS_VOICE", "amy")
THREADS = int(os.environ.get("MIRA_THREADS", "4"))
TTS_THREADS = int(os.environ.get("MIRA_TTS_THREADS", str(THREADS)))
TARGET_PEAK = float(os.environ.get("MIRA_TTS_PEAK", "0.89"))
# faster-whisper only. Moonshine and the sherpa backends cannot be biased, so they ignore these.
# The default vocabulary is what the owner says to the wall that a small model otherwise mishears
# ("3D printers" came back as "pen"); override or empty it from the LaunchAgent env.
DEFAULT_VOCAB = ("Mira, Hermes, 3D printers, Sovol SV08, Onshape, OrcaSlicer, slicer, vault, timer, "
                 "calendar, YouTube Music")
# Measured on the owner's room takes (tests/stt_eval.py): a vocabulary prompt cut tiny.en's word error from 0.51 to
# 0.07 and also fixed "Mira"; hotwords alone helped less. Set MIRA_STT_PROMPT="" to switch it off.
STT_PROMPT = os.environ.get("MIRA_STT_PROMPT", f"Vocabulary: {DEFAULT_VOCAB}.")
STT_HOTWORDS = os.environ.get("MIRA_STT_HOTWORDS", "")
STT_COMPUTE = os.environ.get("MIRA_STT_COMPUTE", "int8")
STT_BEAM = int(os.environ.get("MIRA_STT_BEAM", "1"))

MAX_STT_BYTES = 30 * 16000 * 2 + 4096
MAX_TTS_CHARS = 600
STT_RATE = 16000

START = time.time()


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def read_wav16k(data: bytes) -> np.ndarray:
    """Strict: 16 kHz mono s16 only. Returns float32 in [-1, 1]."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getframerate() != STT_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("need 16 kHz mono 16-bit WAV")
        n = w.getnframes()
        if n > 30 * STT_RATE:
            raise ValueError("clip longer than 30 s")
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def write_wav(samples: np.ndarray, rate: int) -> bytes:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def normalise(samples: np.ndarray) -> np.ndarray:
    """Peak-normalise so every reply plays at the same level whatever the voice or text."""
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < 1e-4:
        return samples
    return samples * (TARGET_PEAK / peak)


def _opt(value, env):
    """An explicit argument wins; None falls back to the env value. "" is a real value (turns it off)."""
    return env if value is None else value


# ---------------------------------------------------------------------------------------------
# Compat classes: thin dispatchers over the backend plugin modules
# ---------------------------------------------------------------------------------------------

_STT_MODULES = {
    "faster-whisper": "stt_faster_whisper",
    "sherpa-whisper": "stt_sherpa_whisper",
    "sherpa-moonshine": "stt_sherpa_moonshine",
    "sherpa-moonshine-v2": "stt_sherpa_moonshine_v2",
    "sherpa-nemo-transducer": "stt_sherpa_transducer",
    "sherpa-transducer": "stt_sherpa_transducer",
}


def _backend(module: str):
    import importlib
    return importlib.import_module(f"speech_plugins.{module}")


class Stt:
    def __init__(self, kind: str, prompt: str | None = None, hotwords: str | None = None,
                 compute_type: str | None = None, beam_size: int | None = None):
        self.kind = kind
        self.prompt = _opt(prompt, STT_PROMPT)
        self.hotwords = _opt(hotwords, STT_HOTWORDS)
        self.beam_size = _opt(beam_size, STT_BEAM)
        backend, _, name = kind.partition(":")
        backend = backend.removesuffix("-fp32")
        self.name = name
        if backend not in _STT_MODULES:
            raise SystemExit(f"unknown STT backend {backend}")
        opts = {"prompt": self.prompt, "hotwords": self.hotwords, "beam_size": self.beam_size,
                "compute": _opt(compute_type, STT_COMPUTE), "language": "en"}
        self._model = _backend(_STT_MODULES[backend]).load_model(kind, MODELS, THREADS, opts)
        self.lock = self._model.lock

    def transcribe(self, samples: np.ndarray) -> str:
        return self._model.transcribe(samples)


# The voice lists the first version hard coded; used only for a Kokoro directory that has no committed table.
_V1_ORDER = ["af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole",
             "af_nova", "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric",
             "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
             "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable",
             "bm_george", "bm_lewis"]
_V019_ORDER = ["af", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael",
               "bf_emma", "bf_isabella", "bm_george", "bm_lewis"]


class Tts:
    """Kokoro or a Piper (VITS) voice. The backend is picked from the model's own files, with the first
    version's directory-name rule as the fallback for a directory neither backend recognises."""

    def __init__(self, model_dir: str, voice: str):
        d = MODELS / model_dir
        piper, kokoro = _backend("tts_piper"), _backend("tts_kokoro")
        if kokoro.handles(d):
            is_kokoro = True
        elif piper.handles(d):
            is_kokoro = False
        else:
            is_kokoro = "kokoro" in model_dir
        if is_kokoro:
            self._model = kokoro.load_model(d, TTS_THREADS, {})
            names = kokoro.voice_names(model_dir)
            if names is None:
                order = _V019_ORDER if "v0_19" in model_dir else _V1_ORDER
                names = {n: i for i, n in enumerate(order)}
            self.voices = names
        else:
            self._model = piper.load_model(d, TTS_THREADS,
                                           {"length_scale": float(os.environ.get("MIRA_TTS_LENGTH_SCALE", "1.0"))})
            self.voices = {}
        self.name = model_dir
        self.rate = self._model.rate
        self.lock = self._model.lock
        self.default_voice = voice

    def speak(self, text: str, voice: str | None) -> tuple[np.ndarray, int]:
        sid = self.voices.get(voice or self.default_voice, self.voices.get(self.default_voice, 0))
        samples, rate = self._model.generate(text, sid, 1.0)
        return normalise(samples), rate


# ---------------------------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------------------------

PLANNED_FRESH_S = 60.0
# Used only when the file says nothing: the schema defaults of core.crash_window_s and core.crash_legacy_after.
_DEFAULT_CRASH_WINDOW_S = 600.0
_DEFAULT_CRASH_LEGACY_AFTER = 4


def config_path() -> Path:
    return Path(os.environ.get("MIRA_SPEECH_CONFIG") or (HOME / ".config/mira/speech.json"))


def _read_config(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unplanned_in_window(starts_file: Path, now: float, window_s: float) -> int:
    n = 0
    try:
        lines = starts_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "unplanned":
            try:
                if now - float(parts[0]) <= window_s:
                    n += 1
            except ValueError:
                continue
    return n


def choose_engine(path: Path, now: float | None = None) -> tuple[str, str]:
    """Decide "legacy" or "plugins" and say why. Reads only; start counting is record_start()."""
    now = time.time() if now is None else now
    cfg = _read_config(path)
    if cfg is None:
        return "legacy", "config file missing or unreadable"
    core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
    if core.get("engine", "legacy") != "plugins":
        return "legacy", "core.engine is legacy"
    try:
        import mirakit.core  # noqa: F401
        import speech_plugins
        speech_plugins.specs()      # imports every plugin module: one broken module means legacy, not a crash loop
    except Exception as e:
        return "legacy", f"plugin engine not importable ({type(e).__name__})"
    window = float(core.get("crash_window_s", _DEFAULT_CRASH_WINDOW_S))
    limit = int(core.get("crash_legacy_after", _DEFAULT_CRASH_LEGACY_AFTER))
    # The start being decided counts too: the limit is "this many unplanned starts inside the window".
    if _unplanned_in_window(path.parent / "state" / "starts", now, window) + 1 >= limit:
        return "legacy", "crash loop: running the legacy server"
    return "plugins", "core.engine is plugins"


def record_start(path: Path, now: float | None = None) -> None:
    """Append this start to <cfgdir>/state/starts; a fresh planned marker makes it a planned start and is consumed."""
    now = time.time() if now is None else now
    state = path.parent / "state"
    planned = state / "planned"
    kind = "unplanned"
    try:
        if planned.exists() and now - planned.stat().st_mtime <= PLANNED_FRESH_S:
            kind = "planned"
        planned.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        state.mkdir(parents=True, exist_ok=True)
        with open(state / "starts", "a", encoding="utf-8") as f:
            f.write(f"{now:.0f} {kind}\n")
    except OSError as e:
        log(f"cannot record start ({e.strerror})")


def _run_legacy():
    import server_v1
    server_v1.main()


def _run_plugins(path: Path):
    from mirakit.core import Core
    import speech_plugins
    core = Core.from_file(str(path), argv=sys.argv[1:], machine="speech", specs=speech_plugins.specs(),
                          mirror=False, changelog=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    core.boot()
    log("plugin engine running")
    try:
        while not stop.wait(1.0):
            if getattr(core, "restart_requested", False):
                break
    finally:
        core.shutdown()


def main():
    path = config_path()
    engine, why = choose_engine(path)
    log(f"engine {engine}: {why}")
    if engine == "legacy":
        return _run_legacy()
    record_start(path)
    return _run_plugins(path)


if __name__ == "__main__":
    main()
