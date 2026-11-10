#!/usr/bin/env python3
"""STT shoot-out for the wall: word error rate, intent words, wake accept rate and latency per config.

Runs on the Mac mini against speech/server.py's Stt class directly (no HTTP, no token, no sound):

    nice -n 19 python stt_eval.py prep  WORKDIR --takes DIR [--voices amy,lessac] [--snr 12,4]
    nice -n 19 python stt_eval.py run   WORKDIR CONFIG [CONFIG ...] [--json OUT]

`prep` builds a manifest of clips in WORKDIR (scratch, never the repo):
  * "take" clips: the owner's far-field recordings (1-close.wav ...) trimmed of digital zero, run through the
    listener's own MicAGC and the listener's Silero VAD settings (0.5 threshold, 0.7 s end silence, 0.15 s min
    speech), one clip per utterance. Expected text comes from fixtures/stt/takes.json (text only; the audio is
    private and stays out of the repo).
  * "domain" clips: the phrases in fixtures/stt/phrases.txt synthesised with the project's piper voices, each
    mixed with room noise taken from the takes' quiet stretches plus a short synthetic reverb, at a few SNRs, then
    through the same AGC.
Nothing is recorded and nothing is played; clips are only written as wav files.

A take listed as "negative" in takes.json (speech that is not "Mira ...") is scored only as the false wake count:
how many of its utterances the transcript would still turn into an accepted wake.
A CONFIG is `label=kind[,key=value ...]` with keys prompt, hotwords, compute, beam (a value may be `@vocab` for the
default vocabulary, `@prompt` for its sentence form). Example:
    base-prompt=faster-whisper:base.en,compute=int8,prompt=@prompt
Scores: WER (punctuation and case ignored, brand names folded, see ALIASES), the intent-critical words that the
reference holds and the hypothesis kept, the wake accept rate (listener's has_wake on the transcript, for clips whose
reference opens with Mira), and per-clip latency with the real-time factor (decode seconds per audio second).
"""
import argparse
import json
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "stt"
for _d in (HERE.parent, HERE.parent / "speech", HERE):
    sys.path.insert(0, str(_d))

RATE = 16000
INTENT_WORDS = ["printers", "print", "onshape", "slicer", "vault", "timer", "ten", "calendar", "weather", "music",
                "pause"]

# The same spoken thing has several written forms; fold them so the score measures hearing, not spelling.
ALIASES = [
    (r"\bsv ?(?:zero |oh |0)?(?:eight|8)\b", "sv08"), (r"\bs v (?:zero |oh |0)?(?:eight|8)\b", "sv08"),
    (r"\bsovol\b", "sovol"), (r"\b3 ?d\b", "3d"), (r"\bthree d\b", "3d"), (r"\b3 d\b", "3d"),
    (r"\bon shape\b", "onshape"), (r"\bslice r\b", "slicer"), (r"\b10\b", "ten"), (r"\bok\b", "okay"),
    (r"\bwhat s\b", "whats"), (r"\bhow s\b", "hows"), (r"\bit s\b", "its"),
]


def norm_words(text):
    t = text.lower().replace("-", " ").replace("'", " ").replace("’", " ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for pat, rep in ALIASES:
        t = re.sub(pat, rep, t)
    return t.split()


def edit_distance(r, h):
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)]


def wer_counts(ref, hyp):
    r = norm_words(ref)
    return edit_distance(r, norm_words(hyp)), len(r)


def intent_hits(ref, hyp):
    """(hit, total): intent words present in the reference, and how many of them the hypothesis kept."""
    r, h = set(norm_words(ref)), set(norm_words(hyp))
    want = [w for w in INTENT_WORDS if w in r]
    return sum(1 for w in want if w in h), len(want)


def wake_ok(text):
    import mira_listen as ml
    return bool(text.strip()) and ml.has_wake(text)


# -------------------------------------------------------------------------------- audio helpers

