"""Test kit for plugin packages (spec sections 2.1, 2.3 and 6.4).

cfg_for, FakeCtx, FakeSource, FakeSpotter, FakeCore and run_plugin_contract. Stdlib only, except that
FakeSource builds numpy arrays lazily (numpy is only needed when a test actually feeds audio).

FakeCore is a TEST DOUBLE, not the real policy engine: it is in-memory, deterministic and simple. It
mirrors the shape of the rules in spec 2.5.5 closely enough for plugin, control-API and Hermes tests, but
it does not persist, apply to plugins, rate-limit by time window or talk to a second machine. The real
engine is mirakit/core.py (P3).
"""
from __future__ import annotations

import difflib
import json
import logging
import math
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Optional, Sequence

from .plugin import (RESET, Change, ConfigError, ConflictError, CoreAPI, Edit, Knob, Paths, PluginBase,
                     PluginConfig, PluginContext, PluginSpec, PolicyError, Scope)
from .services import MULTI_SERVICES

_MISSING = object()
_STRENGTH = {"live": 0, "rebuild": 1, "restart": 2}
_TOPICS = ("", "voice", "hearing", "timing", "wake", "brain", "wall", "system")


def cfg_for(spec: PluginSpec, revision: int = 0, **overrides: Any) -> PluginConfig:
    """Spec defaults plus overrides. An unknown key is a KeyError; nothing else is validated here."""
    values = {k.name: k.default for k in spec.knobs}
    for key, value in overrides.items():
        if key not in values:
            raise KeyError(f"{spec.name}: unknown knob {key!r}")
        values[key] = value
    return PluginConfig(spec.name, values, revision)


# --------------------------------------------------------------------------------------------
# FakeCtx
# --------------------------------------------------------------------------------------------

class FakeCtx:
    """In-memory PluginContext. Records what a plugin did: provided services, effects, events, logs."""

    def __init__(self, machine: str = "listener", cfgs: Optional[Mapping[str, PluginConfig]] = None, *,
                 time_s: float = 0.0, log_name: str = "mira.test") -> None:
        self.machine = machine
        self.log = logging.getLogger(log_name)
        self._tmp = tempfile.TemporaryDirectory(prefix="mirakit-ctx-")
        root = Path(self._tmp.name)
        self.paths = Paths(here=root / "here", config_dir=root / "config", home=root / "home")
        for p in (self.paths.here, self.paths.config_dir, self.paths.home):
            p.mkdir(parents=True, exist_ok=True)
        self.cfgs: dict[str, PluginConfig] = dict(cfgs or {})
        self.time = time_s
        self.emitted: list[dict] = []
        self.handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._services: dict[str, Any] = {}
        self._multi: dict[str, list[Any]] = {}
        self._provided: list[tuple[str, Any]] = []
        self._effects: list[tuple[Callable[[], None], str]] = []

    @classmethod
    def with_specs(cls, *specs: PluginSpec, machine: Optional[str] = None) -> "FakeCtx":
        """A context whose cfg_of knows the default config of every given spec."""
        ctx = cls(machine or (specs[0].machine if specs else "listener"))
        for spec in specs:
            ctx.cfgs[spec.name] = cfg_for(spec)
        return ctx

    # service registry
    def provide(self, service: str, obj: Any) -> None:
        if service in MULTI_SERVICES:
            self._multi.setdefault(service, []).append(obj)
        else:
            self._services[service] = obj
        self._provided.append((service, obj))

    def has(self, service: str) -> bool:
        return service in self._services or bool(self._multi.get(service))

    def get(self, service: str, default: Any = _MISSING) -> Any:
        if service in self._services:
            return self._services[service]
        providers = self.all(service)
        if providers:
            return providers[0]
        if default is _MISSING:
            raise KeyError(service)
        return default

    def all(self, service: str) -> list[Any]:
        if service in MULTI_SERVICES:
            items = list(self._multi.get(service, []))
        elif service in self._services:
            items = [self._services[service]]
        else:
            items = []
        # priority descending, then name ascending; both read from the provider object
        return sorted(items, key=lambda o: (-float(getattr(o, "priority", 0)),
                                            str(getattr(o, "name", type(o).__name__))))

    def effect(self, release: Callable[[], None], label: str = "") -> None:
        self._effects.append((release, label))

    def cfg_of(self, plugin: str) -> PluginConfig:
        return self.cfgs[plugin]

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> float:
        self.time += seconds
        return self.time

    def on(self, event: str, fn: Callable[[dict], None]) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def emit(self, event: str, **payload: Any) -> None:
        record = {"event": event, "at": self.time, **payload}
        self.emitted.append(record)
        for fn in list(self.handlers.get(event, [])):
            try:
                fn(dict(record))
            except Exception:  # an observer must never break the emitter (spec 2.10)
                self.log.exception("observer of %s failed", event)

    def events(self, event: str) -> list[dict]:
        return [e for e in self.emitted if e["event"] == event]

    @property
    def effect_labels(self) -> list[str]:
        return [label for _, label in self._effects]

    def release_all(self) -> None:
        """Run the registered effects LIFO, then forget what this context provided (undone on stop)."""
        first: Optional[BaseException] = None
        while self._effects:
            release, label = self._effects.pop()
            try:
                release()
            except Exception as exc:  # keep releasing, report the first failure afterwards
                self.log.exception("effect %r failed to release", label)
                first = first or exc
        for service, obj in reversed(self._provided):
            if service in MULTI_SERVICES:
                if obj in self._multi.get(service, []):
                    self._multi[service].remove(obj)
            elif self._services.get(service) is obj:
                del self._services[service]
        self._provided.clear()
        if first is not None:
            raise first

    def cleanup(self) -> None:
        self._tmp.cleanup()


