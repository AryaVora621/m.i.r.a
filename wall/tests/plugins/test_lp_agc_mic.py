"""agc.mic: MicAGC copied from v1 with the four knob-fed edits; sample-by-sample against the v1 twin."""
import numpy as np
import pytest

import test_lp_common as H
from listener_plugins import agc_mic as am
from mirakit.plugin import ConfigError

SPEC = am.SPEC


def _audio():
    rng = np.random.default_rng(11)
    speech = H.common.read_wav_float(H.CALENDAR_WAV)
    hiss = (rng.standard_normal(16000 * 6) * 0.003).astype(np.float32)
    zeros = np.zeros(16000 * 2, dtype=np.float32)
    loud = (np.sin(np.linspace(0, 4000, 16000 * 2)) * 0.95).astype(np.float32)
    quiet = speech * 0.08
    return np.concatenate([hiss, quiet, zeros, speech, hiss, loud, zeros, quiet, hiss])


KW = dict(target=0.05, max_gain=10.0, rest=3.0, release=0.25, decay=0.03, gate=1.8, min_rms=0.001, ceiling=0.9)


@pytest.mark.parametrize("extra", [{}, {"target": 0.08, "max_gain": 6.0, "rest": 2.0, "gate": 2.5}])
def test_process_matches_v1_per_sample(monkeypatch, extra):
    H.clean_env(monkeypatch)
    kw = dict(KW, **extra)
    a, b = H.v1.MicAGC(**kw), am.MicAGC(**kw)
    worst = 0.0
    for c in H.chunks_of(_audio()):
        ya, yb = a.process(c.copy()), b.process(c.copy())
        assert ya.dtype == yb.dtype and ya.shape == yb.shape
        worst = max(worst, float(np.max(np.abs(ya - yb))) if len(ya) else 0.0)
        assert (a.gain, a.floor, a.speech, a.level) == (b.gain, b.floor, b.speech, b.level)
    assert worst <= 1e-7


def test_default_constructor_reads_env_like_v1(monkeypatch):
    H.clean_env(monkeypatch)
    monkeypatch.setenv("MIRA_AGC_TARGET", "0.07")
    monkeypatch.setenv("MIRA_AGC_FLOOR", "0.004")
    a, b = H.v1.MicAGC(), am.MicAGC()
    assert (a.target, a.floor, a.max_gain, a.rest) == (b.target, b.floor, b.max_gain, b.rest)
    assert (b.floor_rate_speech, b.floor_rate_low, b.floor_rate_mid, b.level_scale) == (0.0005, 0.2, 0.05, 8.0)


def test_plugin_object_equals_v1_with_adopted_values(monkeypatch):
    H.clean_env(monkeypatch)
    monkeypatch.setenv("MIRA_AGC_FLOOR", "0.5")  # must not leak into the engine: floor_init wins
    plugin, ctx, cfg = H.started("agc.mic", target=0.06, floor_init=0.004)
    mine = ctx.get("agc")
    monkeypatch.delenv("MIRA_AGC_FLOOR")
    ref = H.v1.MicAGC(**dict(KW, target=0.06))
    ref.floor = 0.004
    assert mine.floor == 0.004
    for c in H.chunks_of(_audio()):
        assert np.max(np.abs(mine.process(c.copy()) - ref.process(c.copy())), initial=0.0) <= 1e-7


def test_off_provides_none():
    plugin, ctx, _ = H.started("agc.mic", on=False)
    assert ctx.has("agc") and ctx.get("agc") is None
    plugin.apply({"target": 0.06}, plugin.cfg.replace(target=0.06))  # no object, no error
    assert plugin.status()["on"] is False


def test_level_scale_comes_from_the_reporter_and_applies_on_change():
    ctx = H.make_ctx()
    ctx.cfgs["reporter.agentos"] = ctx.cfgs["reporter.agentos"].replace(level_scale=4.0)
    plugin, ctx, _ = H.started("agc.mic", ctx=ctx)
    agc = ctx.get("agc")
    assert agc.level_scale == 4.0 and "reporter.agentos.level_scale" in SPEC.reads
    ctx.cfgs["reporter.agentos"] = ctx.cfgs["reporter.agentos"].replace(level_scale=16.0)
    plugin.apply({"reporter.agentos.level_scale": 16.0}, plugin.cfg)
    assert agc.level_scale == 16.0
    ref = am.MicAGC(**dict(KW, ), level_scale=4.0)
    c = (np.sin(np.linspace(0, 300, 1600)) * 0.01).astype(np.float32)
    agc.process(c.copy())
    ref.process(c.copy())
    assert 0 < ref.level < 0.25 and agc.level == pytest.approx(min(1.0, ref.level * 4.0), rel=1e-6)


def test_live_knobs_apply_and_rest_reclamps_like_the_constructor():
    plugin, ctx, _ = H.started("agc.mic")
    a = ctx.get("agc")
    H.assert_live_knobs(SPEC, plugin, {
        "target": (0.08, lambda p: a.target), "max_gain": (12.0, lambda p: a.max_gain),
        "rest": (5.0, lambda p: a.rest), "gate": (2.2, lambda p: a.gate), "release": (0.1, lambda p: a.release),
        "decay": (0.05, lambda p: a.decay), "min_rms": (0.002, lambda p: a.min_rms),
        "ceiling": (0.8, lambda p: a.ceiling), "floor_rate_speech": (0.001, lambda p: a.floor_rate_speech),
        "floor_rate_low": (0.3, lambda p: a.floor_rate_low), "floor_rate_mid": (0.07, lambda p: a.floor_rate_mid)})
    # rest above max_gain is clamped exactly as the constructor does it, and restored when max_gain rises again
    base = plugin.cfg
    plugin.apply({"max_gain": 2.0}, base.replace(max_gain=2.0))
    assert a.rest == 2.0 == H.v1.MicAGC(max_gain=2.0, rest=3.0).rest
    plugin.apply({"max_gain": 10.0}, base)
    assert a.rest == 3.0
    plugin.apply({"rest": 0.2}, base.replace(rest=0.2))
    assert a.rest == 1.0 == H.v1.MicAGC(rest=0.2).rest


def test_restart_knobs_are_only_on_and_floor_init():
    assert {k.name for k in SPEC.knobs if k.applies != "live"} == {"on", "floor_init"}


def test_validate_rest_not_above_max_gain():
    ok = {k.name: k.default for k in SPEC.knobs}
    SPEC.validate(ok)
    with pytest.raises(ConfigError) as e:
        SPEC.validate(dict(ok, rest=11.0))
    assert e.value.field == "rest"


def test_agent_policy_is_exactly_the_two_listed_knobs():
    bounded = {k.name: (k.agent_min, k.agent_max, k.agent_step) for k in SPEC.knobs if k.agent == "bounded"}
    assert bounded == {"target": (0.03, 0.10, 0.01), "max_gain": (4, 14, 2)}
