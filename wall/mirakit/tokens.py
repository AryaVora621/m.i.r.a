"""Create missing control token files and their permissions; never print or return a value. Spec 7.1 and 7.2.

Token text exists only in this process, in a 0600 file created with O_EXCL, and (for the root-owned agent token on
the Pi) in a temp file that is installed with `sudo -n install` and then removed. Commands carry paths only.
"""
from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mirakit.deploy import Runner


@dataclass(frozen=True)
class TokenSpec:
    name: str
    path: str
    mode: int
    generate: bool = True               # False: created elsewhere and copied in (the Pi's speech.control.token)
    root_owned: bool = False            # installed with sudo -n install (the agent token)
    acl_user: str = ""                  # setfacl -m u:<user>:r


@dataclass
class TokenReport:
    name: str
    path: str
    state: str                          # created | exists | fixed_mode | missing_copy_from_mini | failed
    mode: str = ""                      # e.g. "0600", "" when absent
    detail: str = ""


def plan(machine: str, home: Path, etc_dir: str = "/etc/mira", agent_user: str = "agent") -> list[TokenSpec]:
    cfg = home / ".config" / "mira"
    if machine == "speech":
        return [TokenSpec("owner", str(cfg / "control.owner.token"), 0o600),
                TokenSpec("pi", str(cfg / "control.pi.token"), 0o600)]
    return [TokenSpec("owner", str(cfg / "control.owner.token"), 0o600),
            TokenSpec("agent", f"{etc_dir}/control.agent.token", 0o640, root_owned=True, acl_user=agent_user),
            TokenSpec("speech", str(cfg / "speech.control.token"), 0o400, generate=False)]


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _write_new(path: Path, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(_new_token() + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _mode_of(path: Path) -> str:
    try:
        return f"{stat.S_IMODE(path.stat().st_mode):04o}"
    except OSError:
        return ""


def install(machine: str, home: Path, runner: Optional[Runner] = None, *, etc_dir: str = "/etc/mira",
            agent_user: str = "agent", owner_group: str = "mira") -> list[TokenReport]:
    runner = runner or Runner()
    reports: list[TokenReport] = []
    for spec in plan(machine, home, etc_dir, agent_user):
        path = Path(spec.path)
        if spec.root_owned:
            reports.append(_install_root_owned(spec, home, runner, owner_group))
            continue
        if path.exists():
            have = stat.S_IMODE(path.stat().st_mode)
            if have & 0o077:
                os.chmod(path, spec.mode)
                reports.append(TokenReport(spec.name, spec.path, "fixed_mode", _mode_of(path),
                                           f"was {have:04o}, now {spec.mode:04o}"))
            else:
                reports.append(TokenReport(spec.name, spec.path, "exists", _mode_of(path)))
            continue
        if not spec.generate:
            reports.append(TokenReport(spec.name, spec.path, "missing_copy_from_mini", "",
                                       "copy the mini's control.pi.token here without printing it"))
            continue
        _write_new(path, spec.mode)
        reports.append(TokenReport(spec.name, spec.path, "created", _mode_of(path)))
    return reports


def _install_root_owned(spec: TokenSpec, home: Path, runner: Runner, group: str) -> TokenReport:
    if runner.run(["sudo", "-n", "test", "-f", spec.path], mutating=False).ok:
        state = "exists"
    else:
        tmp = home / ".config" / "mira" / ".agent-token.tmp"
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        _write_new(tmp, 0o600)
        try:
            res = runner.run(["sudo", "-n", "install", "-d", "-o", "root", "-g", group, "-m", "755",
                              str(Path(spec.path).parent)])
            res = res if not res.ok else runner.run(["sudo", "-n", "install", "-o", "root", "-g", group, "-m",
                                                     f"{spec.mode:o}", str(tmp), spec.path])
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        if not res.ok:
            return TokenReport(spec.name, spec.path, "failed", "", f"sudo install failed: {res.err.strip()[:160]}")
        state = "created"
    if spec.acl_user:
        acl = runner.run(["sudo", "-n", "setfacl", "-m", f"u:{spec.acl_user}:r", spec.path])
        if not acl.ok:
            return TokenReport(spec.name, spec.path, "failed", "", f"setfacl failed: {acl.err.strip()[:160]}")
    return TokenReport(spec.name, spec.path, state, f"{spec.mode:04o}",
                       f"root:{group}, acl u:{spec.acl_user}:r" if spec.acl_user else f"root:{group}")
