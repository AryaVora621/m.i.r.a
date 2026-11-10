"""Plugin registry: specs to running plugins (spec 2.1, 2.2).

Owns provider resolution and conflicts, needs and PENDING, start order (needs first, then `order`, then name;
stop is the reverse), per-plugin states and last_error, effects released LIFO, enable and disable, and
rebuild. It never decides WHEN a plugin method may run: the core calls it only from the loop thread (listener)
or the apply worker (speech). Stdlib only.
"""
from __future__ import annotations

import heapq
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .plugin import (ConfigError, ConflictError, Paths, PluginBase, PluginConfig, PluginSpec, State)
from .services import MULTI_SERVICES

_MISSING = object()
CORE_OWNER = "<core>"


class RebuildFailed(RuntimeError):
    """A failed rebuild whose restore of the previous config failed as well: nothing left to run."""


def describe_error(exc: BaseException) -> str:
    """Class name plus a short message. Plugin errors can carry a path or a URL; they never carry text the
    owner said because plugins are told never to put it in an exception."""
    msg = str(exc).replace("\n", " ")[:200]
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


@dataclass
class Entry:
    spec: PluginSpec
    cfg: PluginConfig
    enabled: bool
    state: State = State.DISABLED
    plugin: Optional[PluginBase] = None
    ctx: Optional["PluginCtx"] = None
    last_error: Optional[str] = None
    missing: list[str] = field(default_factory=list)
    effects: list[tuple[Callable[[], None], str]] = field(default_factory=list)
    rebuild_ms: Optional[float] = None
    started_at: Optional[float] = None


class PluginCtx:
    """The PluginContext of one plugin. Thin: every call goes to the registry or the host (the core)."""

    def __init__(self, registry: "Registry", entry: Entry, host: Any) -> None:
        self._registry, self._entry, self._host = registry, entry, host
        self.machine = host.machine
        self.log = logging.getLogger(f"mira.{entry.spec.name}")
        self.paths: Paths = host.paths

    def provide(self, service: str, obj: Any) -> None:
        self._registry.provide(self._entry.spec.name, service, obj)

    def get(self, service: str, default: Any = _MISSING) -> Any:
        return self._registry.service(service, default)

    def all(self, service: str) -> list[Any]:
        return self._registry.services(service)

    def effect(self, release: Callable[[], None], label: str = "") -> None:
        self._entry.effects.append((release, label))

    def cfg_of(self, plugin: str) -> PluginConfig:
        return self._host.cfg_of(plugin)

    def now(self) -> float:
        return self._host.now()

    def on(self, event: str, fn: Callable[[dict], None]) -> None:
        self._host.on(event, fn)

    def emit(self, event: str, **payload: Any) -> None:
        self._host.emit(event, **payload)


