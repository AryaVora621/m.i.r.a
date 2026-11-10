#!/usr/bin/env python3
"""mira-listen: the wall's ear. Wake word on the Pi, speech and brain elsewhere.

Flow: mic (pw-record on the Pi, or --wav FILE) -> sherpa-onnx streaming keyword spotter ("mira")
-> Silero VAD until end of speech -> POST the clip to the Mac mini's /stt -> either a local music
intent (music-api) or the text to Hermes (Agent OS's session API) -> POST the reply to /tts ->
`mira-audio duck`, `say`, `unduck`. The wall learns the state from Agent OS's /api/hooks/voice.

Only sherpa-onnx and numpy are third-party; everything else is the standard library, so the Pi
venv stays small. No secrets live in the repo: tokens are read from files named by arguments.
"""

import argparse
import collections
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RATE = 16000
CHUNK = 1600  # 100 ms; small enough for snappy VAD, large enough that Python overhead stays low

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

HALLUCINATIONS = {"you", "thank you", "thanks", "thanks for watching", "bye", "okay", "uh", "um", "hmm", "the"}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------- audio sources

def resample(x: np.ndarray, r_in: int, r_out: int) -> np.ndarray:
    """Band-limited resample by FFT; only used for --wav inputs and tests, never in the live loop."""
    if r_in == r_out:
        return x
    n_out = int(round(len(x) * r_out / r_in))
    X = np.fft.rfft(x.astype(np.float64))
    Y = np.zeros(n_out // 2 + 1, dtype=complex)
    m = min(len(X), len(Y))
    Y[:m] = X[:m]
    return (np.fft.irfft(Y, n_out) * (n_out / max(len(x), 1))).astype(np.float32)


def read_wav_float(path) -> np.ndarray:
    return read_wav_float_bytes(path)


def read_wav_float_bytes(src) -> np.ndarray:
    """Path or WAV bytes -> float32 mono at 16 kHz."""
    with wave.open(io.BytesIO(src) if isinstance(src, bytes) else str(src), "rb") as w:
        n, ch, sw, rate = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("only 16-bit WAV is supported")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return resample(x, rate, RATE)


def pcm16_wav_bytes(x: np.ndarray, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767.0).astype("<i2").tobytes())
    return buf.getvalue()


class WavSource:
    """Feeds a file as fast as the pipeline asks. The clock is audio time, so windows still work."""

    live = False

    def __init__(self, path, tail_s=1.0):
        x = read_wav_float(path)
        # A little trailing silence lets the keyword spotter flush its last tokens at EOF.
        self.x = np.concatenate([x, np.zeros(int(tail_s * RATE), dtype=np.float32)])
        self.pos = 0

    def read(self):
        if self.pos >= len(self.x):
            return None
        c = self.x[self.pos:self.pos + CHUNK]
        self.pos += len(c)
        return c

    def clock(self):
        return self.pos / RATE

    def flush(self):
        pass

    def close(self):
        pass


class PwSource:
    """pw-record on stdout. A reader thread keeps the pipe drained so nothing blocks the PipeWire graph."""

    live = True

    def __init__(self, cmd=None):
        cmd = cmd or ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.q = queue.Queue(maxsize=600)  # 60 s of audio; beyond that the oldest is dropped
        self.stop = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        want = CHUNK * 2
        buf = b""
        while not self.stop:
            data = self.proc.stdout.read(want - len(buf))
            if not data:
                self.q.put(None)
                return
            buf += data
            if len(buf) == want:
                try:
                    self.q.put_nowait(np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0)
                except queue.Full:
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                buf = b""

    def read(self):
        return self.q.get()

    def clock(self):
        return time.monotonic()

    def flush(self):
        """Drop audio queued while we were thinking or speaking, so Mira never hears herself."""
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def close(self):
        self.stop = True
        self.proc.terminate()


# ------------------------------------------------------------------------------------------- mic AGC

def _envf(name, default):
    return float(os.environ.get(name, default))


