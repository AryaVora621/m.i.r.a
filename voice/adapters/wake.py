"""Real Sherpa-ONNX open-vocabulary keyword spotter, wired to detect the wake word "mira".

voice/models/ is gitignored (~6MB of ONNX/BPE weights, not worth committing) — reproduce it with:

    mkdir -p voice/models/kws && cd voice/models/kws
    curl -L -o model.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
    tar xjf model.tar.bz2 --strip-components=1 && rm model.tar.bz2
    mv encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx encoder.onnx
    mv decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx decoder.onnx
    mv joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx joiner.onnx
    rm -f encoder-epoch-12-avg-2-chunk-16-left-64.onnx decoder-epoch-12-avg-2-chunk-16-left-64.onnx joiner-epoch-12-avg-2-chunk-16-left-64.onnx
    echo "MIRA" > wake_word_raw.txt  # must be uppercase — the model's BPE vocab is uppercase-cased
    sherpa-onnx-cli text2token --tokens tokens.txt --tokens-type bpe --bpe-model bpe.model \
        wake_word_raw.txt keywords.txt
    rm -f wake_word_raw.txt

text2token needs `sentencepiece` and `pypinyin` installed (not runtime deps — only needed for
this one-time codegen step). keywords.txt is overwritten with only the "mira" entry, dropping
the model's shipped demo keywords (HEY SIRI, ALEXA, etc.) so they can't false-trigger a wake.
"""

from pathlib import Path

import numpy as np
import sherpa_onnx

from voice.adapters.audio_io import SAMPLE_RATE, MicStream

_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "kws"


class SherpaWakeEngine:
    def __init__(self, mic: MicStream, model_dir: Path = _MODEL_DIR):
        self._mic = mic
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / "encoder.onnx"),
            decoder=str(model_dir / "decoder.onnx"),
            joiner=str(model_dir / "joiner.onnx"),
            keywords_file=str(model_dir / "keywords.txt"),
            num_threads=2,
            provider="cpu",
        )

    def listen_for_wake(self) -> None:
        stream = self._kws.create_stream()
        while True:
            frame = self._mic.read_frame()
            # accept_waveform requires float32 samples normalized to [-1, 1], not raw PCM16 bytes.
            samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
            stream.accept_waveform(SAMPLE_RATE, samples)
            while self._kws.is_ready(stream):
                self._kws.decode_stream(stream)
            if self._kws.get_result(stream) != "":
                self._kws.reset_stream(stream)
                return
