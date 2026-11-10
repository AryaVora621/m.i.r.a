"""Releases, staging, activation and rollback (Pi) and snapshot copy (mini). Spec 7.1, 7.2 and 2.5.6.

Every command on another machine, and every git call, goes through one `Runner`, so tests substitute a
subclass and run the whole plan against temp dirs: nothing here talks to ssh unless the Runner has a target.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

SKIP_NAMES = ("__pycache__", ".DS_Store")
SKIP_SUFFIXES = (".pyc", ".pyo")


class DeployError(RuntimeError):
    pass


@dataclass
class Result:
    rc: int = 0
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Runner:
    """Runs argv lists locally (target None) or on `target` over ssh. `dry_run` records mutating commands in
    `log` and skips them; read-only commands (mutating=False) still run so a dry run can diff against the
    real machine. Nothing is interpreted by a shell except through `sh()`, whose script text is fixed and
    whose paths arrive as positional arguments."""

    SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")

    def __init__(self, target: Optional[str] = None, dry_run: bool = False) -> None:
        self.target = target
        self.dry_run = dry_run
        self.log: list[str] = []

    def _final(self, argv: Sequence[str]) -> list[str]:
        if self.target is None:
            return list(argv)
        return ["ssh", *self.SSH_OPTS, self.target, shlex.join(argv)]

    def _exec(self, argv: list[str], input: Optional[bytes], timeout: Optional[float]) -> Result:
        try:
            proc = subprocess.run(argv, input=input, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            return Result(127, b"", str(exc).encode())
        except subprocess.TimeoutExpired:
            return Result(124, b"", b"timed out")
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def run(self, argv: Sequence[str], *, input: Optional[bytes] = None, mutating: bool = True,
            check: bool = False, timeout: Optional[float] = 300) -> Result:
        line = shlex.join(argv) + (" < <stdin>" if input is not None else "")
        if mutating and self.dry_run:
            self.log.append(("+ " + line) if self.target is None else f"+ [{self.target}] {line}")
            return Result(0)
        self.log.append(line if self.target is None else f"[{self.target}] {line}")
        result = self._exec(self._final(argv), input, timeout)
        if check and not result.ok:
            raise DeployError(f"command failed ({result.rc}): {line}: {result.err.strip()[:300]}")
        return result

    def sh(self, script: str, *args: str, input: Optional[bytes] = None, mutating: bool = True,
           check: bool = False) -> Result:
        return self.run(["sh", "-c", script, "sh", *args], input=input, mutating=mutating, check=check)

    # ---- file helpers, all built on run() so a subclass needs to override only _exec
    def exists(self, path: str, kind: str = "e") -> bool:
        return self.run(["test", f"-{kind}", path], mutating=False).ok

    def read_bytes(self, path: str) -> Optional[bytes]:
        res = self.run(["cat", path], mutating=False)
        return res.stdout if res.ok else None

    def write_bytes(self, path: str, data: bytes, mode: int = 0o644) -> Result:
        return self.sh('umask 077; mkdir -p "$(dirname "$1")" && cat > "$1" && chmod "$2" "$1"',
                       path, f"{mode:o}", input=data, check=True)


# --------------------------------------------------------------------------------------------
# manifest and file collection
# --------------------------------------------------------------------------------------------

def load_manifest(wall: Path) -> dict:
    return json.loads((wall / "deploy" / "manifest.json").read_text())


def manifest_entries(spec: dict) -> list[tuple[str, str]]:
    """[(src relative to wall/, dst relative to the install dir)]."""
    out = []
    for item in spec["files"]:
        out.append((item, item) if isinstance(item, str) else (item["src"], item["dst"]))
    return out


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in SKIP_NAMES for p in parts) or rel.endswith(SKIP_SUFFIXES)


def collect_from_tree(wall: Path, entries: Sequence[tuple[str, str]]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for src, dst in entries:
        base = wall / src
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                rel = path.relative_to(base).as_posix()
                if path.is_file() and not _skip(rel) and not _skip(src):
                    files[posixpath.join(dst, rel)] = path.read_bytes()
        elif base.is_file():
            files[dst] = base.read_bytes()
        else:
            raise DeployError(f"manifest file missing in the working tree: wall/{src}")
    return files


def collect_from_tar(data: bytes, entries: Sequence[tuple[str, str]], prefix: str = "wall/") -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            for src, dst in entries:
                if rel == src:
                    target = dst
                elif rel.startswith(src.rstrip("/") + "/"):
                    target = posixpath.join(dst, rel[len(src.rstrip("/")) + 1:])
                else:
                    continue
                if not _skip(rel):
                    handle = tar.extractfile(member)
                    files[target] = handle.read() if handle else b""
                break
    return files


def build_tar(files: dict[str, bytes], extra: Optional[dict[str, bytes]] = None) -> bytes:
    buf = io.BytesIO()
    merged = dict(files)
    merged.update(extra or {})
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in sorted(merged):
            info = tarfile.TarInfo(name)
            info.size = len(merged[name])
            info.mtime = 0           # deterministic archives: the same sha always ships the same bytes
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(merged[name]))
    return buf.getvalue()


def hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def diff_hashes(local: dict[str, str], remote: dict[str, str]) -> list[str]:
    lines = []
    for name in sorted(set(local) | set(remote)):
        if name not in remote:
            lines.append(f"A {name}")
        elif name not in local:
            lines.append(f"D {name}")
        elif local[name] != remote[name]:
            lines.append(f"M {name}")
    return lines


def parse_sha256sum(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"([0-9a-f]{64})\s+\*?(?:\./)?(.+)$", line.strip())
        if m and not _skip(m.group(2)):
            out[m.group(2)] = m.group(1)
    return out


# --------------------------------------------------------------------------------------------
# the deployer
# --------------------------------------------------------------------------------------------

@dataclass
class Deployer:
    machine: str                                  # "pi" or "speech"
    wall: Path
    runner: Runner                                # the remote machine (or a local stand-in in tests)
    local: Runner = field(default_factory=Runner)  # git and tar on this machine
    manifest: Optional[dict] = None
    out: Callable[[str], None] = print
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    python_for_check: Optional[str] = None        # override of <root>/venv/bin/python in tests

    def __post_init__(self) -> None:
        self.manifest = self.manifest or load_manifest(self.wall)
        self.spec = self.manifest[self.machine]
        self.entries = manifest_entries(self.spec)
        self.root = self.spec["root"]
        self.repo = self.wall.parent

    # ---- source of truth for what ships
    def git_head(self) -> str:
        res = self.local.run(["git", "-C", str(self.repo), "rev-parse", "--short=12", "HEAD"], mutating=False, check=True)
        sha = res.out.strip()
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise DeployError(f"git did not return a commit id: {sha[:40]!r}")
        return sha

    def is_dirty(self) -> bool:
        res = self.local.run(["git", "-C", str(self.repo), "status", "--porcelain", "--untracked-files=no", "--",
                              "wall"], mutating=False, check=True)
        return bool(res.out.strip())

    def collect(self, allow_dirty: bool) -> tuple[str, dict[str, bytes]]:
        """(release name, files). A clean tree ships HEAD through git archive; --allow-dirty ships the working tree."""
        sha = self.git_head()
        if allow_dirty:
            return sha + "-dirty", collect_from_tree(self.wall, self.entries)
        if self.is_dirty():
            raise DeployError("wall/ has uncommitted changes: commit first, or pass --allow-dirty for a working-tree "
                              "release (named <sha>-dirty)")
        paths = ["wall/" + src for src, _ in self.entries]
        res = self.local.run(["git", "-C", str(self.repo), "archive", "--format=tar", "HEAD", "--", *paths],
                             mutating=False, check=True)
        files = collect_from_tar(res.stdout, self.entries)
        missing = [dst for _, dst in self.entries if not any(f == dst or f.startswith(dst + "/") for f in files)]
        if missing:
            raise DeployError("not in HEAD: " + ", ".join(missing))
        return sha, files

    def release_info(self, name: str) -> dict[str, bytes]:
        info = {"release": name, "built": int(self.now()), "machine": self.machine}
        return {"RELEASE.json": (json.dumps(info, sort_keys=True) + "\n").encode()}

    # ---- diff against the machine
    def remote_hashes(self, subdir: str) -> dict[str, str]:
        names = " ".join(shlex.quote(dst) for _, dst in self.entries)
        script = (f'cd "$1" 2>/dev/null || exit 0; find {names} -type f ! -path "*/__pycache__/*" 2>/dev/null '
                  f'| sort | while read -r f; do shasum -a 256 "$f" 2>/dev/null || sha256sum "$f"; done')
        res = self.runner.sh(script, posixpath.join(self.root, subdir) if subdir else self.root, mutating=False)
        return parse_sha256sum(res.out)

    def plan_diff(self, files: dict[str, bytes], subdir: str) -> list[str]:
        return diff_hashes(hashes(files), self.remote_hashes(subdir))

    # ---- planned marker (spec 2.5.6)
    def mark_planned(self) -> None:
        path = posixpath.join(self.spec["config_dir"], "state", "planned")
        self.runner.sh('mkdir -p "$(dirname "$1")" && echo "$2" > "$1"', path, str(int(self.now())), check=True)

    # ---- Pi
    def stage_pi(self, allow_dirty: bool = False, run_check: bool = True) -> str:
        name, files = self.collect(allow_dirty)
        dest = posixpath.join(self.root, "releases", name)
        blob = build_tar(files, self.release_info(name))
        self.runner.sh('rm -rf "$1" && mkdir -p "$1" && tar -xf - -C "$1"', dest, input=blob, check=True)
        if run_check and not self.runner.dry_run:
            py = self.python_for_check or posixpath.join(self.root, "venv", "bin", "python")
            res = self.runner.sh('cd "$1" && PYTHONPATH="$1" "$2" -B -m mirakit.cli check --fast', dest, py,
                                 mutating=False)
            if not res.ok:
                raise DeployError(f"mira check --fast failed in the staged release {name}: "
                                  f"{(res.out + res.err).strip()[-400:]}")
        self.out(f"staged {name} in {dest}")
        return name

    def _current(self) -> Optional[str]:
        res = self.runner.sh('readlink "$1"', posixpath.join(self.root, "current"), mutating=False)
        return posixpath.basename(res.out.strip()) if res.ok and res.out.strip() else None

    def _point(self, name: str) -> None:
        self.runner.sh('ln -sfn "releases/$2" "$1/current"', self.root, name, check=True)

    def _restart_pi(self) -> None:
        unit = self.spec["unit"]
        self.runner.sh(f'XDG_RUNTIME_DIR="/run/user/$(id -u)" systemctl --user restart {shlex.quote(unit)}', check=True)

    def _wait_for_startup(self, since: float) -> bool:
        pattern = re.compile(self.spec.get("startup_regex", "mira-listen up"))
        deadline = self.now() + float(self.spec.get("wait_s", 20))
        unit = self.spec["unit"]
        while True:
            res = self.runner.sh(
                f'XDG_RUNTIME_DIR="/run/user/$(id -u)" journalctl --user -u {shlex.quote(unit)} --since "@$1" '
                f'--no-pager -o cat', str(int(since)), mutating=False)
            if pattern.search(res.out):
                return True
            if self.now() >= deadline:
                return False
            self.sleep(1.0)

    def activate_pi(self, name: str) -> None:
        if not re.fullmatch(r"[0-9a-zA-Z._-]+", name):
            raise DeployError(f"not a release name: {name!r}")
        if not self.runner.exists(posixpath.join(self.root, "releases", name), "d") and not self.runner.dry_run:
            raise DeployError(f"release {name} is not staged on the Pi (run --stage first)")
        previous = self._current()
        self.mark_planned()
        started = self.now()
        self._point(name)
        if previous:
            self.runner.write_bytes(posixpath.join(self.root, "releases", ".previous"), (previous + "\n").encode())
        self._restart_pi()
        if self.runner.dry_run:
            self.out(f"dry run: would activate {name}")
            return
        if self._wait_for_startup(started):
            self.out(f"activated {name}: startup line seen")
            return
        self.out(f"no startup line within {self.spec.get('wait_s', 20)} s: flipping back to {previous or '(none)'}")
        if previous:
            self.mark_planned()
            self._point(previous)
            self._restart_pi()
        raise DeployError(f"release {name} did not start; "
                          + (f"flipped back to {previous}" if previous else "no previous release to flip back to"))

    def rollback_pi(self, name: Optional[str] = None) -> str:
        if name is None:
            raw = self.runner.read_bytes(posixpath.join(self.root, "releases", ".previous"))
            name = raw.decode().strip() if raw else ""
            if not name:
                raise DeployError("no previous release recorded; pass the release name to --rollback")
        self.activate_pi(name)
        return name

    # ---- mini
    def deploy_speech(self, allow_dirty: bool = False) -> str:
        """Snapshot, back up server.py, copy the new files. Never restarts: the owner picks the moment."""
        name, files = self.collect(allow_dirty)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now()))
        today = time.strftime("%Y-%m-%d", time.localtime(self.now()))
        snap = posixpath.join(self.root, "snapshots", f"{stamp}.tar")
        dsts = " ".join(shlex.quote(d) for _, d in self.entries)
        # a fresh install has nothing to snapshot yet; tar with no operands would fail the whole deploy
        self.runner.sh(f'mkdir -p "$1/snapshots" && cd "$1" && have="$(for p in {dsts}; do [ -e "$p" ] && echo "$p"; '
                       f'done)"; [ -z "$have" ] || tar -cf "$2" $have', self.root, snap, check=True)
        bak = posixpath.join(self.root, f"server.py.bak-plugins-{today}")
        self.runner.sh('[ -e "$2" ] || { [ -e "$1/server.py" ] && cp -p "$1/server.py" "$2"; }; true', self.root, bak,
                       check=True)
        blob = build_tar(files, self.release_info(name))
        self.runner.sh('mkdir -p "$1" && tar -xf - -C "$1"', self.root, input=blob, check=True)
        self.out(f"copied {name} to {self.root}; snapshot {snap}")
        self.out(self.kickstart_hint())
        return name

    def rollback_speech(self, snapshot: Optional[str] = None) -> str:
        if snapshot is None:
            res = self.runner.sh('ls -1 "$1/snapshots"/*.tar 2>/dev/null | sort | tail -n 1', self.root, mutating=False)
            snapshot = res.out.strip()
        if not snapshot:
            raise DeployError("no snapshot to restore")
        if "/" not in snapshot:
            snapshot = posixpath.join(self.root, "snapshots", snapshot)
        self.runner.sh('cd "$1" && tar -xf "$2"', self.root, snapshot, check=True)
        self.runner.sh('b="$(ls -1 "$1"/server.py.bak-plugins-* 2>/dev/null | sort | tail -n 1)"; '
                       '[ -n "$b" ] && cp -p "$b" "$1/server.py"; true', self.root, check=True)
        self.out(f"restored {snapshot} and server.py from its backup")
        self.out(self.kickstart_hint())
        return snapshot

    def kickstart_hint(self) -> str:
        return (f"nothing was restarted; when you are ready: launchctl kickstart -k "
                f"gui/{self.spec.get('uid', 501)}/{self.spec.get('label', 'org.aryavora.mira-speech')}")

    def dry_run_speech(self, allow_dirty: bool = False) -> list[str]:
        _, files = self.collect(allow_dirty)
        return self.plan_diff(files, "")

    def dry_run_pi(self, allow_dirty: bool = False) -> list[str]:
        _, files = self.collect(allow_dirty)
        return self.plan_diff(files, "current")
