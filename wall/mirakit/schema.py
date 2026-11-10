"""Knob values: coercion, bounds, host rules, time windows and JSON export (spec 2.1, 2.4, 2.5.1 steps 1 to 3).

Stdlib only. Every function raises ConfigError with the dotted key in `field` so the control plane can name the
offender. Coercion and bound checking are separate on purpose: the agent scope runs policy between them (the
addendum puts policy before hard bounds for agents, so a refusal carries the agent range).
"""
from __future__ import annotations

import difflib
import ipaddress
import json
import math
import re
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .plugin import ConfigError, Knob, Paths, PluginSpec

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")
_WINDOW = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d\Z")
_TAILNET = ipaddress.ip_network("100.64.0.0/10")
STRENGTH = {"live": 0, "rebuild": 1, "restart": 2}

VoiceResolver = Callable[[str], str]


def fmt_num(v: Any) -> str:
    """Plain spoken-safe number: 0.8 not 0.80000001, 10 not 10.0."""
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.6g}"
    return str(v)


def fmt_value(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(str(i) for i in v)
    if isinstance(v, Mapping):
        return json.dumps(dict(v), sort_keys=False)
    return fmt_num(v)


# ---------------------------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------------------------

def expand_path(value: str, paths: Paths) -> str:
    """`~`, `${HOME}` and `${HERE}` in a path knob. Anything else is left alone; "" stays ""."""
    if not value:
        return value
    out = value.replace("${HOME}", str(paths.home)).replace("${HERE}", str(paths.here))
    if out == "~" or out.startswith("~/"):
        out = str(paths.home) + out[1:]
    return out


# ---------------------------------------------------------------------------------------------
# url host rules (plugin `validate` hooks call these)
# ---------------------------------------------------------------------------------------------

def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def host_is_loopback(url: str) -> bool:
    host = _host(url)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_is_tailnet(url: str) -> bool:
    """100.64.0.0/10, the Tailscale range."""
    try:
        return ipaddress.ip_address(_host(url)) in _TAILNET
    except ValueError:
        return False


def host_is_loopback_or_tailnet(url: str) -> bool:
    return host_is_loopback(url) or host_is_tailnet(url)


def ip_is_loopback_or_tailnet(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr == "localhost"
    return ip.is_loopback or ip in _TAILNET


# ---------------------------------------------------------------------------------------------
# time windows
# ---------------------------------------------------------------------------------------------

def parse_time_window(text: str) -> Optional[tuple[int, int]]:
    """"HH:MM-HH:MM" to (start_minute, end_minute); "" to None. Raises ValueError on a bad shape."""
    if text == "":
        return None
    if not _WINDOW.match(text):
        raise ValueError(f"{text!r} is not HH:MM-HH:MM")
    a, b = text.split("-")
    return (int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:]))


def in_time_window(text: str, now: datetime) -> bool:
    """Local-time membership. The window may wrap midnight; start == end means the whole day is outside it
    (an empty window), never 24 hours inside."""
    try:
        win = parse_time_window(text)
    except ValueError:
        return False
    if win is None:
        return False
    start, end = win
    minute = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


# ---------------------------------------------------------------------------------------------
# coercion
# ---------------------------------------------------------------------------------------------

def _num(knob: Knob, raw: Any, key: str) -> float:
    if isinstance(raw, bool):
        raise ConfigError(f"{key} must be a number", key)
    try:
        num = float(raw.strip()) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a number", key) from None
    if not math.isfinite(num):
        raise ConfigError(f"{key} must be a finite number", key)
    return num


