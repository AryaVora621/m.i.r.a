"""Plugin control.http (speech machine): the owner and Pi token files behind /ctl/v1 on the speech server.

It binds no port: the routes ride on server.http's existing ThreadingHTTPServer, which asks this plugin's
"control" service which scope a bearer token has and then hands the request to mirakit.control.handle. The two
token files are read on every request, so rotating one needs no restart. The data-plane speech.token is not a
control token and is never accepted here.

FIXED: scope names "owner" (owner_token_file) and "agent" (pi_token_file).
"""
from __future__ import annotations

import hmac
from typing import Optional

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginConfig, PluginSpec

from . import expand_path


class ControlTokens:
    """The "control" service object."""

    def __init__(self, plugin: "ControlHttpPlugin") -> None:
        self._p = plugin
        self.priority = 0
        self.name = "control.http"

    def _token(self, path: str) -> bytes:
        try:
            return expand_path(path, self._p.ctx.paths).read_text(encoding="utf-8").strip().encode()
        except OSError:
            return b""

    def scope_for(self, authorization: str) -> Optional[str]:
        """"owner", "agent" or None for an Authorization header value. Both files are always compared."""
        if not authorization.lower().startswith("bearer "):
            return None
        got = authorization[7:].strip().encode()
        cfg = self._p.cfg
        owner, agent = self._token(cfg.owner_token_file), self._token(cfg.pi_token_file)
        is_owner = bool(owner) and hmac.compare_digest(got, owner)
        is_agent = bool(agent) and hmac.compare_digest(got, agent)
        return "owner" if is_owner else ("agent" if is_agent else None)


class ControlHttpPlugin(PluginBase):
    def start(self) -> None:
        for key in ("owner_token_file", "pi_token_file"):
            path = expand_path(self.cfg[key], self.ctx.paths)
            if not path.exists():
                self.ctx.log.warning("control token file %s does not exist; run mira tokens install", path)
        self.ctx.provide("control", ControlTokens(self))


def _validate(values) -> None:
    if values.get("trial_max_s", 0) < values.get("trial_default_s", 0):
        raise ConfigError("trial_max_s must be at least trial_default_s", "trial_max_s")


SPEC = PluginSpec(
    name="control.http", kind="control", version="1", machine="speech",
    summary="owner and Pi token files for the /ctl/v1 control routes served by server.http",
    provides=("control",), create=lambda ctx, cfg: ControlHttpPlugin(ctx, cfg),
    knobs=(
        Knob("owner_token_file", "path", "~/.config/mira/control.owner.token", "Token file of the owner scope.",
             secret=True, topic="system"),
        Knob("pi_token_file", "path", "~/.config/mira/control.pi.token",
             "Token file the Pi uses; it gets the agent scope.", secret=True, topic="system"),
        Knob("trial_default_s", "float", 600.0, "How long a trial change lasts when none is asked for.",
             unit="s", min=60, max=3600, topic="system"),
        Knob("trial_max_s", "float", 1800.0, "The longest trial a change may ask for.",
             unit="s", min=60, max=7200, topic="system"),
        Knob("agent_writes_per_10min", "int", 12, "Changes the agent scope may make in ten minutes.",
             unit="changes", min=0, max=60, topic="system"),
        Knob("busy_defer_s", "float", 10.0,
             "A trial expiry swap waits until no speech or voice request finished within this time.",
             unit="s", min=0, max=120, topic="system"),
        Knob("history_keep", "int", 50, "Previous config files kept in the history folder.",
             unit="files", min=5, max=500, topic="system"),
        Knob("changelog_max_bytes", "int", 5000000, "The change log is rotated when it grows past this size.",
             unit="bytes", min=100000, max=50000000, topic="system"),
    ),
    required=False, default_enabled=False, toggle="live",
    validate=_validate,
    tests=("wall/tests/speech/test_sp_control_http.py",),
)
