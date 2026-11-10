"""wake.kws: build_kws copied from v1, loading the real spotter model from voice/models/kws."""
import gc
import weakref

import numpy as np
import pytest

import test_lp_common as H
from listener_plugins import wake_kws as wk
from mirakit.testkit import FakeCtx, cfg_for

SPEC = wk.SPEC
KWS_DIR = H.WALL.parent / "voice" / "models" / "kws"
pytestmark = pytest.mark.skipif(not (KWS_DIR / "encoder.onnx").exists(), reason="spotter model not present")


def _ctx():
    ctx = H.make_ctx()
    ctx.paths = type(ctx.paths)(here=H.WALL, config_dir=ctx.paths.config_dir, home=ctx.paths.home)
    return ctx


def _hits(spotter, wav):
    x = H.common.read_wav_float(wav)
    x = np.concatenate([x, np.zeros(16000, dtype=np.float32)])
    s = spotter.create_stream()
    out = []
    for i, c in enumerate(H.chunks_of(x)):
        s.accept_waveform(16000, c)
        while spotter.is_ready(s):
            spotter.decode_stream(s)
        r = spotter.get_result(s)
        if r:
            out.append(i)
            spotter.reset_stream(s)
    return out


def test_plugin_spotter_hits_like_v1_build():
    plugin, ctx, cfg = H.started("wake.kws", ctx=_ctx())
    args = H.v1.build_parser().parse_args([])
    legacy = H.v1.build_kws(args)
    for wav in (H.CALENDAR_WAV, H.PAUSE_WAV):
        a, b = _hits(legacy, wav), _hits(ctx.get("wake"), wav)
        assert a == b and a, (wav.name, a, b)
    ctx.release_all()


def test_namespace_carries_every_value_explicitly():
    plugin, ctx, cfg = H.started("wake.kws", ctx=_ctx(), threshold=0.2, score=1.5, keywords="keywords.txt")
    ns = plugin._namespace(plugin.cfg)
    assert (ns.kws_threshold, ns.kws_score, ns.kws_threads, ns.keywords) == (0.2, 1.5, 1, "keywords.txt")
    assert ns.kws_dir.endswith("voice/models/kws") and "${" not in ns.kws_dir
    ctx.release_all()


class DroppingCtx(FakeCtx):
    """FakeCtx keeps every provided object for release_all; the real registry must not retain a superseded one."""

    def provide(self, service, obj):
        self._provided = [(s, o) for s, o in self._provided if s != service]
        super().provide(service, obj)


def test_rebuild_frees_the_old_spotter_before_the_new_one_exists(monkeypatch):
    ctx = DroppingCtx.with_specs(*H.lp.specs())
    ctx.paths = type(ctx.paths)(here=H.WALL, config_dir=ctx.paths.config_dir, home=ctx.paths.home)
    plugin, ctx, cfg = H.started("wake.kws", ctx=ctx)
    ref = weakref.ref(ctx.get("wake"))
    assert ref() is not None
    seen = {}
    real = wk.build_kws

    def spy(args):
        seen["old_alive"] = ref() is not None
        return real(args)

    monkeypatch.setattr(wk, "build_kws", spy)
    new_cfg = cfg.replace(threshold=0.3)
    plugin.rebuild(new_cfg)
    assert seen["old_alive"] is False
    assert ctx.get("wake") is plugin.kws and plugin.cfg.threshold == 0.3
    assert _hits(ctx.get("wake"), H.PAUSE_WAV) is not None
    ctx.release_all()


def test_rebuild_with_a_stub_spotter_also_frees_it(monkeypatch):
    class Stub:
        def __init__(self, args):
            self.args = args

    monkeypatch.setattr(wk, "build_kws", lambda args: Stub(args))
    ctx = DroppingCtx.with_specs(*H.lp.specs())
    plugin, ctx, cfg = H.started("wake.kws", ctx=ctx)
    ref = weakref.ref(ctx.get("wake"))
    order = []
    monkeypatch.setattr(wk, "build_kws", lambda args: (order.append(ref() is None), Stub(args))[1])
    plugin.rebuild(cfg.replace(score=2.0))
    assert order == [True]
    assert plugin.kws.args.kws_score == 2.0


def test_knob_classes_and_owner_only():
    cls = {k.name: k.applies for k in SPEC.knobs}
    assert cls == {"kws_dir": "rebuild", "keywords": "rebuild", "threshold": "rebuild", "score": "rebuild",
                   "threads": "restart"}
    assert all(k.agent == "deny" for k in SPEC.knobs)
    assert wk.LEGACY_CLASSES == ()
