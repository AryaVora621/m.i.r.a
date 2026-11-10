"""vad.silero: the factory builds the config exactly as v1's Listener.__init__ did."""
import numpy as np
import pytest

import test_lp_common as H
from listener_plugins import vad_silero as vs

SPEC = vs.SPEC
pytestmark = pytest.mark.skipif(not (H.WALL / "models" / "silero_vad.onnx").exists(), reason="silero model not present")


def _ctx():
    ctx = H.make_ctx()
    ctx.paths = type(ctx.paths)(here=H.WALL, config_dir=ctx.paths.config_dir, home=ctx.paths.home)
    return ctx


def _fields(vc):
    s = vc.silero_vad
    return (s.model, s.threshold, s.min_silence_duration, s.min_speech_duration, s.max_speech_duration, s.window_size,
            vc.sample_rate, vc.num_threads)


def _speech_flags(det, wav):
    out = []
    for c in H.chunks_of(H.common.read_wav_float(wav)):
        det.accept_waveform(c)
        out.append(det.is_speech_detected())
    return out


@pytest.fixture
def v1_listener(tmp_path, monkeypatch):
    H.clean_env(monkeypatch)
    args = H.v1.build_parser().parse_args(["--wav", str(H.PAUSE_WAV), "--voice-url", ""])
    lis = H.v1.Listener(args, H.v1.WavSource(H.PAUSE_WAV))
    yield lis
    lis.rep.close()


def test_config_equals_v1_listener_config(v1_listener):
    plugin, ctx, cfg = H.started("vad.silero", ctx=_ctx())
    f = ctx.get("vad")
    assert _fields(f.config) == _fields(v1_listener._vc)
    assert f.buffer_s == 30.0
    ctx.release_all()


def test_detector_makes_the_same_speech_decisions_as_v1(v1_listener):
    plugin, ctx, cfg = H.started("vad.silero", ctx=_ctx())
    mine = _speech_flags(ctx.get("vad").new(), H.CALENDAR_WAV)
    legacy = _speech_flags(v1_listener._sherpa.VoiceActivityDetector(v1_listener._vc, buffer_size_in_seconds=30),
                           H.CALENDAR_WAV)
    assert mine == legacy and any(mine)
    ctx.release_all()


def test_live_knobs_mutate_the_one_config_the_next_detector_uses():
    plugin, ctx, cfg = H.started("vad.silero", ctx=_ctx())
    f = ctx.get("vad")
    vc = f.config
    s = vc.silero_vad
    H.assert_live_knobs(SPEC, plugin, {
        "threshold": (0.6, lambda p: round(s.threshold, 6), 0.6),
        "end_silence": (1.0, lambda p: round(s.min_silence_duration, 6), 1.0),
        "min_speech": (0.2, lambda p: round(s.min_speech_duration, 6), 0.2),
        "max_speech": (20.0, lambda p: s.max_speech_duration),
        "threads": (2, lambda p: vc.num_threads), "buffer_s": (60.0, lambda p: f.buffer_s)})
    assert ctx.get("vad").config is vc  # the same object: F5, no new factory
    plugin.apply({"end_silence": 1.2}, plugin.cfg.replace(end_silence=1.2))
    assert round(f.config.silero_vad.min_silence_duration, 6) == 1.2
    assert f.new() is not None  # builds from the mutated config
    ctx.release_all()


def test_model_is_restart_and_end_silence_is_the_only_agent_knob():
    assert {k.name for k in SPEC.knobs if k.applies == "restart"} == {"model"}
    assert [k.name for k in SPEC.knobs if k.agent == "bounded"] == ["end_silence"]
