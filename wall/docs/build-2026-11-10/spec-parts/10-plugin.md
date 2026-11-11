
## 2. Contracts

All contract code is stdlib only (plus `numpy` in type hints inside `TYPE_CHECKING`). Python 3.13 on the Pi and the mini.

### 2.1 Plugin interface (`mirakit/plugin.py`, normative)

```python
from __future__ import annotations
import enum, logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Optional, Protocol, Sequence

CONTRACT_VERSION = 1

Applies = Literal["live", "rebuild", "restart"]
# live    : the owning plugin's apply() sets attributes on the loop thread at a safe point (Pi) or on the
#           apply worker (mini). Values read per use. VAD knobs are live and take effect at the next capture.
# rebuild : the plugin's rebuild() replaces its object. Pi: only at an ARMED top-of-loop safe point,
#           dispose first (never two spotters in MemoryMax=200M). Mini: build first off the request path,
#           then swap the reference (no request dropped).
# restart : persisted and shown as pending_restart; takes effect when the process restarts.

KnobType = Literal["bool", "int", "float", "str", "path", "url", "enum", "list[str]", "map[str,str]",
                   "regex", "voice", "time_window"]
# path  : "~" and "${HERE}" (install dir of the running code) and "${HOME}" expand; secret=True paths name
#         token files whose contents the core never reads.
# url   : http(s) only; plugins may add a host rule (loopback only, or loopback or 100.64.0.0/10).
# voice : a voice id "<model_dir>#<speaker>" or an alias; validated and canonicalised by voices.catalog.
# time_window : "HH:MM-HH:MM" local time, may wrap midnight, or "" for none.

Topic = Literal["", "voice", "hearing", "timing", "wake", "brain", "wall", "system"]

@dataclass(frozen=True)
class Knob:
    name: str                              # key inside the plugin, [a-z][a-z0-9_]*, no dots
    type: KnobType
    default: Any                           # EQUALS the legacy code default (parity); deployment values live in the file
    description: str                       # one sentence, says what moves when you raise it; <= 160 chars
    unit: str = ""                         # "s", "x", "rms", "chunks(100ms)", "score", "chars", "percent", ...
    min: Optional[float] = None            # inclusive hard bounds for every writer (numbers; length for str/list)
    max: Optional[float] = None
    choices: Optional[tuple[str, ...]] = None   # for enum
    applies: Applies = "live"
    agent: Literal["deny", "bounded"] = "deny"  # may the agent scope (Hermes, voice) set it
    agent_min: Optional[float] = None      # agent bounds, must lie inside min..max; required when agent="bounded" and numeric
    agent_max: Optional[float] = None
    agent_step: Optional[float] = None     # largest |new - old| one agent call may make; required for bounded numerics
    secret: bool = False                   # the value is a path to a secret file; contents never read by the core
    arg: Optional[str] = None              # attribute on the listener's args namespace kept in sync (also the
                                           # argparse dest of the legacy flag when one exists)
    env: Optional[str] = None              # legacy MIRA_* variable read by the v1 code for this value
    topic: Topic = ""                      # groups knobs for mira_tuning_guide and the skill glossary
    spoken: str = ""                       # plain spoken name, e.g. "voice speed"; default: name with spaces
    effect: str = ""                       # what a person notices when it goes up, spoken-safe
    phrases: tuple[str, ...] = ()          # how the owner might say it, e.g. ("you talk too fast",)
    nullable: bool = False

@dataclass(frozen=True)
class PluginSpec:
    name: str                              # "kind.variant", unique across both machines; also the config key
    kind: str
    version: str                           # bump when knobs or behaviour change; shown in inventory
    summary: str                           # one line
    machine: Literal["listener", "speech"]
    provides: tuple[str, ...]              # service names (section 2.2 lists them and which are multi)
    create: Callable[["PluginContext", "PluginConfig"], "PluginBase"]
    knobs: tuple[Knob, ...] = ()
    needs: tuple[str, ...] = ()            # services that must be active before start; else state PENDING
    wants: tuple[str, ...] = ()            # optional services, ctx.get(name, None)
    reads: tuple[str, ...] = ()            # other plugins' knobs this plugin consumes, "plugin.key"; the core
                                           # calls this plugin's apply() when one of them changes
    required: bool = True                  # a required plugin that fails boot makes boot exit 78
    core: bool = False                     # core slot: cannot be disabled, swapped or reloaded (loop.turns only)
    toggle: Literal["live", "restart"] = "restart"   # whether enable/disable takes effect without a restart
    default_enabled: bool = True
    order: int = 100                       # start order tie-break; stop is the reverse
    validate: Optional[Callable[[Mapping[str, Any]], None]] = None  # cross-field check on the full candidate
                                           # config of this plugin; raises ConfigError(message, field)
    docs: str = ""                         # repo-relative README path, optional
    tests: tuple[str, ...] = ()            # repo-relative test files that `mira check` requires to exist

class State(str, enum.Enum):
    PENDING = "pending"        # waiting for needs; inventory lists missing[]
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"          # start or rebuild failed; last_error set; loud in journal and inventory
    DISABLED = "disabled"      # enabled=false in the file
    STOPPED = "stopped"        # after shutdown

class ConfigError(ValueError):
    def __init__(self, message: str, field: str = "", code: str = "invalid"): ...
    message: str; field: str; code: str    # code in {"invalid","unknown_key","unknown_plugin","unknown_voice",
                                           #          "provider_conflict","missing_service"}

class PolicyError(PermissionError):
    def __init__(self, code: str, message: str, *, field: str = "", allowed: dict | None = None,
                 owner_command: str = ""): ...
    # code in {"policy","not_in_voice_turn","agents_locked","rate_limited","quiet_hours","session_cap"}

class ConflictError(RuntimeError):
    def __init__(self, code: str, message: str, *, current: Any = None, revision: int | None = None): ...
    # code in {"stale_revision","undo_conflict","file_changed","provider_conflict"}

class PluginConfig(Mapping[str, Any]):
    """Immutable, validated, defaults filled. cfg.target and cfg["target"]. Equality by values."""
    def __init__(self, plugin: str, values: Mapping[str, Any], revision: int = 0): ...
    plugin: str
    revision: int
    def replace(self, **changes: Any) -> "PluginConfig": ...

class PluginContext(Protocol):
    machine: str
    log: logging.Logger                    # named "mira.<plugin>"; never log transcripts, replies or tokens
    paths: "Paths"
    def provide(self, service: str, obj: Any) -> None: ...        # undone on stop
    def get(self, service: str, default: Any = ...) -> Any: ...   # KeyError when absent and no default
    def all(self, service: str) -> list[Any]: ...                 # multi services, ordered (section 2.2)
    def effect(self, release: Callable[[], None], label: str = "") -> None: ...   # released LIFO on stop
    def cfg_of(self, plugin: str) -> PluginConfig: ...            # another plugin's current snapshot; read at use time
    def now(self) -> float: ...                                   # the source clock (monotonic live, audio time for WAV)
    def on(self, event: str, fn: Callable[[dict], None]) -> None: ...   # observe-only (section 2.10)
    def emit(self, event: str, **payload: Any) -> None: ...

@dataclass(frozen=True)
class Paths:
    here: Path            # install dir of the running code (${HERE})
    config_dir: Path      # ~/.config/mira
    home: Path

class PluginBase:
    def __init__(self, ctx: PluginContext, cfg: PluginConfig) -> None:
        self.ctx, self.cfg = ctx, cfg
    def start(self) -> None: ...          # build the object(s), ctx.provide(...), acquire via ctx.effect
    def stop(self) -> None: ...           # default no-op; effects are released by the core after stop()
    def apply(self, changed: Mapping[str, Any], cfg: PluginConfig) -> None:
        """Live change. changed maps "key" (own) or "plugin.key" (a knob listed in spec.reads) to the new value.
        Set attributes on the service object here. Default: self.cfg = cfg."""
        self.cfg = cfg
    def rebuild(self, cfg: PluginConfig) -> None:
        """Replace the service object. Default: stop(); self.cfg = cfg; start(). Must free the old object
        first on the listener (dispose first)."""
        self.stop(); self.cfg = cfg; self.start()
    def status(self) -> dict:             # small, no text: counters, model loaded, last rebuild ms
        return {}
```

