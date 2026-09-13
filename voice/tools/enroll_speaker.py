"""One-time enrollment: records you saying "Mira" a few times and saves your voice's
embeddings so voice/adapters/speaker_verify.py can reject the wake word from anyone else's
voice. This is enrollment against a pretrained speaker embedding model, not training a
model from scratch — the standard, robust way to do speaker verification with a small
amount of one person's audio.

Requires the speaker embedding model (voice/models/speaker/embedding.onnx, gitignored,
~28MB) -- reproduce it with:

    mkdir -p voice/models/speaker && cd voice/models/speaker
    curl -L -o embedding.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_CAM++.onnx

Usage: voice/.venv/bin/python -m voice.tools.enroll_speaker [num_clips]
"""

import sys
from pathlib import Path

import numpy as np
import sherpa_onnx

from voice.adapters.audio_io import SAMPLE_RATE, MicStream

_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "speaker"
_PROFILE_PATH = _MODEL_DIR / "owner_embeddings.npy"
_RECORD_SECONDS = 2.5


def record_clip(mic: MicStream, seconds: float) -> np.ndarray:
    frames = []
    bytes_needed = int(seconds * SAMPLE_RATE) * 2  # int16 = 2 bytes/sample
    total = 0
    while total < bytes_needed:
        frame = mic.read_frame()
        frames.append(frame)
        total += len(frame)
    pcm16 = b"".join(frames)
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> None:
    model_path = _MODEL_DIR / "embedding.onnx"
    if not model_path.exists():
        print(f"Missing {model_path}. See this module's docstring for the download recipe.", file=sys.stderr)
        sys.exit(1)

    num_clips = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path), num_threads=2, provider="cpu")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    print(f"Recording {num_clips} clips of you saying 'Mira'. This is what teaches Mira your voice.\n")
    mic = MicStream()
    embeddings = []
    try:
        for i in range(1, num_clips + 1):
            input(f"[{i}/{num_clips}] Press Enter, then say 'Mira'...")
            samples = record_clip(mic, _RECORD_SECONDS)
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
            stream.input_finished()
            embeddings.append(np.array(extractor.compute(stream)))
    finally:
        mic.close()

    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    np.save(_PROFILE_PATH, np.stack(embeddings))
    print(f"\nSaved {len(embeddings)} voice embeddings to {_PROFILE_PATH}.")
    print("Mira will now only wake for your voice. Re-run this anytime to re-enroll.")


if __name__ == "__main__":
    main()
