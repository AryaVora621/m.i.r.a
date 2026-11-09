# wall/models

`silero_vad.onnx` (644 KB), Silero VAD for sherpa-onnx.

    curl -L -o silero_vad.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
    echo "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6  silero_vad.onnx" | shasum -a 256 -c

The keyword model lives in `../../voice/models/kws` (see `voice/adapters/wake.py` for the recipe).
