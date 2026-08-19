"""These adapters wrap real ML libraries and real audio hardware — they cannot be meaningfully
unit tested without a microphone/speaker. These smoke tests only prove the modules import
cleanly and their pure-computation helpers behave; real behavior is verified manually (Task 15)."""

import numpy as np
import pytest


def test_stt_adapter_imports_and_handles_silence_without_crashing():
    pytest.importorskip("faster_whisper")
    from voice.adapters.stt import FasterWhisperStt

    stt = FasterWhisperStt(model_size="tiny.en")  # smallest model, fastest to download for this smoke test
    silence = np.zeros(16000, dtype=np.int16).tobytes()  # 1 second of digital silence
    result = stt.transcribe(silence)
    assert isinstance(result, str)


def test_all_adapter_modules_import_cleanly():
    pytest.importorskip("sherpa_onnx")
    pytest.importorskip("silero_vad")
    pytest.importorskip("kokoro")
    import voice.adapters.wake  # noqa: F401
    import voice.adapters.vad  # noqa: F401
    import voice.adapters.tts  # noqa: F401
    import voice.adapters.audio_io  # noqa: F401
