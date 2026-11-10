"""Plugin contract of Mira (spec sections 2.1 and 2.3), stdlib only.

Everything the plugins and the core share lives here so wave 2 packages can import it before
`mirakit/core.py` exists: Knob, PluginSpec, State, the three error types, PluginConfig, Paths,
PluginBase, PluginContext, and the shared names of the Core API (Scope, RESET, RESET_STREAM, Edit,
Change, CoreAPI, RemoteClient). Validation of values is the core's job (P3); the only checks here are
the ones that make a malformed Knob fail at import.
"""
from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
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

_KNOB_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_NUMERIC = ("int", "float")


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

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _KNOB_NAME.match(self.name):
            raise ValueError(f"knob name {self.name!r} must match [a-z][a-z0-9_]* (no dots, no uppercase)")
        if self.agent not in ("deny", "bounded"):
            raise ValueError(f"knob {self.name}: agent must be 'deny' or 'bounded', not {self.agent!r}")
        if self.agent == "bounded" and self.type in _NUMERIC:
            if self.agent_min is None or self.agent_max is None or self.agent_step is None:
                raise ValueError(f"knob {self.name}: a bounded numeric knob needs agent_min, agent_max and agent_step")
        if self.agent_min is not None and self.agent_max is not None and self.agent_min > self.agent_max:
            raise ValueError(f"knob {self.name}: agent_min {self.agent_min} is above agent_max {self.agent_max}")
        if self.agent_step is not None and self.agent_step <= 0:
            raise ValueError(f"knob {self.name}: agent_step must be positive")
        if self.min is not None and self.agent_min is not None and self.agent_min < self.min:
            raise ValueError(f"knob {self.name}: agent_min {self.agent_min} is below the hard min {self.min}")
        if self.max is not None and self.agent_max is not None and self.agent_max > self.max:
            raise ValueError(f"knob {self.name}: agent_max {self.agent_max} is above the hard max {self.max}")
        if self.max is not None and self.agent_min is not None and self.agent_min > self.max:
            raise ValueError(f"knob {self.name}: agent_min {self.agent_min} is above the hard max {self.max}")
        if self.min is not None and self.agent_max is not None and self.agent_max < self.min:
            raise ValueError(f"knob {self.name}: agent_max {self.agent_max} is below the hard min {self.min}")


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
    def __init__(self, message: str, field: str = "", code: str = "invalid"):
        super().__init__(message)
        self.message, self.field, self.code = message, field, code
    # code in {"invalid","unknown_key","unknown_plugin","unknown_voice",
    #          "provider_conflict","missing_service"}


class PolicyError(PermissionError):
    def __init__(self, code: str, message: str, *, field: str = "", allowed: dict | None = None,
                 owner_command: str = ""):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field
        self.allowed, self.owner_command = allowed, owner_command
    # code in {"policy","not_in_voice_turn","agents_locked","rate_limited","quiet_hours","session_cap"}


class ConflictError(RuntimeError):
    def __init__(self, code: str, message: str, *, current: Any = None, revision: int | None = None):
        super().__init__(message)
        self.code, self.message = code, message
        self.current, self.revision = current, revision
    # code in {"stale_revision","undo_conflict","file_changed","provider_conflict"}