def coerce(knob: Knob, raw: Any, key: str = "", *, text: bool = False,
           voice_resolver: Optional[VoiceResolver] = None) -> Any:
    """Type coercion only (no bounds). `text=True` means the value came from an environment variable or the
    command line, so a bare comma list is accepted for list[str]."""
    key = key or knob.name
    if raw is None:
        if knob.nullable:
            return None
        raise ConfigError(f"{key} needs a value", key)
    t = knob.type
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in _TRUE + _FALSE:
            return raw.strip().lower() in _TRUE
        raise ConfigError(f"{key} must be true or false", key)
    if t == "int":
        num = _num(knob, raw, key)
        if not num.is_integer():
            raise ConfigError(f"{key} must be a whole number", key)
        return int(num)
    if t == "float":
        return float(_num(knob, raw, key))
    if t == "enum":
        if not isinstance(raw, str) or raw not in (knob.choices or ()):
            raise ConfigError(f"{key} must be one of {', '.join(knob.choices or ())}", key)
        return raw
    if t in ("str", "path", "url", "regex", "time_window", "voice"):
        if not isinstance(raw, str):
            raise ConfigError(f"{key} must be text", key)
        value = raw
        if t == "url":
            parts = urlsplit(value) if value else None
            if value and (parts is None or parts.scheme not in ("http", "https") or not parts.hostname):
                raise ConfigError(f"{key} must be an http or https URL", key)
        elif t == "regex":
            try:
                re.compile(value)
            except re.error as exc:
                raise ConfigError(f"{key} is not a valid pattern: {exc}", key) from None
        elif t == "time_window":
            try:
                parse_time_window(value)
            except ValueError:
                raise ConfigError(f"{key} must look like 22:00-06:00, or be empty", key) from None
        elif t == "voice" and voice_resolver is not None and value:
            value = voice_resolver(value)
        return value
    if t == "list[str]":
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                if not text:
                    raise ConfigError(f"{key} must be a list of text values", key) from None
                raw = [p.strip() for p in raw.split(",") if p.strip()]
        if not isinstance(raw, (list, tuple)) or not all(isinstance(i, str) for i in raw):
            raise ConfigError(f"{key} must be a list of text values", key)
        return list(raw)
    if t == "map[str,str]":
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raise ConfigError(f"{key} must be a JSON object of text values", key) from None
        if (not isinstance(raw, Mapping)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items())):
            raise ConfigError(f"{key} must be an object of text values", key)
        return dict(raw)
    raise ConfigError(f"{key} has the unknown type {t}", key)


def check_bounds(knob: Knob, value: Any, key: str = "") -> None:
    """Hard bounds and choices for every writer. min and max are numbers for int and float, lengths for str,
    list and map values."""
    key = key or knob.name
    if value is None:
        return
    if knob.type in ("int", "float"):
        measure: float = value
    elif knob.type in ("str", "list[str]", "map[str,str]"):
        measure = len(value)
    else:
        return
    if knob.min is not None and measure < knob.min:
        raise ConfigError(f"{key} is below its minimum of {fmt_num(knob.min)}", key)
    if knob.max is not None and measure > knob.max:
        raise ConfigError(f"{key} is above its maximum of {fmt_num(knob.max)}", key)


def validate_knob(knob: Knob, raw: Any, key: str = "", *, text: bool = False,
                  voice_resolver: Optional[VoiceResolver] = None) -> Any:
    """Coerce then bounds. Returns the canonical value."""
    value = coerce(knob, raw, key, text=text, voice_resolver=voice_resolver)
    check_bounds(knob, value, key)
    return value


def defaults(spec: PluginSpec) -> dict[str, Any]:
    """Fresh copies of the schema defaults (lists and maps must not be shared between configs)."""
    out: dict[str, Any] = {}
    for k in spec.knobs:
        d = k.default
        out[k.name] = list(d) if isinstance(d, (list, tuple)) else dict(d) if isinstance(d, Mapping) else d
    return out


def closest(name: str, names: Sequence[str]) -> str:
    near = difflib.get_close_matches(name, list(names), n=1, cutoff=0.5)
    return near[0] if near else ""


def validate_plugin(spec: PluginSpec, values: Mapping[str, Any], *, fill_defaults: bool = True,
                    text: bool = False, voice_resolver: Optional[VoiceResolver] = None,
                    run_hook: bool = True) -> dict[str, Any]:
    """The full candidate config of one plugin: unknown keys refused (naming the closest), every value coerced
    and bounded, defaults filled, then the plugin's own cross-field `validate` hook."""
    by_name = {k.name: k for k in spec.knobs}
    out = defaults(spec) if fill_defaults else {}
    for name, raw in values.items():
        if name.startswith("$"):
            continue
        knob = by_name.get(name)
        key = f"{spec.name}.{name}"
        if knob is None:
            near = closest(name, list(by_name))
            hint = f"; the closest key is {spec.name}.{near}" if near else ""
            raise ConfigError(f"unknown key {key}{hint}", key, "unknown_key")
        out[name] = validate_knob(knob, raw, key, text=text, voice_resolver=voice_resolver)
    if run_hook and spec.validate is not None:
        spec.validate(out)
    return out


# ---------------------------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------------------------

def knob_to_json(k: Knob) -> dict:
    d = {f: getattr(k, f) for f in k.__dataclass_fields__}
    d["choices"] = list(k.choices) if k.choices is not None else None
    d["phrases"] = list(k.phrases)
    if isinstance(k.default, tuple):
        d["default"] = list(k.default)
    return d