class MicAGC:
    """Gentle software AGC on the mic chunks, ahead of the spotter, the VAD and the clip sent to speech.

    The JV603 delivers speech at rms 0.003 to 0.02 and a far or quiet voice barely clears its hiss (about
    0.0027), so the keyword spotter sees almost nothing from across the room. This lifts speech toward
    `target` rms with these properties:
      * the gain is capped at `max_gain`, and a chunk is never pushed past `ceiling` peak (no clipping);
      * attack is instant (a loud chunk drops the gain at once), release is slow (`release` per 100 ms chunk);
      * a noise floor is tracked (falls fast, rises slowly) and chunks under `gate` x floor are treated as
        silence: they never raise the gain, which relaxes slowly toward `rest`, so room hiss is not pumped;
      * the gain is ramped across each chunk so a change never steps the waveform.
    Digital zero stays zero. `speech` and `level` describe the last chunk for the wall's rings.
    """

    def __init__(self, target=None, max_gain=None, rest=None, release=None, decay=None, gate=None,
                 min_rms=None, ceiling=None):
        self.target = _envf("MIRA_AGC_TARGET", 0.05) if target is None else target
        self.max_gain = _envf("MIRA_AGC_MAX_GAIN", 10.0) if max_gain is None else max_gain
        self.rest = _envf("MIRA_AGC_REST", 3.0) if rest is None else rest
        self.release = _envf("MIRA_AGC_RELEASE", 0.25) if release is None else release
        self.decay = _envf("MIRA_AGC_DECAY", 0.03) if decay is None else decay
        self.gate = _envf("MIRA_AGC_GATE", 1.8) if gate is None else gate
        self.min_rms = _envf("MIRA_AGC_MIN_RMS", 0.001) if min_rms is None else min_rms
        self.ceiling = _envf("MIRA_AGC_CEILING", 0.9) if ceiling is None else ceiling
        self.rest = min(max(self.rest, 1.0), self.max_gain)
        self.gain = self.rest
        self.floor = _envf("MIRA_AGC_FLOOR", 0.003)  # the JV603's hiss; a start in the middle of speech must not set the floor
        self.speech = False
        self.level = 0.0

    def process(self, c):
        rms = float(np.sqrt(np.mean(c * c))) if len(c) else 0.0
        if rms <= 0.0:
            self.speech, self.level = False, 0.0
            return c
        speechlike = rms > max(self.floor * self.gate, self.min_rms)
        # Hiss-like chunks pull the floor in quickly; speech-like chunks only nudge it up, so a long
        # utterance does not teach the gate that speech is noise, yet a louder room is learned in minutes.
        rate = 0.0005 if speechlike else (0.2 if rms < self.floor else 0.05)
        self.floor += (rms - self.floor) * rate
        prev = self.gain
        if speechlike:
            want = min(self.max_gain, max(1.0, self.target / rms))
            self.gain = want if want < prev else prev + (want - prev) * self.release
        else:
            self.gain += (self.rest - self.gain) * self.decay
        peak = float(np.max(np.abs(c)))
        if peak * self.gain > self.ceiling:
            self.gain = max(1.0, self.ceiling / peak)  # loud input passes through untouched, as before the AGC existed
        # Attack is a step at the chunk start (a ramp from the old, higher gain could clip the first samples);
        # release ramps.
        g = np.linspace(prev, self.gain, len(c), dtype=np.float32) if self.gain > prev else self.gain
        out = np.clip(c * g, -1.0, 1.0).astype(np.float32, copy=False)
        self.speech = speechlike
        self.level = min(1.0, float(np.sqrt(np.mean(out * out))) * 8) if speechlike else 0.0
        return out


# -------------------------------------------------------------------------------- small HTTP kit

def http(method, url, body=None, headers=None, timeout=10.0):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def read_secret(path):
    if not path:
        return ""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# ------------------------------------------------------------------------- voice state to Agent OS

