"""control.http: the knobs and a start() that calls mirakit.control lazily (faked here: P6 writes the real one)."""
import subprocess
import sys
import types

import pytest

import test_lp_common as H
from listener_plugins import control_http as ch
from mirakit.plugin import ConfigError
from mirakit.testkit import FakeCore

SPEC = ch.SPEC


class Handle:
    def __init__(self):
        self.stopped = 0
        self.address = ("127.0.0.1", 8650)

    def stop(self):
        self.stopped += 1


def _fake_control(monkeypatch):
    import mirakit
    mod = types.ModuleType("mirakit.control")
    mod.started = []
    mod.remotes = []

    def start_server(core, cfg, *, machine, log, bind=(), port=0):
        mod.started.append({"core": core, "cfg": cfg, "machine": machine, "log": log})
        mod.handle = Handle()
        return mod.handle

    class HttpRemote:
        def __init__(self, base_url, token_file, timeout_s=15.0):
            self.base_url, self.token_file = base_url, token_file
            mod.remotes.append(self)

    mod.start_server, mod.HttpRemote = start_server, HttpRemote
    # `from mirakit import control` reads the package attribute first, so both places are patched
    monkeypatch.setitem(sys.modules, "mirakit.control", mod)
    monkeypatch.setattr(mirakit, "control", mod, raising=False)
    return mod


def test_start_calls_the_server_registers_the_remote_and_stops_through_the_effect(monkeypatch):
    mod = _fake_control(monkeypatch)
    ctx = H.make_ctx()
    core = FakeCore(H.lp.specs())
    ctx.provide("core", core)
    plugin, ctx, cfg = H.started("control.http", ctx=ctx, speech_url="http://127.0.0.1:9", speech_token_file="/t")
    assert len(mod.started) == 1
    call = mod.started[0]
    assert call["core"] is core and call["machine"] == "listener" and call["cfg"] is plugin.cfg and call["log"] is ctx.log
    assert core.remotes["speech"] is mod.remotes[0]
    assert (mod.remotes[0].base_url, mod.remotes[0].token_file) == ("http://127.0.0.1:9", "/t")
    assert plugin.status() == {"address": "('127.0.0.1', 8650)"}
    assert mod.handle.stopped == 0 and "control http" in ctx.effect_labels
    ctx.release_all()
    assert mod.handle.stopped == 1


def test_importing_the_plugin_does_not_import_mirakit_control():
    code = ("import sys; sys.path.insert(0, %r); import listener_plugins as lp; lp.specs(); "
            "import listener_plugins.control_http; print('mirakit.control' in sys.modules)" % str(H.WALL))
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_spec_flags():
    assert SPEC.required is False and SPEC.default_enabled is False and SPEC.toggle == "live" and SPEC.provides == ()
    assert all(k.agent == "deny" for k in SPEC.knobs)
    k = {x.name: x for x in SPEC.knobs}
    assert k["preview_quiet"].default == ""  # addendum: no quiet window unless the owner sets one
    assert (k["port"].default, k["bind"].default, k["agent_writes_per_10min"].default) == (8650, "127.0.0.1", 12)
    assert k["bind"].applies == "restart" and k["port"].applies == "restart"
    assert [n for n, x in k.items() if x.secret] == ["owner_token_file", "agent_token_file", "speech_token_file"]
    assert len(SPEC.knobs) == 25


def _values(**over):
    v = {k.name: k.default for k in SPEC.knobs}
    v.update(over)
    return v


def test_validate_rules():
    SPEC.validate(_values())
    SPEC.validate(_values(trial_default_s=600, trial_max_s=600))
    with pytest.raises(ConfigError) as e:
        SPEC.validate(_values(trial_default_s=900, trial_max_s=600))
    assert e.value.field == "trial_default_s"
    with pytest.raises(ConfigError) as e:
        SPEC.validate(_values(speech_url="http://192.168.1.5:8765"))
    assert e.value.field == "speech_url"
    SPEC.validate(_values(speech_url="http://100.77.98.89:8765"))
    with pytest.raises(ConfigError) as e:
        SPEC.validate(_values(bind="0.0.0.0"))
    assert e.value.field == "bind"
    SPEC.validate(_values(bind="::1"))
    SPEC.validate(_values(bind="127.0.0.2"))


def _real_plugin(tmp_path, **over):
    import socket
    from mirakit import control
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ctx = H.make_ctx()
    core = FakeCore(H.lp.specs())
    ctx.provide("core", core)
    tok = tmp_path / "t"
    tok.write_text("x")
    plugin, ctx, cfg = H.started("control.http", ctx=ctx, port=port, owner_token_file=str(tok),
                                 agent_token_file=str(tok), speech_token_file=str(tok),
                                 speech_url="http://127.0.0.1:9", **over)
    return control, plugin, ctx, core


def test_apply_pushes_the_new_cfg_into_the_running_real_server(tmp_path):
    from mirakit.testkit import cfg_for
    control, plugin, ctx, core = _real_plugin(tmp_path)
    try:
        assert plugin.handle.api.cfg.agent_requires_voice_turn is True
        new = cfg_for(SPEC, port=plugin.cfg.port, agent_requires_voice_turn=False, wait_s_default=2.0,
                      speech_url="http://127.0.0.1:9", owner_token_file=plugin.cfg.owner_token_file,
                      agent_token_file=plugin.cfg.agent_token_file, speech_token_file=plugin.cfg.speech_token_file)
        plugin.apply({"agent_requires_voice_turn": False, "wait_s_default": 2.0}, new)
        assert plugin.handle.api.cfg is new and plugin.handle.api.cfg.agent_requires_voice_turn is False
        assert plugin.cfg is new
    finally:
        ctx.release_all()


def test_apply_reregisters_the_speech_remote_only_when_its_knobs_change(tmp_path):
    from mirakit.testkit import cfg_for
    control, plugin, ctx, core = _real_plugin(tmp_path)
    try:
        first = core.remotes["speech"]
        base = dict(port=plugin.cfg.port, owner_token_file=plugin.cfg.owner_token_file,
                    agent_token_file=plugin.cfg.agent_token_file, speech_token_file=plugin.cfg.speech_token_file)
        plugin.apply({"agents_locked": True}, cfg_for(SPEC, speech_url="http://127.0.0.1:9", agents_locked=True, **base))
        assert core.remotes["speech"] is first
        plugin.apply({"speech_url": "http://127.0.0.1:10"}, cfg_for(SPEC, speech_url="http://127.0.0.1:10", **base))
        assert core.remotes["speech"] is not first and core.remotes["speech"].base_url == "http://127.0.0.1:10"
    finally:
        ctx.release_all()
