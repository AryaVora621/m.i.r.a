"""speech.link: the client of the speech server on the mini. SpeechOffline and Speech are v1's (copied) with the
timeout constructor parameters and the tts additions of spec 4.4 (`tts(text, voice, speed, peak, pin)`, and the
`X-Mira-Voice` header recorded into `reply_voice`).

FIXED: the /stt and /tts routes, the bearer header read from the token file on every call, and the mapping of any
URLError, OSError or TimeoutError (HTTPError included) to SpeechOffline.
"""

import json
import urllib.error
import urllib.request

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

from .common import host_is_loopback_or_tailnet, http, expand_path, read_secret

LEGACY_CLASSES = ("Speech", "SpeechOffline")


class SpeechOffline(Exception):
    pass


class Speech:
    def __init__(self, url, token_file, voice="", stt_timeout_s=30, tts_timeout_s=45):
        self.url, self.token_file, self.voice = url.rstrip("/"), token_file, voice
        self.stt_timeout_s, self.tts_timeout_s = stt_timeout_s, tts_timeout_s
        self.reply_voice = ""  # voice id of the last unpinned /tts reply (X-Mira-Voice), "" when the server sends none

    def _hdr(self, extra=None):
        h = {"Authorization": f"Bearer {read_secret(self.token_file)}"}
        h.update(extra or {})
        return h

    def stt(self, wav_bytes) -> str:
        try:
            _, body = http("POST", f"{self.url}/stt", wav_bytes, self._hdr({"Content-Type": "audio/wav"}), timeout=self.stt_timeout_s)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise SpeechOffline(str(e)) from e
        return json.loads(body).get("text", "")

    def tts(self, text, voice="", speed=0.0, peak=0.0, pin=False) -> bytes:
        voice = voice or self.voice
        if pin and self.reply_voice:
            voice = self.reply_voice
        body, got = self._tts_request(text, voice, speed, peak)
        if not pin:
            self.reply_voice = got
        return body

    def render(self, text, voice="", speed=0.0, peak=0.0) -> bytes:
        """A sample for a preview: the same request, but it never touches reply_voice."""
        return self._tts_request(text, voice, speed, peak)[0]

    def _tts_request(self, text, voice, speed, peak):
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if speed:
            payload["speed"] = speed
        if peak:
            payload["peak"] = peak
        # http() drops the response headers and X-Mira-Voice is one of them, so this is its body inlined.
        req = urllib.request.Request(f"{self.url}/tts", data=json.dumps(payload).encode(), method="POST",
                                     headers=self._hdr({"Content-Type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=self.tts_timeout_s) as r:
                return r.read(), r.headers.get("X-Mira-Voice", "") or ""
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise SpeechOffline(str(e)) from e


class SpeechLink(PluginBase):
    def start(self):
        c = self.cfg
        self.speech = Speech(c.url, expand_path(c.token_file, self.ctx.paths), stt_timeout_s=c.stt_timeout_s,
                             tts_timeout_s=c.tts_timeout_s)
        self.ctx.provide("speech", self.speech)

    def apply(self, changed, cfg):
        self.cfg = cfg
        s = self.speech
        s.url = cfg.url.rstrip("/")
        s.token_file = expand_path(cfg.token_file, self.ctx.paths)
        s.stt_timeout_s, s.tts_timeout_s = cfg.stt_timeout_s, cfg.tts_timeout_s

    def status(self):
        return {"reply_voice": self.speech.reply_voice}


def validate(values):
    if not host_is_loopback_or_tailnet(values["url"]):
        raise ConfigError("url must be loopback or a Tailscale address (100.64.0.0/10): the bearer goes over plain HTTP",
                          "url")


SPEC = PluginSpec(
    name="speech.link", kind="speech", version="1", machine="listener", provides=("speech",),
    summary="Client of the speech server on the mini (STT and TTS over HTTP).",
    create=lambda ctx, cfg: SpeechLink(ctx, cfg), order=70, validate=validate,
    knobs=(
        Knob("url", "url", "http://100.77.98.89:8765", "Speech server (Tailscale on purpose: bearer over HTTP).",
             arg="speech_url", env="MIRA_SPEECH_URL"),
        Knob("token_file", "path", "", "Bearer token file, read on every call.", secret=True,
             arg="speech_token_file", env="MIRA_SPEECH_TOKEN_FILE"),
        Knob("stt_timeout_s", "float", 30.0, "Seconds a transcription may take.", unit="s", min=5, max=120),
        Knob("tts_timeout_s", "float", 45.0, "Seconds one synthesis may take.", unit="s", min=5, max=120),
    ),
    tests=("wall/tests/plugins/test_lp_speech_link.py",),
)
