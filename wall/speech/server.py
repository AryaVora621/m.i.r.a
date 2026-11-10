#!/usr/bin/env python3
"""mira-speech: STT and TTS over HTTP for the wall, run on the Mac mini.

Standard-library ThreadingHTTPServer plus sherpa-onnx (no torch: it has no Intel-Mac wheels).
Models load once at start and stay warm; each model has a lock so requests queue instead of
oversubscribing the mini's four cores. Request text is never logged.

Config comes from env (see CONFIG below) and a bearer token file, never from the repo.
"""

import hmac
import io
import json
import os
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

HOME = Path.home()
BASE = Path(os.environ.get("MIRA_SPEECH_DIR", HOME / "Library/Application Support/mira-speech"))
MODELS = BASE / "models"
TOKEN_FILE = Path(os.environ.get("MIRA_SPEECH_TOKEN_FILE", HOME / ".config/mira/speech.token"))
# Never 0.0.0.0: the bearer token travels over plain HTTP, so the server answers only on loopback and on
# the mini's Tailscale address (encrypted and authenticated by WireGuard). Comma-separated.
BIND = [a.strip() for a in os.environ.get("MIRA_SPEECH_BIND", "127.0.0.1,100.77.98.89").split(",") if a.strip()]
PORT = int(os.environ.get("MIRA_SPEECH_PORT", "8765"))
STT_KIND = os.environ.get("MIRA_STT", "sherpa-moonshine:sherpa-onnx-moonshine-base-en-int8")
TTS_DIR = os.environ.get("MIRA_TTS_DIR", "vits-piper-en_US-amy-medium")
TTS_VOICE = os.environ.get("MIRA_TTS_VOICE", "amy")
THREADS = int(os.environ.get("MIRA_THREADS", "4"))
TTS_THREADS = int(os.environ.get("MIRA_TTS_THREADS", str(THREADS)))
TARGET_PEAK = float(os.environ.get("MIRA_TTS_PEAK", "0.89"))
# faster-whisper only. Moonshine and the sherpa backends cannot be biased, so they ignore these.
# The default vocabulary is what the owner says to the wall that a small model otherwise mishears
# ("3D printers" came back as "pen"); override or empty it from the LaunchAgent env.
DEFAULT_VOCAB = ("Mira, Hermes, 3D printers, Sovol SV08, Onshape, OrcaSlicer, slicer, vault, timer, "
                 "calendar, YouTube Music")
# Measured on the owner's room takes (tests/stt_eval.py): a vocabulary prompt cut tiny.en's word error from 0.51 to
# 0.07 and also fixed "Mira"; hotwords alone helped less. Set MIRA_STT_PROMPT="" to switch it off.
STT_PROMPT = os.environ.get("MIRA_STT_PROMPT", f"Vocabulary: {DEFAULT_VOCAB}.")
STT_HOTWORDS = os.environ.get("MIRA_STT_HOTWORDS", "")
STT_COMPUTE = os.environ.get("MIRA_STT_COMPUTE", "int8")
STT_BEAM = int(os.environ.get("MIRA_STT_BEAM", "1"))

MAX_STT_BYTES = 30 * 16000 * 2 + 4096
MAX_TTS_CHARS = 600
STT_RATE = 16000

