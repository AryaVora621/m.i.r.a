"""Build a fixed corpus of wake-word positives and look-alike negatives from several Piper voices
(including male ones), so KWS settings can be compared on identical audio. Local use only.
Usage: make_kws_clips.py MODELS_DIR OUT_DIR   (MODELS_DIR holds the vits-piper-* folders)"""
import json, os, sys, wave
import numpy as np
import sherpa_onnx
sys.path.insert(0, os.path.dirname(__file__))
from kws_texts import POS, NEG

models, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
voices = [("amy", "vits-piper-en_US-amy-medium", 0), ("jenny", "vits-piper-en_GB-jenny_dioco-medium", 0),
          ("kristin", "vits-piper-en_US-kristin-medium", 0), ("hfc", "vits-piper-en_US-hfc_female-medium", 0)] + \
         [(f"lib{s}", "vits-piper-en_US-libritts_r-medium", s) for s in (3, 120, 400, 650, 800, 880)]
for name, d, sid in voices:
    p = os.path.join(models, d)
    onnx = [f for f in os.listdir(p) if f.endswith(".onnx")][0]
    v = sherpa_onnx.OfflineTtsVitsModelConfig(model=f"{p}/{onnx}", tokens=f"{p}/tokens.txt", data_dir=f"{p}/espeak-ng-data")
    tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(vits=v, num_threads=4)))
    for kind, texts in (("pos", POS), ("neg", NEG)):
        for i, t in enumerate(texts):
            a = tts.generate(t, sid=sid, speed=1.0)
            x = np.array(a.samples, dtype=np.float32)
            n = int(round(len(x) * 16000 / a.sample_rate))
            X = np.fft.rfft(x); Y = np.zeros(n // 2 + 1, dtype=complex); m = min(len(X), len(Y)); Y[:m] = X[:m]
            y = np.fft.irfft(Y, n) * (n / len(x))
            lead = np.zeros(8000, dtype=np.float32)
            y = np.concatenate([lead, y.astype(np.float32), lead])
            with wave.open(f"{out}/{kind}_{name}_{i:02d}.wav", "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    print("voice", name, flush=True)
json.dump({"pos": POS, "neg": NEG}, open(f"{out}/texts.json", "w"))
