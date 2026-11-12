#!/usr/bin/env python3
"""Silent timing of Mira's reply path: how long after the text is known the first audio would start, and how far the
wall's caption runs ahead of (or behind) the voice. Nothing is played and nothing is posted to the wall.

Run it from a release or checkout of the code under test, with that code first on the path (so the same script times
the old and the new facade):

    cd <release>/wall && python3 -B tests/speak_timing.py --speech-url http://127.0.0.1:8766 \\
        --speech-token-file ~/.config/mira/speech.token

It uses the REAL speech server for the TTS calls (the Pi's splitter in production), the real facade `say` and the real
MiraAudio.say_file; only the two mira-audio sub-commands are simulated (sleeps that stand for the measured costs of the
Pi: `duck` 0.2 s, a `say` start-up 0.3 s before sound, then the clip's length) and the reporter records its posts.
The wall is assumed to show a post `--post-latency` seconds after it is made (Agent OS cue, SSE, render).

The caption of the OLD flow is the page's own estimate (2.6 words a second from the moment it shows the whole reply); the
NEW flow's captions are the posts the playback makes on its clock. Lead = when the words are shown minus when they are
spoken, so a positive lead means the text is ahead of the voice.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mira_listen as ml  # noqa: E402
from listener_plugins.playback_miraaudio import MiraAudio  # noqa: E402
from listener_plugins.speech_link import Speech  # noqa: E402
from listener_plugins.tts_remote import TtsRemote  # noqa: E402

REPLY = ('Printers are up on the wall (print studio, 5 minutes). I took "duty printers" to mean your 3D printers. '
         'If you meant a different screen, tell me which one.')
CLIENT_WPS = 2.6        # the page's SPEAKING_WORDS_PER_SECOND
SENT_PAUSE, COMMA_PAUSE, MIN_CHUNK = 0.35, 0.15, 0.5


def page_groups(text):
    """Port of the page's captionChunks + chunkDurations: [(start_offset_s, words)] the old page showed."""
    try:
        from listener_plugins.speaking import caption_words
        groups = caption_words(text)
    except ImportError:  # old code: three words at a time is close enough for the old estimate
        w = text.split()
        groups = [" ".join(w[i:i + 3]) for i in range(0, len(w), 3)]
    words = sum(len(g.split()) for g in groups)
    total = words / CLIENT_WPS
    chars = sum(len(g) for g in groups) or 1
    out, at = [], 0.0
    for g in groups:
        pause = SENT_PAUSE if re.search(r"[.!?:;][\"')\]]*$", g) else COMMA_PAUSE if g.endswith(",") else 0.0
        out.append((at, g))
        at += max(MIN_CHUNK, len(g) / chars * total) + pause
    return out


class Sim:
    def __init__(self, duck_s, start_s, post_latency):
        self.t0 = time.monotonic()
        self.duck_s, self.start_s, self.post_latency = duck_s, start_s, post_latency
        self.events = []            # (t, kind, detail)
        self.sound = []             # (text_chunk, sound_start_t, duration)
        self.posts = []             # (t, state, reply)
        self.lock = threading.Lock()

    def now(self):
        return time.monotonic() - self.t0

    def log(self, kind, detail=""):
        with self.lock:
            self.events.append((self.now(), kind, detail))


def run(label, text, sim, speech, voice_first_chars):
    clean = []

    class Audio(MiraAudio):
        def call(self, *cmd):
            if cmd[0] == "duck":
                sim.log("duck start")
                time.sleep(sim.duck_s)
                sim.log("duck done")
            elif cmd[0] == "say":
                sim.log("say launched")
                time.sleep(sim.start_s)
                dur = clean[-1] if clean else 0
                sim.log("sound start")
                time.sleep(dur)
                sim.log("sound end")
            return 0

    class Rep:
        def update(self, state, heard="", reply="", fast=False):
            sim.posts.append((sim.now(), state, reply))

        def set_level(self, v):
            pass

    audio = Audio()
    # the length of the clip about to be played is known to the fake say through this list
    real_say_file = audio.say_file

    def say_file(path, wav, set_level, captions=None, on_caption=None):
        clean.append(ml.wav_seconds(wav) if hasattr(ml, "wav_seconds") else _wav_s(wav))
        t_launch = sim.now()
        sim.sound.append([wavs.get(id(wav), ""), t_launch + sim.start_s, clean[-1]])
        return real_say_file(path, wav, set_level, captions=captions, on_caption=on_caption) if captions is not None \
            else real_say_file(path, wav, set_level)

    wavs = {}
    audio.say_file = say_file
    try:
        tts = TtsRemote(speech, first_chunk_chars=voice_first_chars)
    except TypeError:
        tts = TtsRemote(speech)
    real_tts = tts.tts

    def timed_tts(chunk, pin=False):
        t = sim.now()
        sim.log("tts request", chunk[:30])
        wav = real_tts(chunk, pin=pin)
        sim.log("tts done", f"{sim.now() - t:.2f}s")
        wavs[id(wav)] = chunk
        return wav

    tts.tts = timed_tts
    lc = types.SimpleNamespace(caption_sync=True, barge_guard_words=r"\bmira\b")
    st = types.SimpleNamespace(tts=tts, playback=audio, rep=Rep(), src=types.SimpleNamespace(clock=time.monotonic),
                               _heard="bring up my duty printers", _reply=text, _lc=lc)
    for name in ("say", "_say_file", "_speaker", "_next_wav", "_guarded", "_unduck", "_audio"):
        if hasattr(ml.Listener, name):
            setattr(st, name, types.MethodType(getattr(ml.Listener, name), st))
    st._audio = lambda *cmd: audio.call(*cmd)
    turn = ml._Turn(1)
    sim.log("text known")
    st.say(text, turn)
    sim.log("done")
    return st


