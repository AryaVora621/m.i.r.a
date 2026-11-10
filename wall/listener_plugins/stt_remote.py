"""stt.remote: speech-to-text through the speech.link object (today the very same object provides `stt`)."""

from mirakit.plugin import PluginBase, PluginSpec

LEGACY_CLASSES = ()


class SttRemote(PluginBase):
    def start(self):
        self.ctx.provide("stt", self.ctx.get("speech"))


SPEC = PluginSpec(
    name="stt.remote", kind="stt", version="1", machine="listener", provides=("stt",), needs=("speech",),
    summary="Speech-to-text through the speech server.",
    create=lambda ctx, cfg: SttRemote(ctx, cfg), order=80, knobs=(),
    tests=("wall/tests/plugins/test_lp_stt_remote.py",),
)
