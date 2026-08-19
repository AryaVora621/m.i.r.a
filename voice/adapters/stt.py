"""Real faster-whisper STT, running the small English model in INT8 on CPU (Apple Silicon
Metal/GPU acceleration is a later optimization, not required for this plan's MVP)."""

import numpy as np
from faster_whisper import WhisperModel

_SAMPLE_RATE = 16000


class FasterWhisperStt:
    def __init__(self, model_size: str = "small.en", device: str = "cpu", compute_type: str = "int8"):
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, pcm16_audio: bytes) -> str:
        samples = np.frombuffer(pcm16_audio, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(samples, beam_size=5)
        return " ".join(segment.text.strip() for segment in segments).strip()
