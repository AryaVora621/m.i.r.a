"""source.wav: WavSource copied from v1."""
import numpy as np

import test_lp_common as H
from listener_plugins import source_wav as sw

SPEC = sw.SPEC


def test_stage_equivalence_with_v1():
    a, b = H.v1.WavSource(H.CALENDAR_WAV), sw.WavSource(H.CALENDAR_WAV)
    n = 0
    while True:
        x, y = a.read(), b.read()
        assert (x is None) == (y is None)
        if x is None:
            break
        assert np.array_equal(x, y) and a.clock() == b.clock()
        n += 1
    assert n > 10
    a.flush(); b.flush(); a.close(); b.close()


def test_tail_is_zeros_and_configurable():
    s = sw.WavSource(H.PAUSE_WAV, tail_s=2.0)
    base = H.v1.WavSource(H.PAUSE_WAV, tail_s=0.0)
    assert len(s.x) - len(base.x) == 2 * 16000
    assert not np.any(s.x[len(base.x):])


def test_plugin_start_provides_source_and_expands_path():
    ctx = H.make_ctx()
    # the fixture lives under ${HERE}/tests/fixtures when HERE is the wall folder
    ctx.paths = type(ctx.paths)(here=H.WALL, config_dir=ctx.paths.config_dir, home=ctx.paths.home)
    plugin, ctx, _ = H.started("source.wav", ctx=ctx, path="${HERE}/tests/fixtures/mira_pause_the_music.wav",
                               tail_s=0.5)
    src = ctx.get("source")
    assert src.live is False and len(src.x) == len(H.v1.WavSource(H.PAUSE_WAV, tail_s=0.5).x)
    ctx.release_all()


def test_spec_is_disabled_by_default_and_maps_wav_flag():
    assert SPEC.default_enabled is False and SPEC.provides == ("source",)
    path = {k.name: k for k in SPEC.knobs}["path"]
    assert path.arg == "wav" and path.default == "" and path.applies == "restart"
