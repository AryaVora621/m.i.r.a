#!/usr/bin/env python3
"""Replays room recordings through the listener's real wake path and prints raw hits, accepted wakes and the
transcripts of rejected hits.

Usage (on the Pi, in the listener venv, niced):
    nice -n 19 ~/mira-listen/venv/bin/python room_eval.py TAKE.wav [TAKE.wav ...] \\
        --kws-dir ~/mira-listen/kws --vad-model ~/mira-listen/models/silero_vad.onnx \\
        --speech-token-file ~/.config/mira/speech.token [--no-stt] [--json OUT] [-- listener args]

The path is the live one: Listener.run() with a WavSource (same 100 ms chunking), the AGC, the spotter built by
build_kws with the listener's own settings, capture() until end of speech, then the wake clip goes to the speech
server's /stt and `has_wake` decides. Only the Hermes/music/TTS half is replaced (handle_clip records instead of
answering). Each hit is judged on its own: no follow-up window, and after each capture the replay goes back to just after the
hit (live, a request swallows the audio that follows; five bare "Mira" would otherwise score as one wake).
Any argument this script does not know is passed to the listener's parser, so
`--kws-threshold 0.1 --agc-max-gain 8 --no-agc` work as in the service. With --no-stt the verdict is not
checked and every raw hit counts as accepted ("raw").

Trailing digital zero (the mic's touch-mute) is cut off each take before replay.
The recordings are the owner's voice: keep them out of the repo; this script reads whatever path it is given.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

# In the repo mira_listen.py is one folder up; copied to the Pi it sits beside this script.
for _d in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent):
    sys.path.insert(0, str(_d))
import mira_listen as ml  # noqa: E402


class EvalSource(ml.WavSource):
    """WavSource over a float array (already trimmed), so the eval reads the file once."""

    def __init__(self, x, tail_s=1.0):
        self.x = np.concatenate([x, np.zeros(int(tail_s * ml.RATE), dtype=np.float32)])
        self.pos = 0


class KeepStream:
    """The spotter, but create_stream() hands back the same (already reset) stream. Live, a capture ends in
    silence and a fresh stream starts there; the eval rewinds into the tail of the previous utterance, and a
    brand-new stream started mid-speech was seen to stay deaf for the next 2 s."""

    def __init__(self, kws):
        self._k, self._s = kws, None

    def create_stream(self):
        if self._s is None:
            self._s = self._k.create_stream()
        return self._s

    def __getattr__(self, name):
        return getattr(self._k, name)


class EvalListener(ml.Listener):
    def __init__(self, args, source, stt):
        super().__init__(args, source)
        self.kws = KeepStream(self.kws)
        self.stt_on = stt
        self.hits = []  # {"t", "raw", "text", "verdict"}
        self._last_hit_t = 0.0

    def _mark(self, kind, detail=""):
        if kind == "wake":
            self._last_hit_t = self.src.clock()
        self.events.append((kind, detail))

    def capture(self, seed, wait_speech_s, max_s):
        # The live loop lets a request swallow the audio that follows a hit, so five bare "Mira" in a row would
        # count as one wake plus four requests. The eval is per utterance: after the capture the source goes
        # back to just after the hit, so every "Mira" in the take gets its own chance to fire.
        p = self.src.pos
        clip = super().capture(seed, wait_speech_s, max_s)
        self.src.pos = p + int(0.3 * ml.RATE)
        return clip

    def handle_clip(self, clip, verify=False):
        t = self._last_hit_t
        if not verify:
            return
        if not self.stt_on:
            self.hits.append({"t": round(t, 1), "text": "", "verdict": "raw"})
            return
        text = self.speech.stt(ml.pcm16_wav_bytes(clip))
        # Same rule as Listener.handle_clip: an empty transcript is a reject, not a wake.
        verdict = "accepted" if text.strip() and ml.has_wake(text) else "rejected"
        self.hits.append({"t": round(t, 1), "text": text.strip(), "verdict": verdict})
        self.followup_until = 0.0


def trim_zero(x):
    nz = np.nonzero(x)[0]
    return x[: nz.max() + 1] if len(nz) else x[:0]


def eval_take(path, argv, stt):
    args = ml.build_parser().parse_args(argv)
    args.voice_url = ""  # nothing is posted to the wall
    x = trim_zero(ml.read_wav_float(path))
    lis = EvalListener(args, EvalSource(x), stt)
    t0 = time.time()
    lis.run()
    raw = sum(1 for k, _ in lis.events if k == "wake")
    return {"take": Path(path).name, "audio_s": round(len(x) / ml.RATE, 1), "raw": raw, "hits": lis.hits,
            "accepted": sum(1 for h in lis.hits if h["verdict"] in ("accepted", "raw")),
            "empty": sum(1 for h in lis.hits if h["verdict"] == "empty"),
            "rejected": [h for h in lis.hits if h["verdict"] == "rejected"], "secs": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wavs", nargs="+")
    ap.add_argument("--no-stt", action="store_true")
    ap.add_argument("--json", help="also write the results here")
    ap.add_argument("--expect", help="comma list of expected wakes per take, for the summary line")
    ns, rest = ap.parse_known_args()
    rest = [a for a in rest if a != "--"]
    results = [eval_take(w, rest, not ns.no_stt) for w in ns.wavs]
    expect = [int(e) for e in ns.expect.split(",")] if ns.expect else [None] * len(results)
    print(f"listener args: {' '.join(rest) or '(defaults)'}")
    for r, e in zip(results, expect):
        acc = r["accepted"] + r["empty"]
        print(f"{r['take']:<16} audio {r['audio_s']:>5}s  raw hits {r['raw']:>2}  accepted {r['accepted']:>2}"
              f"  empty {r['empty']}  rejected {len(r['rejected'])}" + (f"  expected {e}" if e is not None else ""))
        for h in r["hits"]:
            print(f"    {h['t']:>6}s  {h['verdict']:<8} {h['text'][:70]!r}")
    if ns.json:
        Path(ns.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
