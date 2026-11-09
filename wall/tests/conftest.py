"""Fakes (Hermes, the Agent OS voice hook, music-api, mira-audio) and the real mini speech server.

The speech server is the real one: set MIRA_SPEECH_URL (default http://100.77.98.89:8765) and
MIRA_SPEECH_TOKEN_FILE (default ~/.config/mira/speech.token). Tests that need it skip when it is down.
"""
import json
import os
import stat
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

WALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WALL))
import mira_listen as ml  # noqa: E402

RATE = ml.RATE
SPEECH_URL = os.environ.get("MIRA_SPEECH_URL", "http://100.77.98.89:8765")
TOKEN_FILE = os.environ.get("MIRA_SPEECH_TOKEN_FILE", str(Path.home() / ".config/mira/speech.token"))
KWS_DIR = Path(os.environ.get("MIRA_KWS_DIR", WALL.parent / "voice" / "models" / "kws"))


class FakeServer:
    """Records every request; `routes` maps (METHOD, path) to a callable(body, headers) -> (code, bytes, ctype)."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.calls.append({"method": self.command, "path": self.path, "body": body,
                                    "headers": {k.lower(): v for k, v in self.headers.items()}})
                fn = outer.routes.get((self.command, self.path.split("?")[0]))
                code, data, ctype = fn(body, self.headers) if fn else (404, b"{}", "application/json")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                if "event-stream" not in ctype:
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def json_bodies(self, path):
        return [json.loads(c["body"]) for c in self.calls if c["path"] == path and c["body"]]

    def stop(self):
        self.httpd.shutdown()


def sse(events):
    out = b""
    for name, data in events:
        out += f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
    return out


@pytest.fixture
def hermes():
    s = FakeServer()
    s.reply = "You have a physics review at three and nothing else today."

    def create(body, h):
        return 200, json.dumps({"id": "sess_test", "title": "x"}).encode(), "application/json"

    def stream(body, h):
        ev = [("run.started", {"session_id": "sess_test", "run_id": "run_1"}),
              ("assistant.delta", {"delta": s.reply[:5]}),
              ("assistant.completed", {"content": s.reply, "completed": True}),
              ("run.completed", {}), ("done", {})]
        return 200, sse(ev), "text/event-stream"

    s.routes[("POST", "/api/sessions")] = create
    s.routes[("POST", "/api/sessions/sess_test/chat/stream")] = stream
    yield s
    s.stop()


@pytest.fixture
def hook():
    s = FakeServer()
    s.routes[("POST", "/api/hooks/voice")] = lambda b, h: (200, b'{"ok":true}', "application/json")
    yield s
    s.stop()


@pytest.fixture
def music():
    s = FakeServer()
    ok = lambda b, h: (200, json.dumps({"player": {"state": "playing", "volume": 50}}).encode(), "application/json")
    for p in ("play", "pause", "next", "volume", "status"):
        s.routes[("POST", f"/x/echo/{p}")] = ok
    s.routes[("GET", "/x/echo/status")] = ok
    yield s
    s.stop()


@pytest.fixture
def secrets(tmp_path):
    """Token files for the fakes; the speech token is the real one, read from TOKEN_FILE."""
    (tmp_path / "voice_token").write_text("voice-test-token\n")
    (tmp_path / "hermes_key").write_text("hermes-test-key\n")
    (tmp_path / "music_auth.json").write_text(json.dumps({"u": "arya", "t": "tok123", "s": "salt456"}))
    return tmp_path


@pytest.fixture
def fake_audio(tmp_path):
    """A mira-audio stand-in that logs its calls and keeps a copy of each WAV it is asked to say."""
    log = tmp_path / "audio.log"
    keep = tmp_path / "said"
    keep.mkdir()
    script = tmp_path / "mira-audio"
    script.write_text(f"""#!/bin/sh
echo "$1" >> "{log}"
if [ "$1" = say ]; then
  n=$(ls "{keep}" | wc -l | tr -d ' ')
  cp "$2" "{keep}/say_$n.wav"
fi
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    class A:
        bin = str(script)

        def calls(self):
            return log.read_text().split() if log.exists() else []

        def said(self):
            return sorted(keep.glob("say_*.wav"))

    return A()


