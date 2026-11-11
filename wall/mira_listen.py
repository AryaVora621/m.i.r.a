#!/usr/bin/env python3
"""mira-listen: the wall's ear. Wake word on the Pi, speech and brain elsewhere.

Flow: mic (pw-record on the Pi, or --wav FILE) -> sherpa-onnx streaming keyword spotter ("mira")
-> Silero VAD until end of speech -> POST the clip to the Mac mini's /stt -> either a local music
intent (music-api) or the text to Hermes (Agent OS's session API) -> POST the reply to /tts ->
`mira-audio duck`, `say`, `unduck`. The wall learns the state from Agent OS's /api/hooks/voice.

This file is the facade (spec 4): the turn loop is v1's, but every part it uses is a plugin built by the core
(`mirakit.core.Core`), read on use so a rebuild takes effect at the next safe point. The v1 names the tests and
`room_eval` import stay importable from here. `mira_listen_v1.py` is the frozen original.
"""

import argparse
import collections
import os
import queue
import signal
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

import listener_plugins
from listener_plugins.agc_mic import MicAGC
from listener_plugins.brain_hermes import Hermes
from listener_plugins.common import (CHUNK, RATE, http, log, pcm16_wav_bytes, read_secret, read_wav_float,
                                     read_wav_float_bytes, resample)
from listener_plugins.gate_wake import HALLUCINATIONS, WAKE_OK, WAKE_PREFIX, WAKE_WORD, has_wake, strip_wake
from listener_plugins.intent_music import Music, music_intent
from listener_plugins.mute_zero import MuteDetector
from listener_plugins.playback_miraaudio import wav_envelope
from listener_plugins.reporter_agentos import VoiceReporter
from listener_plugins.source_pipewire import PwSource
from listener_plugins.source_wav import WavSource
from listener_plugins.speech_link import Speech, SpeechOffline
from listener_plugins.tts_remote import clean_for_speech, speech_chunks
from listener_plugins.wake_kws import build_kws
from mirakit.core import RESET_STREAM, Core

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "kws_dir": os.environ.get("MIRA_KWS_DIR", str(HERE.parent / "voice" / "models" / "kws")),
    "vad_model": os.environ.get("MIRA_VAD_MODEL", str(HERE / "models" / "silero_vad.onnx")),
    # The mini's Tailscale address, not macmini.local: the bearer token goes over plain HTTP, and an mDNS name
    # can be spoofed by any LAN device, while Tailscale encrypts and authenticates both ends.
    "speech_url": os.environ.get("MIRA_SPEECH_URL", "http://100.77.98.89:8765"),
    "speech_token_file": os.environ.get("MIRA_SPEECH_TOKEN_FILE", ""),
    "hermes_url": os.environ.get("MIRA_HERMES_URL", "http://127.0.0.1:8642"),
    "hermes_key_file": os.environ.get("MIRA_HERMES_KEY_FILE", ""),
    "voice_url": os.environ.get("MIRA_VOICE_URL", "http://127.0.0.1:3456/api/hooks/voice"),
    "voice_token_file": os.environ.get("MIRA_VOICE_TOKEN_FILE", ""),
    "music_url": os.environ.get("MIRA_MUSIC_URL", "http://127.0.0.1:4534"),
    "music_auth_file": os.environ.get("MIRA_MUSIC_AUTH_FILE", ""),
    "mira_audio": os.environ.get("MIRA_AUDIO_BIN", "mira-audio"),
    "identity": os.environ.get("MIRA_IDENTITY_FILE", str(HERE / "MIRA_WALL_IDENTITY.md")),
    "chime": os.environ.get("MIRA_CHIME", str(HERE / "assets" / "offline.wav")),
    "voice": os.environ.get("MIRA_VOICE", ""),
}

# Journal lines whose detail is what was said: dropped when loop.turns.log_text is off.
_TEXT_KINDS = frozenset({"heard", "reply", "wake_accepted", "wake_rejected", "empty"})
_UNSET = object()


