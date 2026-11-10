"""ConfigStore: the layered configuration of one machine (spec 2.4.1, 2.5.1 step 7).

Layers, highest first: flags explicitly present on argv (and in-memory overrides such as `--wav`), the file,
the legacy MIRA_* environment (only while core.legacy_env == "fallback"), the schema default.

The store is the persisted truth. It knows nothing about plugins that are running; the core keeps the applied
per-plugin snapshots separately. All file IO happens under an flock on `<file>.lock`, writes are atomic
(tmp, fsync, os.replace) and the previous file is copied to `history/` first.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .plugin import ConfigError, ConflictError, PluginSpec
from .schema import closest, core_spec, defaults, validate_knob, validate_plugin

log = logging.getLogger("mira.store")

FORMAT = 1
# {(format, "old.key"): "new.key"}: renamed keys of older files. Empty in format 1.
MIGRATIONS: dict[tuple[int, str], str] = {}


class StoreError(ConfigError):
    """The file cannot be used and there is no usable last-good copy."""


class ConfigStore:
    def __init__(self, specs: Sequence[PluginSpec], machine: str, *, path: Optional[Path] = None,
                 argv: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, Any]] = None,
                 enabled_override: Optional[Mapping[str, bool]] = None,
                 values: Optional[Mapping[str, Any]] = None) -> None:
        """`values` seeds the file layer of an in-memory store (from_args). `argv` and `env` are "plugin.key"
        dicts already coerced (see envmap)."""
        self.machine = machine
        self.path = Path(path) if path is not None else None
        self.specs: dict[str, PluginSpec] = {}
        for spec in specs:
            self.specs[spec.name] = spec
        if "core" not in self.specs:
            self.specs["core"] = core_spec(machine)
        self.argv = dict(argv or {})
        self.env = dict(env or {})
        self.enabled_override = dict(enabled_override or {})
        self.lock = threading.RLock()
        self.doc: dict[str, Any] = {"format": FORMAT, "machine": machine, "revision": 1 if path is None else 0,
                                    "core": {}, "plugins": {}}
        if values:
            for key, value in values.items():
                plugin, _, name = key.rpartition(".")
                self._file_layer(plugin)[name] = value
        self.hash: Optional[str] = None
        self.config_error: Optional[str] = None
        self.loaded_from_lastgood = False

    # ---- locations
    @property
    def in_memory(self) -> bool:
        return self.path is None

    @property
    def cfgdir(self) -> Optional[Path]:
        return self.path.parent if self.path else None

    def sibling(self, name: str) -> Optional[Path]:
        return (self.path.parent / name) if self.path else None

    @property
    def revision(self) -> int:
        return int(self.doc.get("revision", 0))

    # ---- structure helpers
    def _file_layer(self, plugin: str) -> dict:
        if plugin == "core":
            return self.doc.setdefault("core", {})
        entry = self.doc.setdefault("plugins", {}).setdefault(plugin, {})
        return entry.setdefault("config", {})

    def spec_of(self, plugin: str) -> PluginSpec:
        try:
            return self.specs[plugin]
        except KeyError:
            near = closest(plugin, list(self.specs))
            hint = f"; the closest plugin is {near}" if near else ""
            raise ConfigError(f"unknown plugin {plugin}{hint}", plugin, "unknown_plugin") from None

    def knob_of(self, key: str):
        plugin, _, name = key.rpartition(".")
        spec = self.spec_of(plugin) if plugin else None
        if spec is None:
            raise ConfigError(f"unknown key {key}", key, "unknown_key")
        for k in spec.knobs:
            if k.name == name:
                return spec, k
        names = [f"{plugin}.{k.name}" for k in spec.knobs]
        near = closest(key, names)
        hint = f"; the closest key is {near}" if near else ""
        raise ConfigError(f"unknown key {key}{hint}", key, "unknown_key")

    # ---- reads
    def legacy_env_active(self) -> bool:
        value = self.doc.get("core", {}).get("legacy_env")
        return (value if value is not None else "fallback") == "fallback"

    def get(self, key: str) -> tuple[Any, str]:
        """(value, source) with source in argv, file, env, default."""
        spec, knob = self.knob_of(key)
        if key in self.argv:
            return self.argv[key], "argv"
        layer = self.doc.get("core", {}) if spec.name == "core" else \
            self.doc.get("plugins", {}).get(spec.name, {}).get("config", {})
        if knob.name in layer:
            return layer[knob.name], "file"
        if key in self.env and (spec.name == "core" or self.legacy_env_active()):
            return self.env[key], "env"
        d = knob.default
        return (list(d) if isinstance(d, (list, tuple)) else dict(d) if isinstance(d, Mapping) else d), "default"

    def value(self, key: str) -> Any:
        return copy.deepcopy(self.get(key)[0])

    def source(self, key: str) -> str:
        return self.get(key)[1]

    def values(self, plugin: str) -> dict[str, Any]:
        spec = self.spec_of(plugin)
        return {k.name: copy.deepcopy(self.get(f"{plugin}.{k.name}")[0]) for k in spec.knobs}

    def sources(self, plugin: str) -> dict[str, str]:
        spec = self.spec_of(plugin)
        return {k.name: self.get(f"{plugin}.{k.name}")[1] for k in spec.knobs}

    def effective(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, spec in self.specs.items():
            for k in spec.knobs:
                out[f"{name}.{k.name}"] = self.get(f"{name}.{k.name}")[0]
        return out

    def enabled(self, plugin: str) -> bool:
        spec = self.spec_of(plugin)
        if plugin in self.enabled_override:
            return self.enabled_override[plugin]
        if spec.core:
            return True
        entry = self.doc.get("plugins", {}).get(plugin, {})
        return bool(entry["enabled"]) if "enabled" in entry else spec.default_enabled

    def is_fixed(self, key: str) -> bool:
        """True when a flag or override above the file decides this key, so a write could not take effect."""
        return key in self.argv

    # ---- validation of a document (file content)
    def _migrate(self, doc: dict) -> dict:
        fmt = int(doc.get("format", FORMAT))
        if not MIGRATIONS:
            return doc
        for (f, old), new in MIGRATIONS.items():
            if f != fmt:
                continue
            op, _, ok = old.rpartition(".")
            np_, _, nk = new.rpartition(".")
            src = doc.get("core", {}) if op == "core" else doc.get("plugins", {}).get(op, {}).get("config", {})
            if ok in src:
                val = src.pop(ok)
                if np_ == "core":
                    doc.setdefault("core", {})[nk] = val
                else:
                    doc.setdefault("plugins", {}).setdefault(np_, {}).setdefault("config", {})[nk] = val
        return doc

    def check_doc(self, raw: Any) -> dict:
        """Validate a parsed file and return a canonical copy (values coerced). Raises ConfigError naming the
        offender and the closest known name. Cross-field `validate` hooks run on file values over defaults."""
        if not isinstance(raw, dict):
            raise ConfigError("the file must hold one JSON object", "")
        doc = self._migrate(copy.deepcopy(raw))
        if doc.get("format", FORMAT) != FORMAT:
            raise ConfigError(f"format {doc.get('format')!r} is not supported", "format")
        if doc.get("machine", self.machine) != self.machine:
            raise ConfigError(f"this file is for machine {doc.get('machine')!r}, not {self.machine!r}", "machine")
        rev = doc.get("revision", 0)
        if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
            raise ConfigError("revision must be a whole number", "revision")
        out: dict[str, Any] = {k: v for k, v in doc.items() if k.startswith("$")}
        out.update({"format": FORMAT, "machine": self.machine, "revision": rev, "core": {}, "plugins": {}})
        core_vals = doc.get("core", {})
        if not isinstance(core_vals, dict):
            raise ConfigError("core must be an object", "core")
        spec = self.specs["core"]
        out["core"] = {k: v for k, v in core_vals.items() if k.startswith("$")}
        out["core"].update(self._coerce_values(spec, core_vals))
        plugins = doc.get("plugins", {})
        if not isinstance(plugins, dict):
            raise ConfigError("plugins must be an object", "plugins")
        for name, entry in plugins.items():
            if name.startswith("$"):
                out["plugins"][name] = entry
                continue
            pspec = self.spec_of(name)
            if not isinstance(entry, dict):
                raise ConfigError(f"{name} must be an object", name)
            new: dict[str, Any] = {k: v for k, v in entry.items() if k.startswith("$")}
            if "enabled" in entry:
                if not isinstance(entry["enabled"], bool):
                    raise ConfigError(f"{name}.enabled must be true or false", f"{name}.enabled")
                new["enabled"] = entry["enabled"]
            cfg = entry.get("config", {})
            if not isinstance(cfg, dict):
                raise ConfigError(f"{name}.config must be an object", name)
            new["config"] = {k: v for k, v in cfg.items() if k.startswith("$")}
            new["config"].update(self._coerce_values(pspec, cfg))
            out["plugins"][name] = new
        # cross-field hooks over file values plus defaults; the core checks the effective config again at boot
        for name in [n for n in out["plugins"] if not n.startswith("$")] + ["core"]:
            pspec = self.specs[name]
            if pspec.validate is not None:
                layer = out["core"] if name == "core" else out["plugins"][name].get("config", {})
                cand = defaults(pspec)
                cand.update({k: v for k, v in layer.items() if not k.startswith("$")})
                pspec.validate(cand)
        return out

    def _coerce_values(self, spec: PluginSpec, values: Mapping[str, Any]) -> dict[str, Any]:
        by_name = {k.name: k for k in spec.knobs}
        out: dict[str, Any] = {}
        for name, raw in values.items():
            if name.startswith("$"):
                continue
            key = f"{spec.name}.{name}"
            knob = by_name.get(name)
            if knob is None:
                near = closest(name, list(by_name))
                hint = f"; the closest key is {spec.name}.{near}" if near else ""
                raise ConfigError(f"unknown key {key}{hint}", key, "unknown_key")
            out[name] = validate_knob(knob, raw, key)
        return out

    # ---- loading
    @staticmethod
    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _read_disk(self, path: Path) -> tuple[dict, str]:
        data = path.read_bytes()
        try:
            raw = json.loads(data.decode("utf-8"))
        except ValueError as exc:
            raise ConfigError(f"the file is not valid JSON: {exc}", "") from None
        return self.check_doc(raw), self.sha(data)

    def load(self, *, use_lastgood: bool = True) -> None:
        """Read the file. An invalid file falls back to `<file>.lastgood` with `config_error` set; with no
        usable fallback a StoreError is raised (the launcher exits 78). A missing file is an empty sparse one."""
        assert self.path is not None
        path = self.path
        if not path.exists():
            self.doc = {"format": FORMAT, "machine": self.machine, "revision": 0, "core": {}, "plugins": {}}
            self.hash = None
            return
        try:
            self.doc, self.hash = self._read_disk(path)
            return
        except (ConfigError, OSError) as exc:
            message = exc.message if isinstance(exc, ConfigError) else str(exc)
            problem = f"{path.name}: {message}"
        log.error("config_error: %s", problem)
        good = Path(str(path) + ".lastgood")
        if use_lastgood and good.exists():
            try:
                doc, _h = self._read_disk(good)
                self.doc = doc
                self.hash = self.sha(path.read_bytes()) if path.exists() else None
                self.config_error = problem
                self.loaded_from_lastgood = True
                return
            except (ConfigError, OSError) as exc2:
                log.error("config_error: the last-good file is unusable too: %s", exc2)
        raise StoreError(f"no usable configuration: {problem}", "", "invalid")

    def use_lastgood(self, problem: str) -> bool:
        """Switch to `<file>.lastgood` when the file loaded fine but the effective config failed a later check
        (provider conflict, cross-field rule). False when there is no usable last-good copy."""
        if self.path is None or self.loaded_from_lastgood:
            return False
        good = Path(str(self.path) + ".lastgood")
        try:
            doc, _h = self._read_disk(good)
        except (ConfigError, OSError):
            return False
        self.doc, self.config_error, self.loaded_from_lastgood = doc, problem, True
        return True

    def read_candidate(self) -> tuple[dict, str]:
        """The file as it is on disk now, validated, and its hash. For `mira config reload`."""
        assert self.path is not None
        return self._read_disk(self.path)

    def adopt_disk(self, doc: dict, digest: str) -> None:
        with self.lock:
            self.doc, self.hash, self.config_error = doc, digest, None

    # ---- diff for reload and edit
    def diff_with(self, doc: dict) -> tuple[dict[str, tuple[Any, Any]], dict[str, tuple[bool, bool]]]:
        """Effective differences if `doc` replaced the file layer: ({key: (old, new)}, {plugin: (old, new)}).
        A knob held by a flag does not change however the file moves."""
        other = ConfigStore(list(self.specs.values()), self.machine, argv=self.argv, env=self.env,
                            enabled_override=self.enabled_override)
        other.doc = doc
        changes: dict[str, tuple[Any, Any]] = {}
        for key, old in self.effective().items():
            new = other.get(key)[0]
            if new != old:
                changes[key] = (old, new)
        enabled: dict[str, tuple[bool, bool]] = {}
        for name in self.specs:
            if self.enabled(name) != other.enabled(name):
                enabled[name] = (self.enabled(name), other.enabled(name))
        return changes, enabled

    # ---- writes
    def stage(self, edits: Mapping[str, Any], enabled: Optional[Mapping[str, bool]] = None) -> dict:
        """A copy of the file layer with the edits laid in (nothing persisted)."""
        doc = copy.deepcopy(self.doc)
        for key, value in edits.items():
            plugin, _, name = key.rpartition(".")
            if plugin == "core":
                doc.setdefault("core", {})[name] = value
            else:
                entry = doc.setdefault("plugins", {}).setdefault(plugin, {})
                entry.setdefault("enabled", self.enabled(plugin))
                entry.setdefault("config", {})[name] = value
        for plugin, flag in (enabled or {}).items():
            entry = doc.setdefault("plugins", {}).setdefault(plugin, {})
            entry["enabled"] = flag
            entry.setdefault("config", {})
        return doc

    def write(self, edits: Mapping[str, Any], enabled: Optional[Mapping[str, bool]] = None, *,
              history_keep: int = 50, bump: bool = True) -> tuple[int, int]:
        """Persist the edits: flock, hash check, history copy, tmp, fsync, os.replace. Returns
        (revision_before, revision_after). Raises ConflictError("file_changed") when the file on disk is not
        the one this store last read or wrote."""
        with self.lock:
            before = self.revision
            doc = self.stage(edits, enabled)
            for plugin in (enabled or {}):
                # an explicit enable or disable replaces an in-memory override such as --wav or the
                # no-control-plane rule of from_args; the owner's later word beats the boot default
                self.enabled_override.pop(plugin, None)
            if bump:
                doc["revision"] = before + 1
            if self.path is None:
                self.doc = doc
                return before, int(doc["revision"])
            self._persist(doc, before, history_keep)
            return before, int(doc["revision"])

    def _persist(self, doc: dict, rev_before: int, history_keep: int) -> None:
        assert self.path is not None
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current = self.sha(path.read_bytes()) if path.exists() else None
            if current != self.hash:
                raise ConflictError("file_changed",
                                    "The settings file changed on disk; run mira config reload first.",
                                    revision=self.revision)
            if path.exists():
                hist = path.parent / "history"
                hist.mkdir(exist_ok=True)
                shutil.copy2(path, hist / f"{self.machine}.{rev_before}.json")
                self._prune(hist, history_keep)
            payload = (json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            os.replace(tmp, path)
            self._fsync_dir(path.parent)
            self.doc, self.hash = doc, self.sha(payload)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _prune(self, hist: Path, keep: int) -> None:
        pat = re.compile(re.escape(self.machine) + r"\.(\d+)\.json\Z")
        files = sorted(((int(m.group(1)), p) for p in hist.iterdir() if (m := pat.match(p.name))),
                       key=lambda t: t[0])
        for _rev, p in files[:max(0, len(files) - keep)]:
            try:
                p.unlink()
            except OSError:
                pass

    def mark_good(self) -> None:
        """Copy the file that just booted cleanly to `<file>.lastgood`."""
        if self.path is None or not self.path.exists() or self.config_error:
            return
        good = Path(str(self.path) + ".lastgood")
        tmp = good.with_name(good.name + ".tmp")
        shutil.copyfile(self.path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, good)
