"""control.http: the Pi's control API (spec 2.7). The routes, auth and policy live in mirakit.control (P6); this
plugin owns the knobs and starts the server. mirakit.control is imported lazily inside start(), so listing the
specs and running `mira check` never need it.

FIXED: loopback bind only, two bearer scopes (owner, agent), one remote client to the speech machine.
"""

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

from .common import bind_is_loopback, host_is_loopback_or_tailnet

LEGACY_CLASSES = ()


class ControlHttp(PluginBase):
    def start(self):
        from mirakit import control
        core = self.ctx.get("core")
        h = control.start_server(core, self.cfg, machine="listener", log=self.ctx.log)
        self.handle = h
        self.ctx.effect(h.stop, "control http")
        core.register_remote("speech", control.HttpRemote(self.cfg.speech_url, self.cfg.speech_token_file))

    def apply(self, changed, cfg):
        # The running server reads its policy and limits from api.cfg, so a bare self.cfg swap would leave
        # agent_requires_voice_turn, trial limits and token files at their start-time values.
        self.cfg = cfg
        h = getattr(self, "handle", None)
        if h is not None:
            h.set_config(cfg)
        if "speech_url" in changed or "speech_token_file" in changed:
            from mirakit import control
            self.ctx.get("core").register_remote("speech", control.HttpRemote(cfg.speech_url, cfg.speech_token_file))

    def status(self):
        h = getattr(self, "handle", None)
        return {"address": str(getattr(h, "address", "")) if h is not None else ""}


def validate(values):
    if values["trial_default_s"] > values["trial_max_s"]:
        raise ConfigError("trial_default_s must not exceed trial_max_s", "trial_default_s")
    if not host_is_loopback_or_tailnet(values["speech_url"]):
        raise ConfigError("speech_url must be loopback or a Tailscale address (100.64.0.0/10)", "speech_url")
    if not bind_is_loopback(values["bind"]):
        raise ConfigError("bind must be a loopback address", "bind")


SPEC = PluginSpec(
    name="control.http", kind="control", version="1", machine="listener", provides=(),
    summary="The Pi's control API: read, tune, preview and undo.",
    create=lambda ctx, cfg: ControlHttp(ctx, cfg), required=False, default_enabled=False, toggle="live", order=200,
    validate=validate,
    knobs=(
        Knob("bind", "str", "127.0.0.1", "Address the control API listens on; loopback only.", applies="restart"),
        Knob("port", "int", 8650, "Port of the control API.", unit="port", min=1024, max=65535, applies="restart"),
        Knob("owner_token_file", "path", "~/.config/mira/control.owner.token", "Owner scope bearer file.", secret=True),
        Knob("agent_token_file", "path", "/etc/mira/control.agent.token", "Agent scope (Hermes) bearer file.",
           secret=True),
        Knob("speech_url", "url", "http://100.77.98.89:8765", "The mini's /ctl/v1 base."),
        Knob("speech_token_file", "path", "~/.config/mira/speech.control.token", "The mini's Pi token file.",
           secret=True),
        Knob("agents_locked", "bool", False, "Kill switch: every agent write and preview is refused."),
        Knob("agent_requires_voice_turn", "bool", True, "Agent writes and previews only during a voice turn's brain phase."),
        Knob("agent_writes_per_10min", "int", 12, "Agent writes allowed in ten minutes; undo and confirm are not counted.",
           unit="writes", min=0, max=60),
        Knob("agent_writes_per_knob_10min", "int", 3, "Agent writes to one knob in ten minutes.", unit="writes", min=1,
           max=20),
        Knob("agent_undo_window_s", "float", 86400, "Agents undo only agent changes this recent.", unit="s", min=600,
           max=604800),
        Knob("trial_default_s", "float", 600, "Trial length when none is given.", unit="s", min=60, max=3600),
        Knob("trial_max_s", "float", 1800, "Longest trial; at least trial_default_s.", unit="s", min=60, max=7200),
        Knob("session_max_s", "float", 900, "Hard cap on a voice tuning session.", unit="s", min=60, max=3600),
        Knob("session_max_changes", "int", 12, "Writes allowed in one session.", unit="writes", min=1, max=50),
        Knob("session_followup_s", "float", 15.0, "Follow-up window while a session is open.", unit="s", min=8, max=30),
        Knob("session_idle_s", "float", 120, "No voice turn and no agent call this long ends the session.", unit="s",
           min=30, max=900),
        Knob("rebuild_quiet_s", "float", 10.0, "No rebuild this soon after a wake or a turn.", unit="s", min=0, max=120),
        Knob("previews_per_10min", "int", 6, "Agent previews allowed in ten minutes.", unit="previews", min=0, max=30),
        Knob("preview_max_chars", "int", 200, "Longest preview text.", unit="chars", min=20, max=600),
        Knob("preview_quiet", "time_window", "", "Window in which agent previews are refused; empty is none."),
        Knob("preview_text", "str", "This is {alias}. This is how I would sound.", "Default sample sentence.",
           unit="chars", min=1, max=200),
        Knob("changelog_max_bytes", "int", 5000000, "Rotate changes.jsonl above this size.", unit="bytes", min=100000,
           max=50000000),
        Knob("history_keep", "int", 50, "Previous config files kept in history.", unit="files", min=5, max=500),
        Knob("wait_s_default", "float", 5.0, "How long a write waits for its apply.", unit="s", min=0, max=30),
    ),
    tests=("wall/tests/plugins/test_lp_control_http.py",),
)
