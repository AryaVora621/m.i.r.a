"""Score a keywords file on the fixed corpus from make_kws_clips.py. Usage: kws_eval.py CLIPDIR KEYWORDS THRESHOLD [SCORE]"""
import glob, json, os, sys, collections
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
import conftest as c
import mira_listen as ml
import sherpa_onnx

clips, kw, th = sys.argv[1], sys.argv[2], float(sys.argv[3])
sc = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
texts = json.load(open(f"{clips}/texts.json"))
k = sherpa_onnx.KeywordSpotter(tokens=str(c.KWS_DIR / "tokens.txt"), encoder=str(c.KWS_DIR / "encoder.onnx"),
                               decoder=str(c.KWS_DIR / "decoder.onnx"), joiner=str(c.KWS_DIR / "joiner.onnx"),
                               keywords_file=kw, num_threads=1, keywords_threshold=th, keywords_score=sc)
def count(x):
    s = k.create_stream(); n = 0
    x = np.concatenate([x, np.zeros(16000, dtype=np.float32)])
    for i in range(0, len(x), 1600):
        s.accept_waveform(16000, x[i:i+1600])
        while k.is_ready(s): k.decode_stream(s)
        if k.get_result(s): n += 1; k.reset_stream(s)
    return n
hit = collections.Counter(); fp = collections.Counter(); P = N = 0
for f in sorted(glob.glob(f"{clips}/*.wav")):
    b = os.path.basename(f); kind, _, i = b[:-4].split("_")
    n = count(ml.read_wav_float(f))
    if kind == "pos":
        P += 1; hit[texts["pos"][int(i)]] += n >= 1
    else:
        N += 1; fp[texts["neg"][int(i)]] += n >= 1
print(f"{os.path.basename(kw)} th={th} score={sc}: hits {sum(hit.values())}/{P}, false triggers {sum(fp.values())}/{N}")
if os.environ.get("V"):
    print(" misses by text:", {t: P//10 - v for t, v in hit.items() if v < P//10})
    print(" FP by text:", {t: v for t, v in fp.items() if v})