def spec_to_json(spec: PluginSpec) -> dict:
    return {"name": spec.name, "kind": spec.kind, "version": spec.version, "summary": spec.summary,
            "provides": list(spec.provides), "needs": list(spec.needs),
            "knobs": [knob_to_json(k) for k in spec.knobs]}


def spoken_name(knob: Knob) -> str:
    return knob.spoken or knob.name.replace("_", " ")


def ladder(knob: Knob) -> list:
    """Agent-reachable values from agent_min to agent_max in agent_step steps."""
    if knob.agent != "bounded" or knob.type not in ("int", "float"):
        return []
    steps = int((knob.agent_max - knob.agent_min) / knob.agent_step + 1e-9)   # never past agent_max
    vals = [round(knob.agent_min + i * knob.agent_step, 6) for i in range(steps + 1)]
    return [int(v) for v in vals] if knob.type == "int" else vals




# ---------------------------------------------------------------------------------------------
# the `core` pseudo-plugin (spec 2.4.2, "core (listener)" and "core (speech)")
# ---------------------------------------------------------------------------------------------

_UNIT_MEM = re.compile(r"\d+[KMG]\Z")
_UNIT_CPU = re.compile(r"\d{1,3}%\Z")


def _core_validate(values: Mapping[str, Any]) -> None:
    mem = values.get("unit_memory_max")
    if mem is not None and not _UNIT_MEM.match(mem):
        raise ConfigError("unit_memory_max must look like 200M", "core.unit_memory_max")
    cpu = values.get("unit_cpu_quota")
    if cpu is not None and not _UNIT_CPU.match(cpu):
        raise ConfigError("unit_cpu_quota must look like 40%", "core.unit_cpu_quota")
    if values["crash_legacy_after"] <= values["crash_guard_after"]:
        raise ConfigError("crash_legacy_after must exceed crash_guard_after", "core.crash_legacy_after")


def core_spec(machine: str) -> PluginSpec:
    """The knobs of the `core` pseudo-plugin. Every one is owner only and none can be applied live: the launcher
    and the unit generator read them, so a change is shown as pending_restart."""
    from .plugin import PluginBase

    def knob(name: str, type_: str, default: Any, description: str, **kw: Any) -> Knob:
        kw.setdefault("applies", "restart")
        kw.setdefault("topic", "system")
        return Knob(name, type_, default, description, **kw)  # type: ignore[arg-type]

    common = [
        knob("engine", "enum", "legacy", "Which engine the launcher runs.", choices=("legacy", "plugins")),
        knob("legacy_env", "enum", "fallback", "Whether MIRA_* environment values fill keys the file lacks.",
             choices=("fallback", "ignore")),
    ]
    crash = [
        knob("crash_window_s", "float", 600.0, "Window for counting unplanned starts.", unit="s", min=60, max=3600),
        knob("crash_guard_after", "int", 3, "Unplanned starts in the window that trigger the boot guard.",
             unit="starts", min=2, max=10),
        knob("crash_legacy_after", "int", 4, "Unplanned starts that switch the launcher to legacy.",
             unit="starts", min=3, max=20),
        knob("crash_guard_revert_s", "float", 1800.0, "How far back the boot guard reverts agent changes.",
             unit="s", min=300, max=86400),
    ]
    if machine == "speech":
        knobs = common + [
            knob("unit_nice", "int", 5, "Process priority in the LaunchAgent.", unit="nice", min=-10, max=19),
            knob("unit_throttle_s", "int", 15, "LaunchAgent ThrottleInterval.", unit="s", min=1, max=300),
            knob("unit_keep_alive", "bool", True, "LaunchAgent KeepAlive."),
        ] + crash
    else:
        knobs = common + [
            knob("unit_memory_max", "str", "200M", "Systemd MemoryMax rendered into the drop-in.", min=2, max=8),
            knob("unit_cpu_quota", "str", "40%", "Systemd CPUQuota rendered into the drop-in.", min=2, max=5),
            knob("unit_nice", "int", 5, "Systemd Nice rendered into the drop-in.", unit="nice", min=-10, max=19),
            knob("unit_restart", "enum", "always", "Systemd Restart policy.", choices=("always", "on-failure")),
            knob("unit_restart_sec", "float", 3.0, "Systemd RestartSec.", unit="s", min=1, max=60),
        ] + crash

    def create(ctx: Any, cfg: Any) -> PluginBase:
        return PluginBase(ctx, cfg)

    return PluginSpec(name="core", kind="core", version="1", summary="launcher and unit settings",
                      machine=machine, provides=(), create=create, knobs=tuple(knobs),  # type: ignore[arg-type]
                      required=False, core=True, validate=_core_validate)