class _Svc:
    """A Listener attribute that reads a core service on every use (a rebuild swaps the object under it). An
    assignment (room_eval wraps the spotter) overrides the service for that instance."""

    def __init__(self, service, many=False):
        self.service, self.many = service, many

    def __set_name__(self, owner, name):
        self.attr = "_ov_" + name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        ov = obj.__dict__.get(self.attr, _UNSET)
        if ov is not _UNSET:
            return ov
        if self.many:
            # priority is live, so the order is taken from the values now (spec 2.2: priority desc, then name)
            return sorted(obj.core.services(self.service), key=lambda p: (-p.priority, getattr(p, "name", "")))
        return obj.core.service(self.service, None)

    def __set__(self, obj, value):
        obj.__dict__[self.attr] = value


class TurnBrain:
    """The `hermes` attribute: forwards everything to the brain service; `ask` also brackets the brain phase for the
    core (previews play inline inside it) and refreshes the persona when it is re-read every turn."""

    def __init__(self, core):
        object.__setattr__(self, "_core", core)

    def _brain(self):
        return self._core.service("brain")

    def ask(self, text, session_ttl, now):
        core, brain = self._core, self._brain()
        if core.cfg("persona.file").reread == "turn":
            persona = core.service("persona", None)
            if persona is not None:
                brain.persona = persona.text()
        core.turn_begin()
        try:
            return brain.ask(text, session_ttl, now)
        finally:
            core.turn_end()

    def __getattr__(self, name):
        return getattr(self._brain(), name)

    def __setattr__(self, name, value):
        setattr(self._brain(), name, value)


