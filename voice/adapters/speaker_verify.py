"""Real speaker verification via sherpa-onnx's speaker embedding extractor (WeSpeaker
CAM++). Gates the wake word so it only fires for an enrolled voice — depends on the
one-time enrollment step in voice/tools/enroll_speaker.py.

voice/models/speaker/ is gitignored — the embedding model (~28MB) plus your enrolled
voiceprint are both there, and the voiceprint is biometric data that must never be
committed. Reproduce the model with:

    mkdir -p voice/models/speaker && cd voice/models/speaker
    curl -L -o embedding.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_CAM++.onnx

Then enroll your voice: voice/.venv/bin/python -m voice.tools.enroll_speaker
"""

import sys
from pathlib import Path

import numpy as np
import sherpa_onnx

from voice.adapters.audio_io import SAMPLE_RATE

_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "speaker"
_PROFILE_PATH = _MODEL_DIR / "owner_embeddings.npy"
_DEFAULT_THRESHOLD = 0.6  # sherpa-onnx's documented default for this model family


class SpeakerVerifier:
    """Fails open (verify() returns True) until a voice is enrolled, so voice/main.py never
    breaks before voice/tools/enroll_speaker.py has been run — it just warns once."""

    _warned = False

    def __init__(self, model_dir: Path = _MODEL_DIR, profile_path: Path = _PROFILE_PATH, threshold: float = _DEFAULT_THRESHOLD):
        self._threshold = threshold
        self._extractor = None
        self._manager = None
        self._enrolled = False

        model_path = model_dir / "embedding.onnx"
        if model_path.exists():
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path), num_threads=2, provider="cpu")
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            if profile_path.exists():
                embeddings = np.load(profile_path)
                if embeddings.shape[0] > 0:
                    self._manager = sherpa_onnx.SpeakerEmbeddingManager(self._extractor.dim)
                    for i, embedding in enumerate(embeddings):
                        self._manager.add(f"owner-{i}", embedding)
                    self._enrolled = True

    @property
    def is_enrolled(self) -> bool:
        return self._enrolled

    def embed(self, float32_samples: np.ndarray) -> np.ndarray:
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=float32_samples)
        stream.input_finished()
        return np.array(self._extractor.compute(stream))

    def verify(self, float32_samples: np.ndarray) -> bool:
        if not self._enrolled:
            if not SpeakerVerifier._warned:
                print(
                    "no enrolled voice profile -- anyone saying the wake word can activate Mira. "
                    "Run: voice/.venv/bin/python -m voice.tools.enroll_speaker",
                    file=sys.stderr,
                )
                SpeakerVerifier._warned = True
            return True
        embedding = self.embed(float32_samples)
        return self._manager.search(embedding, threshold=self._threshold) != ""