Rules the core and `mira check` enforce:
1. `start`, `stop`, `apply`, `rebuild` run on the loop thread (listener) or the apply worker (speech). Never on a control thread.
2. Plugin code (the spec, the PluginBase subclass, `start`/`apply`/`rebuild`) never reads `os.environ`, never holds a tunable as a module constant, and never opens a path that is not a knob value or `ctx.paths`. Protocol constants (section 1.2) are module constants named in the module docstring under `FIXED:`. Moved v1 classes keep their v1 constructor signatures and defaults, including `MicAGC`'s env read when a parameter is None (the unchanged tests construct `ml.MicAGC()` directly); each such module lists them under `LEGACY_CLASSES = (...)`, which `mira check` allow-lists. A plugin always constructs them with every value passed explicitly from its config, so the env read never runs inside the plugin engine.
3. A failing `rebuild` restores the previous snapshot by calling `rebuild(prev_cfg)`, marks the change `failed`, and writes the previous values back to the file. If that also fails, the process exits (systemd restarts it with the reverted file).
4. Default values equal the legacy code. A spec with a default that differs from the v1 constant fails `tests/test_knob_coverage.py`.
5. Every numeric knob has `min`, `max` and `unit`; every knob has `description` (at most 160 chars) and `applies`; every `bounded` numeric knob has `agent_min`, `agent_max`, `agent_step` inside the hard bounds. Agent bounds change only by a code commit the owner reviews; no API, file or knob can widen them.