# --------------------------------------------------------------------------------------------
# FakeSource, FakeSpotter
# --------------------------------------------------------------------------------------------

class FakeSource:
    """Source over a list of chunks (numpy float32 arrays or any object with len()). Not live."""
    live = False
    RATE = 16000

    def __init__(self, chunks: Iterable[Any] = ()) -> None:
        self._chunks = list(chunks)
        self._pos = 0
        self.consumed = 0
        self.flush_calls = 0
        self.close_calls = 0

    @classmethod
    def silence(cls, n_chunks: int, chunk: int = 1600) -> "FakeSource":
        import numpy as np
        return cls([np.zeros(chunk, dtype=np.float32) for _ in range(n_chunks)])

    def read(self) -> Any:
        if self._pos >= len(self._chunks):
            return None
        chunk = self._chunks[self._pos]
        self._pos += 1
        self.consumed += len(chunk)
        return chunk

    def clock(self) -> float:
        return self.consumed / self.RATE

    def flush(self) -> None:
        self.flush_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FakeStream:
    """Stream handle of FakeSpotter; accept_waveform mirrors the sherpa_onnx call."""

    def __init__(self) -> None:
        self.fed = 0
        self.pending = False
        self.result = ""

    def accept_waveform(self, sample_rate: int, samples: Any) -> None:
        self.fed += 1
        self.pending = True


class FakeSpotter:
    """Spotter that reports a hit at chosen chunk indices (0-based count of accept_waveform calls)."""

    def __init__(self, hits: Iterable[int] | Mapping[int, str] = (), text: str = "mira") -> None:
        self.hits: dict[int, str] = dict(hits) if isinstance(hits, Mapping) else {i: text for i in hits}
        self.streams: list[FakeStream] = []

    @property
    def streams_created(self) -> int:
        return len(self.streams)

    def create_stream(self, keywords: Optional[str] = None) -> FakeStream:
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def is_ready(self, stream: FakeStream) -> bool:
        return stream.pending

    def decode_stream(self, stream: FakeStream) -> None:
        stream.pending = False
        stream.result = self.hits.get(stream.fed - 1, "")

    def get_result(self, stream: FakeStream) -> str:
        return stream.result

    def reset_stream(self, stream: FakeStream) -> None:
        stream.result = ""