def _wav_s(wav):
    import io
    import wave
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes() / w.getframerate()


def report(label, sim):
    known = next(t for t, k, _ in sim.events if k == "text known")
    first = min(s for _, s, _ in sim.sound)
    print(f"\n== {label}")
    for t, k, d in sim.events:
        print(f"  {t - known:6.2f}s  {k} {d}")
    print(f"  time to first sound: {first - known:.2f}s after the text")
    # when each caption group is shown vs spoken
    leads = []
    shown = []
    if hasattr(ml, "Speaker"):  # the new flow: the playback posts each group of words on its own clock
        for t, _, reply in sim.posts:
            if reply:
                shown.append((t + sim.post_latency, reply))
    else:  # old flow: the page gets the whole reply once and types it out on its estimate
        first_post = next((t for t, s, r in sim.posts if s == "speaking" and r), None)
        if first_post is not None:
            full = next(r for t, s, r in sim.posts if s == "speaking" and r)
            for off, g in page_groups(full):
                shown.append((first_post + sim.post_latency + off, g))
    # spoken time of each shown group: chunk start plus its character share of the chunk
    spoken = []
    for chunk, start, dur in sim.sound:
        total = max(1, len(chunk))
        pos = 0
        for g in (page_groups(chunk) if True else []):
            idx = chunk.find(g[1].split()[0], pos)
            idx = pos if idx < 0 else idx
            spoken.append((start + dur * idx / total, g[1]))
            pos = idx + len(g[1])
    # match groups by their first word in order
    for (ts, tw), (tshow, gshow) in zip(spoken, shown):
        leads.append(tshow - ts)
        print(f"  group {gshow[:28]!r:32} shown {tshow - known:6.2f}s  spoken {ts - known:6.2f}s  lead {tshow - ts:+.2f}s")
    if leads:
        print(f"  caption lead: first {leads[0]:+.2f}s, max {max(leads):+.2f}s, min {min(leads):+.2f}s, "
              f"mean {sum(leads) / len(leads):+.2f}s")
    gaps = [b[1] - (a[1] + a[2]) for a, b in zip(sim.sound, sim.sound[1:])]
    if gaps:
        print(f"  pause between spoken chunks: {', '.join(f'{g:.2f}s' for g in gaps)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speech-url", default="http://127.0.0.1:8766")
    ap.add_argument("--speech-token-file", default=os.path.expanduser("~/.config/mira/speech.token"))
    ap.add_argument("--text", default=REPLY)
    ap.add_argument("--label", default="")
    ap.add_argument("--first-chars", type=int, default=90, help="first_chunk_chars (ignored by code that lacks it)")
    ap.add_argument("--duck-s", type=float, default=0.2)
    ap.add_argument("--start-s", type=float, default=0.3)
    ap.add_argument("--post-latency", type=float, default=0.25)
    ap.add_argument("--max-sentences", type=int, default=0, help="limit the spoken sentences first (the guard)")
    a = ap.parse_args()
    speech = Speech(a.speech_url, a.speech_token_file, tts_timeout_s=45)
    text = a.text
    if a.max_sentences:
        from listener_plugins.speaking import limit_sentences
        text = limit_sentences(text, a.max_sentences)
    sim = Sim(a.duck_s, a.start_s, a.post_latency)
    run(a.label, text, sim, speech, a.first_chars)
    report(a.label or os.path.basename(os.getcwd()), sim)


if __name__ == "__main__":
    main()
