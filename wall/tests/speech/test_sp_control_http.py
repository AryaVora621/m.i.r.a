"""control.http (speech): the two token files and the control service."""
import logging

import pytest

import sp_fakes as fk
from sp_fakes import real_config_dir_untouched  # noqa: F401  (autouse guard)
from mirakit.plugin import ConfigError
from mirakit.testkit import FakeCtx, cfg_for
from speech_plugins import control_http as ch


def make(tmp_path, **over):
    ctx = FakeCtx("speech")
    fk.token_file(tmp_path / "owner", "owner-secret")
    fk.token_file(tmp_path / "pi", "pi-secret")
    cfg = cfg_for(ch.SPEC, owner_token_file=str(tmp_path / "owner"), pi_token_file=str(tmp_path / "pi"), **over)
    ctx.cfgs[ch.SPEC.name] = cfg
    p = ch.SPEC.create(ctx, cfg)
    p.start()
    return ctx, p


def test_spec_is_optional_and_off_by_default_with_the_documented_defaults():
    s = ch.SPEC
    assert s.required is False and s.default_enabled is False and s.toggle == "live" and s.machine == "speech"
    cfg = cfg_for(s)
    assert cfg.owner_token_file == "~/.config/mira/control.owner.token" and cfg.pi_token_file == "~/.config/mira/control.pi.token"
    assert (cfg.trial_default_s, cfg.trial_max_s, cfg.agent_writes_per_10min, cfg.busy_defer_s) == (600.0, 1800.0, 12, 10.0)
    assert (cfg.history_keep, cfg.changelog_max_bytes) == (50, 5000000)
    assert all(k.agent == "deny" and k.applies == "live" for k in s.knobs)
    assert {k.name for k in s.knobs if k.secret} == {"owner_token_file", "pi_token_file"}


def test_scope_for_maps_each_token_file_and_nothing_else(tmp_path):
    ctx, p = make(tmp_path)
    ctl = ctx.get("control")
    assert ctl.scope_for("Bearer owner-secret") == "owner" and ctl.scope_for("bearer pi-secret") == "agent"
    assert ctl.scope_for("Bearer other") is None and ctl.scope_for("") is None and ctl.scope_for("owner-secret") is None
    assert ctl.scope_for("Bearer ") is None


def test_an_empty_or_missing_token_file_matches_nothing(tmp_path):
    ctx, p = make(tmp_path)
    (tmp_path / "pi").write_text("")
    ctl = ctx.get("control")
    assert ctl.scope_for("Bearer ") is None
    (tmp_path / "owner").unlink()
    assert ctl.scope_for("Bearer owner-secret") is None


def test_tokens_are_reread_without_a_restart(tmp_path):
    ctx, p = make(tmp_path)
    ctl = ctx.get("control")
    fk.token_file(tmp_path / "owner", "new-owner")
    assert ctl.scope_for("Bearer owner-secret") is None and ctl.scope_for("Bearer new-owner") == "owner"
    p.apply({"pi_token_file": str(tmp_path / "owner")}, p.cfg.replace(pi_token_file=str(tmp_path / "owner")))
    assert ctl.scope_for("Bearer new-owner") == "owner"            # owner is checked first


def test_start_logs_a_missing_token_file_but_still_provides(tmp_path, caplog):
    ctx = FakeCtx("speech")
    cfg = cfg_for(ch.SPEC, owner_token_file=str(tmp_path / "nope"), pi_token_file=str(tmp_path / "nope2"))
    with caplog.at_level(logging.WARNING):
        p = ch.SPEC.create(ctx, cfg)
        p.start()
    assert "nope" in caplog.text and ctx.has("control")


def test_default_paths_expand_through_the_context_home(tmp_path, caplog):
    ctx = FakeCtx("speech")
    with caplog.at_level(logging.WARNING):
        ch.SPEC.create(ctx, cfg_for(ch.SPEC)).start()
    assert str(ctx.paths.home) in caplog.text


def test_validate_requires_trial_max_at_least_default():
    ch.SPEC.validate({"trial_default_s": 600, "trial_max_s": 600})
    with pytest.raises(ConfigError) as e:
        ch.SPEC.validate({"trial_default_s": 900, "trial_max_s": 600})
    assert e.value.field == "trial_max_s"