class PluginConfig(Mapping[str, Any]):
    """Immutable, validated, defaults filled. cfg.target and cfg["target"]. Equality by values."""

    __slots__ = ("_plugin", "_revision", "_values")

    def __init__(self, plugin: str, values: Mapping[str, Any], revision: int = 0):
        object.__setattr__(self, "_plugin", plugin)
        object.__setattr__(self, "_revision", revision)
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    @property
    def plugin(self) -> str:
        return self._plugin

    @property
    def revision(self) -> int:
        return self._revision

    def replace(self, **changes: Any) -> "PluginConfig":
        """A new config with some keys changed. An unknown key is a KeyError: a typo must not pass silently."""
        unknown = [k for k in changes if k not in self._values]
        if unknown:
            raise KeyError(f"{self._plugin}: unknown config key {unknown[0]!r}")
        merged = dict(self._values)
        merged.update(changes)
        return PluginConfig(self._plugin, merged, self._revision)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        # only reached when normal lookup fails, so slots and methods win over knob names
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(f"{self._plugin} has no config key {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("PluginConfig is immutable; use replace()")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("PluginConfig is immutable")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PluginConfig):
            return dict(self._values) == dict(other._values)
        if isinstance(other, Mapping):
            return dict(self._values) == dict(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self):
        # slots plus an immutable __setattr__ rule out the default copy and pickle protocol
        return (PluginConfig, (self._plugin, dict(self._values), self._revision))

    def __repr__(self) -> str:
        return f"PluginConfig({self._plugin!r}, {dict(self._values)!r}, revision={self._revision})"


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

    def start(self) -> None:
        """Build the object(s), ctx.provide(...), acquire via ctx.effect."""

    def stop(self) -> None:
        """Default no-op; effects are released by the core after stop()."""

    def apply(self, changed: Mapping[str, Any], cfg: PluginConfig) -> None:
        """Live change. changed maps "key" (own) or "plugin.key" (a knob listed in spec.reads) to the new value.
        Set attributes on the service object here. Default: self.cfg = cfg."""
        self.cfg = cfg

    def rebuild(self, cfg: PluginConfig) -> None:
        """Replace the service object. Default: stop(); self.cfg = cfg; start(). Must free the old object
        first on the listener (dispose first)."""
        self.stop()
        self.cfg = cfg
        self.start()

    def status(self) -> dict:             # small, no text: counters, model loaded, last rebuild ms
        return {}


# ---------------------------------------------------------------------------------------------
# Shared names of the Core API (spec section 2.3). `Core` itself is mirakit/core.py (P3).
# ---------------------------------------------------------------------------------------------

Scope = Literal["owner", "agent", "system"]


class _Reset:
    """Sentinel type behind RESET; one instance, survives copy, deepcopy and pickle."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "RESET"

    def __copy__(self) -> "_Reset":
        return self

    def __deepcopy__(self, memo: dict) -> "_Reset":
        return self

    def __reduce__(self) -> str:
        return "RESET"


RESET = _Reset()                          # Edit.value sentinel: reset the key to its schema default
RESET_STREAM = 1                          # safe_point() flag: the loop must create a new spotter stream and clear pre-roll


@dataclass(frozen=True)
class Edit:
    key: str                              # "plugin.key", e.g. "agc.mic.target", "core.engine", "loop.turns.followup_s"
    value: Any


@dataclass
class Change:                             # wire form in section 2.5.1
    id: str; ts: str; machine: str; actor: str; scope: Scope; session: Optional[str]; reason: str
    edits: list[dict]                     # [{"key", "old", "new"}]
    applies: str                          # strongest class among the edits: live < rebuild < restart
    status: str                           # queued | applied | pending_restart | failed | reverted | confirmed
    rev_before: int; rev_after: int
    trial_expires: Optional[str]; undo_of: Optional[str]; error: Optional[str]; announce: str

    def to_dict(self) -> dict:
        """The 2.5.1 wire form (also the changes.jsonl record). Edits are copied, key order is fixed."""
        return {
            "id": self.id, "ts": self.ts, "machine": self.machine, "actor": self.actor,
            "scope": self.scope, "session": self.session, "reason": self.reason,
            "edits": [dict(e) for e in self.edits],
            "applies": self.applies, "status": self.status,
            "rev_before": self.rev_before, "rev_after": self.rev_after,
            "trial_expires": self.trial_expires, "undo_of": self.undo_of,
            "error": self.error, "announce": self.announce,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Change":
        """Inverse of to_dict. Optional fields may be missing; unknown keys are ignored (forward compatible)."""
        return cls(
            id=d["id"], ts=d["ts"], machine=d["machine"], actor=d["actor"], scope=d["scope"],
            session=d.get("session"), reason=d.get("reason", ""),
            edits=[dict(e) for e in d.get("edits", [])],
            applies=d["applies"], status=d["status"],
            rev_before=d.get("rev_before", 0), rev_after=d.get("rev_after", 0),
            trial_expires=d.get("trial_expires"), undo_of=d.get("undo_of"),
            error=d.get("error"), announce=d.get("announce", ""),
        )


class CoreAPI(Protocol):                  # what control.py and the CLI may call (any thread). Lives in mirakit/plugin.py.
    machine: str
    def status(self) -> dict: ...
    def inventory(self) -> list[dict]: ...
    def describe(self, plugin: str, *, scope: Scope) -> dict: ...
    def schema(self) -> dict: ...
    def config(self, *, effective: bool = True) -> dict: ...
    def tuning_guide(self, topic: str, *, scope: Scope) -> dict: ...
    def submit(self, edits: Sequence[Edit], *, actor: str, scope: Scope, reason: str = "",
               trial_s: Optional[float] = None, expect_revision: Optional[int] = None,
               dry_run: bool = False, wait_s: float = 0.0, session: Optional[str] = None) -> Change: ...
    def undo(self, change_id: Optional[str] = None, *, actor: str, scope: Scope,
             session: Optional[str] = None, force: bool = False) -> list[Change]: ...
    def confirm(self, change_id: Optional[str] = None, *, actor: str, scope: Scope,
                session: Optional[str] = None) -> list[Change]: ...
    def changes(self, *, limit: int = 20, since: Optional[str] = None, session: Optional[str] = None,
                actor: Optional[str] = None) -> list[dict]: ...
    def session(self, action: Literal["start", "get", "keep", "revert", "end"], *, actor: str, scope: Scope,
                topic: str = "") -> Optional[dict]: ...
    def preview(self, *, actor: str, scope: Scope, text: str = "", voice: str = "", speed: float = 0.0,
                peak: float = 0.0, wait_s: float = 15.0) -> dict: ...
    def set_enabled(self, plugin: str, enabled: bool, *, actor: str, scope: Scope) -> Change: ...
    def reload_plugin(self, plugin: str, *, actor: str, scope: Scope) -> Change: ...
    def reload_file(self, *, actor: str, scope: Scope) -> Change: ...
    def request_restart(self, *, actor: str, scope: Scope) -> dict: ...
    def register_remote(self, machine: str, client: "RemoteClient") -> None: ...
    def attach_remote_change(self, change: dict) -> None: ...   # a change made on the mini inside the open session


class RemoteClient(Protocol):             # implemented by control.py (HTTP to the mini /ctl/v1)
    def request(self, method: str, path: str, body: Optional[dict] = None) -> dict: ...   # raises RemoteError
