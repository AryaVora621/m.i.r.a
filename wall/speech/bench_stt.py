"""STT shoot-out on the mini. Makes clips with the chosen TTS voice, then times and scores each STT backend.

Usage: bench_stt.py CLIPDIR [backend:name ...]
Reference sentences include the names the wall must get right (Mira, Hermes, SV08, AP Physics, Calculus).
"""
import json, os, re, sys, time, wave
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import server

SENT = [
    ("Mira, what's on my calendar today?", "Mira, what's on my calendar today?"),
    ("Ask Hermes to check my email, and tell me if the S V zero eight print has finished.",
     "Ask Hermes to check my email, and tell me if the SV08 print has finished."),
    ("Add A P Physics homework to my school list, and remind me tomorrow.",
     "Add AP Physics homework to my school list, and remind me tomorrow."),
    ("Is Calculus due on Friday, or did the teacher move the test?",
     "Is Calculus due on Friday, or did the teacher move the test?"),
    ("Pause the music and turn the volume down a little.",
     "Pause the music and turn the volume down a little."),
    ("What is the weather like, and should I bring a jacket to school?",
     "What is the weather like, and should I bring a jacket to school?"),
]
KEYS = {0: ["mira"], 1: ["hermes", r"(sv08|sv 08|s v 08|s v zero eight|sv zero eight|sv-08)"],
        2: [r"(ap|a p) physics"], 3: ["calculus"]}


def resample_fft(x, r_in, r_out):
    n_out = int(round(len(x) * r_out / r_in))
    X = np.fft.rfft(x)
    Y = np.zeros(n_out // 2 + 1, dtype=complex)
    m = min(len(X), len(Y))
    Y[:m] = X[:m]
    return np.fft.irfft(Y, n_out) * (n_out / len(x))


def write16(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def norm(t):
    t = t.lower().replace("-", " ")
    return re.sub(r"[^a-z0-9 ]", "", t).split()


def wer(ref, hyp):
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)], len(r)


def make_clips(clipdir):
    os.makedirs(clipdir, exist_ok=True)
    tts = server.Tts(server.TTS_DIR, server.TTS_VOICE)
    rng = np.random.default_rng(7)
    clips = []
    for i, (spoken, ref) in enumerate(SENT):
        s, r = tts.speak(spoken, None)
        x = resample_fft(s.astype(np.float64), r, 16000).astype(np.float32) * 0.5
        lead = np.zeros(int(0.3 * 16000), dtype=np.float32)
        x = np.concatenate([lead, x, lead])
        # Fan/room noise at roughly 15 dB below the speech RMS.
        rms = float(np.sqrt(np.mean(x ** 2)))
        noise = rng.normal(0, rms * 0.18, len(x)).astype(np.float32)
        noise = np.convolve(noise, np.ones(8) / 8, mode="same")
        for tag, y in (("clean", x), ("noisy", x + noise)):
            p = f"{clipdir}/s{i}_{tag}.wav"
            write16(p, y)
            clips.append((i, tag, p, ref, len(y) / 16000))
    return clips


def main():
    clipdir = sys.argv[1]
    cands = sys.argv[2:]
    clips = make_clips(clipdir)
    print("clips", [(i, t, round(d, 1)) for i, t, _, _, d in clips])
    for cand in cands:
        t0 = time.perf_counter()
        stt = server.Stt(cand)
        load = time.perf_counter() - t0
        stt.transcribe(np.zeros(16000, dtype=np.float32))
        res = {"clean": [0, 0], "noisy": [0, 0]}
        keyhit = {"clean": [0, 0], "noisy": [0, 0]}
        times = {}
        for i, tag, p, ref, dur in clips:
            with wave.open(p) as w:
                x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
            best = None
            for rep in range(2):
                t = time.perf_counter(); text = stt.transcribe(x); el = time.perf_counter() - t
                best = el if best is None else min(best, el)
            e, n = wer(ref, text)
            res[tag][0] += e; res[tag][1] += n
            joined = " ".join(norm(text))
            for k in KEYS.get(i, []):
                keyhit[tag][1] += 1
                keyhit[tag][0] += bool(re.search(k, joined))
            times[(i, tag)] = (dur, best)
            if tag == "noisy" or os.environ.get("SHOW"):
                print(f"   s{i} {tag}: {text!r}")
        sec = lambda i: times[(i, "clean")]
        print(json.dumps({
            "cand": cand, "load_s": round(load, 1),
            "wer_clean": round(res["clean"][0] / res["clean"][1], 3),
            "wer_noisy": round(res["noisy"][0] / res["noisy"][1], 3),
            "names_clean": f"{keyhit['clean'][0]}/{keyhit['clean'][1]}",
            "names_noisy": f"{keyhit['noisy'][0]}/{keyhit['noisy'][1]}",
            "s0_3s": [round(sec(0)[0], 1), round(sec(0)[1], 2)],
            "s1_5s": [round(sec(1)[0], 1), round(sec(1)[1], 2)],
            "s2": [round(sec(2)[0], 1), round(sec(2)[1], 2)],
            "avg_rtf": round(sum(b / d for (d, b) in times.values()) / len(times), 2),
        }), flush=True)


if __name__ == "__main__":
    main()
