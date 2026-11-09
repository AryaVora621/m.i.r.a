"""KWS false-trigger and miss counts. Run: python tests/kws_report.py  (needs the mini speech server for the synthetic part)."""
import glob, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
import conftest as c
import mira_listen as ml
import sherpa_onnx

THRESH = float(os.environ.get("MIRA_KWS_THRESHOLD", "0.25"))
SCORE = float(os.environ.get("MIRA_KWS_SCORE", "1.0"))


def count(x, threshold=THRESH, score=SCORE):
    k = sherpa_onnx.KeywordSpotter(tokens=str(c.KWS_DIR / "tokens.txt"), encoder=str(c.KWS_DIR / "encoder.onnx"),
                                   decoder=str(c.KWS_DIR / "decoder.onnx"), joiner=str(c.KWS_DIR / "joiner.onnx"),
                                   keywords_file=str(c.KWS_DIR / "keywords.txt"), num_threads=1,
                                   keywords_threshold=threshold, keywords_score=score)
    s = k.create_stream()
    x = np.concatenate([x, np.zeros(RATE, dtype=np.float32)])
    n = 0
    for i in range(0, len(x), 1600):
        s.accept_waveform(16000, x[i:i + 1600])
        while k.is_ready(s):
            k.decode_stream(s)
        if k.get_result(s):
            n += 1
            k.reset_stream(s)
    return n


RATE = 16000
if __name__ == "__main__":
    print(f"threshold {THRESH} score {SCORE}")
    for f in sorted(glob.glob(str(c.KWS_DIR / "test_wavs" / "*.wav"))):
        print("repo", os.path.basename(f), count(ml.read_wav_float(f)))
    print(open(c.KWS_DIR / "test_wavs" / "trans.txt").read()[:300])
    sp = ml.Speech(c.SPEECH_URL, c.TOKEN_FILE)
    import tempfile
    def say(t):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(sp.tts(t)); f.flush(); return ml.read_wav_float(f.name)
    pos = ["Mira", "Mira, what's on my calendar today?", "Hey Mira, pause the music.", "Mira, what time is it?",
           "Mira, is the printer done?", "Okay Mira, add physics homework to my list.", "Mira, play some music.",
           "Mira, remind me to call mom.", "Mira, what's the weather like?", "Mira, how many emails do I have?"]
    neg = ["Mirror", "Maria", "Mirror, what's on my calendar today?", "Maria, what's on my calendar today?", "Mia",
           "Mara", "Mirage", "Hey Siri, what's the weather?", "The mirror on the wall is cracked.",
           "I'm in America right now.", "Let's go to the miracle store.", "Ask Maria to call me back.",
           "Hermes, check my email.", "What's on my calendar today?", "Turn the volume down a little.",
           "Calculus is due on Friday.", "I admire your work, Marianne.", "Miranda is coming over at six.",
           "That is a mirror image of the printer part.", "Is the S V zero eight print finished?"]
    hit = [(t, count(ml.resample(say(t), 16000, 16000))) for t in pos]
    fp = [(t, count(say(t))) for t in neg]
    print("positives fired:", sum(1 for _, n in hit if n >= 1), "/", len(pos), "misses:", [t for t, n in hit if n == 0])
    print("negatives false triggers:", sum(1 for _, n in fp if n >= 1), "/", len(neg), [t for t, n in fp if n])
    print("positives firing more than once:", [t for t, n in hit if n > 1])