# --------------------------------------------------------------------------------------------
# FakeCore
# --------------------------------------------------------------------------------------------

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def _fmt(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _knob_json(k: Knob) -> dict:
    d = {f: getattr(k, f) for f in k.__dataclass_fields__}
    d["choices"] = list(k.choices) if k.choices is not None else None
    d["phrases"] = list(k.phrases)
    return d


_WINDOW = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d")


def _coerce(knob: Knob, value: Any, key: str) -> Any:
    t = knob.type
    if value is None:
        if knob.nullable:
            return None
        raise ConfigError(f"{key} needs a value", key)
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
            return value.strip().lower() in _TRUE
        raise ConfigError(f"{key} must be true or false", key)
    if t in ("int", "float"):
        if isinstance(value, bool):
            raise ConfigError(f"{key} must be a number", key)
        try:
            num = float(value.strip()) if isinstance(value, str) else float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be a number", key) from None
        if not math.isfinite(num):
            raise ConfigError(f"{key} must be a finite number", key)
        if t == "float":
            return num
        if not num.is_integer():
            raise ConfigError(f"{key} must be a whole number", key)
        return int(num)
    if t == "enum":
        if not isinstance(value, str) or value not in (knob.choices or ()):
            raise ConfigError(f"{key} must be one of {', '.join(knob.choices or ())}", key)
        return value
    if t == "list[str]":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ConfigError(f"{key} must be a JSON list of strings", key) from None
        if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
            raise ConfigError(f"{key} must be a list of strings", key)
        return list(value)
    if t == "map[str,str]":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ConfigError(f"{key} must be a JSON object of strings", key) from None
        if not isinstance(value, Mapping) or not all(isinstance(k, str) and isinstance(v, str)
                                                     for k, v in value.items()):
            raise ConfigError(f"{key} must be an object of strings", key)
        return dict(value)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be text", key)
    # 2.5.1 step 3: regex compile, http(s) only for url, HH:MM-HH:MM or "" for time_window
    if t == "regex":
        try:
            re.compile(value)
        except re.error:
            raise ConfigError(f"{key} is not a valid pattern", key) from None
    elif t == "url" and value and not re.match(r"https?://[^/\s]+", value):
        raise ConfigError(f"{key} must be an http or https address", key)
    elif t == "time_window" and value and not _WINDOW.fullmatch(value):
        raise ConfigError(f"{key} must look like 23:00-06:00, or be empty", key)
    return value


def _check_bounds(knob: Knob, value: Any, key: str) -> None:
    if value is None:
        return
    if knob.type in ("int", "float"):
        measure = value
    elif knob.type in ("str", "list[str]"):
        measure = len(value)
    else:
        return
    if knob.min is not None and measure < knob.min:
        raise ConfigError(f"{key} is below its minimum of {_fmt(knob.min)}", key)
    if knob.max is not None and measure > knob.max:
        raise ConfigError(f"{key} is above its maximum of {_fmt(knob.max)}", key)


class FakeCore(CoreAPI):
    """In-memory CoreAPI built from PluginSpecs. A test double, not the real policy engine.

    Knobs you can set on the instance: `voice_turn` (True: the brain phase of a voice turn is open),
    `agents_locked`, `voice_allowed` (callable for agent voice values, default allows all),
    `agent_write_limit` and `session_max_changes` (plain counters, no time window), `clock` (epoch seconds,
    moved with advance()). Values live in `values` as "plugin.key"; `enabled` maps plugin to bool.

    Modelled: unknown key, coercion (bool, int, float, str, list[str], map[str,str], enum), hard bounds,
    the agent scope (bounded knobs only, agent bounds, step, forced trial, voice turn, lock, counters),
    batches as one Change, undo with conflict detection, confirm, one voice session, previews (recorded),
    tuning guide, schema, describe, inventory. Not modelled: persistence, plugin apply, rebuild queues,
    quiet hours, time-window rate limits, the speech proxy. Change ids are L-000001 (S- on the speech machine).
    """

    def __init__(self, specs: Sequence[PluginSpec], values: Optional[Mapping[str, Any]] = None, *,
                 machine: str = "listener", version: str = "test", clock: float = 1_700_000_000.0) -> None:
        self.machine = machine
        self.version = version
        self.specs: dict[str, PluginSpec] = {s.name: s for s in specs}
        self._knobs: dict[str, tuple[PluginSpec, Knob]] = {}
        self.values: dict[str, Any] = {}
        for spec in specs:
            for knob in spec.knobs:
                self._knobs[f"{spec.name}.{knob.name}"] = (spec, knob)
                self.values[f"{spec.name}.{knob.name}"] = knob.default
        for key, value in (values or {}).items():
            if key not in self._knobs:
                raise KeyError(f"unknown knob {key!r}")
            self.values[key] = value
        self.enabled: dict[str, bool] = {s.name: s.default_enabled for s in specs}
        self.revision = 1
        self.voice_turn = True
        self.agents_locked = False
        self.voice_allowed: Optional[Callable[[Any], bool]] = None
        self.agent_write_limit = 12
        self.session_max_changes = 12
        self.agent_undo_window_s = 86400.0
        self.trial_default_s, self.trial_max_s = 600.0, 1800.0
        self.session_max_s, self.session_followup_s = 900.0, 15.0
        self.clock = clock
        self.pending_restart: set[str] = set()
        self.previews: list[dict] = []
        self.remotes: dict[str, Any] = {}
        self.remote_changes: list[dict] = []
        self.restart_requested = False
        self.reloads: list[str] = []
        self._changes: list[Change] = []
        self._counter = 0
        self._agent_writes = 0
        self._session: Optional[dict] = None
        self._session_counter = 0
        self._prefix = "L" if machine == "listener" else "S"

    # ---- helpers
    def advance(self, seconds: float) -> float:
        self.clock += seconds
        return self.clock

    def _ts(self, offset: float = 0.0) -> str:
        return datetime.fromtimestamp(self.clock + offset, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")

    def _next_id(self, consume: bool = True) -> str:
        n = self._counter + 1
        if consume:
            self._counter = n
        return f"{self._prefix}-{n:06d}"

    def _lookup(self, key: str) -> tuple[PluginSpec, Knob]:
        if key in self._knobs:
            return self._knobs[key]
        plugin, _, name = key.rpartition(".")
        if plugin and plugin not in self.specs:
            near = difflib.get_close_matches(plugin, list(self.specs), n=1)
            hint = f"; closest plugin is {near[0]}" if near else ""
            raise ConfigError(f"unknown plugin {plugin}{hint}", key, "unknown_plugin")
        near = difflib.get_close_matches(key, list(self._knobs), n=1)
        hint = f"; closest key is {near[0]}" if near else ""
        raise ConfigError(f"unknown key {key}{hint}", key, "unknown_key")

    @staticmethod
    def _spoken(knob: Knob) -> str:
        return knob.spoken or knob.name.replace("_", " ")

    def _source(self, key: str) -> str:
        return "default" if self.values[key] == self._knobs[key][1].default else "file"

    def _current_turn(self) -> Optional[str]:
        return "T-000001" if self.voice_turn else None

    # ---- reads
    def status(self) -> dict:
        return {
            "ok": True, "machine": self.machine, "engine": "plugins", "version": self.version,
            "revision": self.revision, "uptime_s": 0.0,
            "loop": "turn" if self.voice_turn else "armed",
            "turn": {"id": "T-000001", "phase": "brain", "since_s": 0.0} if self.voice_turn else None,
            "mic": None, "failed": [], "pending_restart": sorted(self.pending_restart),
            "session": dict(self._session) if self._session else None,
            "agents_locked": self.agents_locked, "config_error": None,
            "speech": {"reachable": True, "engine": "plugins", "stt": "", "voice": ""},
        }

    def inventory(self) -> list[dict]:
        rows = []
        for spec in self.specs.values():
            on = self.enabled[spec.name]
            rows.append({
                "name": spec.name, "kind": spec.kind, "version": spec.version, "machine": spec.machine,
                "state": "active" if on else "disabled", "enabled": on, "required": spec.required,
                "core": spec.core, "provides": list(spec.provides), "needs": list(spec.needs),
                "missing": [], "toggle": spec.toggle, "rev": self.revision,
                "pending_restart": sorted(k.split(".")[-1] for k in self.pending_restart
                                          if k.rpartition(".")[0] == spec.name),
                "last_error": None, "status": {},
            })
        return rows

    def _knob_row(self, spec: PluginSpec, knob: Knob, scope: Scope) -> dict:
        key = f"{spec.name}.{knob.name}"
        value, default = self.values[key], knob.default
        lo, hi = knob.min, knob.max
        if scope == "agent" and knob.agent == "bounded" and knob.type in ("int", "float"):
            lo, hi = knob.agent_min, knob.agent_max
        if knob.secret:
            value, default = {"set": bool(value)}, {"set": bool(default)}
        return {
            "key": knob.name, "type": knob.type, "value": value, "default": default,
            "source": self._source(key), "min": lo, "max": hi, "unit": knob.unit, "applies": knob.applies,
            "agent": knob.agent, "agent_min": knob.agent_min, "agent_max": knob.agent_max,
            "agent_step": knob.agent_step, "settable_by_voice": knob.agent == "bounded",
            "description": knob.description, "spoken": self._spoken(knob), "effect": knob.effect,
            "topic": knob.topic, "pending": "restart" if key in self.pending_restart else None,
            "secret": knob.secret,
        }

    def describe(self, plugin: str, *, scope: Scope) -> dict:
        if plugin not in self.specs:
            near = difflib.get_close_matches(plugin, list(self.specs), n=1)
            hint = f"; closest plugin is {near[0]}" if near else ""
            raise ConfigError(f"unknown plugin {plugin}{hint}", plugin, "unknown_plugin")
        spec = self.specs[plugin]
        return {
            "spec": {"name": spec.name, "kind": spec.kind, "version": spec.version, "summary": spec.summary,
                     "machine": spec.machine, "provides": list(spec.provides), "needs": list(spec.needs),
                     "wants": list(spec.wants), "reads": list(spec.reads), "required": spec.required,
                     "core": spec.core, "toggle": spec.toggle, "enabled": self.enabled[plugin]},
            "knobs": [self._knob_row(spec, k, scope) for k in spec.knobs],
        }

    def schema(self) -> dict:
        return {
            "machine": self.machine, "contract_version": 1,
            "plugins": [{"name": s.name, "kind": s.kind, "version": s.version, "summary": s.summary,
                         "provides": list(s.provides), "needs": list(s.needs),
                         "knobs": [_knob_json(k) for k in s.knobs]} for s in self.specs.values()],
        }

    def config(self, *, effective: bool = True) -> dict:
        """effective=True: each key is {"value", "source"}; False: the bare values. Secrets are {"set": bool}.
        A spec named "core" fills the "core" object; every other plugin goes under "plugins"."""
        out: dict[str, Any] = {"revision": self.revision, "core": {}, "plugins": {}}
        for spec in self.specs.values():
            body: dict[str, Any] = {}
            for knob in spec.knobs:
                key = f"{spec.name}.{knob.name}"
                value = {"set": bool(self.values[key])} if knob.secret else self.values[key]
                body[knob.name] = {"value": value, "source": self._source(key)} if effective else value
            if spec.name == "core":
                out["core"] = body
            else:
                out["plugins"][spec.name] = {"enabled": self.enabled[spec.name], "config": body}
        return out

    def tuning_guide(self, topic: str, *, scope: Scope) -> dict:
        if topic not in _TOPICS:
            raise ConfigError(f"unknown topic {topic}", "topic")
        knobs, owner_only = [], []
        for key, (spec, knob) in self._knobs.items():
            if knob.topic != topic:
                continue
            value = self.values[key]
            if knob.agent == "bounded":
                numeric = knob.type in ("int", "float")
                ladder: list[Any] = []
                if numeric:
                    steps = int(round((knob.agent_max - knob.agent_min) / knob.agent_step))
                    ladder = [round(knob.agent_min + i * knob.agent_step, 6) for i in range(steps + 1)]
                    if knob.type == "int":
                        ladder = [int(v) for v in ladder]
                knobs.append({
                    "key": key, "spoken": self._spoken(knob), "value": value, "unit": knob.unit,
                    "settable_by_voice": True,
                    "agent_min": knob.agent_min, "agent_max": knob.agent_max, "agent_step": knob.agent_step,
                    "ladder": ladder, "effect": knob.effect, "phrases": list(knob.phrases),
                    "applies": knob.applies,
                })
            else:
                owner_only.append({
                    "key": key, "spoken": self._spoken(knob),
                    "why": "Only the owner may change this setting.",
                    "owner_command": f"mira set {key}={_fmt(value)} --trial 600",
                })
        return {"topic": topic, "knobs": knobs, "owner_only": owner_only}

    # ---- the write path
    def _policy(self, knob: Knob, key: str, old: Any, new: Any) -> None:
        spoken = self._spoken(knob)
        if self.agents_locked:
            raise PolicyError("agents_locked", "Settings changes are locked right now.", field=key)
        if knob.agent != "bounded":
            raise PolicyError("policy", "I'm not allowed to change that one by voice.", field=key,
                              owner_command=f"mira set {key}={_fmt(new)} --trial 600")
        if not self.voice_turn:
            raise PolicyError("not_in_voice_turn",
                              "I can only change my settings while we're talking on the wall.", field=key)
        if knob.type in ("int", "float"):
            allowed = {"min": knob.agent_min, "max": knob.agent_max, "step": knob.agent_step}
            if new < knob.agent_min or new > knob.agent_max:
                raise PolicyError("policy", f"By voice I can only set {spoken} between "
                                  f"{_fmt(knob.agent_min)} and {_fmt(knob.agent_max)}.",
                                  field=key, allowed=allowed)
            if abs(new - old) > knob.agent_step + 1e-9:
                raise PolicyError("policy", f"I can only move {spoken} by {_fmt(knob.agent_step)} at a time.",
                                  field=key, allowed=allowed)
        elif knob.type == "voice" and self.voice_allowed is not None and not self.voice_allowed(new):
            raise PolicyError("policy", "I'm not allowed to use that voice by voice command.", field=key)

    def _announce(self, edits: list[dict], trial_s: Optional[float]) -> str:
        parts = []
        for e in edits:
            spec_knob = self._knobs.get(e["key"])
            if spec_knob is None:
                parts.append(f"{e['key']} to {_fmt(e['new'])}")
                continue
            knob = spec_knob[1]
            u = f" {knob.unit}" if knob.unit and knob.type in ("int", "float") else ""
            parts.append(f"{self._spoken(knob)} to {_fmt(e['new'])}{u}, was {_fmt(e['old'])}{u}")
        text = "I changed " + " and ".join(parts)
        if trial_s:
            text += f", for {max(1, round(trial_s / 60))} minutes"
        return text + "."

    def _session_touch(self) -> None:
        if self._session:
            self._session["last_activity"] = self._ts()

    def _open_session(self, topic: str) -> dict:
        self._session_counter += 1
        self._session = {
            "id": f"V-{self._session_counter:04d}", "state": "open", "topic": topic,
            "started": self._ts(), "expires": self._ts(self.session_max_s), "last_activity": self._ts(),
            "changes": [], "followup_s": self.session_followup_s, "turn": self._current_turn(),
        }
        return self._session

    def submit(self, edits: Sequence[Edit], *, actor: str, scope: Scope, reason: str = "",
               trial_s: Optional[float] = None, expect_revision: Optional[int] = None,
               dry_run: bool = False, wait_s: float = 0.0, session: Optional[str] = None) -> Change:
        planned: list[dict] = []
        applies = "live"
        for edit in edits:
            spec, knob = self._lookup(edit.key)
            old = self.values[edit.key]
            raw = knob.default if edit.value is RESET else edit.value
            new = _coerce(knob, raw, edit.key)
            if new is not None and knob.type != "voice":
                _check_bounds(knob, new, edit.key)
            planned.append({"key": edit.key, "old": old, "new": new})
            if _STRENGTH[knob.applies] > _STRENGTH[applies]:
                applies = knob.applies
        for spec_name in {e["key"].rpartition(".")[0] for e in planned}:
            spec = self.specs[spec_name]
            if spec.validate is not None:
                candidate = {k.name: self.values[f"{spec_name}.{k.name}"] for k in spec.knobs}
                candidate.update({e["key"].rpartition(".")[2]: e["new"] for e in planned
                                  if e["key"].rpartition(".")[0] == spec_name})
                spec.validate(candidate)
        if scope == "agent":
            for e in planned:
                self._policy(self._knobs[e["key"]][1], e["key"], e["old"], e["new"])
            if self._agent_writes >= self.agent_write_limit:
                raise PolicyError("rate_limited", "That's enough changes for now; try again in a few minutes.")
            if self._session and len(self._session["changes"]) >= self.session_max_changes:
                raise PolicyError("session_cap", "That's the most changes I may make in one session.")
            trial_s = min(max(trial_s or self.trial_default_s, 60.0), self.trial_max_s)
        if expect_revision is not None and expect_revision != self.revision:
            raise ConflictError("stale_revision", "The settings changed since you looked.",
                                current=self.revision, revision=self.revision)
        status = "pending_restart" if applies == "restart" else "applied"
        if dry_run:
            status = "dry_run"
        sess_id: Optional[str] = None
        if not dry_run and scope == "agent":
            if self._session is None:
                topic = self._knobs[planned[0]["key"]][1].topic if planned else ""
                self._open_session(topic)
            sess_id = self._session["id"]
        elif session and self._session and self._session["id"] == session:
            sess_id = session
        rev_before = self.revision
        change = Change(
            id=self._next_id(consume=not dry_run), ts=self._ts(), machine=self.machine, actor=actor,
            scope=scope, session=sess_id, reason=reason[:120], edits=planned, applies=applies,
            status=status, rev_before=rev_before,
            rev_after=rev_before if dry_run else rev_before + 1,
            trial_expires=self._ts(trial_s) if trial_s and not dry_run else None,
            undo_of=None, error=None, announce=self._announce(planned, trial_s),
        )
        if dry_run:
            return change
        for e in planned:
            self.values[e["key"]] = e["new"]
            if applies == "restart" and _STRENGTH[self._knobs[e["key"]][1].applies] == 2:
                self.pending_restart.add(e["key"])
        self.revision += 1
        if scope == "agent":
            self._agent_writes += 1
        if sess_id and self._session:
            self._session["changes"].append(change.id)
            self._session_touch()
        self._changes.append(change)
        return change

    def _find(self, change_id: str) -> Change:
        for c in self._changes:
            if c.id == change_id:
                return c
        raise ConfigError(f"unknown change {change_id}", "change_id")

    def _targets(self, change_id: Optional[str], session: Optional[str], scope: Scope) -> list[Change]:
        if session is not None:
            return [c for c in reversed(self._changes)
                    if c.session == session and c.status not in ("reverted", "confirmed")]
        if change_id:
            return [self._find(change_id)]
        for c in reversed(self._changes):
            if c.scope == scope and c.status not in ("reverted", "confirmed") and c.undo_of is None:
                return [c]
        return []

    def _agent_may_touch(self, c: Change) -> None:
        if c.scope != "agent" or self._age(c) > self.agent_undo_window_s:
            raise PolicyError("policy", "I'm not allowed to undo or confirm that change by voice.",
                              field=c.id)

    def _age(self, c: Change) -> float:
        return self.clock - datetime.strptime(c.ts, "%Y-%m-%dT%H:%M:%S%z").timestamp()

    def undo(self, change_id: Optional[str] = None, *, actor: str, scope: Scope,
             session: Optional[str] = None, force: bool = False) -> list[Change]:
        if force and scope == "agent":
            raise PolicyError("policy", "Only the owner may force an undo.")
        out: list[Change] = []
        for target in self._targets(change_id, session, scope):
            if scope == "agent":
                self._agent_may_touch(target)
            if target.status in ("reverted", "confirmed"):
                raise ConflictError("undo_conflict", f"{target.id} is already {target.status}.",
                                    current=target.status)
            for e in target.edits:
                if not force and self.values[e["key"]] != e["new"]:
                    raise ConflictError("undo_conflict", "That setting has moved since this change.",
                                        current=self.values[e["key"]], revision=self.revision)
            edits = [{"key": e["key"], "old": e["new"], "new": e["old"]} for e in reversed(target.edits)]
            applies = target.applies
            rev_before = self.revision
            undo = Change(
                id=self._next_id(), ts=self._ts(), machine=self.machine, actor=actor, scope=scope,
                session=target.session, reason="", edits=edits, applies=applies,
                status="pending_restart" if applies == "restart" else "applied",
                rev_before=rev_before, rev_after=rev_before + 1, trial_expires=None,
                undo_of=target.id, error=None, announce=self._announce(edits, None),
            )
            for e in edits:
                self.values[e["key"]] = e["new"]
                self.pending_restart.discard(e["key"])
            self.revision += 1
            target.status, target.trial_expires = "reverted", None
            self._changes.append(undo)
            out.append(undo)
        return out

    def confirm(self, change_id: Optional[str] = None, *, actor: str, scope: Scope,
                session: Optional[str] = None) -> list[Change]:
        if scope == "agent":
            if self.agents_locked:
                raise PolicyError("agents_locked", "Settings changes are locked right now.")
            if not self.voice_turn:
                raise PolicyError("not_in_voice_turn",
                                  "I can only change my settings while we're talking on the wall.")
        out: list[Change] = []
        for target in self._targets(change_id, session, scope):
            if scope == "agent":
                self._agent_may_touch(target)
            target.status, target.trial_expires = "confirmed", None
            out.append(target)
        return out

    def expire_due(self) -> list[Change]:
        """Test helper: revert every trial whose expiry has passed (actor "expiry", scope "system")."""
        done: list[Change] = []
        for c in list(self._changes):
            if c.trial_expires and c.status in ("applied", "pending_restart"):
                due = datetime.strptime(c.trial_expires, "%Y-%m-%dT%H:%M:%S%z").timestamp() <= self.clock
                in_session = self._session is not None and c.session == self._session["id"]
                if due and not in_session:
                    done.extend(self.undo(c.id, actor="expiry", scope="system", force=True))
        return done

    def changes(self, *, limit: int = 20, since: Optional[str] = None, session: Optional[str] = None,
                actor: Optional[str] = None) -> list[dict]:
        rows = list(self._changes)
        if since is not None:
            rows = [c for c in rows if c.id > since]
        if session is not None:
            rows = [c for c in rows if c.session == session]
        if actor is not None:
            rows = [c for c in rows if c.actor == actor]
        return [c.to_dict() for c in reversed(rows)][:limit]

    def session(self, action: Literal["start", "get", "keep", "revert", "end"], *, actor: str, scope: Scope,
                topic: str = "") -> Optional[dict]:
        if action == "get":
            return dict(self._session) if self._session else None
        if scope == "agent" and action in ("start", "keep"):
            if self.agents_locked:
                raise PolicyError("agents_locked", "Settings changes are locked right now.")
            if not self.voice_turn:
                raise PolicyError("not_in_voice_turn",
                                  "I can only change my settings while we're talking on the wall.")
        if action == "start":
            return dict(self._session or self._open_session(topic))
        if self._session is None:
            return None
        sess = self._session
        if action == "keep":
            self.confirm(actor=actor, scope=scope if scope != "agent" else "system", session=sess["id"])
            sess["state"] = "kept"
        elif action == "revert":
            self.undo(actor=actor, scope="system" if scope == "agent" else scope, session=sess["id"],
                      force=True)
            sess["state"] = "reverted"
        else:
            sess["state"] = "ended"
        self._session = None
        return dict(sess)

    def preview(self, *, actor: str, scope: Scope, text: str = "", voice: str = "", speed: float = 0.0,
                peak: float = 0.0, wait_s: float = 15.0) -> dict:
        if scope == "agent":
            if self.agents_locked:
                raise PolicyError("agents_locked", "Settings changes are locked right now.")
            if not self.voice_turn:
                raise PolicyError("not_in_voice_turn",
                                  "I can only change my settings while we're talking on the wall.")
        call = {"actor": actor, "scope": scope, "text": text, "voice": voice, "speed": speed, "peak": peak}
        self.previews.append(call)
        resolved = voice or str(self.values.get("tts.engine.voice", ""))
        if self.voice_turn:
            return {"played": True, "ms": 1000, "voice": resolved}
        return {"queued": True, "voice": resolved}

    # ---- owner operations
    def _owner_only(self, scope: Scope) -> None:
        if scope == "agent":
            raise PolicyError("policy", "I'm not allowed to change that one by voice.")

    def _op_change(self, edits: list[dict], applies: str, actor: str, scope: Scope, announce: str) -> Change:
        change = Change(
            id=self._next_id(), ts=self._ts(), machine=self.machine, actor=actor, scope=scope, session=None,
            reason="", edits=edits, applies=applies,
            status="pending_restart" if applies == "restart" else "applied",
            rev_before=self.revision, rev_after=self.revision + 1, trial_expires=None, undo_of=None,
            error=None, announce=announce,
        )
        self.revision += 1
        self._changes.append(change)
        return change

    def set_enabled(self, plugin: str, enabled: bool, *, actor: str, scope: Scope) -> Change:
        self._owner_only(scope)
        if plugin not in self.specs:
            raise ConfigError(f"unknown plugin {plugin}", plugin, "unknown_plugin")
        spec = self.specs[plugin]
        if spec.core:
            raise PolicyError("policy", "That core part cannot be switched off.", field=plugin)
        old, self.enabled[plugin] = self.enabled[plugin], bool(enabled)
        return self._op_change([{"key": f"{plugin}.enabled", "old": old, "new": bool(enabled)}],
                               spec.toggle, actor, scope, f"I {'enabled' if enabled else 'disabled'} {plugin}.")

    def reload_plugin(self, plugin: str, *, actor: str, scope: Scope) -> Change:
        self._owner_only(scope)
        if plugin not in self.specs:
            raise ConfigError(f"unknown plugin {plugin}", plugin, "unknown_plugin")
        if self.specs[plugin].core:
            raise PolicyError("policy", "That core part cannot be reloaded.", field=plugin)
        self.reloads.append(plugin)
        return self._op_change([], "rebuild", actor, scope, f"I reloaded {plugin}.")

    def reload_file(self, *, actor: str, scope: Scope) -> Change:
        self._owner_only(scope)
        self.reloads.append("<file>")
        return self._op_change([], "live", "file" if actor == "owner" else actor, scope,
                               "I reloaded the settings file.")

    def request_restart(self, *, actor: str, scope: Scope) -> dict:
        self._owner_only(scope)
        self.restart_requested = True
        return {"ok": True, "at": self._ts()}

    def register_remote(self, machine: str, client: Any) -> None:
        self.remotes[machine] = client

    def attach_remote_change(self, change: dict) -> None:
        self.remote_changes.append(dict(change))
        if self._session is not None:
            self._session["changes"].append(change["id"])
            self._session_touch()


# --------------------------------------------------------------------------------------------
# run_plugin_contract
# --------------------------------------------------------------------------------------------

def _fail(spec: PluginSpec, msg: str) -> None:
    raise AssertionError(f"{spec.name}: {msg}")


def _other_value(k: Knob) -> Any:
    """A valid value different from the default, or _MISSING when none can be invented safely."""
    d = k.default
    if k.type == "bool":
        return not d
    if k.type in ("int", "float") and not isinstance(d, bool) and d is not None:
        lo = k.min if k.min is not None else d - 1
        hi = k.max if k.max is not None else d + 1
        for cand in (lo, hi, (lo + hi) / 2, d + (k.agent_step or 1), d - (k.agent_step or 1)):
            cand = int(cand) if k.type == "int" else float(cand)
            if cand != d and lo <= cand <= hi:
                return cand
        return _MISSING
    if k.type == "enum" and k.choices:
        for c in k.choices:
            if c != d:
                return c
    if k.type == "str" and isinstance(d, str):
        cand = d + "x"
        if k.max is None or len(cand) <= k.max:
            return cand
    if k.type == "list[str]" and isinstance(d, (list, tuple)):
        cand = list(d) + ["extra"]
        if k.max is None or len(cand) <= k.max:
            return cand
    return _MISSING


def _static_checks(spec: PluginSpec) -> None:
    names = [k.name for k in spec.knobs]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        _fail(spec, f"duplicate knob names: {dupes}")
    for k in spec.knobs:
        tag = f"knob {k.name}"
        if not k.description.strip():
            _fail(spec, f"{tag}: empty description")
        if len(k.description) > 160:
            _fail(spec, f"{tag}: description is {len(k.description)} chars, limit 160")
        d = k.default
        if d is None:
            if not k.nullable:
                _fail(spec, f"{tag}: default is None but the knob is not nullable")
        elif k.type in ("int", "float"):
            if isinstance(d, bool) or not isinstance(d, (int, float)):
                _fail(spec, f"{tag}: default {d!r} is not a number")
            if k.min is not None and d < k.min:
                _fail(spec, f"{tag}: default {d} below min {k.min}")
            if k.max is not None and d > k.max:
                _fail(spec, f"{tag}: default {d} above max {k.max}")
        elif k.type == "bool":
            if not isinstance(d, bool):
                _fail(spec, f"{tag}: default {d!r} is not a bool")
        elif k.type == "enum":
            if not k.choices:
                _fail(spec, f"{tag}: enum without choices")
            elif d not in k.choices:
                _fail(spec, f"{tag}: default {d!r} not in choices {k.choices}")
        elif k.type == "list[str]":
            if not isinstance(d, (list, tuple)):
                _fail(spec, f"{tag}: default is not a list")
            elif (k.min is not None and len(d) < k.min) or (k.max is not None and len(d) > k.max):
                _fail(spec, f"{tag}: default length {len(d)} outside {k.min}..{k.max}")
        elif k.type == "map[str,str]":
            if not isinstance(d, Mapping):
                _fail(spec, f"{tag}: default is not a mapping")
        elif not isinstance(d, str):
            _fail(spec, f"{tag}: default {d!r} is not text")
        elif k.type == "str" and ((k.min is not None and len(d) < k.min) or (k.max is not None and len(d) > k.max)):
            _fail(spec, f"{tag}: default length {len(d)} outside {k.min}..{k.max}")
        if k.agent == "bounded" and k.type in ("int", "float"):
            if k.agent_min is None or k.agent_max is None or not k.agent_step:
                _fail(spec, f"{tag}: bounded without agent_min, agent_max and a step")
            if k.min is not None and k.agent_min < k.min:
                _fail(spec, f"{tag}: agent_min {k.agent_min} below hard min {k.min}")
            if k.max is not None and k.agent_max > k.max:
                _fail(spec, f"{tag}: agent_max {k.agent_max} above hard max {k.max}")
            if k.agent_min > k.agent_max:
                _fail(spec, f"{tag}: agent_min above agent_max")


def _live_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.is_alive() and not t.daemon}


def run_plugin_contract(spec: PluginSpec, make_ctx: Optional[Callable[[], Any]] = None,
                        start: bool = True) -> None:
    """The generic checks of spec 6.4. Raises AssertionError naming the plugin and the broken rule.

    Static: unique knob names, defaults inside bounds and choices, agent bounds inside hard bounds with a step,
    descriptions non-empty and at most 160 chars. With start=True: create with FakeCtx and cfg_for, start,
    check the provided services, apply one other valid value per live knob, stop plus release_all, check that
    no new non-daemon thread remains, then start and stop a second time. A plugin whose start needs real
    models passes start=False."""
    _static_checks(spec)
    if not start:
        return
    ctx = make_ctx() if make_ctx is not None else FakeCtx(spec.machine)
    cfg = cfg_for(spec)
    if hasattr(ctx, "cfgs"):
        ctx.cfgs.setdefault(spec.name, cfg)
    before = _live_threads()
    plugin = spec.create(ctx, cfg)
    if not isinstance(plugin, PluginBase):
        _fail(spec, f"create() returned {type(plugin).__name__}, not a PluginBase")
    for cycle in (1, 2):
        plugin.cfg = cfg
        plugin.start()
        try:
            for service in spec.provides:
                if hasattr(ctx, "has") and not ctx.has(service):
                    _fail(spec, f"start() did not provide service {service!r} (cycle {cycle})")
            if cycle == 1:
                for k in spec.knobs:
                    if k.applies != "live":
                        continue
                    other = _other_value(k)
                    if other is _MISSING:
                        continue
                    try:
                        plugin.apply({k.name: other}, cfg.replace(**{k.name: other}))
                    except Exception as exc:
                        _fail(spec, f"apply({{{k.name!r}: {other!r}}}) raised {type(exc).__name__}: {exc}")
                plugin.apply({}, cfg)
        finally:
            plugin.stop()
            ctx.release_all()
        deadline = time.monotonic() + 2.0
        while True:
            leaked = _live_threads() - before
            if not leaked or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        if leaked:
            _fail(spec, f"non-daemon threads still alive after stop (cycle {cycle}): "
                        f"{sorted(t.name for t in leaked)}")
