"""Render the systemd drop-in (Pi) and the LaunchAgent keys (mini) from the `core.unit_*` knobs. Spec 7.1 and 2.4.2.

`apply` writes the file and, on the Pi, runs `daemon-reload`; it never restarts anything. Stdlib only.
"""
from __future__ import annotations

import difflib
import json
import os
import plistlib
import re
import string
from pathlib import Path
from typing import Mapping, Optional

from mirakit.deploy import Runner

# Schema defaults of the core.unit_* knobs (2.4.2). A file may omit them, so rendering must not depend on the
# core being importable; `tests/kit/test_kit_unitgen.py` pins these to the spec table.
PI_DEFAULTS = {"unit_memory_max": "200M", "unit_cpu_quota": "40%", "unit_nice": 5, "unit_restart": "always",
               "unit_restart_sec": 3.0}
SPEECH_DEFAULTS = {"unit_nice": 5, "unit_throttle_s": 15, "unit_keep_alive": True}
DEFAULTS = {"listener": PI_DEFAULTS, "speech": SPEECH_DEFAULTS}
PLIST_LABEL = "org.aryavora.mira-speech"

TEMPLATE_NAME = "mira-listen.dropin.tmpl"


def _wall_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def dropin_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / "mira-listen.service.d" / "mira.conf"


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def values_from_file(config_path: Path, machine: str) -> dict:
    """core.unit_* values: the file's `core` section over the schema defaults. A missing file gives defaults."""
    values = dict(DEFAULTS[machine])
    try:
        data = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return values
    core = data.get("core", {}) if isinstance(data, dict) else {}
    for key in values:
        if key in core and not isinstance(core[key], (dict, list)):
            values[key] = core[key]
    return values


def _num(value: float) -> str:
    return f"{value:g}"


def render_dropin(values: Mapping, template: Optional[str] = None) -> str:
    merged = dict(PI_DEFAULTS)
    merged.update({k: v for k, v in values.items() if k in PI_DEFAULTS})
    text = template if template is not None else (_wall_dir() / "deploy" / TEMPLATE_NAME).read_text()
    return string.Template(text).substitute(
        memory_max=merged["unit_memory_max"], cpu_quota=merged["unit_cpu_quota"], nice=_num(merged["unit_nice"]),
        restart=merged["unit_restart"], restart_sec=_num(merged["unit_restart_sec"]))


def render_plist(values: Mapping, base: Optional[bytes] = None) -> bytes:
    """The installed LaunchAgent with only Nice, ThrottleInterval and KeepAlive replaced (the rest, including the
    environment that the legacy server reads, stays as the owner left it)."""
    merged = dict(SPEECH_DEFAULTS)
    merged.update({k: v for k, v in values.items() if k in SPEECH_DEFAULTS})
    if base:
        doc = plistlib.loads(base)
    else:
        doc = {"Label": PLIST_LABEL, "RunAtLoad": True}
    doc["Nice"] = int(merged["unit_nice"])
    doc["ThrottleInterval"] = int(merged["unit_throttle_s"])
    doc["KeepAlive"] = bool(merged["unit_keep_alive"])
    return plistlib.dumps(doc, sort_keys=False)


def render(machine: str, values: Mapping, home: Path) -> tuple[Path, bytes]:
    """(destination path, rendered bytes) for this machine."""
    if machine == "speech":
        dest = plist_path(home)
        base = dest.read_bytes() if dest.exists() else None
        if base is None:
            fallback = _wall_dir() / "speech" / f"{PLIST_LABEL}.plist"
            base = fallback.read_bytes() if fallback.exists() else None
        return dest, render_plist(values, base)
    return dropin_path(home), render_dropin(values).encode()


def check(machine: str, values: Mapping, home: Path) -> tuple[bool, str]:
    """(matches, unified diff of installed versus rendered). A missing installed file counts as a difference."""
    dest, wanted = render(machine, values, home)
    have = dest.read_bytes() if dest.exists() else b""
    if have == wanted:
        return True, ""
    a = have.decode("utf-8", "replace").splitlines(keepends=True)
    b = wanted.decode("utf-8", "replace").splitlines(keepends=True)
    return False, "".join(difflib.unified_diff(a, b, f"installed {dest}", f"rendered {dest}"))


RELEASE_NAME = re.compile(r"[0-9a-zA-Z._-]+\Z")


def link_current(root: Path, release: Optional[str] = None) -> str:
    """Point `<root>/current` at a release (spec 7.1 step 3). Without `release`, an existing `current` is left alone
    and a missing one is pointed at the newest staged release; the drop-in's ExecStart lives under `current/`, so
    the unit cannot start without it. The swap is atomic (symlink to a temp name, then rename)."""
    releases = root / "releases"
    current = root / "current"
    if release is None:
        if current.is_symlink() or current.exists():
            return f"current stays {os.readlink(current) if current.is_symlink() else current}"
        staged = sorted((p for p in releases.iterdir() if p.is_dir() and not p.name.startswith(".")),
                        key=lambda p: p.stat().st_mtime) if releases.is_dir() else []
        if not staged:
            return "no release is staged yet: run `mira deploy pi --stage` from the Mac"
        release = staged[-1].name
    if not RELEASE_NAME.match(release) or not (releases / release).is_dir():
        raise ValueError(f"release {release!r} is not staged under {releases}")
    tmp = root / ".current.tmp"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(f"releases/{release}", tmp)
    os.replace(tmp, current)
    return f"current -> releases/{release}"


def apply(machine: str, values: Mapping, home: Path, runner: Optional[Runner] = None, *,
          release: Optional[str] = None, install_root: Optional[Path] = None) -> list[str]:
    """Write the file; on the Pi also make sure `current` exists and run daemon-reload. Returns what was done.
    Never restarts a service."""
    dest, wanted = render(machine, values, home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(wanted)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    done = [f"wrote {dest}"]
    if machine == "listener":
        done.append(link_current(install_root or home / "mira-listen", release))
        runner = runner or Runner()
        res = runner.sh('XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" systemctl --user daemon-reload')
        done.append("systemctl --user daemon-reload" + ("" if res.ok else f" (failed: {res.err.strip()[:120]})"))
    else:
        done.append("restart is yours: launchctl kickstart -k gui/$(id -u)/" + PLIST_LABEL)
    return done
