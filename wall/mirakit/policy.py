"""Who may change what (spec 2.5.5, the normative rule table), plus the rate windows and previews rules.

Default is deny. A refusal is a PolicyError whose `message` is safe to read aloud (no jargon, no values the
owner did not already hear), `allowed` is {"min","max","step"} for a bounded numeric knob, and for a knob the
agent may never set `owner_command` is the exact `mira set ...` line the skill files for the owner.

Scopes: owner (CLI, owner token, file reload), agent (Hermes by voice), system (expiry, boot guard: reverts
only). The core passes `in_turn` (is a voice turn in its brain phase); on the speech machine the Pi has
already applied the voice-turn rule, so the core passes True there.
"""
from __future__ import annotations

import json
import shlex
import threading
from collections import deque
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from .plugin import Knob, PolicyError
from .schema import fmt_num, fmt_value, in_time_window, spoken_name

WINDOW_S = 600.0
_EPS = 1e-9

MSG_LOCKED = "Settings changes are locked right now."
MSG_TURN = "I can only change my settings while we're talking on the wall."
MSG_RATE = "That's enough changes for now; try again in a few minutes."
MSG_CAP = "That's the most changes I may make in one session."
MSG_QUIET = "I can't play samples during the quiet hours that are set."
MSG_PREVIEWS = "That's enough samples for now; try again in a few minutes."


@dataclass(frozen=True)
class Settings:
    """The control.http knobs the core reads. The plugin may be disabled or absent (synthetic spec sets,
    the speech machine), so every field has the spec default."""
    agents_locked: bool = False
    agent_requires_voice_turn: bool = True
    agent_writes_per_10min: int = 12
    agent_writes_per_knob_10min: int = 3
    agent_undo_window_s: float = 86400.0
    trial_default_s: float = 600.0
    trial_max_s: float = 1800.0
    session_max_s: float = 900.0
    session_max_changes: int = 12
    session_followup_s: float = 15.0
    session_idle_s: float = 120.0
    rebuild_quiet_s: float = 10.0
    previews_per_10min: int = 6
    preview_max_chars: int = 200
    preview_quiet: str = ""
    preview_text: str = "This is {alias}. This is how I would sound."
    changelog_max_bytes: int = 5_000_000
    history_keep: int = 50
    wait_s_default: float = 5.0
    busy_defer_s: float = 10.0


_FIELD_NAMES = {f.name for f in fields(Settings)}


def settings_from(values: Optional[Mapping[str, Any]]) -> Settings:
    if not values:
        return Settings()
    return Settings(**{k: v for k, v in values.items() if k in _FIELD_NAMES})


def owner_command(key: str, value: Any, secret: bool = False) -> str:
    # a secret knob (a token file path) must not leave the process in a line the agent reads aloud or files
    if secret:
        return f"mira set {key}=<value> --trial 600"
    text = value if isinstance(value, str) else json.dumps(value) if isinstance(value, (list, dict)) else fmt_num(value)
    return f"mira set {key}={shlex.quote(text)} --trial 600"


