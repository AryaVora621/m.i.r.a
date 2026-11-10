"""vad.silero: the Silero voice activity detector. The configuration is built exactly as v1's Listener.__init__
built it; the factory keeps that one VadModelConfig and makes a fresh detector from it on `new()`, so a live knob
changes the config and takes effect at the next capture.

FIXED: window_size = 512, sample_rate = 16000.
"""

from mirakit.plugin import Knob, PluginBase, PluginSpec

from .common import RATE, expand_path

LEGACY_CLASSES = ()


class VadFactory:
    """The `vad` service: `config` is the sherpa_onnx.VadModelConfig the next detector is built from."""

    def __init__(self, sherpa, config, buffer_s):
        self._sherpa, self.config, self.buffer_s = sherpa, config, buffer_s

    def new(self):
        return self._sherpa.VoiceActivityDetector(self.config, buffer_size_in_seconds=self.buffer_s)


class VadSilero(PluginBase):
    def start(self):
        import sherpa_onnx
        c = self.cfg
        vc = sherpa_onnx.VadModelConfig()
        vc.silero_vad.model = expand_path(c.model, self.ctx.paths)
        vc.silero_vad.threshold = c.threshold
        vc.silero_vad.min_silence_duration = c.end_silence
        vc.silero_vad.min_speech_duration = c.min_speech
        vc.silero_vad.max_speech_duration = c.max_speech
        vc.silero_vad.window_size = 512
        vc.sample_rate = RATE
        vc.num_threads = c.threads
        self.factory = VadFactory(sherpa_onnx, vc, c.buffer_s)
        self.ctx.provide("vad", self.factory)

    def apply(self, changed, cfg):
        self.cfg = cfg
        f = self.factory
        s = f.config.silero_vad
        s.threshold = cfg.threshold
        s.min_silence_duration = cfg.end_silence
        s.min_speech_duration = cfg.min_speech
        s.max_speech_duration = cfg.max_speech
        f.config.num_threads = cfg.threads
        f.buffer_s = cfg.buffer_s

    def status(self):
        return {"end_silence": self.factory.config.silero_vad.min_silence_duration}


SPEC = PluginSpec(
    name="vad.silero", kind="vad", version="1", machine="listener", provides=("vad",),
    summary="Silero voice activity detector that ends a request.",
    create=lambda ctx, cfg: VadSilero(ctx, cfg), order=60,
    knobs=(
        Knob("model", "path", "${HERE}/models/silero_vad.onnx", "Silero model file.", applies="restart",
             arg="vad_model", env="MIRA_VAD_MODEL"),
        Knob("threshold", "float", 0.5, "Speech detection sensitivity.", unit="prob", min=0.1, max=0.9,
             arg="vad_threshold"),
        Knob("end_silence", "float", 0.7, "Silence that ends a request; also adds that much latency.", unit="s",
             min=0.2, max=3.0, agent="bounded", agent_min=0.5, agent_max=1.2, agent_step=0.1, arg="end_silence",
             topic="timing", spoken="how long I wait for you to finish",
             effect="fewer cut-offs, slower answers", phrases=("you cut me off", "you wait too long")),
        Knob("min_speech", "float", 0.15, "Shortest sound that counts as speech.", unit="s", min=0.05, max=1.0),
        Knob("max_speech", "float", 30.0, "The VAD's own segment cap.", unit="s", min=5, max=60),
        Knob("threads", "int", 1, "VAD CPU threads.", unit="threads", min=1, max=2),
        Knob("buffer_s", "float", 30.0, "VAD buffer length.", unit="s", min=10, max=120),
    ),
    tests=("wall/tests/plugins/test_lp_vad_silero.py",),
)
