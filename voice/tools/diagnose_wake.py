"""Wake-word diagnostic: records short clips of you saying "Mira", shows what faster-whisper
actually hears, and sweeps the keyword-spotter's sensitivity threshold against the same audio.
Separates the two possible failure modes: the model mishearing your pronunciation entirely
(whisper's transcript will show it) vs. the default threshold being too strict (the sweep
will show a lower threshold catching it).

Usage: voice/.venv/bin/python -m voice.tools.diagnose_wake [num_clips]
"""

import sys
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

from voice.adapters.audio_io import SAMPLE_RATE, MicStream
from voice.adapters.stt import FasterWhisperStt

_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "kws"
_SAMPLES_DIR = Path(__file__).resolve().parent / "wake_samples"
_THRESHOLDS = [0.1, 0.15, 0.2, 0.25, 0.3]
_RECORD_SECONDS = 2.5
_TAIL_SILENCE_SECONDS = 0.5


def record_clip(mic: MicStream, seconds: float) -> bytes:
    frames = []
    bytes_needed = int(seconds * SAMPLE_RATE) * 2  # int16 = 2 bytes/sample
    total = 0
    while total < bytes_needed:
        frame = mic.read_frame()
        frames.append(frame)
        total += len(frame)
    return b"".join(frames)


def save_wav(path: Path, pcm16: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm16)


def triggers_at_threshold(pcm16: bytes, threshold: float) -> bool:
    kws = sherpa_onnx.KeywordSpotter(
        tokens=str(_MODEL_DIR / "tokens.txt"),
        encoder=str(_MODEL_DIR / "encoder.onnx"),
        decoder=str(_MODEL_DIR / "decoder.onnx"),
        joiner=str(_MODEL_DIR / "joiner.onnx"),
        keywords_file=str(_MODEL_DIR / "keywords.txt"),
        num_threads=2,
        provider="cpu",
        keywords_threshold=threshold,
    )
    stream = kws.create_stream()
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    stream.accept_waveform(SAMPLE_RATE, samples)
    # Trailing silence flushes the decoder so it finishes processing the last chunk.
    tail = np.zeros(int(SAMPLE_RATE * _TAIL_SILENCE_SECONDS), dtype=np.float32)
    stream.accept_waveform(SAMPLE_RATE, tail)
    while kws.is_ready(stream):
        kws.decode_stream(stream)
    return kws.get_result(stream) != ""


def main() -> None:
    num_clips = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print(f"Recording {num_clips} clips. After each prompt, say 'Mira' once.\n")

    mic = MicStream()
    stt = FasterWhisperStt()

    try:
        for i in range(1, num_clips + 1):
            input(f"[{i}/{num_clips}] Press Enter, then say 'Mira'...")
            pcm16 = record_clip(mic, _RECORD_SECONDS)
            clip_path = _SAMPLES_DIR / f"mira_{i:02d}.wav"
            save_wav(clip_path, pcm16)

            heard = stt.transcribe(pcm16)
            hits = [t for t in _THRESHOLDS if triggers_at_threshold(pcm16, t)]

            print(f"  whisper heard: {heard!r}")
            print(f"  wake triggers at thresholds: {hits or 'none tested'}")
            print(f"  saved: {clip_path}\n")
    finally:
        mic.close()

    print(f"All clips saved under {_SAMPLES_DIR}/ for replay.")
    print("Current production threshold (voice/adapters/wake.py) is 0.25.")


if __name__ == "__main__":
    main()