START = time.time()


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def read_wav16k(data: bytes) -> np.ndarray:
    """Strict: 16 kHz mono s16 only. Returns float32 in [-1, 1]."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getframerate() != STT_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("need 16 kHz mono 16-bit WAV")
        n = w.getnframes()
        if n > 30 * STT_RATE:
            raise ValueError("clip longer than 30 s")
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def write_wav(samples: np.ndarray, rate: int) -> bytes:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def normalise(samples: np.ndarray) -> np.ndarray:
    """Peak-normalise so every reply plays at the same level whatever the voice or text."""
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < 1e-4:
        return samples
    return samples * (TARGET_PEAK / peak)


def _opt(value, env):
    """An explicit argument wins; None falls back to the env value. "" is a real value (turns it off)."""
    return env if value is None else value


class Stt:
    def __init__(self, kind: str, prompt: str | None = None, hotwords: str | None = None,
                 compute_type: str | None = None, beam_size: int | None = None):
        self.kind = kind
        self.prompt = _opt(prompt, STT_PROMPT)
        self.hotwords = _opt(hotwords, STT_HOTWORDS)
        self.beam_size = _opt(beam_size, STT_BEAM)
        self.lock = threading.Lock()
        backend, _, name = kind.partition(":")
        # "-fp32" variants skip the int8 files: an AVX-only CPU has no int8 dot-product units.
        fp32 = backend.endswith("-fp32")
        backend = backend.removesuffix("-fp32")
        self.name = name
        if backend == "faster-whisper":
            from faster_whisper import WhisperModel
            self._fw = WhisperModel(name, device="cpu", compute_type=_opt(compute_type, STT_COMPUTE),
                                    cpu_threads=THREADS,
                                    download_root=str(MODELS / "fw"))
            self._rec = None
        else:
            import sherpa_onnx
            d = MODELS / name
            if backend == "sherpa-whisper":
                files = sorted(p.name for p in d.iterdir())
                pre = _prefix(files)
                # Prefer the int8 files (smaller, faster on a CPU with only AVX).
                def pick(kind):
                    order = (f"{pre}-{kind}.onnx", f"{pre}-{kind}.int8.onnx") if fp32 else \
                            (f"{pre}-{kind}.int8.onnx", f"{pre}-{kind}.onnx")
                    for suffix in order:
                        if suffix in files:
                            return str(d / suffix)
                    raise FileNotFoundError(kind)
                self._rec = sherpa_onnx.OfflineRecognizer.from_whisper(
                    encoder=pick("encoder"), decoder=pick("decoder"),
                    tokens=str(d / f"{pre}-tokens.txt"),
                    language="en", task="transcribe", num_threads=THREADS, provider="cpu")
            elif backend == "sherpa-moonshine":
                files = sorted(p.name for p in d.iterdir())
                f = lambda part: str(d / next(x for x in files if x.startswith(part) and x.endswith(".onnx")))
                self._rec = sherpa_onnx.OfflineRecognizer.from_moonshine(
                    preprocessor=f("preprocess"), encoder=f("encode"),
                    uncached_decoder=f("uncached_decode"), cached_decoder=f("cached_decode"),
                    tokens=str(d / "tokens.txt"), num_threads=THREADS, provider="cpu")
            elif backend == "sherpa-moonshine-v2":
                # The 2026 Moonshine packages: one encoder and one merged decoder in ORT format.
                self._rec = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
                    encoder=str(next(d.glob("encoder_model*"))), decoder=str(next(d.glob("decoder_model*"))),
                    tokens=str(d / "tokens.txt"), num_threads=THREADS, provider="cpu")
            elif backend in ("sherpa-nemo-transducer", "sherpa-transducer"):
                # NeMo (Parakeet) or k2 zipformer transducers: encoder/decoder/joiner(.int8).onnx + tokens.txt.
                files = sorted(p.name for p in d.iterdir())

                def pick(part):
                    order = (f"{part}.onnx", f"{part}.int8.onnx") if fp32 else (f"{part}.int8.onnx", f"{part}.onnx")
                    for suffix in order:
                        if suffix in files:
                            return str(d / suffix)
                    raise FileNotFoundError(part)
                self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=pick("encoder"), decoder=pick("decoder"), joiner=pick("joiner"),
                    tokens=str(d / "tokens.txt"), num_threads=THREADS, provider="cpu",
                    model_type="nemo_transducer" if backend == "sherpa-nemo-transducer" else "transducer")
            else:
                raise SystemExit(f"unknown STT backend {backend}")
            self._fw = None

    def transcribe(self, samples: np.ndarray) -> str:
        with self.lock:
            if self._fw is not None:
                # An empty string must reach faster-whisper as None: "" would still be tokenised as a prompt.
                segs, _ = self._fw.transcribe(samples, beam_size=self.beam_size, language="en",
                                              condition_on_previous_text=False,
                                              initial_prompt=self.prompt or None,
                                              hotwords=self.hotwords or None)
                return " ".join(s.text.strip() for s in segs).strip()
            text = self._decode(samples)
            if not text and len(samples) > STT_RATE and float(np.sqrt(np.mean(samples ** 2))) > 0.01:
                # Moonshine sometimes returns nothing for a clip that plainly has speech in it, and a
                # different start offset fixes it. Costs one extra decode only in that case.
                text = self._decode(samples[int(0.2 * STT_RATE):])
            return text

    def _decode(self, samples):
        s = self._rec.create_stream()
        s.accept_waveform(STT_RATE, samples)
        self._rec.decode_stream(s)
        return s.result.text.strip()


def _prefix(files):
    for f in files:
        if f.endswith("-encoder.onnx") or f.endswith("-encoder.int8.onnx"):
            return f.split("-encoder")[0]
    raise FileNotFoundError("no whisper encoder in model dir")


class Tts:
    """Kokoro or a Piper (VITS) voice, picked by the model directory name."""

    def __init__(self, model_dir: str, voice: str):
        import sherpa_onnx
        d = MODELS / model_dir
        files = sorted(p.name for p in d.iterdir())
        model = next(f for f in files if f.endswith(".onnx") and not f.startswith("voices"))
        if "kokoro" in model_dir:
            lex = ",".join(str(d / f) for f in files if f.startswith("lexicon"))
            kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(d / model), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
                data_dir=str(d / "espeak-ng-data"), lexicon=lex,
                dict_dir=str(d / "dict") if (d / "dict").is_dir() else "")
            mcfg = sherpa_onnx.OfflineTtsModelConfig(kokoro=kokoro, num_threads=TTS_THREADS, provider="cpu")
            self.voices = _voice_names(d)
        else:
            vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(d / model), tokens=str(d / "tokens.txt"), data_dir=str(d / "espeak-ng-data"),
                length_scale=float(os.environ.get("MIRA_TTS_LENGTH_SCALE", "1.0")))
            mcfg = sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=TTS_THREADS, provider="cpu")
            self.voices = {}
        self._tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=mcfg))
        self.name = model_dir
        self.rate = self._tts.sample_rate
        self.lock = threading.Lock()
        self.default_voice = voice

    def speak(self, text: str, voice: str | None) -> tuple[np.ndarray, int]:
        sid = self.voices.get(voice or self.default_voice, self.voices.get(self.default_voice, 0))
        with self.lock:
            audio = self._tts.generate(text, sid=sid, speed=1.0)
        if not audio.samples:
            raise RuntimeError("tts produced no audio")
        return normalise(np.array(audio.samples, dtype=np.float32)), audio.sample_rate


def _voice_names(d: Path) -> dict:
    """Kokoro voice ids in the order the voices.bin was built (the model README lists them)."""
    order_v1 = ["af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole",
                "af_nova", "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric",
                "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
                "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable",
                "bm_george", "bm_lewis"]
    order_v019 = ["af", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael",
                  "bf_emma", "bf_isabella", "bm_george", "bm_lewis"]
    order = order_v019 if "v0_19" in d.name else order_v1
    return {n: i for i, n in enumerate(order)}


class State:
    stt: Stt
    tts: Tts
    token: bytes = b""


def load_token() -> bytes:
    try:
        return TOKEN_FILE.read_text().strip().encode()
    except OSError:
        return b""


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # The stock HTTPServer.server_bind reverse-resolves the address (getfqdn), which stalls for
        # tens of seconds on a Tailscale address. Nothing here needs the name.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server_version = "mira-speech"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # request lines only, never bodies
        log(f"{self.client_address[0]} {fmt % args}")

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def _authed(self) -> bool:
        tok = State.token
        got = self.headers.get("Authorization", "")
        if not tok or not got.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(got[7:].strip().encode(), tok)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._json(200, {"ok": True, "stt": State.stt.kind, "tts": State.tts.name,
                             "voice": State.tts.default_voice,
                             "uptime_s": round(time.time() - START, 1)})
        else:
            self._json(404, {"error": "not_found"})

    def _body(self, limit):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0 or n > limit:
            return None
        return self.rfile.read(n)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/stt", "/tts"):
            return self._json(404, {"error": "not_found"})
        if not self._authed():
            # Drain nothing: closing avoids keeping an unauthenticated body in memory.
            self.close_connection = True
            return self._json(401, {"error": "unauthorized"})
        try:
            if path == "/stt":
                data = self._body(MAX_STT_BYTES)
                if data is None:
                    return self._json(413, {"error": "bad_or_oversized_body"})
                try:
                    samples = read_wav16k(data)
                except (ValueError, wave.Error, EOFError) as e:
                    return self._json(400, {"error": str(e)})
                t0 = time.perf_counter()
                text = State.stt.transcribe(samples)
                ms = round((time.perf_counter() - t0) * 1000)
                log(f"stt {len(samples)/STT_RATE:.1f}s audio -> {len(text)} chars in {ms} ms")
                return self._json(200, {"text": text, "ms": ms})
            data = self._body(4096)
            if data is None:
                return self._json(413, {"error": "bad_or_oversized_body"})
            try:
                req = json.loads(data)
                text = str(req["text"]).strip()
                voice = req.get("voice")
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "need JSON {text, voice?}"})
            if not text or len(text) > MAX_TTS_CHARS:
                return self._json(400, {"error": f"text must be 1..{MAX_TTS_CHARS} chars"})
            t0 = time.perf_counter()
            samples, rate = State.tts.speak(text, voice)
            ms = round((time.perf_counter() - t0) * 1000)
            dur = len(samples) / rate
            log(f"tts {len(text)} chars -> {dur:.1f}s audio in {ms} ms (rtf {ms/1000/max(dur,0.01):.2f})")
            self._send(200, write_wav(samples, rate), "audio/wav")
        except Exception as e:  # keep the server up; the message carries no request text
            log(f"error {type(e).__name__}: {str(e)[:120]}")
            self._json(500, {"error": "internal"})


def main():
    State.token = load_token()
    if not State.token:
        raise SystemExit(f"no token in {TOKEN_FILE}")
    log(f"loading stt {STT_KIND}")
    State.stt = Stt(STT_KIND)
    log(f"loading tts {TTS_DIR} voice {TTS_VOICE}")
    State.tts = Tts(TTS_DIR, TTS_VOICE)
    # Warm both models so the first real request is not the slow one.
    State.stt.transcribe(np.zeros(STT_RATE, dtype=np.float32))
    State.tts.speak("Ready.", None)
    if "0.0.0.0" in BIND or "::" in BIND:
        raise SystemExit("refusing to bind a wildcard address; set MIRA_SPEECH_BIND to specific addresses")

    def serve(addr):
        # tailscaled can come up after this LaunchAgent, so the Tailscale address may not exist yet:
        # keep retrying until the interface appears.
        warned = False
        while True:
            try:
                srv = Server((addr, PORT), Handler)
            except OSError as e:
                if not warned:
                    log(f"cannot bind {addr}:{PORT} yet ({e.strerror}); retrying")
                    warned = True
                time.sleep(5)
                continue
            srv.daemon_threads = True
            log(f"listening on {addr}:{PORT}")
            srv.serve_forever()

    threads = [threading.Thread(target=serve, args=(a,), daemon=True) for a in BIND]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