class VoiceReporter:
    """Posts {state, level, heard, reply, at} on transitions and text changes (state posts at most 2 per
    second), plus level-only posts (same state, new `level`) at up to 5 per second while the last posted state
    is `listening` or `speaking`, so the wall's rings can follow the voice.

    Every state change is kept in order (a quick listening -> thinking must not collapse); a text-only
    change replaces a still-queued item with the same state. A hold keeps `error` on the wall for a while
    before the follow-up idle goes out. A level-only post is only ever made when no state item is queued and
    does not count toward the state gap, so it can neither delay nor reorder a state change.
    """

    MIN_GAP = 0.5
    LEVEL_GAP = 0.2  # 5 level posts per second
    LEVEL_STATES = ("listening", "speaking")
    LEVEL_STEP = 0.03  # smaller moves than this are not worth a post

    def __init__(self, url, token_file, error_hold=4.0):
        self.url, self.token_file, self.error_hold = url, token_file, error_hold
        self.q = []
        self.cv = threading.Condition()
        self.last = None  # (state, heard, reply) of the last item enqueued
        self.level = 0.0
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_level(self, v):
        self.level = float(v)

    def update(self, state, heard="", reply=""):
        key = (state, heard, reply)
        with self.cv:
            if key == self.last:
                return
            item = {"state": state, "level": round(self.level, 3), "heard": heard, "reply": reply}
            if self.q and self.q[-1]["state"] == state and self.last and self.last[0] == state:
                self.q[-1].update(item)  # text change on a queued item: coalesce
            else:
                self.q.append(item)
            self.last = key
            self.cv.notify()

    def _next(self, posted, last_level_at):
        """Block until there is something to post: a queued state item first, else a due level-only item."""
        with self.cv:
            while True:
                if self.q:
                    return self.q.pop(0), False
                if self.closed:
                    return None, False
                if posted is None or posted["state"] not in self.LEVEL_STATES:
                    self.cv.wait()
                    continue
                due = last_level_at + self.LEVEL_GAP - time.monotonic()
                lvl = round(self.level, 3)
                if due <= 0 and abs(lvl - posted["level"]) >= self.LEVEL_STEP:
                    return dict(posted, level=lvl), True
                self.cv.wait(timeout=max(due, 0.05))

    def _run(self):
        last_sent = 0.0  # last STATE post: the 2 per second limit and the error hold are about states
        last_level_at = 0.0
        hold_until = 0.0
        posted = None
        while True:
            item, level_only = self._next(posted, last_level_at)
            if item is None:
                return
            if not level_only:
                wait = max(last_sent + self.MIN_GAP, hold_until) - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            item["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self._post(item)
            now = time.monotonic()
            last_level_at = now
            posted = item
            if not level_only:
                last_sent = now
                hold_until = last_sent + self.error_hold if item["state"] == "error" else 0.0

    def _post(self, item):
        token = read_secret(self.token_file)
        if not self.url:
            return
        try:
            http("POST", self.url, json.dumps(item).encode(),
                 {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, timeout=3.0)
        except Exception as e:  # the wall display is never worth stopping the assistant for
            log(f"voice hook failed: {type(e).__name__}")

    def close(self, wait=True):
        with self.cv:
            self.closed = True
            self.cv.notify()
        if wait:
            self.thread.join(timeout=20)


# ------------------------------------------------------------------------------------- the clients

class SpeechOffline(Exception):
    pass


class Speech:
    def __init__(self, url, token_file, voice=""):
        self.url, self.token_file, self.voice = url.rstrip("/"), token_file, voice

    def _hdr(self, extra=None):
        h = {"Authorization": f"Bearer {read_secret(self.token_file)}"}
        h.update(extra or {})
        return h

    def stt(self, wav_bytes) -> str:
        try:
            _, body = http("POST", f"{self.url}/stt", wav_bytes, self._hdr({"Content-Type": "audio/wav"}), timeout=30)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise SpeechOffline(str(e)) from e
        return json.loads(body).get("text", "")

    def tts(self, text) -> bytes:
        payload = {"text": text}
        if self.voice:
            payload["voice"] = self.voice
        try:
            _, body = http("POST", f"{self.url}/tts", json.dumps(payload).encode(),
                           self._hdr({"Content-Type": "application/json"}), timeout=45)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise SpeechOffline(str(e)) from e
        return body


class Hermes:
    """The Hermes API as Agent OS's chat.py uses it: POST /api/sessions, then
    POST /api/sessions/{id}/chat/stream {message, instructions} answered as server-sent events."""

    def __init__(self, url, key_file, persona):
        self.url, self.key_file, self.persona = url.rstrip("/"), key_file, persona
        self.session_id = None
        self.last_used = 0.0

    def _hdr(self):
        return {"Authorization": f"Bearer {read_secret(self.key_file)}", "Content-Type": "application/json"}

    def new_session(self):
        title = f"Mira wall {time.strftime('%Y-%m-%d %H:%M:%S')}"
        for attempt in range(2):
            try:
                _, body = http("POST", f"{self.url}/api/sessions", json.dumps({"title": title}).encode(),
                               self._hdr(), timeout=15)
                obj = json.loads(body)
                sess = obj.get("session") or obj.get("data") or obj
                return sess["id"]
            except urllib.error.HTTPError as e:
                # Hermes titles are unique; a second conversation in the same second gets a suffix.
                if e.code == 400 and attempt == 0:
                    title += f" ({hex(int(time.time()))[2:]})"
                    continue
                raise

    def ask(self, text, session_ttl, now):
        if self.session_id is None or now - self.last_used > session_ttl:
            self.session_id = self.new_session()
        payload = {"message": text, "instructions": self.persona}
        req = urllib.request.Request(f"{self.url}/api/sessions/{self.session_id}/chat/stream",
                                     data=json.dumps(payload).encode(), method="POST", headers=self._hdr())
        reply, deltas, event = None, [], None
        with urllib.request.urlopen(req, timeout=150) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if event == "assistant.delta":
                        deltas.append(d.get("delta", ""))
                    elif event == "assistant.completed":
                        reply = d.get("content", "")
                    elif event == "error":
                        raise RuntimeError(str(d.get("error") or d.get("message") or "hermes error")[:200])
                elif not line:
                    event = None
        self.last_used = now
        return (reply if reply is not None else "".join(deltas)).strip()


class Music:
    """Local music intents against music-api (CONTRACT.md routes under /x/echo)."""

    def __init__(self, url, auth_file):
        self.url = url.rstrip("/")
        self.auth_file = auth_file

    def _hdr(self):
        try:
            a = json.loads(Path(self.auth_file).read_text())
        except (OSError, ValueError):
            a = {}
        return {"X-Music-User": a.get("u", ""), "X-Music-Token": a.get("t", ""), "X-Music-Salt": a.get("s", ""),
                "Content-Type": "application/json"}

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        try:
            _, raw = http(method, f"{self.url}{path}", data, self._hdr(), timeout=8)
            return json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return {"error": "echo_down"}
            raise

    def handle(self, intent):
        if intent == "play":
            r = self.call("POST", "/x/echo/play")
            return "The speaker is not connected." if r.get("error") else "Playing."
        if intent == "pause":
            self.call("POST", "/x/echo/pause")
            return "Paused."
        if intent == "next":
            r = self.call("POST", "/x/echo/next")
            return "The speaker is not connected." if r.get("error") else "Next song."
        if intent == "status":
            st = self.call("GET", "/x/echo/status").get("player", {})
            return {"playing": "Music is playing.", "paused": "Music is paused."}.get(st.get("state"), "Nothing is playing.")
        if intent in ("volume_up", "volume_down"):
            vol = int(self.call("GET", "/x/echo/status").get("player", {}).get("volume", 50))
            vol = max(0, min(100, vol + (10 if intent == "volume_up" else -10)))
            self.call("POST", "/x/echo/volume", {"volume": vol})
            return f"Volume {vol}."
        return None


def music_intent(text):
    t = re.sub(r"[^a-z0-9' ]", " ", text.lower())
    t = " ".join(t.split())
    pats = [
        ("status", r"(what'?s|what is|what song is) (playing|on)( right now| now)?|what am i listening to|what song is this"),
        ("volume_up", r"(turn |make it |volume )?(the )?(volume )?(up|louder)( a (little|bit))?|volume up|turn (it|the music|the volume) up|louder"),
        ("volume_down", r"volume down|turn (it|the music|the volume) down|(turn |make it )?(the )?(volume )?(down|quieter|softer)( a (little|bit))?|quieter"),
        ("pause", r"(pause[ds]?|stop|mute)( the)?( music| song| playback)?|pause it"),
        ("next", r"(next|skip(ped)?)( the)?( song| track)?|next song|skip it"),
        ("play", r"(play(s|ed)?|resume|start)( the)?( music| song| playback)?|play it"),
    ]
    for name, p in pats:
        if re.fullmatch(f"(please )?({p})( please)?", t):
            return name
    return None


# The recognizer spells "Mira" many ways (Mira, Meera, Beerah, Miura...). This accepts up to two letters, an i/ee/y vowel, one or
# more r, then a/ah (Moonshine says Beera, Viera, Vera on short clips), and so rejects mirror, Maria, Mara, miracle, Miranda.
WAKE_WORD = r"[a-z]{0,2}(?:i|ee|ea|e|y|ie|iu)r+(?:a|ah|ar|uh)h?"
WAKE_OK = re.compile(rf"^(?:(?:hey|hi|ok|okay)\W+)?(?:{WAKE_WORD})\b", re.I)
WAKE_PREFIX = re.compile(rf"^\s*(?:(?:hey|hi|ok|okay)\W+)?(?:{WAKE_WORD})\b[\s,.!?:;-]*", re.I)


def has_wake(text):
    """True when the transcript opens with the wake word (the spotter's own verdict is not trusted alone)."""
    return bool(WAKE_OK.match(text.strip()))


def strip_wake(text):
    # The brain does not need the wake word; drop it from the front of the request.
    return WAKE_PREFIX.sub("", text, count=1).strip()


def clean_for_speech(text):
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"(?m)^\s{0,3}(#{1,6}|[-*+>]|\d+[.)])\s+", "", t)
    t = re.sub(r"[*_~#|]+", "", t)
    return " ".join(t.split())


def speech_chunks(text, limit=280, max_chunks=3):
    """Whole sentences up to ~280 chars each, at most three: long replies are cut, not read for a minute."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        if len(s) > limit:
            s = s[:limit].rsplit(" ", 1)[0]
        if cur and len(cur) + 1 + len(s) > limit:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return chunks[:max_chunks]


# --------------------------------------------------------------------------------------- the loop

def build_kws(args):
    """The keyword spotter exactly as the listener runs it (tests build theirs through this too)."""
    import sherpa_onnx
    kd = Path(args.kws_dir)
    return sherpa_onnx.KeywordSpotter(
        tokens=str(kd / "tokens.txt"), encoder=str(kd / "encoder.onnx"), decoder=str(kd / "decoder.onnx"),
        joiner=str(kd / "joiner.onnx"), keywords_file=str(kd / args.keywords),
        num_threads=args.kws_threads, provider="cpu",
        keywords_threshold=args.kws_threshold, keywords_score=args.kws_score)


def wav_envelope(wav_bytes, step=0.1):
    """[(start_s, level)] of a WAV's loudness every `step` seconds, scaled like the mic level (rms * 8, capped at 1)
    so the wall's rings read a spoken reply the same way they read a voice. Empty when the WAV is unreadable."""
    try:
        x = read_wav_float_bytes(wav_bytes)
    except (wave.Error, ValueError, EOFError):
        return []
    n = int(step * RATE)
    return [(i / RATE, min(1.0, float(np.sqrt(np.mean(x[i:i + n] ** 2))) * 8)) for i in range(0, len(x), n)]


class Listener:
    def __init__(self, args, source):
        import sherpa_onnx
        self.a, self.src = args, source
        self.kws = build_kws(args)
        vc = sherpa_onnx.VadModelConfig()
        vc.silero_vad.model = args.vad_model
        vc.silero_vad.threshold = args.vad_threshold
        vc.silero_vad.min_silence_duration = args.end_silence
        vc.silero_vad.min_speech_duration = 0.15
        vc.silero_vad.max_speech_duration = 30.0
        vc.silero_vad.window_size = 512
        vc.sample_rate = RATE
        vc.num_threads = 1
        self._sherpa, self._vc = sherpa_onnx, vc
        self.vad = sherpa_onnx.VoiceActivityDetector(vc, buffer_size_in_seconds=30)
        persona = Path(args.identity).read_text() if Path(args.identity).exists() else "You are Mira."
        self.speech = Speech(args.speech_url, args.speech_token_file, args.voice)
        self.hermes = Hermes(args.hermes_url, args.hermes_key_file, persona)
        self.music = Music(args.music_url, args.music_auth_file)
        self.rep = VoiceReporter(args.voice_url, args.voice_token_file, error_hold=args.error_hold)
        self.agc = MicAGC(**{k: v for k, v in (("target", args.agc_target), ("max_gain", args.agc_max_gain),
                                               ("rest", args.agc_rest), ("gate", args.agc_gate)) if v is not None}) \
            if args.agc else None
        self.followup_until = 0.0
        self.wakes = 0
        self.events = []  # (kind, detail) trail for tests and --verbose
        self._heard = ""
        self._reply = ""

    # -- helpers
    def heard_texts(self):
        return [d for k, d in self.events if k == "heard"]

    def _mark(self, kind, detail=""):
        self.events.append((kind, detail))
        log(f"{kind} {detail}".strip())

    def _audio(self, *cmd):
        try:
            return subprocess.run([self.a.mira_audio, *cmd], timeout=120, capture_output=True).returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"mira-audio {cmd[0]} failed: {type(e).__name__}")
            return 1

    def _read(self):
        """One mic chunk through the AGC, with the wall's level taken from it (silence reads as 0)."""
        c = self.src.read()
        if c is None:
            return None
        if self.agc is not None:
            c = self.agc.process(c)
            self.rep.set_level(self.agc.level)
        else:
            self.rep.set_level(min(1.0, float(np.sqrt(np.mean(c ** 2))) * 8))
        return c

    def _reset_vad(self):
        self.vad = self._sherpa.VoiceActivityDetector(self._vc, buffer_size_in_seconds=30)

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
        chunks = speech_chunks(clean_for_speech(text))
        if not chunks:
            return
        q = queue.Queue()
        err = []

        def synth():
            try:
                for ch in chunks:
                    q.put(self.speech.tts(ch))
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
        env = wav_envelope(wav)
        done = threading.Event()

        def follow():
            t0 = time.monotonic() + self.a.level_lag  # pw-play needs a moment to start
            for at, lvl in env:
                wait = t0 + at - time.monotonic()
                if wait > 0 and done.wait(wait):
                    return
                self.rep.set_level(lvl)
            self.rep.set_level(0.0)

        th = threading.Thread(target=follow, daemon=True)
        th.start()
        try:
            self._audio("say", path)
        finally:
            done.set()
            th.join(timeout=1)
            self.rep.set_level(0.0)

    def fail_offline(self, why):
        self._mark("offline", why)
        self.rep.update("error", self._heard, "Speech is offline")
        if os.path.exists(self.a.chime):
            self._audio("say", self.a.chime)
        self.rep.update("idle")

    def handle_clip(self, clip, verify=False):
        self._heard = ""
        self._reply = ""
        self.rep.update("thinking")
        try:
            text = self.speech.stt(pcm16_wav_bytes(clip))
        except SpeechOffline as e:
            return self.fail_offline(f"stt: {e}")
        if verify and not (text.strip() and has_wake(text)):
            # The tiny spotter fires on look-alikes (mirror, miracle, Miranda); the transcript decides. An empty
            # transcript is a reject too: the low threshold lets room noise through, and accepting nothing would
            # open the 8 s follow-up window, where anything said in the room goes to Hermes.
            self._mark("wake_rejected", f"heard {text.strip()[:60]!r}" if text.strip() else "heard nothing")
            self.rep.update("idle")
            return
        if verify:
            # Only the first words of the transcript: the journal needs the verdict, not the whole request.
            self._mark("wake_accepted", f"opens {' '.join(text.split()[:3])!r}")
        text = strip_wake(text)
        norm = " ".join(re.sub(r"[^a-z0-9' ]", "", text.lower()).split())
        if not norm or norm in HALLUCINATIONS:
            # Just "Mira" (or noise): keep listening for a few seconds without needing the wake word.
            self._mark("empty", norm)
            self.rep.update("idle")
            self.followup_until = self.src.clock() + self.a.followup_s
            return
        self._heard = text
        self.rep.update("thinking", text)
        self._mark("heard", text)
        intent = music_intent(text)
        try:
            if intent:
                self._mark("music", intent)
                try:
                    self._reply = self.music.handle(intent) or "Done."
                except Exception as e:
                    log(f"music-api failed: {type(e).__name__}")
                    self._reply = "I could not reach the music player."
            else:
                try:
                    self._reply = self.hermes.ask(text, self.a.session_ttl, self.src.clock()) or "I have nothing to add."
                except Exception as e:
                    log(f"hermes failed: {type(e).__name__}: {str(e)[:100]}")
                    self._reply = "I cannot reach Hermes right now."
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
        pre = collections.deque(maxlen=15)  # last 1.5 s: follow-up starts at the first syllable; wake clips keep the word for verification
        fvad_on = False
        try:
            while True:
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
                        clip = self.capture(np.concatenate(list(pre)[-5:]), wait_speech_s=1.0, max_s=self.a.max_s)
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
                    clip = self.capture(np.concatenate(before[-3:]), wait_speech_s=self.a.wait_speech,
                                        max_s=self.a.max_s)
                    stream = self.kws.create_stream()
                    pre.clear()
                    if clip is None:
                        self._mark("no_speech")
                        self.rep.update("idle")
                        continue
                    head = np.concatenate(before[:-3]) if len(before) > 3 else clip[:0]
                    self.handle_clip(np.concatenate([head, clip]), verify=True)
            self._mark("eof")
        finally:
            self.rep.update("idle", self._heard, self._reply)
            self.rep.close()
            self.src.close()


def build_parser():
    d = DEFAULTS
    p = argparse.ArgumentParser(description="Mira's wall listener")
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


def main(argv=None):
    args = build_parser().parse_args(argv)
    src = WavSource(args.wav) if args.wav else PwSource()
    lis = Listener(args, src)
    agc = lis.agc
    log(f"mira-listen up ({'wav' if args.wav else 'mic'}), kws threshold {args.kws_threshold} score {args.kws_score}, "
        + (f"agc target {agc.target} max {agc.max_gain}x rest {agc.rest}x gate {agc.gate}" if agc else "agc off"))
    try:
        lis.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