class Listener:
    kws = _Svc("wake")
    speech = _Svc("speech")
    stt = _Svc("stt")
    tts = _Svc("tts")
    gate = _Svc("gate")
    playback = _Svc("playback")
    rep = _Svc("reporter")
    agc = _Svc("agc")
    mute = _Svc("mute")
    vadf = _Svc("vad")
    intents = _Svc("intent", many=True)

    @property
    def _vc(self):
        return self.vadf.config

    @property
    def music(self):
        return next((p.music for p in self.intents if hasattr(p, "music")), None)

    def __init__(self, args, source, core=None):
        if core is None:
            core = Core.from_args(args, source=source, specs=listener_plugins.specs())
            core.boot()
        self.core = core
        core.bind_args(args)
        self.a, self.src = args, source
        n = core.cfg("loop.turns").events_max
        self.events = collections.deque(maxlen=n) if n > 0 else []  # (kind, detail) trail for tests and --verbose
        self.hermes = TurnBrain(core)
        self.vad = self.vadf.new()
        self.followup_until = 0.0
        self.wakes = 0
        self._heard = ""
        self._reply = ""
        self._hit_mark = None

    # -- helpers
    def heard_texts(self):
        return [d for k, d in self.events if k == "heard"]

    def _mark(self, kind, detail=""):
        self.events.append((kind, detail))
        shown = "" if kind in _TEXT_KINDS and not self._log_text() else detail
        log(f"{kind} {shown}".strip())
        self.core.note(kind)
        self._emit_for(kind, detail)

    def _log_text(self):
        try:
            return self.core.cfg("loop.turns").log_text
        except Exception:
            return True

    def _emit_for(self, kind, detail):
        """The first eight events of spec 2.10, from the loop's own marks; an observer never breaks the loop."""
        try:
            emit = self.core.emit
            if kind == "wake":
                agc = self.agc
                emit("wake.hit", agc_gain=float(agc.gain) if agc else None)
            elif kind == "wake_accepted":
                emit("wake.accepted")
            elif kind == "wake_rejected":
                emit("wake.rejected", reason="empty" if detail == "heard nothing" else "no_wake")
            elif kind == "heard":
                emit("turn.heard", words=len(detail.split()))
            elif kind == "reply":
                hit, self._hit_mark = self._hit_mark, None
                if hit is not None:
                    source = "fail" if detail == hit.fail_reply else "intent"
                else:
                    source = "fail" if detail == self.hermes.fail_reply else "brain"
                emit("turn.replied", source=source)
            elif kind == "followup":
                emit("turn.spoken", chunks=len(self.tts.chunks(self._reply)))
            elif kind == "offline":
                emit("turn.offline", stage="tts" if detail.startswith("tts") else "stt")
            elif kind == "mic":
                emit("mic.changed", mic=detail)
            else:
                for p in self.intents:
                    if p.mark == kind:
                        self._hit_mark = p
                        break
        except Exception as e:
            log(f"event {kind} failed: {type(e).__name__}")

    def _audio(self, *cmd):
        return self.playback.call(*cmd)

    def _read(self):
        """One mic chunk through the AGC, with the wall's level taken from it (silence reads as 0)."""
        c = self.src.read()
        if c is None:
            return None
        verdict = self.mute.feed(c)  # raw chunk: the AGC must not touch the zero count
        if verdict:
            if verdict != "repeat":
                self._mark("mic", verdict)  # logs "mic muted" / "mic live"
            self.rep.set_mic(self.mute.state)
        if self.agc is not None:
            c = self.agc.process(c)
            self.rep.set_level(self.agc.level)
        else:
            self.rep.set_level(min(1.0, float(np.sqrt(np.mean(c ** 2))) * self.a.level_scale))
        return c

    def _reset_vad(self):
        self.vad = self.vadf.new()

    # -- capture: from the first chunk after the wake word (or follow-up start) to end of speech
    def capture(self, seed, wait_speech_s, max_s):
        self._reset_vad()
        clip = [seed] if seed is not None and len(seed) else []
        for c in clip:
            self.vad.accept_waveform(c)
        start = self.src.clock()
        heard = False
        while True:
            c = self._read()
            if c is None:
                break
            clip.append(c)
            self.vad.accept_waveform(c)
            speaking = self.vad.is_speech_detected()
            if speaking:
                heard = True
            elapsed = self.src.clock() - start
            if heard and not speaking:
                break
            if not heard and elapsed > wait_speech_s:
                return None
            if elapsed > max_s:
                break
        return np.concatenate(clip) if heard else None

    def say(self, text):
        """TTS the text (pipelined per chunk) and play it ducked. Raises SpeechOffline."""
        chunks = self.tts.chunks(text)
        if not chunks:
            return
        q = queue.Queue()
        err = []

        def synth():
            try:
                for i, ch in enumerate(chunks):
                    q.put(self.tts.tts(ch, pin=i > 0))
            except SpeechOffline as e:
                err.append(e)
            q.put(None)

        threading.Thread(target=synth, daemon=True).start()
        first = q.get()
        if first is None:
            raise err[0] if err else SpeechOffline("no audio")
        self.rep.update("speaking", self._heard, self._reply)
        self._audio("duck")
        try:
            wav = first
            while wav is not None:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    f.write(wav)
                try:
                    self._say_file(f.name, wav)
                finally:
                    os.unlink(f.name)
                wav = q.get()
        finally:
            self._audio("unduck")

    def _say_file(self, path, wav):
        """mira-audio say FILE (blocking) while a thread feeds the reporter the reply's loudness in step with playback."""
        self.playback.say_file(path, wav, self.rep.set_level)

    def fail_offline(self, why):
        self._mark("offline", why)
        self.rep.update("error", self._heard, self.a.offline_text)
        if os.path.exists(self.a.chime):
            self._audio("say", self.a.chime)
        self.rep.update("idle")

    def handle_clip(self, clip, verify=False):
        self._heard = ""
        self._reply = ""
        self.rep.update("thinking")
        try:
            text = self.stt.stt(pcm16_wav_bytes(clip))
        except SpeechOffline as e:
            return self.fail_offline(f"stt: {e}")
        if verify and not (text.strip() and self.gate.has_wake(text)):
            # The tiny spotter fires on look-alikes (mirror, miracle, Miranda); the transcript decides. An empty
            # transcript is a reject too: the low threshold lets room noise through, and accepting nothing would
            # open the 8 s follow-up window, where anything said in the room goes to Hermes.
            self._mark("wake_rejected", f"heard {text.strip()[:60]!r}" if text.strip() else "heard nothing")
            self.rep.update("idle")
            return
        if verify:
            # Only the first words of the transcript: the journal needs the verdict, not the whole request.
            self._mark("wake_accepted", f"opens {' '.join(text.split()[:3])!r}")
        text = self.gate.strip_wake(text)
        norm = self.gate.normalize(text)
        if self.gate.is_empty(norm):
            # Just "Mira" (or noise): keep listening for a few seconds without needing the wake word.
            self._mark("empty", norm)
            self.rep.update("idle")
            self.followup_until = self.src.clock() + self.a.followup_s
            return
        self._heard = text
        self.rep.update("thinking", text)
        self._mark("heard", text)
        hit = next(((p, i) for p in self.intents for i in [p.match(text)] if i), None)
        try:
            if hit:
                p, intent = hit
                self._mark(p.mark, intent)
                try:
                    self._reply = p.handle(intent) or p.done_reply
                except Exception as e:
                    log(f"{p.log_name} failed: {type(e).__name__}")
                    self._reply = p.fail_reply
            else:
                try:
                    self._reply = self.hermes.ask(text, self.a.session_ttl, self.src.clock()) or self.hermes.empty_reply
                except Exception as e:
                    log(f"hermes failed: {type(e).__name__}: {str(e)[:100]}")
                    self._reply = self.hermes.fail_reply
                    self.rep.update("error", text, self._reply)
            self._mark("reply", self._reply)
            self.say(self._reply)
        except SpeechOffline as e:
            return self.fail_offline(f"tts: {e}")
        self.rep.update("idle", text, self._reply)
        self.src.flush()
        self.followup_until = self.src.clock() + self.a.followup_s
        self._mark("followup", f"{self.a.followup_s}s")

    def run(self):
        stream = self.kws.create_stream()
        self.rep.update("idle")
        pre = collections.deque(maxlen=self.a.preroll_chunks)  # last 1.5 s: follow-up starts at the first syllable; wake clips keep the word for verification
        fvad_on = False
        try:
            while True:
                flags = self.core.safe_point(self.src.clock() >= self.followup_until)
                if flags & RESET_STREAM:
                    stream = self.kws.create_stream()
                    pre.clear()
                    fvad_on = False
                c = self._read()
                if c is None:
                    break
                pre.append(c)
                if self.src.clock() < self.followup_until:
                    # No wake word needed in the window: speech starting is enough.
                    if not fvad_on:
                        self._reset_vad()
                        fvad_on = True
                    self.vad.accept_waveform(c)
                    if self.vad.is_speech_detected():
                        fvad_on = False
                        self.rep.update("listening")
                        clip = self.capture(np.concatenate(list(pre)[-self.a.followup_seed_chunks:]),
                                            wait_speech_s=self.a.followup_wait_s, max_s=self.a.max_s)
                        stream = self.kws.create_stream()
                        pre.clear()
                        if clip is not None:
                            self._mark("followup_capture", f"{len(clip)/RATE:.1f}s")
                            self.handle_clip(clip)
                        else:
                            self.rep.update("idle")
                    continue
                fvad_on = False
                stream.accept_waveform(RATE, c)
                while self.kws.is_ready(stream):
                    self.kws.decode_stream(stream)
                hit = self.kws.get_result(stream)
                if hit:
                    self.wakes += 1
                    agc = f", agc {self.agc.gain:.1f}x" if self.agc else ""
                    self._mark("wake", f"spotter hit {hit!r}{agc}")
                    self.kws.reset_stream(stream)
                    self.rep.update("listening")
                    # The keyword fires just after the word ends, so a short pre-roll catches an
                    # "in one breath" request without dragging in much of the word itself.
                    # The VAD only needs the last 0.3 s; the STT also gets the 1.2 s before it, so the
                    # transcript still contains the wake word and can confirm the spotter was right.
                    before = list(pre)
                    k = self.a.wake_seed_chunks
                    clip = self.capture(np.concatenate(before[-k:]), wait_speech_s=self.a.wait_speech,
                                        max_s=self.a.max_s)
                    stream = self.kws.create_stream()
                    pre.clear()
                    if clip is None:
                        self._mark("no_speech")
                        self.rep.update("idle")
                        continue
                    head = np.concatenate(before[:-k]) if len(before) > k else clip[:0]
                    self.handle_clip(np.concatenate([head, clip]), verify=True)
            self._mark("eof")
        finally:
            self.rep.update("idle", self._heard, self._reply)
            self.rep.close()
            self.src.close()


