"""The change log (spec 2.5.1, 2.5.3, 2.5.6): `changes.jsonl`, one JSON line per change plus status lines.

Appends are fsynced, the file is mode 0600 and rotates to `.1` above `max_bytes` (one old generation). Readers
merge status lines into their change by id, reading `.1` first and the live file after it. `path=None` is the
in-memory mode used by Core.from_args: same behaviour, nothing on disk.

Privacy: a record holds knob keys, old and new values, the actor and a reason of at most 120 characters. No
transcript or reply text is ever stored here.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from .plugin import Change

log = logging.getLogger("mira.changelog")

TS_FMT = "%Y-%m-%dT%H:%M:%S%z"
LIVE_STATUSES = ("queued", "applied", "pending_restart")


def fmt_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime(TS_FMT)


def parse_ts(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.strptime(text, TS_FMT).timestamp()
    except ValueError:
        return None


def _num_of(ident: str, prefix: str) -> int:
    if ident.startswith(prefix + "-"):
        tail = ident[len(prefix) + 1:]
        if tail.isdigit():
            return int(tail)
    return 0


class ChangeLog:
    def __init__(self, path: Optional[Path], machine: str, max_bytes: int = 5_000_000) -> None:
        self.path = Path(path) if path is not None else None
        self.machine = machine
        self.max_bytes = max_bytes
        self.prefix = "L" if machine == "listener" else "S"
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._max: dict[str, int] = {"L": 0, "S": 0, "V": 0, "T": 0}
        self._mem_lines: list[dict] = []
        if self.path is not None:
            self.load()

    @property
    def rotated(self) -> Optional[Path]:
        return Path(str(self.path) + ".1") if self.path else None

    # ---- reading
    def _read_lines(self, path: Path) -> Iterable[dict]:
        try:
            fh = open(path, "rb")
        except OSError:
            return
        with fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except ValueError:
                    log.warning("skipping an unreadable line in %s", path.name)
                    continue
                if isinstance(rec, dict) and "id" in rec:
                    yield rec

    def _merge(self, rec: dict) -> None:
        ident = rec["id"]
        if "edits" in rec:
            self._records[ident] = dict(rec)
            for prefix in self._max:
                self._max[prefix] = max(self._max[prefix], _num_of(ident, prefix))
            if rec.get("session"):
                self._max["V"] = max(self._max["V"], _num_of(rec["session"], "V"))
            return
        base = self._records.get(ident)
        if base is None:
            return  # the full record rotated away; the status alone says nothing useful
        for k, v in rec.items():
            if k in ("id", "at"):
                continue
            base[k] = v

    def load(self) -> None:
        with self._lock:
            self._records = {}
            if self.path is None:
                for rec in self._mem_lines:
                    self._merge(rec)
                return
            for p in (self.rotated, self.path):
                for rec in self._read_lines(p):
                    self._merge(rec)

    def all(self) -> list[dict]:
        """Every merged record, oldest first (copies)."""
        with self._lock:
            return [dict(r, edits=[dict(e) for e in r.get("edits", [])]) for r in self._records.values()]

    def get(self, ident: str) -> Optional[dict]:
        with self._lock:
            r = self._records.get(ident)
            return dict(r, edits=[dict(e) for e in r.get("edits", [])]) if r else None

    def position(self, ident: str) -> Optional[int]:
        with self._lock:
            for i, k in enumerate(self._records):
                if k == ident:
                    return i
        return None

    # ---- ids
    def peek_id(self, prefix: Optional[str] = None) -> str:
        prefix = prefix or self.prefix
        width = 4 if prefix == "V" else 6
        with self._lock:
            return f"{prefix}-{self._max[prefix] + 1:0{width}d}"

    def next_id(self, prefix: Optional[str] = None) -> str:
        """Max id in the log plus one, then a counter in memory (the log is only read at start)."""
        prefix = prefix or self.prefix
        width = 4 if prefix == "V" else 6
        with self._lock:
            self._max[prefix] += 1
            return f"{prefix}-{self._max[prefix]:0{width}d}"

    # ---- writing
    def _write(self, rec: dict) -> None:
        if self.path is None:
            self._mem_lines.append(json.loads(json.dumps(rec)))
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _maybe_rotate(self) -> None:
        if self.path is None:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= self.max_bytes:
            return
        os.replace(self.path, self.rotated)
        os.chmod(self.rotated, 0o600)
        self.load()

    def append(self, change: Change | Mapping[str, Any]) -> dict:
        rec = change.to_dict() if isinstance(change, Change) else dict(change)
        with self._lock:
            self._maybe_rotate()
            self._write(rec)
            self._merge(rec)
        return rec

    def status(self, ident: str, status: str, *, error: Optional[str] = None, at: Optional[float] = None,
               **extra: Any) -> None:
        """Append a status line; readers merge it into the change."""
        rec: dict[str, Any] = {"id": ident, "status": status, "at": fmt_ts(at if at is not None else _now()),
                               "error": error}
        rec.update(extra)
        with self._lock:
            self._write(rec)
            self._merge(rec)

    # ---- queries
    def trials(self) -> list[dict]:
        """Changes that still carry a running trial (not undone, not confirmed, not themselves an undo)."""
        return [r for r in self.all()
                if r.get("trial_expires") and r.get("status") in LIVE_STATUSES and not r.get("undo_of")]

    def session_changes(self, session: str) -> list[dict]:
        return [r for r in self.all() if r.get("session") == session]


def _now() -> float:
    import time
    return time.time()


# ---------------------------------------------------------------------------------------------
# boot replay and boot guard planning (pure; the core applies the plans)
# ---------------------------------------------------------------------------------------------

@dataclass
class RevertPlan:
    change: dict
    reason: str                                   # "expiry" or "boot_guard"
    edits: list[dict] = field(default_factory=list)     # [{"key", "old", "new"}] already inverted, to write back
    skipped: list[str] = field(default_factory=list)    # keys that moved on and were left alone


def plan_reverts(records: list[dict], current: Callable[[str], Any], now: float, *,
                 guard_window_s: Optional[float] = None, expire: bool = True) -> list[RevertPlan]:
    """Which changes to revert at boot, newest first. A trial whose expiry passed (`expire`) and, when the
    boot guard runs (`guard_window_s` given), every agent change younger than the window. A revert only
    touches keys that still hold the change's `new` value: a key that moved on is skipped and reported, never
    clobbered (an owner edit made after an agent trial stands)."""
    chosen: list[tuple[dict, str]] = []
    for rec in reversed(records):
        if rec.get("undo_of") or rec.get("status") not in LIVE_STATUSES + ("confirmed",):
            continue
        ts = parse_ts(rec.get("ts")) or 0.0
        if guard_window_s is not None and rec.get("scope") == "agent" and ts >= now - guard_window_s:
            chosen.append((rec, "boot_guard"))
            continue
        if expire and rec.get("status") in LIVE_STATUSES:
            due = parse_ts(rec.get("trial_expires"))
            if due is not None and due <= now:
                chosen.append((rec, "expiry"))
    work: dict[str, Any] = {}
    plans: list[RevertPlan] = []
    for rec, reason in chosen:
        plan = RevertPlan(rec, reason)
        for edit in reversed(rec.get("edits", [])):
            key = edit["key"]
            have = work[key] if key in work else current(key)
            if have == edit["new"]:
                plan.edits.append({"key": key, "old": edit["new"], "new": edit["old"]})
                work[key] = edit["old"]
            else:
                plan.skipped.append(key)
        plans.append(plan)
    return plans