def read_wav(path):
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, path
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if rate != RATE:
        n = int(round(len(x) * RATE / rate))
        X = np.fft.rfft(x)
        Y = np.zeros(n // 2 + 1, dtype=complex)
        m = min(len(X), len(Y))
        Y[:m] = X[:m]
        x = (np.fft.irfft(Y, n) * (n / len(x))).astype(np.float32)
    return x


def write_wav(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def trim_zero(x):
    nz = np.nonzero(x)[0]
    return x[: nz.max() + 1] if len(nz) else x[:0]


def agc_all(x):
    """The listener's MicAGC over 100 ms chunks, as in the live path (fresh state per file)."""
    import mira_listen as ml
    agc = ml.MicAGC()
    out = [agc.process(x[i:i + ml.CHUNK].astype(np.float32)) for i in range(0, len(x), ml.CHUNK)]
    return np.concatenate(out) if out else x


def vad_segments(x, vad_model, pad_s=0.25):
    """Utterances by the listener's Silero settings; each segment keeps a short lead and tail like a live clip."""
    import sherpa_onnx
    vc = sherpa_onnx.VadModelConfig()
    vc.silero_vad.model = str(vad_model)
    vc.silero_vad.threshold = 0.5
    vc.silero_vad.min_silence_duration = 0.7
    vc.silero_vad.min_speech_duration = 0.15
    vc.silero_vad.max_speech_duration = 30.0
    vc.silero_vad.window_size = 512
    vc.sample_rate = RATE
    vc.num_threads = 1
    vad = sherpa_onnx.VoiceActivityDetector(vc, buffer_size_in_seconds=60)
    segs = []
    pad = int(pad_s * RATE)
    for i in range(0, len(x), 512):
        vad.accept_waveform(x[i:i + 512])
        while not vad.empty():
            s = vad.front
            start = s.start
            segs.append(x[max(0, start - pad): start + len(s.samples) + pad])
            vad.pop()
    vad.flush()
    while not vad.empty():
        s = vad.front
        segs.append(x[max(0, s.start - pad): s.start + len(s.samples) + pad])
        vad.pop()
    return segs


def quiet_noise(x, win_s=1.0, n=3):
    """The n quietest non-zero windows of a raw take (room hiss), concatenated. Used as the noise bed."""
    w = int(win_s * RATE)
    cands = []
    for i in range(0, len(x) - w, w // 2):
        seg = x[i:i + w]
        r = float(np.sqrt(np.mean(seg ** 2)))
        if r > 1e-5:
            cands.append((r, i))
    cands.sort()
    parts = [x[i:i + w] for _, i in cands[:n]]
    return np.concatenate(parts) if parts else np.zeros(w, np.float32)


def reverb_ir(rt60=0.35, seed=7):
    """A short synthetic room: exponentially decaying noise. Deterministic."""
    rng = np.random.default_rng(seed)
    n = int(rt60 * RATE)
    t = np.arange(n) / RATE
    ir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    ir[0] = 1.0
    return (ir / np.sqrt(np.sum(ir ** 2))).astype(np.float32)


def degrade(speech, noise, snr_db, seed=0, level=0.01):
    """Reverberate, scale the speech to a far-field rms, then add room noise at the given SNR."""
    rng = np.random.default_rng(seed)
    wet = np.convolve(speech, reverb_ir())[: len(speech)]
    r = float(np.sqrt(np.mean(wet ** 2))) or 1.0
    wet = wet * (level / r)
    reps = int(np.ceil((len(wet) + RATE) / len(noise)))
    bed = np.tile(noise, reps)
    off = int(rng.integers(0, max(1, len(bed) - len(wet) - RATE)))
    bed = bed[off: off + len(wet) + RATE]
    nr = float(np.sqrt(np.mean(bed ** 2))) or 1e-6
    bed = bed * (level / (10 ** (snr_db / 20)) / nr)
    out = np.concatenate([np.zeros(int(0.3 * RATE), np.float32), wet, np.zeros(RATE - int(0.3 * RATE), np.float32)])
    out = out + bed[: len(out)]
    return out.astype(np.float32)


# -------------------------------------------------------------------------------- prep

def load_phrases():
    rows = []
    for line in (FIX / "phrases.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            shown, _, said = line.partition("|")
            rows.append((shown.strip(), (said or shown).strip()))
    return rows


def prep(ns):
    import server
    work = Path(ns.workdir)
    (work / "clips").mkdir(parents=True, exist_ok=True)
    vad_model = ns.vad_model or str(server.MODELS / "silero_vad.onnx")
    manifest = []
    raw_for_noise = []
    expected = json.loads((FIX / "takes.json").read_text())
    if ns.takes:
        for name, exp in expected.items():
            if name.startswith("_"):
                continue
            negative = exp == "negative"
            p = Path(ns.takes) / name
            if not p.exists():
                print(f"skip {name}: not in {ns.takes}")
                continue
            raw = trim_zero(read_wav(p))
            raw_for_noise.append(raw)
            segs = vad_segments(agc_all(raw), vad_model)
            if negative:
                # Speech that is not a wake word: the new config must not turn it into one.
                exp = [""] * len(segs)
            print(f"{name}: {len(segs)} VAD segments, expected {len(exp)}: {[round(len(s) / RATE, 1) for s in segs]}")
            if len(segs) != len(exp):
                print("  count mismatch: scoring only the first segments that have a reference")
            for i, seg in enumerate(segs):
                text = exp[i] if i < len(exp) else None
                f = work / "clips" / f"take_{Path(name).stem}_{i:02d}.wav"
                write_wav(f, seg)
                if text is not None:
                    manifest.append({"id": f.stem, "group": "negative" if negative else "take", "wav": str(f),
                                     "ref": text})
    noise = quiet_noise(np.concatenate(raw_for_noise)) if raw_for_noise else \
        (np.random.default_rng(1).standard_normal(RATE * 2) * 0.003).astype(np.float32)
    phrases = load_phrases()
    snrs = [float(s) for s in ns.snr.split(",")]
    for voice in ns.voices.split(","):
        d, sid = voice_dir(voice)
        tts = server.Tts(d, voice)
        for pi, (shown, said) in enumerate(phrases):
            samples, rate = tts.speak(said, voice)
            if rate != RATE:
                n = int(round(len(samples) * RATE / rate))
                X = np.fft.rfft(samples)
                Y = np.zeros(n // 2 + 1, dtype=complex)
                m = min(len(X), len(Y))
                Y[:m] = X[:m]
                samples = (np.fft.irfft(Y, n) * (n / len(samples))).astype(np.float32)
            for snr in snrs:
                x = agc_all(degrade(samples, noise, snr, seed=pi))
                f = work / "clips" / f"dom_{voice}_{pi:02d}_snr{int(snr)}.wav"
                write_wav(f, x)
                manifest.append({"id": f.stem, "group": "domain", "wav": str(f), "ref": shown})
    (work / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"manifest: {len(manifest)} clips ({sum(1 for m in manifest if m['group'] == 'take')} take, "
          f"{sum(1 for m in manifest if m['group'] == 'domain')} domain)")


def voice_dir(voice):
    """Piper voice name -> model directory name (all are single-speaker, so sid 0)."""
    names = {"amy": "vits-piper-en_US-amy-medium", "lessac": "vits-piper-en_US-lessac-medium-int8",
             "kristin": "vits-piper-en_US-kristin-medium", "hfc": "vits-piper-en_US-hfc_female-medium",
             "jenny": "vits-piper-en_GB-jenny_dioco-medium"}
    return names[voice], 0


# -------------------------------------------------------------------------------- run

def parse_config(spec, server):
    label, _, rest = spec.partition("=")
    parts = rest.split(",")
    kind, opts = parts[0], {}
    for kv in parts[1:]:
        k, _, v = kv.partition("=")
        opts[k] = v
    vocab = server.DEFAULT_VOCAB
    sub = {"@vocab": vocab, "@prompt": f"Vocabulary: {vocab}."}
    kw = {}
    if "prompt" in opts:
        kw["prompt"] = sub.get(opts["prompt"], opts["prompt"])
    if "hotwords" in opts:
        kw["hotwords"] = sub.get(opts["hotwords"], opts["hotwords"])
    if "compute" in opts:
        kw["compute_type"] = opts["compute"]
    if "beam" in opts:
        kw["beam_size"] = int(opts["beam"])
    # An unset prompt/hotwords in an eval config means off, not "whatever the env says".
    kw.setdefault("prompt", "")
    kw.setdefault("hotwords", "")
    return label, kind, kw


def run_config(spec, manifest, server, warm=True):
    label, kind, kw = parse_config(spec, server)
    t0 = time.perf_counter()
    stt = server.Stt(kind, **kw)
    load_s = time.perf_counter() - t0
    if warm:
        stt.transcribe(np.zeros(RATE, np.float32))
    rows = []
    for m in manifest:
        x = read_wav(m["wav"])
        t = time.perf_counter()
        text = stt.transcribe(x)
        dt = time.perf_counter() - t
        err, n = wer_counts(m["ref"], text)
        hit, tot = intent_hits(m["ref"], text)
        rows.append({"id": m["id"], "group": m["group"], "ref": m["ref"], "hyp": text, "err": err, "n": n,
                     "hit": hit, "tot": tot, "wake": wake_ok(text) if norm_words(m["ref"])[:1] == ["mira"] else None,
                     "false_wake": wake_ok(text) if m["group"] == "negative" else None,
                     "audio_s": len(x) / RATE, "dec_s": dt})
    return {"label": label, "kind": kind, "opts": kw, "load_s": round(load_s, 1), "rows": rows}


def summarise(res):
    out = {"label": res["label"], "load_s": res["load_s"]}
    neg = [r for r in res["rows"] if r["group"] == "negative"]
    if neg:
        out["false_wake"] = f"{sum(1 for r in neg if r['false_wake'])}/{len(neg)}"
    res = dict(res, rows=[r for r in res["rows"] if r["group"] != "negative"])
    for g in ("take", "domain", "all"):
        rs = [r for r in res["rows"] if g == "all" or r["group"] == g]
        if not rs:
            continue
        out[f"wer_{g}"] = round(sum(r["err"] for r in rs) / max(1, sum(r["n"] for r in rs)), 3)
        tot = sum(r["tot"] for r in rs)
        out[f"intent_{g}"] = f"{sum(r['hit'] for r in rs)}/{tot}"
        w = [r["wake"] for r in rs if r["wake"] is not None]
        out[f"wake_{g}"] = f"{sum(w)}/{len(w)}"
    audio = sum(r["audio_s"] for r in res["rows"])
    dec = sum(r["dec_s"] for r in res["rows"])
    out["rtf"] = round(dec / audio, 2)
    four = [r["dec_s"] / r["audio_s"] * 4.0 for r in res["rows"] if r["audio_s"] > 1.0]
    out["s_per_4s_clip"] = round(float(np.mean(four)), 2) if four else None
    out["p95_per_4s"] = round(float(np.percentile(four, 95)), 2) if four else None
    return out


def print_table(sums):
    cols = ["label", "wer_take", "wer_domain", "intent_all", "wake_take", "false_wake", "rtf", "s_per_4s_clip", "p95_per_4s", "load_s"]
    print("  ".join(f"{c:<14}" if i == 0 else f"{c:>12}" for i, c in enumerate(cols)))
    for s in sums:
        print("  ".join((f"{str(s.get(c, '-')):<14}" if i == 0 else f"{str(s.get(c, '-')):>12}") for i, c in enumerate(cols)))


def run(ns):
    import os
    import server
    work = Path(ns.workdir)
    manifest = json.loads((work / "manifest.json").read_text())
    if ns.group:
        manifest = [m for m in manifest if m["group"] == ns.group]
    results, sums = [], []
    for spec in ns.configs:
        try:
            # Back off when the box is busy: the mini runs other jobs.
            if os.getloadavg()[0] > ns.max_load:
                print(f"load {os.getloadavg()[0]:.1f} above {ns.max_load}; waiting up to 5 min")
                for _ in range(30):
                    time.sleep(10)
                    if os.getloadavg()[0] <= ns.max_load:
                        break
            res = run_config(spec, manifest, server)
        except Exception as e:
            print(f"{spec.partition('=')[0]}: FAILED {type(e).__name__}: {str(e)[:200]}")
            continue
        results.append(res)
        sums.append(summarise(res))
        print(json.dumps(sums[-1]), flush=True)
        if ns.verbose:
            for r in res["rows"]:
                print(f"   [{r['id']}] ref={r['ref']!r} hyp={r['hyp']!r} err={r['err']}/{r['n']} {r['dec_s']:.2f}s")
    print_table(sums)
    if ns.json:
        Path(ns.json).write_text(json.dumps(results, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("workdir")
    p.add_argument("--takes", help="folder holding 1-close.wav ... (private; optional)")
    p.add_argument("--voices", default="amy,lessac")
    p.add_argument("--snr", default="12,4")
    p.add_argument("--vad-model")
    r = sub.add_parser("run")
    r.add_argument("workdir")
    r.add_argument("configs", nargs="+")
    r.add_argument("--group", choices=["take", "domain"])
    r.add_argument("--json")
    r.add_argument("--verbose", "-v", action="store_true")
    r.add_argument("--max-load", type=float, default=3.0)
    ns = ap.parse_args()
    (prep if ns.cmd == "prep" else run)(ns)


if __name__ == "__main__":
    main()