class Policy:
    def __init__(self, machine: str, get_settings: Callable[[], Settings], clock: Callable[[], float]) -> None:
        self.machine = machine
        self._settings = get_settings
        self._clock = clock
        self._lock = threading.Lock()
        self._writes: deque[tuple[float, tuple[str, ...]]] = deque()
        self._previews: deque[float] = deque()

    @property
    def settings(self) -> Settings:
        return self._settings()

    # ---- shared checks
    def _locked(self, scope: str) -> None:
        if scope == "agent" and self.settings.agents_locked:
            raise PolicyError("agents_locked", MSG_LOCKED)

    def _turn(self, scope: str, in_turn: bool) -> None:
        if scope == "agent" and self.settings.agent_requires_voice_turn and not in_turn:
            raise PolicyError("not_in_voice_turn", MSG_TURN)

    def _prune(self, now: float) -> None:
        while self._writes and now - self._writes[0][0] >= WINDOW_S:
            self._writes.popleft()
        while self._previews and now - self._previews[0] >= WINDOW_S:
            self._previews.popleft()

    # ---- writes
    def check_knob(self, knob: Knob, key: str, old: Any, new: Any,
                   voice_ok: Optional[Callable[[Any], bool]] = None) -> None:
        """One edit in the agent scope: bounded knobs only, agent range and step, voice rule."""
        spoken = spoken_name(knob)
        if knob.agent != "bounded":
            raise PolicyError("policy", f"I'm not allowed to change {spoken} by voice.", field=key,
                              owner_command=owner_command(key, new, secret=knob.secret))
        if knob.type in ("int", "float") and new is not None:
            allowed = {"min": knob.agent_min, "max": knob.agent_max, "step": knob.agent_step}
            unit = f" {knob.unit}" if knob.unit else ""
            if new < knob.agent_min - _EPS or new > knob.agent_max + _EPS:
                raise PolicyError(
                    "policy", f"By voice I can only set {spoken} between {fmt_num(knob.agent_min)} and "
                              f"{fmt_num(knob.agent_max)}{unit}.", field=key, allowed=allowed)
            if old is not None and abs(new - old) > knob.agent_step + _EPS:
                raise PolicyError("policy", f"I can only move {spoken} by {fmt_num(knob.agent_step)}{unit} at a time.",
                                  field=key, allowed=allowed)
        elif knob.type == "voice":
            if voice_ok is None or not voice_ok(new):
                raise PolicyError("policy", "I'm not allowed to use that voice by voice command.", field=key,
                                  owner_command=owner_command(key, new, secret=knob.secret))

    def check_write(self, scope: str, items: Sequence[Mapping[str, Any]], *, in_turn: bool,
                    session_changes: int = 0, voice_ok: Optional[Callable[[Any], bool]] = None) -> None:
        """Steps of rule 1 to 3 for a write. `items`: {"key", "knob", "old", "new"} with knob None for the
        pseudo key `<plugin>.enabled` (owner only). Raises PolicyError; records nothing."""
        if scope == "system":
            raise PolicyError("policy", "Only a revert may be made by the system.")
        if scope != "agent":
            return
        self._locked(scope)
        for it in items:
            knob = it["knob"]
            if knob is None:
                raise PolicyError("policy", "I'm not allowed to switch parts on or off by voice.", field=it["key"])
            self.check_knob(knob, it["key"], it["old"], it["new"], voice_ok)
        self._turn(scope, in_turn)
        s = self.settings
        now = self._clock()
        with self._lock:
            self._prune(now)
            if len(self._writes) >= s.agent_writes_per_10min:
                raise PolicyError("rate_limited", MSG_RATE)
            for it in items:
                used = sum(1 for _t, keys in self._writes if it["key"] in keys)
                if used >= s.agent_writes_per_knob_10min:
                    raise PolicyError("rate_limited", MSG_RATE, field=it["key"])
        if session_changes >= s.session_max_changes:
            raise PolicyError("session_cap", MSG_CAP)

    def record_write(self, keys: Sequence[str]) -> None:
        with self._lock:
            self._writes.append((self._clock(), tuple(keys)))

    def trial_for(self, scope: str, requested: Optional[float]) -> Optional[float]:
        s = self.settings
        if scope == "agent":
            return min(max(float(requested or s.trial_default_s), 60.0), s.trial_max_s)
        if requested is None:
            return None
        return min(max(float(requested), 1.0), 7200.0)

    # ---- undo, confirm, sessions
    def check_undo_target(self, scope: str, change: Mapping[str, Any], *, force: bool, age_s: float) -> None:
        if scope != "agent":
            return
        if force:
            raise PolicyError("policy", "Only the owner may force an undo.")
        if change.get("scope") != "agent" or age_s > self.settings.agent_undo_window_s:
            raise PolicyError("policy", "I'm not allowed to undo or confirm that change by voice.",
                              field=str(change.get("id", "")))

    def check_confirm(self, scope: str, *, in_turn: bool) -> None:
        self._locked(scope)
        self._turn(scope, in_turn)

    def check_session(self, action: str, scope: str, *, in_turn: bool) -> None:
        if action in ("start", "keep"):
            self._locked(scope)
            self._turn(scope, in_turn)
        # get, revert and end only move back toward the owner's values: allowed at any time

    # ---- previews
    def check_preview(self, scope: str, *, in_turn: bool, now_local: datetime) -> None:
        self._locked(scope)
        if scope != "agent":
            return
        self._turn(scope, in_turn)
        s = self.settings
        if s.preview_quiet and in_time_window(s.preview_quiet, now_local):
            raise PolicyError("quiet_hours", MSG_QUIET)
        now = self._clock()
        with self._lock:
            self._prune(now)
            if len(self._previews) >= s.previews_per_10min:
                raise PolicyError("rate_limited", MSG_PREVIEWS)

    def record_preview(self) -> None:
        with self._lock:
            self._previews.append(self._clock())

    # ---- owner-only operations
    def need_owner(self, scope: str, what: str = "") -> None:
        if scope == "agent":
            raise PolicyError("policy", "I'm not allowed to do that by voice.", field=what)
        if scope == "system":
            raise PolicyError("policy", "Only the owner may do that.", field=what)
