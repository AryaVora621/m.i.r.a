"""Recording stdlib fakes for the golden harness and the characterization tests.

Nothing here touches the network beyond 127.0.0.1 ephemeral ports, plays sound, or reads real secrets.
Every fake records each request as {method, path, headers, body, t} where `headers` never carries the
Authorization value (only whether one arrived, as `has_auth`), so recordings stay free of tokens.
"""
import hashlib
import io
import json
import math
import re
import socket
import stat
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def tone_wav(seconds=0.4, rate=22050, freq=220.0):
    """A fixed small mono 16-bit WAV, generated in code so the bytes never change."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)))
    return buf.getvalue()


TTS_WAV = tone_wav()


def free_port():
    """A port that was just free and is now closed: connecting to it is refused."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RecordingServer:
    """Base: subclasses implement `respond(method, path, body, headers, index) -> (code, bytes, ctype)`.
    `index` counts earlier calls to the same (method, path-key) so replies can be scripted by call order."""

    def __init__(self):
        self.calls = []
        self._counts = {}
        self._lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                has_auth = "authorization" in hdrs
                hdrs.pop("authorization", None)
                path = self.path.split("?")[0]
                with outer._lock:
                    key = outer.count_key(self.command, path)
                    idx = outer._counts.get(key, 0)
                    outer._counts[key] = idx + 1
                    outer.calls.append({"method": self.command, "path": path, "headers": hdrs, "has_auth": has_auth,
                                        "body": body, "t": time.monotonic()})
                code, data, ctype = outer.respond(self.command, path, body, hdrs, idx)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def count_key(self, method, path):
        return (method, path)

    def respond(self, method, path, body, headers, index):
        return 404, b"{}", "application/json"

    def json_bodies(self, path):
        return [json.loads(c["body"]) for c in self.calls if c["path"] == path and c["body"]]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeSpeech(RecordingServer):
    """/stt answers `stt` in call order. An item is a str (the transcript), ("status", code) for an HTTP error,
    or ("raw", bytes) for a body that is not JSON. Past the end of the script the transcript is empty.
    /tts answers TTS_WAV, or the HTTP status in `tts_errors[call_index]`. It never sends X-Mira-Voice."""

    def __init__(self, stt=(), tts_errors=None):
        self.stt = list(stt)
        self.tts_errors = dict(tts_errors or {})
        super().__init__()

    def respond(self, method, path, body, headers, index):
        if path == "/stt":
            item = self.stt[index] if index < len(self.stt) else ""
            if isinstance(item, tuple) and item[0] == "status":
                return item[1], b'{"error":"scripted"}', "application/json"
            if isinstance(item, tuple) and item[0] == "raw":
                return 200, item[1], "application/json"
            return 200, json.dumps({"text": item}).encode(), "application/json"
        if path == "/tts":
            if index in self.tts_errors:
                return self.tts_errors[index], b'{"error":"scripted"}', "application/json"
            return 200, TTS_WAV, "audio/wav"
        return 404, b"{}", "application/json"

    def stt_sha256(self):
        return [hashlib.sha256(c["body"]).hexdigest() for c in self.calls if c["path"] == "/stt"]


def sse(events):
    return b"".join(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in events)


class FakeHermes(RecordingServer):
    """POST /api/sessions hands out sess_1, sess_2, ... POST /api/sessions/<id>/chat/stream answers the scripted
    items in call order: a str is a reply (one delta, then completed), ("error", msg) is an SSE error event,
    ("http", code) is an HTTP error, ("deltas", [..]) streams deltas with no completed event."""

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.sessions = 0
        super().__init__()

    def count_key(self, method, path):
        return (method, re.sub(r"/api/sessions/[^/]+/", "/api/sessions/<id>/", path))

    def respond(self, method, path, body, headers, index):
        if method == "POST" and path == "/api/sessions":
            with self._lock:
                self.sessions += 1
                n = self.sessions
            return 200, json.dumps({"id": f"sess_{n}", "title": "x"}).encode(), "application/json"
        if method == "POST" and re.fullmatch(r"/api/sessions/[^/]+/chat/stream", path):
            item = self.replies[index] if index < len(self.replies) else "Okay."
            if isinstance(item, tuple) and item[0] == "http":
                return item[1], b'{"error":"scripted"}', "application/json"
            ev = [("run.started", {"run_id": "run_1"})]
            if isinstance(item, tuple) and item[0] == "error":
                ev.append(("error", {"error": item[1]}))
            elif isinstance(item, tuple) and item[0] == "deltas":
                ev += [("assistant.delta", {"delta": d}) for d in item[1]]
            else:
                ev += [("assistant.delta", {"delta": item[:5]}),
                       ("assistant.completed", {"content": item, "completed": True})]
            ev += [("run.completed", {}), ("done", {})]
            return 200, sse(ev), "text/event-stream"
        return 404, b"{}", "application/json"


class FakeHook(RecordingServer):
    """The Agent OS voice hook: records JSON bodies and always says ok."""

    def respond(self, method, path, body, headers, index):
        return 200, b'{"ok":true}', "application/json"

    def posts(self):
        return [(c["t"], json.loads(c["body"])) for c in self.calls if c["method"] == "POST" and c["body"]]


class FakeMusic(RecordingServer):
    """music-api: GET /x/echo/status answers the volumes in `volumes` by call order (state playing); POSTs are ok.
    `echo_down` makes every /x/echo route answer 409."""

    def __init__(self, volumes=(50,), echo_down=False):
        self.volumes = list(volumes)
        self.echo_down = echo_down
        super().__init__()

    def respond(self, method, path, body, headers, index):
        if not path.startswith("/x/echo/"):
            return 404, b"{}", "application/json"
        if self.echo_down:
            return 409, b'{"error":"echo_down"}', "application/json"
        if path == "/x/echo/status":
            vol = self.volumes[min(index, len(self.volumes) - 1)]
            return 200, json.dumps({"player": {"state": "playing", "volume": vol}}).encode(), "application/json"
        return 200, b'{"player":{"state":"playing","volume":50}}', "application/json"


class FakeAudio:
    """A mira-audio stand-in. It logs `duck`, `say <basename> <sha256>` and `unduck` and never plays."""

    def __init__(self, directory):
        d = Path(directory)
        self.log = d / "audio.log"
        self.bin = d / "mira-audio"
        self.bin.write_text(f"""#!/bin/sh
if [ "$1" = say ]; then
  echo "say $(basename "$2") $(shasum -a 256 "$2" | cut -d' ' -f1)" >> "{self.log}"
else
  echo "$1" >> "{self.log}"
fi
exit 0
""")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IEXEC)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


def write_tokens(directory):
    """Throwaway secrets for the fakes; returns the argv fragments that point at them."""
    d = Path(directory)
    (d / "speech_token").write_text("speech-test-token\n")
    (d / "hermes_key").write_text("hermes-test-key\n")
    (d / "voice_token").write_text("voice-test-token\n")
    (d / "music_auth.json").write_text(json.dumps({"u": "arya", "t": "tok123", "s": "salt456"}))
    return {"speech": str(d / "speech_token"), "hermes": str(d / "hermes_key"),
            "voice": str(d / "voice_token"), "music": str(d / "music_auth.json")}
