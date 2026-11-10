"""The Core (spec 2.3, 2.5, 2.6): one write path, safe points, trials, undo, voice sessions, previews.

Threading contract (2.3.1): on the listener the loop thread owns every plugin object. Control threads (HTTP,
CLI) may only validate, check policy, persist, append to the change log, put the change on the apply queue and
read snapshots. Only `safe_point`, `turn_begin`/`turn_end` and the speech apply worker ever call `start`,
`stop`, `apply` or `rebuild` or assign an attribute on a plugin object or the args namespace. The one
exception is the inline preview of 2.6, which runs on the control thread while the loop thread is blocked in
Brain.ask.

Two views of every plugin config are kept apart on purpose: the store holds what is PERSISTED (used for `old`
values and undo conflicts), and `self._cfgs` holds what is APPLIED (swapped by reference at apply time; what
`cfg()` and `ctx.cfg_of` return).

Time: one injected Clock. Trials, sessions, rate windows and the quiet gate read `clock.wall()` (epoch
seconds); the rebuild-quiet and busy-defer gates read `clock.mono()`. Expiries are kept as floats in memory
because the ISO stamps of the log have one-second resolution.
"""
from __future__ import annotations

import collections
import copy
import logging
import os
import signal
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, Sequence

from . import VERSION
from .changelog import LIVE_STATUSES, ChangeLog, fmt_ts, parse_ts, plan_reverts
from .envmap import arg_attr, argv_layer, env_layer, explicit_from_argv, layer_from_args, legacy_mirror
from .plugin import (RESET, RESET_STREAM, CONTRACT_VERSION, Change, ConfigError, ConflictError, CoreAPI, Edit,
                     Knob, Paths, PluginConfig, PluginSpec, PolicyError, RemoteClient, Scope, State)
from .policy import Policy, Settings, settings_from
from .registry import _MISSING, CORE_OWNER, RebuildFailed, Registry
from .schema import (STRENGTH, check_bounds, closest, coerce, core_spec, expand_path, fmt_num, fmt_value,
                     knob_to_json, ladder, spec_to_json, spoken_name, validate_plugin)
from .services import MULTI_SERVICES, REQUIRED_LISTENER, REQUIRED_SPEECH
from .store import ConfigStore, StoreError

log = logging.getLogger("mira.core")
_INF = float("inf")
_UNITS = {"s": "seconds", "x": "times", "percent": "percent", "chars": "characters", "chunks(100ms)": "chunks",
          "rms": "", "score": "", "prob": "", "threads": "threads", "items": "items", "bytes": "bytes"}