class Registry:
    def __init__(self, specs: Sequence[PluginSpec], host: Any, *,
                 required_services: Sequence[str] = (),
                 on_state: Optional[Callable[[str, State], None]] = None) -> None:
        self.host = host
        self.required_services = tuple(required_services)
        self.on_state = on_state
        self.log = logging.getLogger("mira.registry")
        self.entries: dict[str, Entry] = {}
        self.specs: dict[str, PluginSpec] = {}
        self._single: dict[str, tuple[str, Any]] = {}
        self._multi: dict[str, list[tuple[str, Any]]] = {}
        self._started: list[str] = []
        for spec in specs:
            if spec.name in self.specs:
                raise ConfigError(f"two plugins are named {spec.name}", spec.name)
            self.specs[spec.name] = spec

    # ---- entries
    def add(self, spec: PluginSpec, cfg: PluginConfig, enabled: bool) -> Entry:
        entry = Entry(spec, cfg, enabled, State.DISABLED if not enabled else State.PENDING)
        self.entries[spec.name] = entry
        return entry

    def entry(self, name: str) -> Entry:
        return self.entries[name]

    # ---- services
    def provide(self, plugin: str, service: str, obj: Any) -> None:
        if service in MULTI_SERVICES:
            rows = self._multi.setdefault(service, [])
            rows[:] = [r for r in rows if r[0] != plugin]
            rows.append((plugin, obj))
            return
        held = self._single.get(service)
        if held is not None and held[0] != plugin:
            raise ConflictError("provider_conflict",
                                f"two providers for {service!r}: {held[0]}, {plugin}; set enabled=false on one")
        self._single[service] = (plugin, obj)

    def unprovide(self, plugin: str) -> None:
        for service in [s for s, (p, _o) in self._single.items() if p == plugin]:
            del self._single[service]
        for service, rows in self._multi.items():
            rows[:] = [r for r in rows if r[0] != plugin]

    def has_service(self, service: str) -> bool:
        return service in self._single or bool(self._multi.get(service))

    def _priority(self, plugin: str, obj: Any) -> float:
        try:
            cfg = self.host.cfg_of(plugin)
            if "priority" in cfg:
                return float(cfg["priority"])
        except (KeyError, TypeError, ValueError):
            pass
        return float(getattr(obj, "priority", 0))

    def services(self, service: str) -> list[Any]:
        if service in MULTI_SERVICES:
            rows = sorted(self._multi.get(service, []), key=lambda r: (-self._priority(r[0], r[1]), r[0]))
            return [o for _p, o in rows]
        held = self._single.get(service)
        return [held[1]] if held is not None else []

    def service(self, service: str, default: Any = _MISSING) -> Any:
        if service in MULTI_SERVICES:
            rows = self.services(service)
            if rows:
                return rows[0]
        elif service in self._single:
            return self._single[service][1]
        if default is _MISSING:
            raise KeyError(service)
        return default

    def provider_of(self, service: str) -> list[str]:
        if service in MULTI_SERVICES:
            return [p for p, _o in self._multi.get(service, [])]
        return [self._single[service][0]] if service in self._single else []

    # ---- providers and order
    def enabled_providers(self, service: str) -> list[str]:
        return sorted(e.spec.name for e in self.entries.values() if e.enabled and service in e.spec.provides)

    def check_providers(self) -> None:
        """Two enabled providers of one exclusive service is a ConfigError naming both."""
        seen: dict[str, list[str]] = {}
        for e in self.entries.values():
            if not e.enabled:
                continue
            for service in e.spec.provides:
                if service not in MULTI_SERVICES:
                    seen.setdefault(service, []).append(e.spec.name)
        for service, names in seen.items():
            if len(names) > 1:
                names = sorted(names)
                raise ConfigError(f"two providers for {service!r}: {', '.join(names)}; set enabled=false on one",
                                  service, "provider_conflict")

    def order(self) -> list[str]:
        """Start order of the enabled plugins: providers before the plugins that need them, then `order`, then name."""
        names = [n for n, e in self.entries.items() if e.enabled]
        provides: dict[str, list[str]] = {}
        for n in names:
            for s in self.entries[n].spec.provides:
                provides.setdefault(s, []).append(n)
        deps: dict[str, set[str]] = {n: set() for n in names}
        for n in names:
            for need in self.entries[n].spec.needs:
                deps[n].update(p for p in provides.get(need, []) if p != n)
        key = lambda n: (self.entries[n].spec.order, n)  # noqa: E731
        heap = [(key(n), n) for n in names if not deps[n]]
        heapq.heapify(heap)
        done: set[str] = set()
        out: list[str] = []
        while heap:
            _k, n = heapq.heappop(heap)
            out.append(n)
            done.add(n)
            for m in names:
                if m not in done and n in deps[m] and deps[m] <= done and (key(m), m) not in heap:
                    heapq.heappush(heap, (key(m), m))
        out.extend(sorted((n for n in names if n not in done), key=key))   # a cycle: left to go PENDING
        return out

    def missing_needs(self, spec: PluginSpec) -> list[str]:
        return [n for n in spec.needs if not self.has_service(n)]

    # ---- lifecycle
    def _set_state(self, entry: Entry, state: State) -> None:
        if entry.state != state:
            entry.state = state
            if self.on_state:
                try:
                    self.on_state(entry.spec.name, state)
                except Exception:
                    self.log.exception("state observer failed")

    def start_entry(self, entry: Entry) -> bool:
        """Create and start one plugin. Never raises for a plugin fault: the entry goes FAILED with last_error."""
        spec = entry.spec
        missing = self.missing_needs(spec)
        if missing:
            entry.missing = missing
            self._set_state(entry, State.PENDING)
            return False
        entry.missing = []
        self._set_state(entry, State.LOADING)
        entry.effects = []
        entry.ctx = PluginCtx(self, entry, self.host)
        t0 = time.monotonic()
        try:
            entry.plugin = spec.create(entry.ctx, entry.cfg)
            entry.plugin.start()
            for service in spec.provides:
                if not self.has_service(service):
                    raise RuntimeError(f"start() did not provide the service {service!r}")
        except Exception as exc:
            self.log.exception("plugin %s failed to start", spec.name)
            entry.last_error = describe_error(exc)
            self._dispose(entry, call_stop=entry.plugin is not None)
            self._set_state(entry, State.FAILED)
            return False
        entry.last_error = None
        entry.started_at = time.monotonic()
        entry.rebuild_ms = round((entry.started_at - t0) * 1000, 1)
        if spec.name not in self._started:
            self._started.append(spec.name)
        self._set_state(entry, State.ACTIVE)
        return True

    def _dispose(self, entry: Entry, *, call_stop: bool) -> None:
        """stop(), then effects LIFO, then the services the plugin provided."""
        if call_stop and entry.plugin is not None:
            try:
                entry.plugin.stop()
            except Exception:
                self.log.exception("plugin %s stop failed", entry.spec.name)
        self._release(entry)
        self.unprovide(entry.spec.name)

    def _release(self, entry: Entry) -> None:
        effects, entry.effects = entry.effects, []
        while effects:
            release, label = effects.pop()
            try:
                release()
            except Exception:
                self.log.exception("effect %r of %s failed to release", label, entry.spec.name)

    def stop_entry(self, entry: Entry, final: State = State.STOPPED) -> None:
        if entry.state in (State.ACTIVE, State.FAILED, State.LOADING) or entry.plugin is not None:
            self._dispose(entry, call_stop=entry.plugin is not None)
        entry.plugin = None
        entry.ctx = None
        if entry.spec.name in self._started:
            self._started.remove(entry.spec.name)
        self._set_state(entry, final)

    def boot_all(self) -> None:
        for name in self.order():
            self.start_entry(self.entries[name])
        progress = True
        while progress:       # a provider that came up late (or a failed one retried) can free a pending plugin
            progress = False
            for name in self.order():
                e = self.entries[name]
                if e.state == State.PENDING and not self.missing_needs(e.spec):
                    progress = self.start_entry(e) or progress

    def stop_all(self) -> None:
        for name in reversed(list(self._started)):
            self.stop_entry(self.entries[name])
        for e in self.entries.values():
            if e.state in (State.PENDING, State.DISABLED):
                continue
            if e.state != State.STOPPED:
                self._set_state(e, State.STOPPED)

    def missing_required_services(self) -> list[str]:
        return [s for s in self.required_services if not self.has_service(s)]

    def failed_required(self) -> list[tuple[str, str]]:
        out = []
        for e in self.entries.values():
            if e.enabled and e.spec.required and e.state in (State.FAILED, State.PENDING):
                out.append((e.spec.name, e.last_error or "waiting for " + ", ".join(e.missing)))
        return out

    # ---- toggle
    def enable(self, name: str) -> bool:
        entry = self.entries[name]
        for service in entry.spec.provides:
            if service in MULTI_SERVICES:
                continue
            others = [n for n in self.enabled_providers(service) if n != name]
            if others:
                raise ConflictError("provider_conflict",
                                    f"two providers for {service!r}: {', '.join(sorted(others + [name]))}; "
                                    "set enabled=false on one")
        entry.enabled = True
        entry.last_error = None
        return self.start_entry(entry)

    def check_disable(self, name: str) -> None:
        entry = self.entries[name]
        for service in entry.spec.provides:
            if service in self.required_services and not [
                    n for n in self.enabled_providers(service) if n != name]:
                raise ConfigError(f"{name} provides the required service {service!r} and cannot be disabled",
                                  name, "missing_service")
        for other in self.entries.values():
            if other.enabled and other.state == State.ACTIVE and other.spec.name != name:
                for need in other.spec.needs:
                    if need in entry.spec.provides and not [
                            n for n in self.enabled_providers(need) if n != name]:
                        raise ConfigError(f"{other.spec.name} needs {need!r}, which only {name} provides",
                                          name, "missing_service")

    def disable(self, name: str) -> None:
        entry = self.entries[name]
        self.check_disable(name)
        entry.enabled = False
        self.stop_entry(entry, State.DISABLED)

    # ---- rebuild
    def _rebuild_once(self, entry: Entry, cfg: PluginConfig) -> None:
        plugin = entry.plugin
        assert plugin is not None
        if type(plugin).rebuild is PluginBase.rebuild:
            # the default rebuild is stop(); start(), which would keep the old effects and services: do the
            # same steps here so the old object is fully released before the new one is built (dispose first)
            plugin.stop()
            self._release(entry)
            self.unprovide(entry.spec.name)
            plugin.cfg = cfg
            plugin.start()
        else:
            plugin.rebuild(cfg)
        for service in entry.spec.provides:
            if not self.has_service(service):
                raise RuntimeError(f"rebuild() did not provide the service {service!r}")

    def rebuild(self, name: str, new: PluginConfig, prev: PluginConfig) -> Optional[str]:
        """Rebuild one plugin with `new`. On failure restore `prev` and return the error text; if the restore
        fails too, raise RebuildFailed. A plugin that is not ACTIVE is started instead."""
        entry = self.entries[name]
        if entry.plugin is None or entry.state != State.ACTIVE:
            entry.cfg = new
            if entry.enabled and self.start_entry(entry):
                return None
            return entry.last_error or "did not start"
        t0 = time.monotonic()
        try:
            self._rebuild_once(entry, new)
        except Exception as exc:
            error = describe_error(exc)
            self.log.exception("rebuild of %s failed; restoring the previous config", name)
            try:
                self._rebuild_once(entry, prev)
            except Exception as exc2:
                entry.last_error = describe_error(exc2)
                self._set_state(entry, State.FAILED)
                raise RebuildFailed(f"{name}: rebuild failed ({error}) and the restore failed too "
                                    f"({entry.last_error})") from exc2
            entry.cfg = prev
            entry.last_error = error
            return error
        entry.cfg = new
        entry.last_error = None
        entry.rebuild_ms = round((time.monotonic() - t0) * 1000, 1)
        return None

    # ---- reads
    def row(self, entry: Entry, pending_restart: Sequence[str] = ()) -> dict:
        status: dict = {}
        if entry.plugin is not None and entry.state == State.ACTIVE:
            try:
                status = dict(entry.plugin.status() or {})
            except Exception as exc:     # a status() fault must not break inventory
                status = {"status_error": type(exc).__name__}
        if entry.rebuild_ms is not None:
            status.setdefault("last_rebuild_ms", entry.rebuild_ms)
        spec = entry.spec
        return {"name": spec.name, "kind": spec.kind, "version": spec.version, "machine": spec.machine,
                "state": entry.state.value, "enabled": entry.enabled, "required": spec.required,
                "core": spec.core, "provides": list(spec.provides), "needs": list(spec.needs),
                "missing": list(entry.missing), "toggle": spec.toggle, "rev": entry.cfg.revision,
                "pending_restart": list(pending_restart), "last_error": entry.last_error, "status": status}