def _speech_up():
    try:
        with urllib.request.urlopen(f"{SPEECH_URL}/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def speech():
    if not _speech_up() or not Path(TOKEN_FILE).exists():
        pytest.skip(f"speech server {SPEECH_URL} or token file not available")
    return ml.Speech(SPEECH_URL, TOKEN_FILE)


@pytest.fixture(scope="session")
def say_wav(speech, tmp_path_factory):
    """Text -> 16 kHz float samples, spoken by the mini's own TTS (the voice Mira will answer in)."""
    cache = {}

    def make(text):
        if text not in cache:
            wav = speech.tts(text)
            p = tmp_path_factory.mktemp("tts") / "x.wav"
            p.write_bytes(wav)
            cache[text] = ml.read_wav_float(p)
        return cache[text]

    return make


def kws_fires(x, threshold=0.15):
    """Whether the wake-word spotter alone fires on x (same model and settings as the listener)."""
    import sherpa_onnx
    k = sherpa_onnx.KeywordSpotter(tokens=str(KWS_DIR / "tokens.txt"), encoder=str(KWS_DIR / "encoder.onnx"),
                                   decoder=str(KWS_DIR / "decoder.onnx"), joiner=str(KWS_DIR / "joiner.onnx"),
                                   keywords_file=str(KWS_DIR / "keywords.txt"), num_threads=1,
                                   keywords_threshold=threshold)
    s = k.create_stream()
    x = compose(2.0, x, 1.0)  # the same lead-in the test wavs use: the spotter is causal
    for i in range(0, len(x), 1600):
        s.accept_waveform(RATE, x[i:i + 1600])
        while k.is_ready(s):
            k.decode_stream(s)
        if k.get_result(s):
            return True
    return False


@pytest.fixture(scope="session")
def say_wake(say_wav, speech, tmp_path_factory):
    """Like say_wav, for utterances that start with "Mira". Piper adds random variation to every
    synthesis and the 3 MB spotter misses a share of them (see wall/README.md for the measured rate),
    so draw up to 8 renderings and take the first one the spotter hears. Tests then check the
    pipeline, not the spotter's hit rate."""
    def make(text):
        for _ in range(8):
            wav = speech.tts(text)
            p = tmp_path_factory.mktemp("wake") / "x.wav"
            p.write_bytes(wav)
            x = ml.read_wav_float(p)
            if kws_fires(x):
                return x
        pytest.fail(f"spotter never fired on 8 renderings of {text!r}")
    return make


def silence(seconds, noise=0.003, seed=1):
    """A quiet room: low broadband noise rather than digital zero, which the VAD never sees in real life."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    return np.convolve(rng.normal(0, noise, n), np.ones(4) / 4, mode="same").astype(np.float32)


def compose(*parts):
    return np.concatenate([p if isinstance(p, np.ndarray) else silence(p) for p in parts]).astype(np.float32)


def write_wav(path, x):
    Path(path).write_bytes(ml.pcm16_wav_bytes(x))
    return str(path)


def run_listener(wav, secrets, fake_audio, hermes=None, hook=None, music=None, speech_url=None, extra=()):
    argv = ["--wav", str(wav), "--kws-dir", str(KWS_DIR),
            "--speech-url", speech_url or SPEECH_URL, "--speech-token-file", TOKEN_FILE,
            "--hermes-key-file", str(secrets / "hermes_key"),
            "--voice-token-file", str(secrets / "voice_token"),
            "--music-auth-file", str(secrets / "music_auth.json"),
            "--mira-audio", fake_audio.bin, "--error-hold", "0.3"]
    if hermes:
        argv += ["--hermes-url", hermes.url]
    if hook:
        argv += ["--voice-url", hook.url + "/api/hooks/voice"]
    if music:
        argv += ["--music-url", music.url]
    argv += list(extra)
    args = ml.build_parser().parse_args(argv)
    lis = ml.Listener(args, ml.WavSource(args.wav))
    lis.run()
    return lis


def hook_states(hook):
    out = []
    for b in hook.json_bodies("/api/hooks/voice"):
        if not out or out[-1] != b["state"]:
            out.append(b["state"])
    return out