### 2.2 Services (`mirakit/services.py`, normative `typing.Protocol`s)

Each Protocol describes the interface of the ORIGINAL object the plugin provides. Exclusive unless marked multi. "Required" means boot exits 78 without an active provider.

```python
from __future__ import annotations
import threading
from typing import Callable, Optional, Protocol, Sequence, TYPE_CHECKING
if TYPE_CHECKING:
    import numpy as np

MULTI_SERVICES = frozenset({"intent", "stt-backend", "tts-backend"})
# "core": the Core itself (a mirakit.plugin.CoreAPI), registered by the core before any plugin starts.
REQUIRED_LISTENER = ("source", "mute", "wake", "vad", "speech", "stt", "tts", "gate", "brain", "persona",
                     "playback", "reporter", "loop")
REQUIRED_SPEECH = ("http", "stt", "tts", "voices")

class Source(Protocol):             # "source": PwSource or WavSource, unchanged classes
    live: bool
    def read(self) -> "np.ndarray | None": ...   # float32 mono 16 kHz, exactly CHUNK samples (last WAV chunk may be shorter); None at EOF
    def clock(self) -> float: ...                # live: time.monotonic(); WAV: audio position (does not advance while busy)
    def flush(self) -> None: ...                 # drop queued audio
    def close(self) -> None: ...                 # idempotent

class Mute(Protocol):               # "mute": MuteDetector
    state: Optional[str]                          # None | "muted" | "live"
    def feed(self, chunk: "np.ndarray") -> Optional[str]: ...   # RAW chunk; "muted"|"live"|"repeat"|None

class Agc(Protocol):                # "agc": MicAGC, or None when agc.mic.on is false (the service exists, value None)
    gain: float; floor: float; speech: bool; level: float
    target: float; max_gain: float; rest: float; gate: float; release: float; decay: float
    min_rms: float; ceiling: float; floor_rate_speech: float; floor_rate_low: float; floor_rate_mid: float
    level_scale: float
    def process(self, chunk: "np.ndarray") -> "np.ndarray": ...

class Spotter(Protocol):            # "wake": the sherpa_onnx.KeywordSpotter itself
    def create_stream(self, keywords: Optional[str] = None) -> object: ...
    def is_ready(self, stream: object) -> bool: ...
    def decode_stream(self, stream: object) -> None: ...
    def get_result(self, stream: object) -> str: ...
    def reset_stream(self, stream: object) -> None: ...

class VadFactory(Protocol):         # "vad"
    config: object                                # the sherpa_onnx.VadModelConfig the next detector is built from
    def new(self) -> object: ...                  # sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=buffer_s)

class SpeechLink(Protocol):         # "speech": the Speech class (url, token per call, timeouts, offline mapping)
    url: str; token_file: str; voice: str; stt_timeout_s: float; tts_timeout_s: float
    reply_voice: str                              # voice id from the last unpinned /tts reply header, "" if none
    def stt(self, wav_bytes: bytes) -> str: ...   # raises SpeechOffline on URLError/OSError/TimeoutError (HTTPError included)
    def tts(self, text: str, voice: str = "", speed: float = 0.0, peak: float = 0.0,
            pin: bool = False) -> bytes: ...      # body {"text"} plus non-default fields only; raises SpeechOffline

class Stt(Protocol):                # "stt": provided by stt.remote; today the same Speech object
    def stt(self, wav_bytes: bytes) -> str: ...

class Tts(Protocol):                # "tts": provided by tts.remote
    def chunks(self, text: str) -> list[str]: ...           # speech_chunks(clean_for_speech(text), chunk_limit, max_chunks)
    def tts(self, text: str, pin: bool = False) -> bytes: ...   # Speech.tts with this plugin's voice/speed/peak
    def render_preview(self, text: str, voice: str, speed: float, peak: float) -> bytes: ...   # no pin, no state

class Gate(Protocol):               # "gate": gate.wake
    def has_wake(self, text: str) -> bool: ...
    def strip_wake(self, text: str) -> str: ...
    def normalize(self, text: str) -> str: ...    # " ".join(re.sub(r"[^a-z0-9' ]", "", text.lower()).split())
    def is_empty(self, norm: str) -> bool: ...    # not norm or norm in hallucinations

class Intent(Protocol):             # "intent" (multi), ordered by priority desc, then name
    priority: int
    mark: str                                     # event kind logged on a hit, "music" for intent.music
    log_name: str                                 # "music-api"
    done_reply: str                               # "Done."
    fail_reply: str                               # "I could not reach the music player."
    def match(self, text: str) -> Optional[str]: ...   # fullmatch semantics inside
    def handle(self, intent: str) -> Optional[str]: ...

class Persona(Protocol):            # "persona"
    def text(self) -> str: ...                    # reread="start": read once; "turn": mtime-cached per call

class Brain(Protocol):              # "brain": the Hermes class, unchanged
    session_id: Optional[str]; last_used: float; persona: str
    fail_reply: str; empty_reply: str
    def ask(self, text: str, session_ttl: float, now: float) -> str: ...

class Playback(Protocol):           # "playback"
    lock: threading.Lock                          # held by previews only (section 2.6)
    def call(self, *cmd: str) -> int: ...         # subprocess.run([bin, *cmd], timeout=call_timeout_s); OSError/timeout -> 1
    def say_file(self, path: str, wav: bytes, set_level: Callable[[float], None]) -> None: ...
    def envelope(self, wav: bytes) -> list[tuple[float, float]]: ...   # wav_envelope(wav, step, scale)

class Reporter(Protocol):           # "reporter": VoiceReporter, unchanged behaviour
    last: Optional[tuple[str, str, str]]
    def update(self, state: str, heard: str = "", reply: str = "") -> None: ...
    def set_level(self, v: float) -> None: ...
    def set_mic(self, mic: str) -> None: ...
    def close(self, wait: bool = True) -> None: ...

# speech machine
class SttBackend(Protocol):         # "stt-backend" (multi); one per backend plugin
    backend_ids: tuple[str, ...]                  # e.g. ("faster-whisper",), ("sherpa-transducer", "sherpa-nemo-transducer")
    def load(self, kind: str, models: "Path", threads: int, opts: dict) -> "SttModel": ...
class SttModel(Protocol):
    kind: str; lock: threading.Lock
    def transcribe(self, samples: "np.ndarray") -> str: ...
class TtsBackend(Protocol):         # "tts-backend" (multi)
    engine: str                                   # "piper" | "kokoro"
    def handles(self, model_dir: "Path") -> bool: ...
    def load(self, model_dir: "Path", threads: int, opts: dict) -> "TtsModel": ...
class TtsModel(Protocol):
    model_dir: str; rate: int; num_speakers: int; lock: threading.Lock
    def generate(self, text: str, sid: int, speed: float) -> "tuple[np.ndarray, int]": ...
class VoiceCatalog(Protocol):       # "voices"
    def list(self, lang: str = "", include_unusable: bool = False) -> list[dict]: ...
    def resolve(self, voice: str) -> dict: ...    # id or alias -> entry; raises ConfigError(code="unknown_voice") with near matches
    def default_for(self, model_dir: str) -> dict: ...
    def agent_allowed(self, entry: dict) -> bool: ...   # lang en_* and (featured or rtf <= agent_max_rtf); policy calls it
class SttEngine(Protocol):          # "stt": stt.engine
    def transcribe(self, samples: "np.ndarray") -> tuple[str, int]: ...    # (text, ms)
    kind: str
class TtsEngine(Protocol):          # "tts": tts.engine
    def synth(self, text: str, voice: str = "", speed: float = 0.0, peak: float = 0.0) -> tuple[bytes, str]: ...
                                                  # (wav bytes, resolved voice id)
    def loaded(self) -> list[dict]: ...
    voice_id: str
```

Provider rules:
1. An exclusive service may have exactly one ENABLED provider. Two enabled providers of one exclusive service is a `ConfigError(code="provider_conflict")` at boot naming both ("two providers for 'source': source.pipewire, source.wav; set enabled=false on one"). A runtime enable that would create a conflict is refused with `ConflictError("provider_conflict")`.
2. `--wav FILE` on the command line is an in-memory override: `source.wav.enabled=true`, `source.wav.path=FILE`, `source.pipewire.enabled=false`. Nothing is written.
3. Disabling a plugin that provides a required service is refused at runtime; in the file it makes boot exit 78 with the missing service named.
4. Multi services are ordered by the provider's `priority` knob (descending), then plugin name.
5. `toggle="live"` plugins (`intent.*`) start or stop at the next safe point; all others are `restart`, `control.http` included (the CLI that would enable it needs the API it starts, so enabling it is `mira enable control.http --offline` plus one restart).
