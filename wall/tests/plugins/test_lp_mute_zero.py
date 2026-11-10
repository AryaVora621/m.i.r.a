"""mute.zero: MuteDetector copied from v1; verdict sequences against the v1 twin."""
import numpy as np
import pytest

import test_lp_common as H
from listener_plugins import mute_zero as mz
from mirakit.plugin import ConfigError

SPEC = mz.SPEC


def _signal(seed=3):
    rng = np.random.default_rng(seed)
    zeros = lambda s: np.zeros(int(16000 * s), dtype=np.float32)
    noise = lambda s: (rng.standard_normal(int(16000 * s)) * 0.003).astype(np.float32)
    spiky = zeros(12)
    spiky[::4000] = 0.2  # the mute's start spikes
    return np.concatenate([noise(5), zeros(14), spiky, zeros(75), noise(4), zeros(3), noise(2)])


@pytest.mark.parametrize("kw", [{}, {"mute_s": 3.0, "repeat_s": 20.0}, {"block_s": 0.5, "mute_share": 0.95, "live_share": 0.8}])
def test_verdict_sequence_equals_v1(kw):
    a, b = H.v1.MuteDetector(**kw), mz.MuteDetector(**kw)
    seq_a, seq_b = [], []
    for c in H.chunks_of(_signal()):
        seq_a.append(a.feed(c)); seq_b.append(b.feed(c))
    assert seq_a == seq_b
    verdicts = [v for v in seq_a if v]
    assert "muted" in verdicts and "live" in verdicts and "repeat" in verdicts
    assert a.state == b.state


def test_plugin_object_matches_v1_with_config_values():
    plugin, ctx, _ = H.started("mute.zero", mute_s=4.0, repeat_s=15.0)
    mine = ctx.get("mute")
    ref = H.v1.MuteDetector(mute_s=4.0, repeat_s=15.0)
    for c in H.chunks_of(_signal()):
        assert mine.feed(c) == ref.feed(c)
    assert mine.state == plugin.status()["state"]


def test_live_knobs_apply():
    plugin, ctx, _ = H.started("mute.zero")
    m = ctx.get("mute")
    H.assert_live_knobs(SPEC, plugin, {
        "mute_s": (20.0, lambda p: m.mute_s), "mute_share": (0.9, lambda p: m.mute_share),
        "live_share": (0.7, lambda p: m.live_share), "repeat_s": (30.0, lambda p: m.repeat_s)})
    assert {k.name for k in SPEC.knobs if k.applies == "restart"} == {"block_s"}


def test_validate_live_share_not_above_mute_share():
    ok = {k.name: k.default for k in SPEC.knobs}
    SPEC.validate(ok)
    with pytest.raises(ConfigError) as e:
        SPEC.validate(dict(ok, live_share=0.995))
    assert e.value.field == "live_share"
