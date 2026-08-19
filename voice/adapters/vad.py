"""Real Silero VAD, used both to capture a full utterance (LISTENING) and to watch for barge-in
speech one short frame at a time (SPEAKING)."""

import numpy as np
import torch
from silero_vad import load_silero_vad

from voice.adapters.audio_io import SAMPLE_RATE, MicStream

_SILENCE_TAIL_MS = 350
_FRAME_MS = 32  # matches audio_io._FRAME_SAMPLES (512 samples at 16kHz)
_SPEECH_PROB_THRESHOLD = 0.5


class SileroVad:
    def __init__(self, mic: MicStream):
        self._mic = mic
        self._model = load_silero_vad()

    def capture_utterance(self) -> bytes:
        frames: list[bytes] = []
        silence_ms = 0
        while True:
            frame = self._mic.read_frame()
            frames.append(frame)
            if self.has_speech(frame):
                silence_ms = 0
            else:
                silence_ms += _FRAME_MS
                if silence_ms >= _SILENCE_TAIL_MS and len(frames) > 1:
                    break
        return b"".join(frames)

    def read_frame(self) -> bytes:
        return self._mic.read_frame()

    def has_speech(self, frame: bytes) -> bool:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(samples)
        with torch.no_grad():
            prob = self._model(tensor, SAMPLE_RATE).item()
        return prob >= _SPEECH_PROB_THRESHOLD