class Clock:
    def wall(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class Turn:
    id: str
    phase: str
    started: float


class _Item:
    """One queued change, applied on the loop thread (or the speech worker)."""
    __slots__ = ("change_id", "edits", "plugins", "rebuild", "reload", "event", "rev_after", "enabled_edits")

    def __init__(self, change_id: str, edits: list[dict], plugins: set[str], rebuild: bool,
                 rev_after: int, reload: str = "") -> None:
        self.change_id, self.edits, self.plugins = change_id, edits, plugins
        self.rebuild, self.reload, self.rev_after = rebuild, reload, rev_after
        self.event = threading.Event()
        self.enabled_edits: list[dict] = []


class _Job:
    __slots__ = ("wav", "alias", "event", "result", "voice")

    def __init__(self, wav: bytes, alias: str, voice: str) -> None:
        self.wav, self.alias, self.voice = wav, alias, voice
        self.event = threading.Event()
        self.result: Optional[dict] = None


def _applies_of(knobs_applies: Sequence[str]) -> str:
    best = "live"
    for a in knobs_applies:
        if STRENGTH[a] > STRENGTH[best]:
            best = a
    return best


def _minutes(trial_s: float) -> str:
    if trial_s < 90:
        return f"{int(round(trial_s))} seconds"
    return f"{int(round(trial_s / 60))} minutes"


class Core(CoreAPI):
    # ------------------------------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------------------------------
    def __init__(self, *, specs: Sequence[PluginSpec], store: ConfigStore, machine: str,
                 changelog: ChangeLog, mirror: bool = False, clock: Optional[Clock] = None,
                 here: Optional[Path] = None, home: Optional[Path] = None,
                 required_services: Optional[Sequence[str]] = None, source: Any = None,
                 version: str = VERSION, boot_guard: bool = False, worker: bool = True,
                 lenient: bool = False) -> None:
        self.machine = machine
        self.store = store
        self.log = changelog
        self.mirror = mirror
        self.clock = clock or Clock()
        self._wall, self._mono = self.clock.wall, self.clock.mono
        self.version = version
        self.boot_guard = boot_guard
        self.use_worker = worker and machine == "speech"
        self.here = Path(here) if here else Path(__file__).resolve().parent.parent
        self.home = Path(home) if home else Path(os.environ.get("HOME") or os.path.expanduser("~"))
        cfgdir = store.cfgdir or (self.home / ".config" / "mira")
        self.paths = Paths(here=self.here, config_dir=cfgdir, home=self.home)
        self._source_given = source
        self.lenient = lenient

        self.specs: dict[str, PluginSpec] = {s.name: s for s in specs if s.name != "core"}
        self.specs["core"] = store.specs["core"]
        required = (REQUIRED_SPEECH if machine == "speech" else REQUIRED_LISTENER) \
            if required_services is None else tuple(required_services)
        self.registry = Registry([s for s in specs if s.name != "core"], self, required_services=required,
                                 on_state=self._on_state)
        self.policy = Policy(machine, self.settings, self._wall)

        # applied snapshots (see module docstring)
        self._cfgs: dict[str, PluginConfig] = {}
        self._build_cfgs()
        for name, spec in self.specs.items():
            if name != "core":
                self.registry.add(spec, self._cfgs[name], store.enabled(name))
        self._validate_effective()

        # loop-visible state; the fast path of safe_point reads only these
        self._inbox: collections.deque[_Item] = collections.deque()
        self._pending: list[_Item] = []
        self._deferred: list[_Job] = []
        self._dlock = threading.Lock()
        self.pending_flush = False
        self._next_due = _INF
        self._want_overlay = False
        self._have_overlay = False
        self.restart_requested = False
        self.turn: Optional[Turn] = None
        self._turn_counter = 0
        self._armed = True
        self._last_turn_event = -_INF
        self._last_busy = -_INF
        self._counts: collections.Counter = collections.Counter()
        self.args: Any = None

        # write side
        self._wlock = threading.RLock()
        self._inline_lock = threading.Lock()
        self._trials: dict[str, dict] = {}          # change id -> {"due": epoch, "session": id|None}
        self._session: Optional[dict] = None
        self._sess_f: dict[str, float] = {}         # float view of the open session's clocks
        self._booted = False
        self._started_mono = self._mono()
        self._observers: dict[str, list[Callable[[dict], None]]] = {}
        self.remotes: dict[str, RemoteClient] = {}
        self._remote_cache: dict[str, Any] = {"schema": None, "rev": None, "status": None, "status_at": -_INF}
        self._worker: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.on_fatal: Callable[[BaseException], None] = self._default_fatal
        self.on_restart: Optional[Callable[[], None]] = None
        self._shut = False

    @classmethod
    def from_args(cls, args: Any, *, specs: Sequence[PluginSpec], source: Any = None,
                  machine: str = "listener", **kw: Any) -> "Core":
        """Test and legacy-compatible mode: in-memory store, no file under HOME, no port, no change log on
        disk, no mirror. Values come from the args namespace (Knob.arg), then Knob.env, then the default."""
        layer = layer_from_args(args, specs)
        override: dict[str, bool] = {"control.http": False}     # no port, ever, in this mode
        if source is not None:
            override.update({"source.pipewire": False, "source.wav": False})
        elif layer.get("source.wav.path"):
            override.update({"source.wav": True, "source.pipewire": False})
        store = ConfigStore(specs, machine, values=layer, enabled_override={
            k: v for k, v in override.items() if any(s.name == k for s in specs)})
        # lenient: v1 had no bounds (the golden scenario S9 runs --session-ttl 2 against a minimum of 30), so a
        # value that arrives through the args namespace or the environment is logged when it is out of range
        # and kept. Writes made through submit are still bounded.
        kw.setdefault("lenient", True)
        return cls(specs=specs, store=store, machine=machine, changelog=ChangeLog(None, machine),
                   mirror=False, source=source, **kw)

    @classmethod
    def from_file(cls, path: Any, *, argv: Sequence[str], machine: str, specs: Sequence[PluginSpec],
                  parser: Any = None, mirror: bool = False, changelog: bool = True, **kw: Any) -> "Core":
        """Production. Layers, highest first: flags explicitly on argv, the file, MIRA_* env while
        core.legacy_env is "fallback", the schema default. An invalid file falls back to the last-good copy
        (status.config_error says why); without one this exits 78."""
        explicit = explicit_from_argv(parser, argv) if parser is not None else {}
        try:
            argv_vals = argv_layer(explicit, specs)
            env_vals = env_layer(os.environ, specs, strict=False)
        except ConfigError as exc:
            log.error("boot: a command line flag is not usable: %s", exc.message)
            raise SystemExit(78) from None
        override: dict[str, bool] = {}
        if argv_vals.get("source.wav.path"):
            override = {"source.wav": True, "source.pipewire": False}
        store = ConfigStore(specs, machine, path=Path(path), argv=argv_vals, env=env_vals,
                            enabled_override={k: v for k, v in override.items()
                                              if any(s.name == k for s in specs)})
        try:
            store.load()
        except StoreError as exc:
            log.error("boot: %s", exc.message)
            raise SystemExit(78) from None
        kw.setdefault("boot_guard", os.environ.get("MIRA_BOOT_GUARD") == "1")
        max_bytes = int(store.value("control.http.changelog_max_bytes")) \
            if any(s.name == "control.http" and any(k.name == "changelog_max_bytes" for k in s.knobs)
                   for s in specs) else 5_000_000
        clog = ChangeLog(store.sibling("changes.jsonl") if changelog else None, machine, max_bytes)
        try:
            return cls(specs=specs, store=store, machine=machine, changelog=clog, mirror=mirror, **kw)
        except ConfigError as exc:
            if store.use_lastgood(f"{exc.message}"):
                log.error("config_error: %s; running the last-good file", exc.message)
                try:
                    return cls(specs=specs, store=store, machine=machine, changelog=clog, mirror=mirror, **kw)
                except ConfigError as exc2:
                    exc = exc2
            log.error("boot: invalid configuration: %s", exc.message)
            raise SystemExit(78) from None

    # ------------------------------------------------------------------------------------------
    # config snapshots and validation
    # ------------------------------------------------------------------------------------------
    def settings(self) -> Settings:
        """The control.http knobs the policy and timers read, from the PERSISTED values (an edit of a policy
        knob takes effect for the next call, not at the next safe point)."""
        spec = self.store.specs.get("control.http")
        if spec is None:
            return Settings()
        try:
            return settings_from(self.store.values("control.http"))
        except Exception:
            return Settings()

    def _expanded(self, spec: PluginSpec, name: str, value: Any) -> Any:
        for k in spec.knobs:
            if k.name == name and k.type == "path" and isinstance(value, str):
                return expand_path(value, self.paths)
        return value

    def _expanded_values(self, name: str) -> dict[str, Any]:
        spec = self.specs[name]
        return {k: self._expanded(spec, k, v) for k, v in self.store.values(name).items()}

    def _build_cfgs(self) -> None:
        for name in self.specs:
            self._cfgs[name] = PluginConfig(name, self._expanded_values(name), self.store.revision)

    def _validate_effective(self) -> None:
        """Cross-field hooks over the EFFECTIVE config of every enabled plugin, and provider conflicts."""
        for name, spec in self.specs.items():
            if name != "core" and not self.store.enabled(name):
                continue
            try:
                validate_plugin(spec, self.store.values(name))
            except ConfigError as exc:
                if not self.lenient:
                    raise
                log.warning("legacy value kept although it is outside the schema: %s", exc.message)
        self.registry.check_providers()

    def cfg(self, plugin: str) -> PluginConfig:
        return self._cfgs[plugin]

    def cfg_of(self, plugin: str) -> PluginConfig:
        return self._cfgs[plugin]

    # ------------------------------------------------------------------------------------------
    # services, events, clock (the registry's host interface)
    # ------------------------------------------------------------------------------------------
    def service(self, name: str, default: Any = _MISSING) -> Any:
        return self.registry.service(name, default)

    def services(self, name: str) -> list[Any]:
        return self.registry.services(name)

    def now(self) -> float:
        src = self.registry.service("source", None)
        if src is not None:
            try:
                return float(src.clock())
            except Exception:
                pass
        return self._mono()

    def on(self, event: str, fn: Callable[[dict], None]) -> None:
        self._observers.setdefault(event, []).append(fn)

    def emit(self, event: str, **payload: Any) -> None:
        handlers = self._observers.get(event)
        if not handlers:
            return
        turn = self.turn
        rec = {"event": event, "at": self.now(), "turn": turn.id if turn else None, **payload}
        for fn in list(handlers):
            try:
                fn(dict(rec))
            except Exception:
                log.exception("observer of %s failed", event)

    def _on_state(self, plugin: str, state: State) -> None:
        self.emit("plugin.state", plugin=plugin, state=state.value)

    # ------------------------------------------------------------------------------------------
    # boot and shutdown
    # ------------------------------------------------------------------------------------------
    def boot(self) -> None:
        if self._booted:
            return
        if self.store.path is not None:
            self._boot_replay()
            self._build_cfgs()
            for name, entry in self.registry.entries.items():
                entry.cfg = self._cfgs[name]
                entry.enabled = self.store.enabled(name)
        self.registry.provide(CORE_OWNER, "core", self)
        if self._source_given is not None:
            self.registry.provide(CORE_OWNER, "source", self._source_given)
        self.registry.boot_all()
        problems = [f"required service {s!r} has no active provider" for s in self.registry.missing_required_services()]
        problems += [f"required plugin {n} is not running: {why}" for n, why in self.registry.failed_required()]
        if problems:
            for p in problems:
                log.error("boot: %s", p)
            self.registry.stop_all()
            raise SystemExit(78)
        self._booted = True
        self._started_mono = self._mono()
        try:
            self.store.mark_good()
        except OSError:
            log.exception("could not write the last-good copy")
        self._write_mirror()
        if self.use_worker:
            self._stop.clear()
            self._worker = threading.Thread(target=self._worker_loop, name="mira-apply", daemon=True)
            self._worker.start()

    def _boot_replay(self) -> None:
        """Before plugins start: revert expired trials (and, under the boot guard, recent agent changes) in the
        file first; keep the remaining time of unexpired trials; settle statuses left over from a crash."""
        now = self._wall()
        records = self.log.all()
        cur = self._current
        guard_s = float(self.store.value("core.crash_guard_revert_s")) if self.boot_guard else None
        plans = plan_reverts(records, cur, now, guard_window_s=guard_s, expire=True)
        with self._wlock:
            for plan in plans:
                actor = plan.reason
                if plan.skipped:
                    log.warning("boot %s: %s moved on since %s and was left alone", plan.reason,
                                ", ".join(plan.skipped), plan.change["id"])
                if not plan.edits:
                    self.log.status(plan.change["id"], plan.change["status"], trial_expires=None, at=now)
                    continue
                file_edits = {e["key"]: e["new"] for e in plan.edits if not e["key"].endswith(".enabled")}
                enabled = {e["key"].rpartition(".")[0]: e["new"] for e in plan.edits if e["key"].endswith(".enabled")}
                rev_before, rev_after = self.store.write(file_edits, enabled,
                                                         history_keep=self.settings().history_keep)
                undo = self._new_change(plan.edits, actor=actor, scope="system", reason="", session=None,
                                        trial=None, undo_of=plan.change["id"], rev=(rev_before, rev_after),
                                        status="applied", announce=self._announce(plan.edits, None, undo=True),
                                        applies=plan.change["applies"])
                self.log.append(undo)
                self.log.status(plan.change["id"], "reverted", trial_expires=None, at=now)
            for rec in self.log.all():
                if rec.get("status") in ("queued", "pending_restart"):
                    # a restart has happened since: whatever was persisted is what just started
                    self.log.status(rec["id"], "applied", at=now)
            for rec in self.log.trials():
                due = parse_ts(rec.get("trial_expires"))
                if due is not None:
                    self._trials[rec["id"]] = {"due": due, "session": None}
            self._recalc_due()

    def _write_mirror(self) -> None:
        if not self.mirror or self.store.cfgdir is None or self.machine != "listener":
            return
        try:
            env_text, args_lines, _lost = legacy_mirror(self.store.effective(), list(self.specs.values()),
                                                        self.paths)
            d = self.store.cfgdir
            d.mkdir(parents=True, exist_ok=True)
            envp = d / "listen.env"
            if envp.exists():
                head = envp.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
                if not (head and head[0].startswith("# generated by mirakit")):
                    stamp = datetime.fromtimestamp(self._wall()).strftime("%Y-%m-%d")
                    bak = d / f"listen.env.bak-adopt-{stamp}"
                    if not bak.exists():
                        envp.replace(bak)
            for name, text in (("listen.env", env_text), ("legacy.args", "\n".join(args_lines) + ("\n" if args_lines else ""))):
                p = d / name
                tmp = p.with_name(p.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp, p)
        except Exception:
            log.exception("could not write the legacy mirror")

    def write_mirror(self) -> None:
        """Regenerate listen.env and legacy.args now (used by `mira adopt --write`)."""
        self._write_mirror()

    def legacy_mirror_report(self) -> list[dict]:
        """Knobs the legacy listener cannot represent whose value differs from the code default."""
        return legacy_mirror(self.store.effective(), list(self.specs.values()), self.paths)[2]

    def shutdown(self) -> None:
        if self._shut:
            return
        self._shut = True
        self._stop.set()
        self._wake.set()
        w = self._worker
        if w is not None and w is not threading.current_thread():
            w.join(timeout=5)
        self.registry.stop_all()
        self._booted = False

    # ------------------------------------------------------------------------------------------
    # the loop thread
    # ------------------------------------------------------------------------------------------
    def bind_args(self, ns: Any) -> None:
        """Adds every loop.turns knob and every arg-mapped knob to the namespace with its applied value, and
        keeps them in sync from then on (loop thread only)."""
        self.args = ns
        for name, spec in self.specs.items():
            if name == "core":
                continue
            for k in spec.knobs:
                attr = arg_attr(spec, k)
                if attr:
                    setattr(ns, attr, self._cfgs[name][k.name])
        self._apply_overlay()

    def note(self, kind: str) -> None:
        self._last_turn_event = self._mono()
        self._counts[kind] += 1

    def note_busy(self) -> None:
        self._last_busy = self._mono()

    def turn_begin(self) -> None:
        self._turn_counter += 1
        self._last_turn_event = self._mono()
        self.turn = Turn(f"T-{self._turn_counter:06d}", "brain", self._last_turn_event)
        sess = self._session
        if sess is not None:
            self._sess_f["last_activity"] = self._wall()

    def turn_end(self) -> None:
        self.turn = None
        self._last_turn_event = self._mono()
        with self._inline_lock:         # an inline preview that is still playing finishes before the reply
            pass
        self._drain(armed=False, live_only=True)
        self._apply_overlay()

    def safe_point(self, armed: bool) -> int:
        # the empty case is a handful of attribute reads and one float compare: no lock, no allocation
        if (not self._inbox and not self._pending and not self._deferred and not self.pending_flush
                and self._want_overlay is self._have_overlay and not self.restart_requested
                and self._wall() < self._next_due):
            self._armed = armed
            return 0
        return self._safe_point_slow(armed)

    def _safe_point_slow(self, armed: bool) -> int:
        self._armed = armed
        if self.restart_requested:
            self._do_restart()
        self._timers()
        rebuilt = self._drain(armed=armed)
        self._apply_overlay()
        previewed = False
        if armed and self._deferred:
            previewed = self._run_deferred()
        if rebuilt or previewed or self.pending_flush:
            src = self.registry.service("source", None)
            if src is not None:
                try:
                    src.flush()
                except Exception:
                    log.exception("source flush failed")
            self.pending_flush = False
            return RESET_STREAM
        return 0

    def tick(self) -> None:
        """The speech machine's apply worker pass: timers, then every queued change (rebuilds included)."""
        self._timers()
        self._drain(armed=True)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except RebuildFailed as exc:
                self.on_fatal(exc)
            except Exception:
                log.exception("apply worker pass failed")

    def _default_fatal(self, exc: BaseException) -> None:
        log.critical("fatal: %s", exc)
        if threading.current_thread() is threading.main_thread():
            raise SystemExit(70)
        os._exit(70)

    def _do_restart(self) -> None:
        if self.on_restart is not None:
            self.restart_requested = False
            self.on_restart()
            return
        if self.machine == "speech":
            self.restart_requested = False
            os.kill(os.getpid(), signal.SIGTERM)
            return
        raise SystemExit(0)

    # ---- the drain
    def _rebuild_ok(self, armed: bool) -> bool:
        if self.machine == "speech":
            return True
        if not armed or self._session is not None:
            return False
        return self._mono() - self._last_turn_event >= self.settings().rebuild_quiet_s

    def _drain(self, *, armed: bool, live_only: bool = False) -> bool:
        while self._inbox:
            self._pending.append(self._inbox.popleft())
        if not self._pending:
            return False
        items, self._pending = self._pending, []
        remain: list[_Item] = []
        blocked: set[str] = set()
        rebuilt = False
        gate = None
        for it in items:
            if it.plugins & blocked:
                remain.append(it)
                blocked |= it.plugins
                continue
            if it.rebuild:
                if gate is None:
                    gate = (not live_only) and self._rebuild_ok(armed)
                if not gate:
                    remain.append(it)
                    blocked |= it.plugins
                    continue
            try:
                self._apply_item(it)
            except RebuildFailed as exc:
                self._pending = remain + self._pending
                self.on_fatal(exc)
                return rebuilt
            rebuilt = rebuilt or it.rebuild
        self._pending = remain + self._pending
        return rebuilt

    # ---- applying one change
    def _apply_item(self, it: _Item) -> None:
        rec = self.log.get(it.change_id) or {}
        error: Optional[str] = None
        restore: list[Callable[[], None]] = []
        restart_pending = False
        by_plugin: dict[str, dict[str, Any]] = {}
        old_by_plugin: dict[str, dict[str, Any]] = {}
        enabled_edits = []
        for e in it.edits:
            plugin, _, name = e["key"].rpartition(".")
            if name == "enabled":
                enabled_edits.append((plugin, e))
                continue
            by_plugin.setdefault(plugin, {})[name] = e["new"]
            old_by_plugin.setdefault(plugin, {})[name] = e["old"]
        try:
            # rebuild groups first: they are the ones that can fail, and nothing live has happened yet
            order = sorted(by_plugin, key=lambda p: 0 if any(
                self._knob(p, n).applies == "rebuild" for n in by_plugin[p]) else 1)
            for plugin in order:
                changed = by_plugin[plugin]
                classes = {n: self._knob(plugin, n).applies for n in changed}
                if any(c == "restart" for c in classes.values()):
                    restart_pending = True
                now_keys = {n: self._expanded(self.specs[plugin], n, v) for n, v in changed.items()
                            if classes[n] != "restart"}
                if not now_keys:
                    continue
                prev = self._cfgs[plugin]
                new = PluginConfig(plugin, {**prev, **now_keys}, it.rev_after)
                running = self._running(plugin)
                if running and any(c == "rebuild" for c in classes.values()):
                    err = self.registry.rebuild(plugin, new, prev)
                    if err:
                        raise _ApplyFailed(err)
                    self._cfgs[plugin] = new
                    restore.append(lambda p=plugin, n=new, pv=prev: self._safe_rebuild(p, pv, n))
                else:
                    old_vals = {k: self._expanded(self.specs[plugin], k, old_by_plugin[plugin][k])
                                for k in now_keys}
                    restore.append(lambda p=plugin, ch=old_vals, n=new, pv=prev: self._safe_live(p, ch, pv, n))
                    self._apply_live(plugin, now_keys, new, prev)
            for plugin, e in enabled_edits:
                spec = self.specs[plugin]
                if spec.toggle == "restart":
                    restart_pending = True
                    continue
                if e["new"]:
                    self.registry.enable(plugin)
                    restore.append(lambda p=plugin: self.registry.disable(p))
                else:
                    self.registry.disable(plugin)
                    restore.append(lambda p=plugin: self.registry.enable(p))
            if it.reload:
                prev = self._cfgs[it.reload]
                err = self.registry.rebuild(it.reload, prev, prev)
                if err:
                    raise _ApplyFailed(err)
        except RebuildFailed:
            self._mark_failed(it, rec, "rebuild failed and the previous settings could not be restored")
            raise
        except _ApplyFailed as exc:
            error = str(exc)
        except Exception as exc:
            log.exception("applying %s failed", it.change_id)
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
        if error is not None:
            for undo_step in reversed(restore):
                try:
                    undo_step()
                except Exception:
                    log.exception("could not restore after a failed apply")
            self._mark_failed(it, rec, error)
            return
        self._sync_args(by_plugin)
        status = "pending_restart" if restart_pending else "applied"
        with self._wlock:
            if (self.log.get(it.change_id) or {}).get("status") == "queued":
                self.log.status(it.change_id, status, at=self._wall())
        self._write_mirror()
        self.emit("setting.changed", change_id=it.change_id, keys=[e["key"] for e in it.edits], status=status)
        it.event.set()

    def _running(self, plugin: str) -> bool:
        """True when the plugin has a live object (or failed and should be tried again). A plugin that is
        disabled or waiting for its needs only takes the new snapshot: nothing to rebuild yet."""
        entry = self.registry.entries.get(plugin)
        return entry is not None and entry.state in (State.ACTIVE, State.FAILED)

    def _knob(self, plugin: str, name: str) -> Knob:
        for k in self.specs[plugin].knobs:
            if k.name == name:
                return k
        raise KeyError(f"{plugin}.{name}")

    def _apply_live(self, plugin: str, changed: dict, new: PluginConfig, prev: PluginConfig) -> None:
        entry = self.registry.entries.get(plugin)
        if entry is not None and entry.plugin is not None and entry.state == State.ACTIVE:
            entry.plugin.apply(dict(changed), new)
        self._cfgs[plugin] = new
        if entry is not None:
            entry.cfg = new
        for reader_name, reader in self.registry.entries.items():
            if reader_name == plugin or reader.plugin is None or reader.state != State.ACTIVE:
                continue
            hit = {f"{plugin}.{k}": v for k, v in changed.items() if f"{plugin}.{k}" in reader.spec.reads}
            if hit:
                reader.plugin.apply(hit, reader.cfg)

    def _safe_live(self, plugin: str, old_changed: dict, prev: PluginConfig, cur: PluginConfig) -> None:
        entry = self.registry.entries.get(plugin)
        if entry is not None and entry.plugin is not None and entry.state == State.ACTIVE:
            entry.plugin.apply(dict(old_changed), prev)
        self._cfgs[plugin] = prev
        if entry is not None:
            entry.cfg = prev

    def _safe_rebuild(self, plugin: str, prev: PluginConfig, cur: PluginConfig) -> None:
        self.registry.rebuild(plugin, prev, cur)
        self._cfgs[plugin] = prev

    def _sync_args(self, by_plugin: Mapping[str, Mapping[str, Any]]) -> None:
        if self.args is None:
            return
        for plugin, changed in by_plugin.items():
            spec = self.specs[plugin]
            for name in changed:
                knob = self._knob(plugin, name)
                if knob.applies == "restart":
                    continue
                attr = arg_attr(spec, knob)
                if not attr:
                    continue
                if self._have_overlay and plugin == "loop.turns" and name == "followup_s":
                    continue
                setattr(self.args, attr, self._cfgs[plugin][name])

    def _mark_failed(self, it: _Item, rec: dict, error: str) -> None:
        """The change failed: status failed, the previous values written back to the file."""
        log.error("change %s failed: %s", it.change_id, error)
        with self._wlock:
            self._trials.pop(it.change_id, None)
            # only keys that still hold this change's value: a later change that was persisted behind it keeps its
            # own (it will apply on top of the restored plugin, per-plugin FIFO)
            back = {e["key"]: e["old"] for e in it.edits
                    if not e["key"].endswith(".enabled") and self._current(e["key"]) == e["new"]}
            enabled = {e["key"].rpartition(".")[0]: e["old"] for e in it.edits
                       if e["key"].endswith(".enabled") and self._current(e["key"]) == e["new"]}
            try:
                if back or enabled:
                    self.store.write(back, enabled, history_keep=self.settings().history_keep)
            except Exception:
                log.exception("could not write the previous values back")
            self.log.status(it.change_id, "failed", error=error[:200], trial_expires=None, at=self._wall())
            self._recalc_due()
        self.emit("setting.changed", change_id=it.change_id, keys=[e["key"] for e in it.edits], status="failed")
        it.event.set()

    # ------------------------------------------------------------------------------------------
    # the one write path
    # ------------------------------------------------------------------------------------------
    def _current(self, key: str) -> Any:
        plugin, _, name = key.rpartition(".")
        if name == "enabled" and plugin in self.specs:
            return self.store.enabled(plugin)
        return self.store.value(key)

    def _in_brain(self) -> bool:
        t = self.turn
        return t is not None and t.phase == "brain"

    def _in_turn_for_policy(self) -> bool:
        # the speech machine has no turn: the Pi applied the voice-turn rule before forwarding (2.5.5 rule 4)
        return True if self.machine == "speech" else self._in_brain()

    def _voice_resolver(self):
        if self.machine != "speech":
            return None
        voices = self.registry.service("voices", None)
        if voices is None:
            return None
        return lambda v: voices.resolve(v)["id"]

    def _voice_ok(self, value: Any) -> bool:
        voices = self.registry.service("voices", None)
        if voices is None:
            return False
        try:
            return bool(voices.agent_allowed(voices.resolve(value)))
        except Exception:
            return False

    def _parse(self, edits: Sequence[Edit], scope: str) -> list[dict]:
        if not edits:
            raise ConfigError("there is nothing to change", "", "invalid")
        out: list[dict] = []
        seen: set[str] = set()
        resolver = self._voice_resolver()
        for edit in edits:
            key = edit.key
            if key in seen:
                raise ConfigError(f"{key} is listed twice", key)
            seen.add(key)
            plugin, _, name = key.rpartition(".")
            if name == "enabled":
                spec = self.store.spec_of(plugin)
                if spec.core:
                    raise PolicyError("policy", "That core part cannot be switched off.", field=plugin)
                new = coerce(Knob("enabled", "bool", True, "enabled"), edit.value, key)
                out.append({"key": key, "spec": spec, "knob": None, "old": self.store.enabled(plugin), "new": new})
                continue
            spec, knob = self.store.knob_of(key)
            if self.store.is_fixed(key):
                raise ConfigError(f"{key} is fixed by a command line flag for this run", key)
            raw = copy.deepcopy(knob.default) if edit.value is RESET else edit.value
            new = coerce(knob, raw, key, voice_resolver=resolver)
            out.append({"key": key, "spec": spec, "knob": knob, "old": self.store.value(key), "new": new})
        return out

    def _check_hard(self, items: list[dict]) -> None:
        """Hard bounds, choices, then the plugin's cross-field validate over the full candidate."""
        for it in items:
            if it["knob"] is not None:
                check_bounds(it["knob"], it["new"], it["key"])
        touched: dict[str, dict] = {}
        for it in items:
            if it["knob"] is None:
                continue
            plugin = it["spec"].name
            cand = touched.setdefault(plugin, self.store.values(plugin))
            cand[it["knob"].name] = it["new"]
        for plugin, cand in touched.items():
            hook = self.specs[plugin].validate
            if hook is not None:
                hook(cand)
        for it in items:
            if it["knob"] is None:
                spec = it["spec"]
                if it["new"]:
                    for service in spec.provides:
                        if service in MULTI_SERVICES:
                            continue
                        others = [n for n in self.registry.enabled_providers(service) if n != spec.name]
                        if others:
                            raise ConflictError("provider_conflict",
                                                f"two providers for {service!r}: "
                                                f"{', '.join(sorted(others + [spec.name]))}; set enabled=false on one")
                else:
                    self.registry.check_disable(spec.name)

    def _session_id_for(self, scope: str, session: Optional[str]) -> Optional[str]:
        if session:
            return session
        if scope == "agent" and self._session is not None:
            return self._session["id"]
        return None

    def submit(self, edits: Sequence[Edit], *, actor: str, scope: Scope, reason: str = "",
               trial_s: Optional[float] = None, expect_revision: Optional[int] = None,
               dry_run: bool = False, wait_s: float = 0.0, session: Optional[str] = None) -> Change:
        item: Optional[_Item] = None
        with self._wlock:
            items = self._parse(edits, scope)
            sess = self._session
            if scope == "agent":
                # policy before hard bounds: the refusal carries the agent range (addendum 1)
                self.policy.check_write(scope, items, in_turn=self._in_turn_for_policy(),
                                        session_changes=len(sess["changes"]) if sess else 0,
                                        voice_ok=self._voice_ok)
                self._check_hard(items)
            else:
                self._check_hard(items)
                self.policy.check_write(scope, items, in_turn=True)
            if expect_revision is not None and expect_revision != self.store.revision:
                raise ConflictError("stale_revision", "The settings changed since you looked.",
                                    current=self.store.revision, revision=self.store.revision)
            trial = self.policy.trial_for(scope, trial_s)
            rec_edits = [{"key": i["key"], "old": i["old"], "new": i["new"]} for i in items]
            applies = _applies_of([(i["knob"].applies if i["knob"] else i["spec"].toggle) for i in items])
            announce = self._announce(rec_edits, trial)
            now = self._wall()
            if dry_run:
                return Change.from_dict(self._public(dict(
                    id=self.log.peek_id(), ts=fmt_ts(now), machine=self.machine, actor=actor, scope=scope,
                    session=self._session_id_for(scope, session), reason=reason[:120], edits=rec_edits,
                    applies=applies, status="dry_run", rev_before=self.store.revision,
                    rev_after=self.store.revision, trial_expires=None, undo_of=None, error=None,
                    announce=announce)))
            file_edits = {i["key"]: i["new"] for i in items if i["knob"] is not None}
            enabled = {i["spec"].name: i["new"] for i in items if i["knob"] is None}
            rev = self.store.write(file_edits, enabled, history_keep=self.policy.settings.history_keep)
            sess_id = self._session_id_for(scope, session)
            if scope == "agent" and self._session is None and not session and self.machine == "listener":
                topic = next((i["knob"].topic for i in items if i["knob"] is not None and i["knob"].topic), "")
                sess_id = self._open_session(topic)["id"]
            change = self._new_change(rec_edits, actor=actor, scope=scope, reason=reason, session=sess_id,
                                      trial=trial, undo_of=None, rev=rev, status=None, announce=announce,
                                      applies=applies)
            item = self._enqueue(change, rec_edits)
            if scope == "agent":
                self.policy.record_write([e["key"] for e in rec_edits])
            if sess_id and self._session is not None and self._session["id"] == sess_id:
                self._session["changes"].append(change.id)
                self._touch_session()
            if trial:
                self._register_trial(change.id, now + trial, sess_id)
            self._after_commit()
        return self._result(change.id, item, wait_s)

    # ---- helpers of the write path (callers hold _wlock)
    def _new_change(self, rec_edits: list[dict], *, actor: str, scope: str, reason: str, session: Optional[str],
                    trial: Optional[float], undo_of: Optional[str], rev: tuple[int, int],
                    status: Optional[str], announce: str, applies: str) -> Change:
        now = self._wall()
        return Change(id=self.log.next_id(), ts=fmt_ts(now), machine=self.machine, actor=actor, scope=scope,  # type: ignore[arg-type]
                      session=session, reason=(reason or "")[:120], edits=rec_edits, applies=applies,
                      status=status or "queued", rev_before=rev[0], rev_after=rev[1],
                      trial_expires=fmt_ts(now + trial) if trial else None, undo_of=undo_of, error=None,
                      announce=announce)

    def _enqueue(self, change: Change, rec_edits: list[dict], reload: str = "") -> Optional[_Item]:
        """Append to the log and put on the apply queue. Pure restart changes (and every change while no process
        is running, as in the CLI's offline mode) settle at once."""
        plugins: set[str] = set()
        rebuild = bool(reload)
        needs_apply = bool(reload)
        for e in rec_edits:
            plugin, _, name = e["key"].rpartition(".")
            plugins.add(plugin)
            if name == "enabled":
                if self.specs[plugin].toggle == "live":
                    needs_apply = True
            else:
                cls = self._knob(plugin, name).applies
                if cls != "restart":
                    needs_apply = True
                if cls == "rebuild":
                    rebuild = True
        if reload:
            plugins.add(reload)
        if not needs_apply or not self._booted:
            change.status = "pending_restart" if change.applies == "restart" else "applied"
            self.log.append(change)
            self._write_mirror()
            return None
        self.log.append(change)
        item = _Item(change.id, rec_edits, plugins, rebuild, change.rev_after, reload)
        self._inbox.append(item)
        self._wake.set()
        return item

    def _result(self, change_id: str, item: Optional[_Item], wait_s: float) -> Change:
        if item is not None and wait_s > 0:
            item.event.wait(wait_s)
        return Change.from_dict(self._public(self.log.get(change_id) or {}))

    def _is_secret(self, key: str) -> bool:
        try:
            return self.store.knob_of(key)[1].secret
        except ConfigError:
            return False

    def _public(self, rec: dict) -> dict:
        """What leaves the process: the values of secret knobs (token file paths) become {"set": bool}. The
        change log itself keeps the real values (mode 0600) because undo needs them."""
        if not rec.get("edits"):
            return rec
        edits = []
        for e in rec["edits"]:
            if self._is_secret(e["key"]):
                e = {"key": e["key"], "old": {"set": bool(e["old"])}, "new": {"set": bool(e["new"])}}
            edits.append(e)
        return dict(rec, edits=edits)

    def _register_trial(self, change_id: str, due: float, session: Optional[str]) -> None:
        self._trials[change_id] = {"due": due, "session": session}
        self._recalc_due()

    def _after_commit(self) -> None:
        self._recalc_due()

    def _announce(self, edits: list[dict], trial_s: Optional[float], undo: bool = False) -> str:
        parts = []
        for e in edits:
            plugin, _, name = e["key"].rpartition(".")
            if name == "enabled":
                parts.append(f"{plugin} {'on' if e['new'] else 'off'}")
                continue
            try:
                knob = self._knob(plugin, name)
            except KeyError:
                continue
            spoken = spoken_name(knob)
            if knob.secret or isinstance(e["new"], (Mapping,)) or (isinstance(e["new"], str) and len(e["new"]) > 40):
                parts.append(spoken)
                continue
            unit = _UNITS.get(knob.unit, knob.unit) if knob.type in ("int", "float") else ""
            u = f" {unit}" if unit else ""
            if undo:
                parts.append(f"{spoken} back to {fmt_value(e['new'])}{u}")
            else:
                parts.append(f"{spoken} to {fmt_value(e['new'])}{u}, from {fmt_value(e['old'])}{u}")
        body = " and ".join(parts) or "nothing"
        text = f"I put {body}." if undo else f"I changed {body}"
        if undo:
            return text
        if trial_s:
            text += f", for {_minutes(trial_s)}"
        if any(self._restart_class(e["key"]) for e in edits):
            text += ", which takes effect after the next restart"
        return text + "."

    def _restart_class(self, key: str) -> bool:
        plugin, _, name = key.rpartition(".")
        try:
            if name == "enabled":
                return self.specs[plugin].toggle == "restart"
            return self._knob(plugin, name).applies == "restart"
        except KeyError:
            return False

    # ------------------------------------------------------------------------------------------
    # undo, confirm, trials
    # ------------------------------------------------------------------------------------------
    def _age(self, rec: Mapping[str, Any]) -> float:
        ts = parse_ts(rec.get("ts")) or 0.0
        return self._wall() - ts

    def _find(self, change_id: str) -> dict:
        rec = self.log.get(change_id)
        if rec is None:
            raise ConfigError(f"unknown change {change_id}", "change_id")
        return rec

    def _is_remote_id(self, ident: str) -> bool:
        return not ident.startswith(("L-" if self.machine == "listener" else "S-"))

    def _targets(self, change_id: Optional[str], session: Optional[str], scope: str) -> list[dict]:
        if session is not None:
            ids: list[str]
            sess = self._session
            if sess is not None and sess["id"] == session:
                ids = list(reversed(sess["changes"]))
            else:
                ids = [r["id"] for r in reversed(self.log.session_changes(session)) if not r.get("undo_of")]
            out = []
            for i in ids:
                if self._is_remote_id(i):
                    out.append({"id": i, "remote": True})
                    continue
                rec = self.log.get(i)
                if rec and rec.get("status") not in ("reverted", "confirmed", "failed"):
                    out.append(rec)
            return out
        if change_id:
            if self._is_remote_id(change_id):
                return [{"id": change_id, "remote": True}]
            return [self._find(change_id)]
        for rec in reversed(self.log.all()):
            if rec.get("scope") == scope and not rec.get("undo_of") and rec.get("status") in LIVE_STATUSES:
                return [rec]
        return []

    def undo(self, change_id: Optional[str] = None, *, actor: str, scope: Scope,
             session: Optional[str] = None, force: bool = False) -> list[Change]:
        if scope == "agent" and force:
            raise PolicyError("policy", "Only the owner may force an undo.")
        out: list[Change] = []
        with self._wlock:
            targets = self._targets(change_id, session, scope)
            work: dict[str, Any] = {}
            plan = []
            for rec in targets:
                if rec.get("remote"):
                    plan.append(rec)
                    continue
                self.policy.check_undo_target(scope, rec, force=force, age_s=self._age(rec))
                if rec.get("status") in ("reverted", "confirmed", "failed", "dry_run"):
                    raise ConflictError("undo_conflict", f"{rec['id']} is already {rec['status']}.",
                                        current=rec["status"], revision=self.store.revision)
                if not force:
                    for e in rec["edits"]:
                        have = work[e["key"]] if e["key"] in work else self._current(e["key"])
                        if have != e["new"]:
                            raise ConflictError("undo_conflict", "That setting has moved since this change.",
                                                current=have, revision=self.store.revision)
                for e in rec["edits"]:
                    work[e["key"]] = e["old"]
                plan.append(rec)
            slots: list[Any] = []
            for rec in plan:
                if rec.get("remote"):
                    slots.append(rec)
                    continue
                slots.append(self._do_undo(rec, actor=actor, scope=scope))
            self._after_commit()
        # the mini can be slow: its HTTP calls run with the lock released so the loop thread's safe_point and
        # timers never wait behind them
        for slot in slots:
            if isinstance(slot, Change):
                out.append(slot)
                continue
            got = self._remote_call("POST", "/v1/undo", {"change_id": slot["id"], "actor": actor})
            if isinstance(got, list):
                out.extend(c for c in (self._as_change(x) for x in got) if c)
        return out

    def _as_change(self, d: Any) -> Optional[Change]:
        try:
            return Change.from_dict(d)
        except Exception:
            return None

    def _do_undo(self, rec: dict, *, actor: str, scope: str) -> Change:
        """Persist and queue the inverse of one change (caller holds _wlock; conflicts already checked)."""
        inv = [{"key": e["key"], "old": e["new"], "new": e["old"]} for e in reversed(rec["edits"])]
        file_edits = {e["key"]: e["new"] for e in inv if not e["key"].endswith(".enabled")}
        enabled = {e["key"].rpartition(".")[0]: e["new"] for e in inv if e["key"].endswith(".enabled")}
        rev = self.store.write(file_edits, enabled, history_keep=self.settings().history_keep) \
            if inv else (self.store.revision, self.store.revision)
        change = self._new_change(inv, actor=actor, scope=scope, reason="", session=rec.get("session"),
                                  trial=None, undo_of=rec["id"], rev=rev, status=None,
                                  announce=self._announce(inv, None, undo=True), applies=rec["applies"])
        self._enqueue(change, inv)
        self.log.status(rec["id"], "reverted", trial_expires=None, at=self._wall())
        self._trials.pop(rec["id"], None)
        return Change.from_dict(self._public(self.log.get(change.id) or change.to_dict()))

    def confirm(self, change_id: Optional[str] = None, *, actor: str, scope: Scope,
                session: Optional[str] = None) -> list[Change]:
        self.policy.check_confirm(scope, in_turn=self._in_turn_for_policy())
        slots: list[Any] = []
        with self._wlock:
            for rec in self._targets(change_id, session, scope):
                if rec.get("remote"):
                    slots.append(rec)
                    continue
                self.policy.check_undo_target(scope, rec, force=False, age_s=self._age(rec))
                if rec.get("status") == "reverted":
                    raise ConflictError("undo_conflict", f"{rec['id']} is already reverted.",
                                        current="reverted", revision=self.store.revision)
                self.log.status(rec["id"], "confirmed", trial_expires=None, at=self._wall())
                self._trials.pop(rec["id"], None)
                slots.append(Change.from_dict(self._public(self.log.get(rec["id"]))))
            self._recalc_due()
        out: list[Change] = []
        for slot in slots:
            if isinstance(slot, Change):
                out.append(slot)
                continue
            got = self._remote_call("POST", "/v1/confirm", {"change_id": slot["id"], "actor": actor})
            if isinstance(got, list):
                out.extend(c for c in (self._as_change(x) for x in got) if c)
        return out

    def _remote_call(self, method: str, path: str, body: Optional[dict]) -> Any:
        client = self.remotes.get("speech")
        if client is None:
            return None
        try:
            return client.request(method, path, body)
        except Exception as exc:
            log.warning("speech machine call %s %s failed: %s", method, path, type(exc).__name__)
            return None

    # ---- timers
    def _recalc_due(self) -> None:
        due = _INF
        sess = self._session
        open_id = sess["id"] if sess else None
        for cid, t in self._trials.items():
            if t["session"] and t["session"] == open_id:
                continue
            due = min(due, t["due"])
        if sess is not None:
            due = min(due, self._sess_f.get("last_activity", 0.0) + self.policy.settings.session_idle_s,
                      self._sess_f.get("expires", _INF))
        self._next_due = due

    def _timers(self) -> None:
        now = self._wall()
        if now < self._next_due:
            return
        with self._wlock:
            sess = self._session
            if sess is not None:
                s = self.policy.settings
                if now >= self._sess_f.get("expires", _INF) or \
                        now >= self._sess_f.get("last_activity", now) + s.session_idle_s:
                    self._end_session("ended")
            open_id = self._session["id"] if self._session else None
            for cid, t in list(self._trials.items()):
                if t["due"] > now:
                    continue
                if t["session"] and t["session"] == open_id:
                    continue
                rec = self.log.get(cid)
                if rec is None or rec.get("status") not in LIVE_STATUSES:
                    self._trials.pop(cid, None)
                    continue
                if self.machine == "speech" and self._has_rebuild(rec):
                    wait = self.policy.settings.busy_defer_s - (self._mono() - self._last_busy)
                    if wait > 0:
                        t["due"] = now + wait
                        continue
                self._expire(rec)
            self._recalc_due()

    def _has_rebuild(self, rec: Mapping[str, Any]) -> bool:
        for e in rec.get("edits", []):
            if self._restart_class(e["key"]):
                continue
            plugin, _, name = e["key"].rpartition(".")
            if name != "enabled" and self._knob(plugin, name).applies == "rebuild":
                return True
        return False

    def _expire(self, rec: dict) -> None:
        """A due trial becomes an undo by the system. Later trials on the same keys go first (newest first) so
        the chain unwinds to the original value; a later confirmed or owner change leaves the value alone."""
        cid = rec["id"]
        self._trials.pop(cid, None)
        keys = {e["key"] for e in rec["edits"]}
        later = [r for r in reversed(self.log.all()) if r["id"] != cid and not r.get("undo_of")
                 and r.get("status") in LIVE_STATUSES and self.log.position(r["id"]) > self.log.position(cid)
                 and keys & {e["key"] for e in r["edits"]}]
        for r in later:
            if r["id"] in self._trials and r.get("trial_expires"):
                self._trials.pop(r["id"], None)
                self._do_undo_checked(r, actor="expiry")
        self._do_undo_checked(rec, actor="expiry")

    def _do_undo_checked(self, rec: dict, *, actor: str) -> None:
        for e in rec["edits"]:
            if self._current(e["key"]) != e["new"]:
                log.info("trial %s moved on; its expiry leaves %s alone", rec["id"], e["key"])
                self.log.status(rec["id"], rec["status"], trial_expires=None, at=self._wall())
                return
        self._do_undo(rec, actor=actor, scope="system")

    # ------------------------------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------------------------------
    def _open_session(self, topic: str) -> dict:
        s = self.policy.settings
        now = self._wall()
        turn = self.turn
        sid = self.log.next_id("V")
        self._session = {"id": sid, "state": "open", "topic": topic or "voice", "started": fmt_ts(now),
                         "expires": fmt_ts(now + s.session_max_s), "last_activity": fmt_ts(now), "changes": [],
                         "followup_s": s.session_followup_s, "turn": turn.id if turn else None}
        self._sess_f = {"expires": now + s.session_max_s, "last_activity": now}
        self._want_overlay = True
        self._recalc_due()
        return self._session

    def _touch_session(self) -> None:
        now = self._wall()
        self._sess_f["last_activity"] = now
        if self._session is not None:
            self._session["last_activity"] = fmt_ts(now)
        self._recalc_due()

    def _end_session(self, state: str) -> Optional[dict]:
        sess = self._session
        if sess is None:
            return None
        sess = dict(sess, state=state, changes=list(sess["changes"]))
        self._session = None
        self._sess_f = {}
        self._want_overlay = False
        for t in self._trials.values():
            if t["session"] == sess["id"]:
                t["session"] = None
        self._recalc_due()
        return sess

    def session(self, action: Literal["start", "get", "keep", "revert", "end"], *, actor: str, scope: Scope,
                topic: str = "") -> Optional[dict]:
        with self._wlock:
            if action == "get":
                s = self._session
                return dict(s, changes=list(s["changes"])) if s else None
            self.policy.check_session(action, scope, in_turn=self._in_turn_for_policy())
            if action == "start":
                s = self._session or self._open_session(topic)
                self._touch_session()
                return dict(s, changes=list(s["changes"]))
            sess = self._session
            if sess is None:
                return None
            sid = sess["id"]
            if action == "end":
                return self._end_session("ended")
        # keep and revert may call the mini: run them without the lock (confirm and undo take it only for the
        # local bookkeeping), then close the session if it is still the same one
        if action == "keep":
            self.confirm(session=sid, actor=actor, scope=scope)
            state = "kept"
        else:
            try:
                self.undo(session=sid, actor=actor, scope=scope, force=False)
            except ConflictError:
                # something moved on inside the session: the owner's value wins, revert what still matches
                self._revert_session_leniently(sid, actor, scope)
            state = "reverted"
        with self._wlock:
            if self._session is not None and self._session["id"] == sid:
                return self._end_session(state)
        return None

    def _revert_session_leniently(self, sid: str, actor: str, scope: str) -> None:
        remote_ids: list[str] = []
        with self._wlock:
            for rec in self._targets(None, sid, scope):
                if rec.get("remote"):
                    remote_ids.append(rec["id"])
                    continue
                if all(self._current(e["key"]) == e["new"] for e in rec["edits"]):
                    self._do_undo(rec, actor=actor, scope=scope)
        for cid in remote_ids:
            self._remote_call("POST", "/v1/undo", {"change_id": cid, "actor": actor})

    def attach_remote_change(self, change: dict) -> None:
        with self._wlock:
            sess = self._session
            if sess is None or not isinstance(change, Mapping) or not change.get("id"):
                return
            if change["id"] not in sess["changes"]:
                sess["changes"].append(change["id"])
            self._touch_session()

    def _apply_overlay(self) -> None:
        want = self._want_overlay
        if want is self._have_overlay:
            return
        self._have_overlay = want
        spec = self.specs.get("loop.turns")
        if spec is None or self.args is None:
            return
        knob = next((k for k in spec.knobs if k.name == "followup_s"), None)
        if knob is None:
            return
        attr = arg_attr(spec, knob)
        value = self.policy.settings.session_followup_s if want else self._cfgs["loop.turns"]["followup_s"]
        setattr(self.args, attr, value)

    # ------------------------------------------------------------------------------------------
    # previews (2.6)
    # ------------------------------------------------------------------------------------------
    def preview(self, *, actor: str, scope: Scope, text: str = "", voice: str = "", speed: float = 0.0,
                peak: float = 0.0, wait_s: float = 15.0) -> dict:
        if self.machine == "speech":
            raise ConfigError("the speech machine never plays sound; preview from the wall", "preview")
        in_brain = self._in_brain()
        self.policy.check_preview(scope, in_turn=in_brain, now_local=datetime.fromtimestamp(self._wall()))
        s = self.policy.settings
        cfg_voice = self._cfgs["tts.remote"]["voice"] if "tts.remote" in self._cfgs else ""
        alias = voice or cfg_voice or "my voice"
        sample = (text or s.preview_text).replace("{alias}", alias)
        if len(sample) > s.preview_max_chars:
            raise ConfigError(f"the sample is longer than {s.preview_max_chars} characters", "text")
        tts = self.registry.service("tts", None)
        playback = self.registry.service("playback", None)
        reporter = self.registry.service("reporter", None)
        if tts is None or playback is None or reporter is None:
            raise ConfigError("a preview needs the tts, playback and reporter parts running", "preview",
                              "missing_service")
        if scope == "agent":
            self.policy.record_preview()
        try:
            wav = tts.render_preview(sample, voice, float(speed or 0.0), float(peak or 0.0))
        except Exception as exc:
            return {"played": False, "error": type(exc).__name__, "voice": voice or alias}
        job = _Job(wav, alias, voice or alias)
        if in_brain:
            res = self._play_inline(job, playback, reporter)
            if res is not None:
                return res
        with self._dlock:
            self._deferred.append(job)
        self._wake.set()
        if wait_s > 0:
            job.event.wait(wait_s)
        return job.result or {"queued": True, "voice": job.voice}

    def _play_inline(self, job: _Job, playback: Any, reporter: Any) -> Optional[dict]:
        t0 = self._mono()
        with self._inline_lock:
            if not self._in_brain():
                return None     # the turn ended while the sample rendered: queue it for an armed safe point
            with playback.lock:
                self._play(job, playback, reporter, restore_state="thinking")
        self.pending_flush = True
        return {"played": True, "ms": int((self._mono() - t0) * 1000), "voice": job.voice}

    def _play(self, job: _Job, playback: Any, reporter: Any, *, restore_state: str) -> None:
        prev = reporter.last
        heard = prev[1] if prev else ""
        reporter.update("speaking", heard, f"Voice preview: {job.alias}")
        fd, tmp = tempfile.mkstemp(prefix="mira-preview-", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(job.wav)
            playback.call("duck")
            try:
                playback.say_file(tmp, job.wav, reporter.set_level)
            finally:
                playback.call("unduck")
        finally:
            try:
                if prev:
                    reporter.update(*prev)
                else:
                    reporter.update(restore_state)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _run_deferred(self) -> bool:
        with self._dlock:
            jobs, self._deferred = self._deferred, []
        playback = self.registry.service("playback", None)
        reporter = self.registry.service("reporter", None)
        played = False
        for job in jobs:
            t0 = self._mono()
            try:
                if playback is None or reporter is None:
                    raise RuntimeError("no playback")
                with playback.lock:
                    self._play(job, playback, reporter, restore_state="idle")
                job.result = {"played": True, "ms": int((self._mono() - t0) * 1000), "voice": job.voice}
                played = True
            except Exception as exc:
                log.exception("queued preview failed")
                job.result = {"played": False, "error": type(exc).__name__, "voice": job.voice}
            job.event.set()
        return played

    # ------------------------------------------------------------------------------------------
    # owner operations
    # ------------------------------------------------------------------------------------------
    def set_enabled(self, plugin: str, enabled: bool, *, actor: str, scope: Scope) -> Change:
        self.policy.need_owner(scope, plugin)
        return self.submit([Edit(f"{plugin}.enabled", bool(enabled))], actor=actor, scope=scope)

    def reload_plugin(self, plugin: str, *, actor: str, scope: Scope) -> Change:
        self.policy.need_owner(scope, plugin)
        spec = self.store.spec_of(plugin)
        if spec.core:
            raise PolicyError("policy", "That core part cannot be reloaded.", field=plugin)
        with self._wlock:
            if plugin not in self.registry.entries:
                raise ConfigError(f"unknown plugin {plugin}", plugin, "unknown_plugin")
            rev = (self.store.revision, self.store.revision)
            change = self._new_change([], actor=actor, scope=scope, reason="", session=None, trial=None,
                                      undo_of=None, rev=rev, status=None, announce=f"I reloaded {plugin}.",
                                      applies="rebuild")
            item = self._enqueue(change, [], reload=plugin)
        return self._result(change.id, item, 0.0)

    def reload_file(self, *, actor: str, scope: Scope) -> Change:
        """Validate the file on disk and apply the difference as one batch (actor "file")."""
        self.policy.need_owner(scope, "file")
        if self.store.path is None:
            raise ConfigError("there is no settings file to reload", "file")
        with self._wlock:
            doc, digest = self.store.read_candidate()
            changes, enabled = self.store.diff_with(doc)
            rec_edits = [{"key": k, "old": o, "new": n} for k, (o, n) in changes.items()]
            rec_edits += [{"key": f"{p}.enabled", "old": o, "new": n} for p, (o, n) in enabled.items()]
            for e in rec_edits:
                if not e["key"].endswith(".enabled"):
                    check_bounds(self.store.knob_of(e["key"])[1], e["new"], e["key"])
            self.store.adopt_disk(doc, digest)
            rev = self.store.write({}, None, history_keep=self.settings().history_keep)
            applies = _applies_of([self._restart_class(e["key"]) and "restart" or
                                   ("rebuild" if self._rebuild_class(e["key"]) else "live") for e in rec_edits])
            change = self._new_change(rec_edits, actor="file", scope=scope, reason="settings file reloaded",
                                      session=None, trial=None, undo_of=None, rev=rev, status=None,
                                      announce="I reloaded the settings file.", applies=applies)
            item = self._enqueue(change, rec_edits)
            self._after_commit()
        return self._result(change.id, item, 0.0)

    def _rebuild_class(self, key: str) -> bool:
        plugin, _, name = key.rpartition(".")
        try:
            return name != "enabled" and self._knob(plugin, name).applies == "rebuild"
        except KeyError:
            return False

    def request_restart(self, *, actor: str, scope: Scope) -> dict:
        self.policy.need_owner(scope, "restart")
        d = self.store.cfgdir
        if d is not None:
            try:
                (d / "state").mkdir(parents=True, exist_ok=True)
                (d / "state" / "planned").write_text(f"{int(self._wall())}\n", encoding="utf-8")
            except OSError:
                log.exception("could not write the planned-restart marker")
        self.restart_requested = True
        self._wake.set()
        return {"ok": True, "at": fmt_ts(self._wall())}

    def register_remote(self, machine: str, client: RemoteClient) -> None:
        self.remotes[machine] = client
        self._remote_cache.update({"schema": None, "rev": None})

    def remote(self, machine: str = "speech") -> Optional[RemoteClient]:
        return self.remotes.get(machine)

    # ------------------------------------------------------------------------------------------
    # reads (any thread; snapshots only)
    # ------------------------------------------------------------------------------------------
    def _pending_restart_keys(self) -> list[str]:
        out: list[str] = []
        for name, spec in self.specs.items():
            applied = self._cfgs.get(name)
            if applied is None:
                continue
            for k in spec.knobs:
                if k.applies != "restart":
                    continue
                want = self._expanded(spec, k.name, self.store.value(f"{name}.{k.name}"))
                if want != applied[k.name]:
                    out.append(f"{name}.{k.name}")
            entry = self.registry.entries.get(name)
            if entry is not None and spec.toggle == "restart" and entry.enabled != self.store.enabled(name):
                out.append(f"{name}.enabled")
        return sorted(out)

    def _queued_keys(self) -> set[str]:
        keys: set[str] = set()
        for it in list(self._pending) + list(self._inbox):
            keys.update(e["key"] for e in it.edits)
        return keys

    def status(self) -> dict:
        turn = self.turn
        mute = self.registry.service("mute", None)
        sess = self._session
        failed = sorted(n for n, e in self.registry.entries.items() if e.state == State.FAILED)
        return {
            "ok": not failed and self.store.config_error is None, "machine": self.machine, "engine": "plugins",
            "version": self.version, "revision": self.store.revision,
            "uptime_s": round(self._mono() - self._started_mono, 1),
            "loop": "turn" if turn else ("armed" if self._armed else "followup"),
            "turn": {"id": turn.id, "phase": turn.phase, "since_s": round(self._mono() - turn.started, 1)}
            if turn else None,
            "mic": getattr(mute, "state", None) if mute is not None else None,
            "failed": failed, "pending_restart": self._pending_restart_keys(),
            "session": dict(sess, changes=list(sess["changes"])) if sess else None,
            "agents_locked": bool(self.policy.settings.agents_locked), "config_error": self.store.config_error,
            "speech": self._speech_status(),
        }

    def _speech_status(self) -> dict:
        if self.machine == "speech":
            tts = self.registry.service("tts", None)
            stt = self.registry.service("stt", None)
            return {"reachable": True, "engine": "plugins", "stt": str(getattr(stt, "kind", "")),
                    "voice": str(getattr(tts, "voice_id", ""))}
        client = self.remotes.get("speech")
        base = {"reachable": False, "engine": "", "stt": "", "voice": ""}
        if client is None:
            return base
        cache = self._remote_cache
        if self._mono() - cache["status_at"] < 5.0 and cache["status"] is not None:
            return cache["status"]
        try:
            st = client.request("GET", "/v1/status")
            base = {"reachable": True, "engine": str(st.get("engine", "")),
                    "stt": str((st.get("speech") or {}).get("stt", "")),
                    "voice": str((st.get("speech") or {}).get("voice", ""))}
        except Exception:
            pass
        cache.update({"status": base, "status_at": self._mono()})
        return base

    def inventory(self) -> list[dict]:
        pr = self._pending_restart_keys()
        rows = []
        for name, entry in self.registry.entries.items():
            keys = [k.rpartition(".")[2] for k in pr if k.rpartition(".")[0] == name]
            rows.append(self.registry.row(entry, keys))
        return rows

    def _knob_row(self, spec: PluginSpec, knob: Knob, scope: str, queued: set[str], restart: set[str]) -> dict:
        key = f"{spec.name}.{knob.name}"
        value, source = self.store.get(key)
        value = copy.deepcopy(value)
        default = knob.default
        if self._session is not None and key == "loop.turns.followup_s":
            value, source = self.policy.settings.session_followup_s, "session"
        lo, hi = knob.min, knob.max
        if scope == "agent" and knob.agent == "bounded" and knob.type in ("int", "float"):
            lo, hi = knob.agent_min, knob.agent_max
        if knob.secret:
            value, default = {"set": bool(value)}, {"set": bool(default)}
        elif isinstance(default, tuple):
            default = list(default)
        return {"key": knob.name, "type": knob.type, "value": value, "default": default, "source": source,
                "min": lo, "max": hi, "unit": knob.unit, "applies": knob.applies, "agent": knob.agent,
                "agent_min": knob.agent_min, "agent_max": knob.agent_max, "agent_step": knob.agent_step,
                "settable_by_voice": knob.agent == "bounded", "description": knob.description,
                "spoken": spoken_name(knob), "effect": knob.effect, "topic": knob.topic,
                "pending": "restart" if key in restart else ("queued" if key in queued else None),
                "secret": knob.secret}

    def describe(self, plugin: str, *, scope: Scope) -> dict:
        spec = self.store.spec_of(plugin)
        queued, restart = self._queued_keys(), set(self._pending_restart_keys())
        entry = self.registry.entries.get(plugin)
        return {
            "spec": {"name": spec.name, "kind": spec.kind, "version": spec.version, "summary": spec.summary,
                     "machine": spec.machine, "provides": list(spec.provides), "needs": list(spec.needs),
                     "wants": list(spec.wants), "reads": list(spec.reads), "required": spec.required,
                     "core": spec.core, "toggle": spec.toggle,
                     "enabled": entry.enabled if entry else True},
            "knobs": [self._knob_row(spec, k, scope, queued, restart) for k in spec.knobs],
            "revision": self.store.revision,
        }

    def schema(self) -> dict:
        return {"machine": self.machine, "contract_version": CONTRACT_VERSION, "revision": self.store.revision,
                "plugins": [spec_to_json(s) for n, s in self.specs.items() if n != "core"]}

    def config(self, *, effective: bool = True) -> dict:
        """effective=True: every key is {"value", "source"}; False: the bare effective values. Secrets show only
        {"set": bool}. The core pseudo-plugin fills "core"."""
        out: dict[str, Any] = {"revision": self.store.revision, "core": {}, "plugins": {}}
        for name, spec in self.specs.items():
            body: dict[str, Any] = {}
            for k in spec.knobs:
                value, source = self.store.get(f"{name}.{k.name}")
                value = {"set": bool(value)} if k.secret else copy.deepcopy(value)
                body[k.name] = {"value": value, "source": source} if effective else value
            if name == "core":
                out["core"] = body
            else:
                out["plugins"][name] = {"enabled": self.store.enabled(name), "config": body}
        return out

    def changes(self, *, limit: int = 20, since: Optional[str] = None, session: Optional[str] = None,
                actor: Optional[str] = None) -> list[dict]:
        rows = self.log.all()
        if since is not None:
            pos = self.log.position(since)
            if pos is not None:
                rows = rows[pos + 1:]
        if session is not None:
            rows = [r for r in rows if r.get("session") == session]
        if actor is not None:
            rows = [r for r in rows if r.get("actor") == actor]
        return [self._public(r) for r in reversed(rows)][:max(0, limit)]

    # ---- tuning guide
    def _remote_knobs(self) -> tuple[dict[str, Knob], dict[str, Any]]:
        client = self.remotes.get("speech")
        if client is None or self.machine == "speech":
            return {}, {}
        cache = self._remote_cache
        try:
            cfg = client.request("GET", "/v1/config?effective=1")
            rev = cfg.get("revision")
            if cache["schema"] is None or cache["rev"] != rev:
                cache["schema"], cache["rev"] = client.request("GET", "/v1/schema"), rev
        except Exception:
            if cache["schema"] is None:
                return {}, {}
            cfg = {}
        knobs: dict[str, Knob] = {}
        for p in (cache["schema"] or {}).get("plugins", []):
            for kd in p.get("knobs", []):
                try:
                    d = dict(kd)
                    d["choices"] = tuple(d["choices"]) if d.get("choices") else None
                    d["phrases"] = tuple(d.get("phrases") or ())
                    knobs[f"{p['name']}.{d['name']}"] = Knob(**d)
                except Exception:
                    continue
        values: dict[str, Any] = {}
        merged = dict((cfg.get("core") or {}))
        for name, body in (cfg.get("plugins") or {}).items():
            for k, v in (body.get("config") or {}).items():
                merged[f"{name}.{k}"] = v
        for k, v in merged.items():
            values[k] = v["value"] if isinstance(v, Mapping) and "value" in v and "source" in v else v
        return knobs, values

    def tuning_guide(self, topic: str, *, scope: Scope) -> dict:
        topics = ("", "voice", "hearing", "timing", "wake", "brain", "wall", "system")
        if topic not in topics:
            raise ConfigError(f"unknown topic {topic}", "topic")
        pool: list[tuple[str, Knob, Any]] = []
        for name, spec in self.specs.items():
            for k in spec.knobs:
                if k.topic == topic:
                    pool.append((f"{name}.{k.name}", k, self.store.value(f"{name}.{k.name}")))
        rk, rv = self._remote_knobs()
        for key, k in rk.items():
            if k.topic == topic:
                pool.append((key, k, rv.get(key, k.default)))
        knobs, owner_only = [], []
        for key, k, value in pool:
            if k.agent == "bounded":
                knobs.append({"key": key, "spoken": spoken_name(k), "value": value, "unit": k.unit,
                              "settable_by_voice": True, "agent_min": k.agent_min, "agent_max": k.agent_max,
                              "agent_step": k.agent_step, "ladder": ladder(k), "effect": k.effect,
                              "phrases": list(k.phrases), "applies": k.applies})
            else:
                owner_only.append({"key": key, "spoken": spoken_name(k),
                                   "why": "Only the owner may change this setting.",
                                   "owner_command": _owner_cmd(key, value, k.secret)})
        return {"topic": topic, "knobs": knobs, "owner_only": owner_only}


class _ApplyFailed(Exception):
    pass


def _owner_cmd(key: str, value: Any, secret: bool = False) -> str:
    from .policy import owner_command
    return owner_command(key, value, secret=secret)