def build_parser():
    d = DEFAULTS
    p = argparse.ArgumentParser(description="Mira's wall listener")
    p.add_argument("--config", default=None, help="mira.json: the plugin configuration (see mira --help)")
    p.add_argument("--wav", help="read this 16-bit WAV instead of the microphone (tests)")
    p.add_argument("--kws-dir", default=d["kws_dir"])
    p.add_argument("--keywords", default="keywords.txt")
    p.add_argument("--kws-threads", type=int, default=int(os.environ.get("MIRA_KWS_THREADS", "1")))
    p.add_argument("--kws-threshold", type=float, default=float(os.environ.get("MIRA_KWS_THRESHOLD", "0.15")))
    p.add_argument("--kws-score", type=float, default=float(os.environ.get("MIRA_KWS_SCORE", "1.0")))
    p.add_argument("--agc", action=argparse.BooleanOptionalAction, default=os.environ.get("MIRA_AGC", "1") != "0",
                   help="software AGC on the mic (MIRA_AGC=0 turns it off); knobs are the MIRA_AGC_* env vars")
    for knob in ("target", "max-gain", "rest", "gate"):
        p.add_argument(f"--agc-{knob}", type=float, default=None, help=f"override MIRA_AGC_{knob.upper().replace('-', '_')}")
    p.add_argument("--vad-model", default=d["vad_model"])
    p.add_argument("--vad-threshold", type=float, default=0.5)
    p.add_argument("--end-silence", type=float, default=0.7, help="seconds of silence that end a request")
    p.add_argument("--wait-speech", type=float, default=4.0, help="seconds to wait for speech after the wake word")
    p.add_argument("--max-s", type=float, default=10.0, help="longest request")
    p.add_argument("--followup-s", type=float, default=8.0)
    p.add_argument("--session-ttl", type=float, default=180.0, help="idle seconds before a new Hermes session")
    p.add_argument("--speech-url", default=d["speech_url"])
    p.add_argument("--speech-token-file", default=d["speech_token_file"])
    p.add_argument("--voice", default=d["voice"])
    p.add_argument("--hermes-url", default=d["hermes_url"])
    p.add_argument("--hermes-key-file", default=d["hermes_key_file"])
    p.add_argument("--identity", default=d["identity"])
    p.add_argument("--voice-url", default=d["voice_url"])
    p.add_argument("--voice-token-file", default=d["voice_token_file"])
    p.add_argument("--level-lag", type=float, default=float(os.environ.get("MIRA_LEVEL_LAG", "0.15")),
                   help="seconds from starting mira-audio say to sound, for the wall's reply level")
    p.add_argument("--error-hold", type=float, default=4.0, help="seconds the error state stays on the wall")
    p.add_argument("--music-url", default=d["music_url"])
    p.add_argument("--music-auth-file", default=d["music_auth_file"])
    p.add_argument("--mira-audio", default=d["mira_audio"])
    p.add_argument("--chime", default=d["chime"])
    return p




def _terminate(signum, frame):
    raise SystemExit(0)  # through Listener.run's finally, which posts the final idle


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    specs = listener_plugins.specs()
    if args.config:
        core = Core.from_file(args.config, argv=argv, machine="listener", specs=specs, parser=build_parser(),
                              mirror=True, changelog=True)
    else:
        core = Core.from_args(args, specs=specs)
    try:
        core.boot()
        src = core.service("source")
        lis = Listener(args, src, core)
        agc = lis.agc
        log(f"mira-listen up ({'wav' if args.wav or isinstance(src, WavSource) else 'mic'}), "
            f"kws threshold {args.kws_threshold} score {args.kws_score}, "
            + (f"agc target {agc.target} max {agc.max_gain}x rest {agc.rest}x gate {agc.gate}" if agc else "agc off"))
        if core.cfg("loop.turns").sigterm_idle:
            signal.signal(signal.SIGTERM, _terminate)
        try:
            lis.run()
        except KeyboardInterrupt:
            pass
    finally:
        core.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
