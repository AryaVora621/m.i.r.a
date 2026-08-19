"""Real Kokoro TTS. Synthesizes per-sentence chunks so playback can be cancelled between them for
barge-in support — Kokoro's generator already yields per-segment audio, which this adapter plays
one segment at a time via sounddevice, checking should_cancel() before each."""

from typing import Callable

from kokoro import KPipeline

from voice.adapters.audio_io import play_pcm, stop_playback

_SAMPLE_RATE = 24000


class KokoroTts:
    def __init__(self, lang_code: str = "a", voice: str = "af_heart"):
        self._pipeline = KPipeline(lang_code=lang_code)
        self._voice = voice

    def speak(self, text: str, should_cancel: Callable[[], bool]) -> None:
        generator = self._pipeline(text, voice=self._voice, speed=1, split_pattern=r"\n+|(?<=[.!?])\s+")
        for _graphemes, _phonemes, audio in generator:
            if should_cancel():
                stop_playback()
                return
            play_pcm(audio, _SAMPLE_RATE)
