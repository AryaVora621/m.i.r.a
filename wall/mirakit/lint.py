"""`mira check`: the contract lint of spec 2.11. Stdlib only.

Every rule returns `Finding`s with a one-line message that names the file or plugin and the knob. `fast=True`
runs only the rules that need nothing but the code (what `mira deploy --stage` runs inside a release, which has
no tests, docs or examples): knob metadata, duplicate names, exclusive providers, and the source scans.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from mirakit.plugin import Knob, PluginSpec
from mirakit.services import MULTI_SERVICES

# Class names that may read env or open literal paths in every plugin module (the verbatim v1 classes live in
# each module's own LEGACY_CLASSES, and moved v1 helper functions in its LEGACY_FUNCTIONS; this list is for
# class names exempt everywhere and starts empty on purpose).
LEGACY_CLASSES: tuple[str, ...] = ()

DASHES = (chr(0x2013), chr(0x2014))
TEXT_SUFFIXES = (".py", ".md", ".json", ".txt", ".tmpl", ".service", ".plist", ".env", ".example", ".sh",
                 ".command", ".toml", ".cfg", ".ini", ".yml", ".yaml")
SKIP_DIRS = {"__pycache__", "models", ".git", "venv", "node_modules", ".pytest_cache", ".venv"}
TRIVIAL_NUMBERS = (0, 1, -1)

# Values the example files may carry that differ from the schema default (spec 2.4.5, plus the owner decision of
# 2026-11-10 that the Pi example sets a night window). "plugin.key" or "plugin.enabled".
DEPLOYMENT_DIFFS = {
    "listener": {"core.engine", "core.legacy_env", "wake.kws.kws_dir", "wake.kws.threshold", "vad.silero.model",
                 "speech.link.token_file", "intent.music.url", "intent.music.auth_file", "brain.hermes.key_file",
                 "persona.file.path", "playback.miraaudio.chime", "reporter.agentos.token_file",
                 "loop.turns.events_max", "playback.miraaudio.night_window"},
    "speech": {"core.engine", "core.legacy_env", "stt.engine.kind", "server.http.token_file", "server.http.base_dir",
               "control.http.enabled"},
}
HOMES = {"listener": "/home/mira", "speech": "/Users/aryavora"}
EXAMPLE_FILES = {"mira.example.json": "listener", "speech.example.json": "speech"}


@dataclass(frozen=True)
class Finding:
    rule: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.where}: {self.message}"


# --------------------------------------------------------------------------------------------
# rule 1 to 3: knob metadata
# --------------------------------------------------------------------------------------------

def check_knobs(specs: Sequence[PluginSpec]) -> list[Finding]:
    out: list[Finding] = []
    for spec in specs:
        for k in spec.knobs:
            where = f"{spec.name}.{k.name}"
            if not k.description.strip():
                out.append(Finding("knob-description", where, "knob has no description"))
            elif len(k.description) > 160:
                out.append(Finding("description-length", where,
                                   f"description is {len(k.description)} chars, the limit is 160"))
            numeric = k.type in ("int", "float")
            if numeric and not k.unit.strip():
                out.append(Finding("knob-unit", where, "numeric knob has no unit"))
            if numeric and (k.min is None or k.max is None):
                out.append(Finding("knob-bounds", where, "numeric knob needs both min and max"))
            if k.applies not in ("live", "rebuild", "restart"):
                out.append(Finding("knob-applies", where, f"applies must be live, rebuild or restart, not {k.applies!r}"))
            if k.agent == "bounded" and numeric:
                if k.agent_min is None or k.agent_max is None or k.agent_step is None:
                    out.append(Finding("knob-agent", where, "bounded knob needs agent_min, agent_max and agent_step"))
                else:
                    if k.min is not None and k.agent_min < k.min or k.max is not None and k.agent_max > k.max:
                        out.append(Finding("knob-agent", where, "agent bounds lie outside the hard bounds"))
                    if k.agent_step <= 0 or (k.agent_max - k.agent_min) < k.agent_step - 1e-12:
                        out.append(Finding("knob-agent", where, "agent_step does not fit inside the agent range"))
            if k.agent == "bounded" and k.applies != "live" and spec.machine == "listener":
                out.append(Finding("knob-agent", where, "a bounded knob on the listener must be live"))
    return out


# --------------------------------------------------------------------------------------------
# rule 4 and 5: source scans
# --------------------------------------------------------------------------------------------

def module_file(spec: PluginSpec) -> Optional[Path]:
    create = spec.create
    name = getattr(create, "__module__", None)
    module = sys.modules.get(name) if name else None
    path = getattr(module, "__file__", None)
    return Path(path) if path else None


def _literal_names(tree: ast.Module, var: str) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == var for t in node.targets):
            if isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
                return {e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return set()


def _fixed_numbers(tree: ast.Module) -> set[float]:
    doc = ast.get_docstring(tree) or ""
    nums: set[float] = set()
    for line in doc.splitlines():
        if line.strip().startswith("FIXED:"):
            nums.update(float(n) for n in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", line))
    return nums


def _exempt_ids(tree: ast.Module, legacy: set[str], legacy_functions: set[str] = frozenset()) -> set[int]:
    """ids of nodes whose subtree is exempt: legacy classes and functions, Knob and PluginSpec calls, UPPER_CASE
    constants, docstrings, and the default values in function signatures (an interface default of a moved v1
    helper; the live value arrives from the knob at the call site)."""
    ids: set[int] = set()

    def mark(node: ast.AST) -> None:
        for sub in ast.walk(node):
            ids.add(id(sub))

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in legacy:
            mark(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in legacy_functions:
            mark(node)
        elif isinstance(node, ast.arguments):
            for default in [*node.defaults, *[d for d in node.kw_defaults if d is not None]]:
                mark(default)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in ("Knob", "PluginSpec"):
                mark(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and targets and all(isinstance(t, ast.Name) and t.id.isupper() for t in targets):
                mark(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                mark(body[0])
    return ids


def scan_source(spec: PluginSpec, path: Path) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [Finding("source", str(path), f"cannot parse: {type(exc).__name__}")]
    legacy = _literal_names(tree, "LEGACY_CLASSES") | set(LEGACY_CLASSES)
    exempt = _exempt_ids(tree, legacy, _literal_names(tree, "LEGACY_FUNCTIONS"))
    fixed = _fixed_numbers(tree)
    defaults: dict[float, str] = {}
    for k in spec.knobs:
        if k.type in ("int", "float") and isinstance(k.default, (int, float)) and not isinstance(k.default, bool):
            if k.default not in TRIVIAL_NUMBERS:
                defaults.setdefault(float(k.default), k.name)
    out: list[Finding] = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        where = f"{path.name}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                and node.attr in ("environ", "getenv", "putenv"):
            out.append(Finding("environ", where, f"plugin {spec.name} reads os.{node.attr}; take the value from a knob "
                                                 f"(Knob.env maps the legacy variable)"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open" \
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            out.append(Finding("open-literal", where, f"plugin {spec.name} opens the literal path "
                                                      f"{node.args[0].value!r}; make it a path knob"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool) and float(node.value) in defaults \
                and float(node.value) not in fixed:
            out.append(Finding("literal-default", where,
                               f"bare literal {node.value} equals the default of knob "
                               f"{spec.name}.{defaults[float(node.value)]}; read the knob or list it under FIXED:"))
    return out


# --------------------------------------------------------------------------------------------
# rules 6 to 8: structure
# --------------------------------------------------------------------------------------------

def check_tests_exist(specs: Sequence[PluginSpec], root: Path) -> list[Finding]:
    out = []
    for spec in specs:
        if not spec.tests:
            out.append(Finding("tests", spec.name, "spec declares no test files"))
            continue
        for rel in spec.tests:
            if not any((base / rel).exists() for base in (root, root.parent)):
                out.append(Finding("tests", spec.name, f"test file {rel} does not exist"))
    return out


def check_unique_names(specs: Sequence[PluginSpec]) -> list[Finding]:
    """One name per machine: control.http legitimately exists once on the listener and once on the speech server
    (the spec table of 2.4.2 lists it for both), so uniqueness is checked inside a machine."""
    seen: dict[tuple[str, str], int] = {}
    for spec in specs:
        seen[(spec.machine, spec.name)] = seen.get((spec.machine, spec.name), 0) + 1
    return [Finding("duplicate-name", f"{machine}:{name}", f"{n} specs share this name")
            for (machine, name), n in sorted(seen.items()) if n > 1]


def check_exclusive_providers(specs: Sequence[PluginSpec]) -> list[Finding]:
    by_service: dict[tuple[str, str], list[str]] = {}
    for spec in specs:
        if not spec.default_enabled:
            continue
        for service in spec.provides:
            if service not in MULTI_SERVICES and service != "core":
                by_service.setdefault((spec.machine, service), []).append(spec.name)
    return [Finding("exclusive-provider", f"{machine}:{service}",
                    f"exclusive service has {len(names)} default-enabled providers: {', '.join(sorted(names))}")
            for (machine, service), names in sorted(by_service.items()) if len(names) > 1]


# --------------------------------------------------------------------------------------------
# rule 9: the example files
# --------------------------------------------------------------------------------------------

_TIME_WINDOW = re.compile(r"(\d\d:\d\d-\d\d:\d\d)?\Z")


def _expand(value: Any, machine: str) -> Any:
    if isinstance(value, str):
        home = HOMES[machine]
        return value.replace("${HOME}", home).replace("~", home, 1) if value.startswith("~") or "${HOME}" in value else value
    return value


def value_problem(k: Knob, value: Any) -> str:
    """'' when the value is acceptable for the knob, else a short reason (no core needed)."""
    if value is None:
        return "" if k.nullable else "null for a knob that is not nullable"
    t = k.type
    if t == "bool":
        return "" if isinstance(value, bool) else "not a boolean"
    if t in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (t == "int" and int(value) != value):
            return f"not {'a whole number' if t == 'int' else 'a number'}"
        if (k.min is not None and value < k.min) or (k.max is not None and value > k.max):
            return f"{value} is outside {k.min}..{k.max}"
        return ""
    if t == "enum":
        return "" if value in (k.choices or ()) else f"not one of {list(k.choices or ())}"
    if t == "list[str]":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return "not a list of text"
        if (k.min is not None and len(value) < k.min) or (k.max is not None and len(value) > k.max):
            return f"{len(value)} items is outside {k.min}..{k.max}"
        return ""
    if t == "map[str,str]":
        return "" if isinstance(value, dict) and all(isinstance(a, str) and isinstance(b, str) for a, b in value.items()) \
            else "not a map of text"
    if t == "regex":
        if not isinstance(value, str):
            return "not text"
        try:
            re.compile(value)
        except re.error as exc:
            return f"does not compile: {exc}"
        return ""
    if t == "time_window":
        return "" if isinstance(value, str) and _TIME_WINDOW.match(value) else "not HH:MM-HH:MM or empty"
    if t == "url":
        return "" if isinstance(value, str) and re.match(r"https?://", value) else "not an http(s) url"
    if not isinstance(value, str):
        return "not text"
    if t in ("str",) and ((k.min is not None and len(value) < k.min) or (k.max is not None and len(value) > k.max)):
        return f"{len(value)} chars is outside {k.min}..{k.max}"
    return ""


def check_example(path: Path, specs: Sequence[PluginSpec], machine: str) -> list[Finding]:
    name = path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Finding("example", name, f"not valid JSON: {exc}")]
    out: list[Finding] = []
    by_name = {s.name: s for s in specs if s.machine == machine}
    if data.get("format") != 1 or data.get("machine") != machine or not isinstance(data.get("revision"), int):
        out.append(Finding("example", name, f'needs "format": 1, "machine": "{machine}" and an integer revision'))
    allowed = DEPLOYMENT_DIFFS[machine]
    plugins = data.get("plugins", {})
    for plugin in sorted(set(by_name) - set(plugins) - {"core"}):
        out.append(Finding("example", name, f"plugin {plugin} is missing"))
    for plugin, body in plugins.items():
        spec = by_name.get(plugin)
        if spec is None:
            out.append(Finding("example", name, f"unknown plugin {plugin}"))
            continue
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            out.append(Finding("example", name, f"{plugin} needs a boolean enabled"))
            continue
        if body["enabled"] != spec.default_enabled and f"{plugin}.enabled" not in allowed:
            out.append(Finding("example", name, f"{plugin}.enabled is {body['enabled']} but the spec default is "
                                                f"{spec.default_enabled} and it is not a documented deployment value"))
        knobs = {k.name: k for k in spec.knobs}
        for key, value in (body.get("config") or {}).items():
            if key.startswith("$"):
                continue
            knob = knobs.get(key)
            if knob is None:
                out.append(Finding("example", name, f"{plugin}.{key} is not a knob"))
                continue
            dotted = f"{plugin}.{key}"
            default = _expand(knob.default, machine)
            if value == default or (isinstance(default, str) and "${HERE}" in default):
                continue
            problem = value_problem(knob, value)
            if problem:
                out.append(Finding("example", name, f"{dotted} {problem}"))
            elif dotted not in allowed:
                out.append(Finding("example", name, f"{dotted} is {value!r} but the schema default is {default!r} and it "
                                                    f"is not a documented deployment value (2.4.5)"))
    core_spec = by_name.get("core")
    if core_spec is None:
        try:
            from mirakit.schema import core_spec as make_core_spec   # P3 owns the core pseudo-plugin knobs
            core_spec = make_core_spec(machine)
        except Exception:
            core_spec = None
    if core_spec is not None:
        knobs = {k.name: k for k in core_spec.knobs}
        for key, value in (data.get("core") or {}).items():
            if key.startswith("$"):
                continue
            knob = knobs.get(key)
            if knob is None:
                out.append(Finding("example", name, f"core.{key} is not a knob"))
            elif value != knob.default and f"core.{key}" not in allowed:
                out.append(Finding("example", name, f"core.{key} differs from the schema default and is not a "
                                                    f"documented deployment value"))
    return out


def check_examples(specs: Sequence[PluginSpec], root: Path) -> list[Finding]:
    out: list[Finding] = []
    deploy = root / "deploy"
    for fname, machine in EXAMPLE_FILES.items():
        path = deploy / fname
        if path.exists():
            out += check_example(path, specs, machine)
    return out


# --------------------------------------------------------------------------------------------
# rule 10 and 11: generated files and dashes
# --------------------------------------------------------------------------------------------

def check_dashes(root: Path) -> list[Finding]:
    out: list[Finding] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            path = Path(base) / fname
            if not (fname.endswith(TEXT_SUFFIXES) or fname == "mira"):
                continue
            try:
                if path.stat().st_size > 4_000_000:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if any(c in line for c in DASHES):
                    out.append(Finding("dash", f"{path.relative_to(root)}:{lineno}", "contains an en or em dash"))
                    break
    return out


def check_generated(specs: Sequence[PluginSpec], root: Path) -> list[Finding]:
    from mirakit import docsgen
    return [Finding("stale-generated", rel, "differs from the specs; run `mira docs`")
            for rel in docsgen.stale_files(root, specs)]


# --------------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------------

def lint(specs: Sequence[PluginSpec], root: Path, fast: bool = False) -> list[Finding]:
    root = Path(root)
    out = check_unique_names(specs) + check_knobs(specs) + check_exclusive_providers(specs)
    seen_files: set[Path] = set()
    for spec in specs:
        path = module_file(spec)
        if path is not None and path.suffix == ".py" and "mirakit" not in path.parts[-2:-1]:
            out += scan_source(spec, path)
            seen_files.add(path)
    if not fast:
        out += check_tests_exist(specs, root) + check_examples(specs, root) + check_generated(specs, root)
        out += check_dashes(root)
    return out
