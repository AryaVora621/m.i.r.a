"""Plugin server.http: the speech server's HTTP front (spec 2.9.1), provides "http".

The v1 Server and Handler, serving from the services: /health, /stt, /tts, and /ctl/v1/... which is handed to
mirakit.control.handle after a token check against the two control token files. Kept exactly from v1: no
wildcard bind and a bind retry every BIND_RETRY_S until tailscaled has brought the interface up, a
server_bind without the getfqdn reverse lookup, /health without auth, 401 that closes without draining the
body, unknown POST paths 404 before auth, request lines in the log and request text never, the data-plane
token read once at start.

FIXED: BIND_RETRY_S, STT_RATE, STT_BODY_SLACK (header and padding bytes allowed on top of the longest clip),
the control path prefix CTL_PREFIX, the 16-bit mono WAV contract.
"""
from __future__ import annotations

import hmac
import importlib
import io
import ipaddress
import json
import logging
import socketserver
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginConfig, PluginSpec

from . import expand_path
from .tts_engine import BusyError

BIND_RETRY_S = 5.0
STT_RATE = 16000
STT_BODY_SLACK = 4096
CTL_PREFIX = "/ctl/v1"
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


def read_wav16k(data: bytes, max_s: float) -> np.ndarray:
    """Strict: 16 kHz mono s16 only. Returns float32 in [-1, 1]."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getframerate() != STT_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("need 16 kHz mono 16-bit WAV")
        n = w.getnframes()
        if n > int(max_s * STT_RATE):
            raise ValueError(f"clip longer than {max_s:g} s")
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _check_bind(addr: str) -> None:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        raise ConfigError(f"bind address {addr!r} is not an IP address", "bind") from None
    if ip.is_unspecified:
        raise ConfigError("refusing to bind a wildcard address; list specific addresses", "bind")
    if not (ip.is_loopback or ip in _TAILNET):
        raise ConfigError(f"bind address {addr} must be loopback or a tailnet address (100.64.0.0/10)", "bind")


def _validate(values) -> None:
    for addr in values.get("bind", []):
        _check_bind(str(addr))


class _Server(ThreadingHTTPServer):
    log = None   # set by the plugin; the stock handle_error prints a traceback to stderr

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if self.log is not None:
            # A reset or a closed socket is routine (the 401 path closes on purpose); never print a traceback.
            level = logging.DEBUG if isinstance(exc, (ConnectionError, TimeoutError)) else logging.WARNING
            self.log.log(level, "request from %s failed: %s", client_address[0], type(exc).__name__)

    def server_bind(self):
        # The stock HTTPServer.server_bind reverse-resolves the address (getfqdn), which stalls for
        # tens of seconds on a Tailscale address. Nothing here needs the name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


def make_handler(plugin: "ServerHttpPlugin"):
    ctx = plugin.ctx

    class Handler(BaseHTTPRequestHandler):
        server_version = "mira-speech"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # request lines only, never bodies
            ctx.log.info("%s %s", self.client_address[0], fmt % args)

        _busy_pending = False

        def _note_busy_once(self):
            # The note must land before the first byte of the reply leaves: a client (or a test) that acts on
            # the reply would otherwise race a note made after the write and see the engine as idle.
            if self._busy_pending:
                self._busy_pending = False
                plugin.note_busy()

        def _send(self, code, body: bytes, ctype="application/json", headers=None):
            self._note_busy_once()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj, headers=None):
            self._send(code, json.dumps(obj).encode(), headers=headers)

        def _authed(self) -> bool:
            tok = plugin.token
            got = self.headers.get("Authorization", "")
            if not tok or not got.lower().startswith("bearer "):
                return False
            return hmac.compare_digest(got[7:].strip().encode(), tok)

        def _body(self, limit):
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0 or n > limit:
                return None
            return self.rfile.read(n)

        def _is_ctl(self, path: str) -> bool:
            return path == CTL_PREFIX or path.startswith(CTL_PREFIX + "/")

        def _ctl(self):
            control = ctx.get("control", None)
            if control is None:
                # control.http ships off and `adopt` writes it off, so a machine with tokens installed but the plugin
                # never enabled lands here. A bare not_found made `mira status` look like a routing bug.
                return self._json(404, {"error": "not_found", "message":
                                        "the control service is not running: control.http is disabled on this machine "
                                        "(run `mira enable control.http --offline`, then `mira restart`)"})
            scope = control.scope_for(self.headers.get("Authorization", ""))
            if scope is None:
                # The data-plane token is not a control token. Nothing is read from the body.
                self.close_connection = True
                return self._json(401, {"error": "unauthorized"})
            try:
                importlib.import_module("mirakit.control").handle(ctx.get("core", None), self, self.path, scope)
            except Exception as e:  # keep the server up; the message carries no request text
                ctx.log.error("control error %s: %s", type(e).__name__, str(e)[:120])
                self._json(500, {"error": "internal"})

        def do_GET(self):
            path = self.path.split("?")[0]
            if self._is_ctl(path):
                return self._ctl()
            if path == "/health":
                return self._json(200, plugin.health())
            self._json(404, {"error": "not_found"})

        def do_PUT(self):
            if self._is_ctl(self.path.split("?")[0]):
                return self._ctl()
            self._json(404, {"error": "not_found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            if self._is_ctl(path):
                return self._ctl()
            if path not in ("/stt", "/tts"):
                return self._json(404, {"error": "not_found"})
            if not self._authed():
                # Drain nothing: closing avoids keeping an unauthenticated body in memory.
                self.close_connection = True
                return self._json(401, {"error": "unauthorized"})
            self._busy_pending = True
            try:
                if path == "/stt":
                    self._stt()
                else:
                    self._tts()
            except Exception as e:  # keep the server up; the message carries no request text
                ctx.log.error("error %s: %s", type(e).__name__, str(e)[:120])
                self._json(500, {"error": "internal"})
            finally:
                self._note_busy_once()      # only when no reply was written at all (a dropped connection)

        def _stt(self):
            cfg = plugin.cfg
            data = self._body(int(cfg.max_stt_s * STT_RATE * 2) + STT_BODY_SLACK)
            if data is None:
                return self._json(413, {"error": "bad_or_oversized_body"})
            try:
                samples = read_wav16k(data, cfg.max_stt_s)
            except (ValueError, wave.Error, EOFError) as e:
                return self._json(400, {"error": str(e)})
            text, ms = ctx.get("stt").transcribe(samples)
            ctx.log.info("stt %.1fs audio -> %d chars in %d ms", len(samples) / STT_RATE, len(text), ms)
            return self._json(200, {"text": text, "ms": ms})

        def _tts(self):
            cfg = plugin.cfg
            tts = ctx.get("tts")
            hdr = {"X-Mira-Voice": tts.voice_id}
            data = self._body(cfg.tts_body_max)
            if data is None:
                return self._json(413, {"error": "bad_or_oversized_body"}, hdr)
            try:
                req = json.loads(data)
                text = str(req["text"]).strip()
                voice = req.get("voice")
                voice = "" if voice is None else str(voice)
                speed, peak = req.get("speed"), req.get("peak")
            except (ValueError, KeyError, TypeError, AttributeError):
                return self._json(400, {"error": "need JSON {text, voice?}"}, hdr)
            for name, val in (("speed", speed), ("peak", peak)):
                if val is not None and (isinstance(val, bool) or not isinstance(val, (int, float))):
                    return self._json(400, {"error": f"{name} must be a number"}, hdr)
            if not text or len(text) > cfg.max_tts_chars:
                return self._json(400, {"error": f"text must be 1..{cfg.max_tts_chars} chars"}, hdr)
            t0 = time.perf_counter()
            try:
                wav, vid = tts.synth(text, voice, float(speed or 0.0), float(peak or 0.0))
            except ConfigError as e:
                body: dict[str, Any] = {"error": e.code, "message": e.message}
                if e.field:
                    body["field"] = e.field
                if e.code == "unknown_voice":
                    body["effective"] = tts.voice_id
                    body["near"] = list(getattr(e, "near", []))
                return self._json(400, body, hdr)
            except BusyError:
                return self._json(429, {"error": "busy", "message": "A voice preview is already being rendered."}, hdr)
            ms = round((time.perf_counter() - t0) * 1000)
            with wave.open(io.BytesIO(wav), "rb") as w:
                dur = w.getnframes() / w.getframerate()
            ctx.log.info("tts %d chars -> %.1fs audio in %d ms (rtf %.2f)", len(text), dur, ms,
                         ms / 1000 / max(dur, 0.01))
            self._send(200, wav, "audio/wav", {"X-Mira-Voice": vid})

    return Handler


class ServerHttpPlugin(PluginBase):
    def __init__(self, ctx, cfg: PluginConfig) -> None:
        super().__init__(ctx, cfg)
        self.token = b""
        self.servers: list[_Server] = []
        self.addresses: list[tuple[str, int]] = []
        self._threads: list[threading.Thread] = []
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._started = 0.0

    # ---- service surface -----------------------------------------------------------------

    def health(self) -> dict:
        stt, tts = self.ctx.get("stt", None), self.ctx.get("tts", None)
        voice_id = getattr(tts, "voice_id", "")
        alias = voice_id.partition("#")[2] or voice_id
        try:
            alias = self.ctx.get("voices").resolve(voice_id)["alias"]
        except Exception:
            pass
        engine, revision = "plugins", 0
        core = self.ctx.get("core", None)
        try:
            status = core.status()
            engine, revision = status.get("engine", engine), status.get("revision", revision)
        except Exception:
            pass
        return {"ok": stt is not None and tts is not None, "stt": getattr(stt, "kind", ""),
                "tts": getattr(tts, "model_dir", ""), "voice": alias,
                "uptime_s": round(time.time() - self._started, 1), "engine": engine, "revision": revision,
                "voice_id": voice_id}

    def note_busy(self) -> None:
        core = self.ctx.get("core", None)
        fn = getattr(core, "note_busy", None)
        if fn is not None:
            fn()

    # ---- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        cfg = self.cfg
        for addr in cfg.bind:
            _check_bind(str(addr))
        path = expand_path(cfg.token_file, self.ctx.paths)
        try:
            self.token = path.read_text(encoding="utf-8").strip().encode()
        except OSError:
            self.token = b""
        if not self.token:
            raise RuntimeError(f"no token in {path}")
        self._started = time.time()
        self._stop_evt.clear()
        handler = make_handler(self)
        for addr in cfg.bind:
            self._open(str(addr), handler)
        self.ctx.provide("http", self)

    def _open(self, addr: str, handler) -> None:
        port = int(self.cfg.port)
        try:
            srv = _Server((addr, port), handler)
        except OSError as e:
            # tailscaled can come up after this process, so the Tailscale address may not exist yet.
            self.ctx.log.info("cannot bind %s:%s yet (%s); retrying", addr, port, e.strerror)
            t = threading.Thread(target=self._retry, args=(addr, port, handler), name=f"bind-{addr}", daemon=True)
            self._threads.append(t)
            t.start()
            return
        self._serve(srv)

    def _retry(self, addr: str, port: int, handler) -> None:
        while not self._stop_evt.wait(BIND_RETRY_S):
            try:
                srv = _Server((addr, port), handler)
            except OSError:
                continue
            if self._stop_evt.is_set():
                srv.server_close()
                return
            self._serve(srv, in_thread=True)
            return

    def _serve(self, srv: _Server, in_thread: bool = False) -> None:
        srv.daemon_threads = True
        srv.log = self.ctx.log
        with self._lock:
            self.servers.append(srv)
            self.addresses.append((srv.server_address[0], srv.server_address[1]))
        self.ctx.log.info("listening on %s:%s", srv.server_address[0], srv.server_address[1])
        if in_thread:
            srv.serve_forever(poll_interval=0.1)
            return
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, name="speech-http", daemon=True)
        self._threads.append(t)
        t.start()

    def stop(self) -> None:
        self._stop_evt.set()
        with self._lock:
            servers, self.servers = self.servers, []
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        self.addresses = []

    def status(self) -> dict:
        return {"listening": [f"{h}:{p}" for h, p in self.addresses]}


SPEC = PluginSpec(
    name="server.http", kind="server", version="1", machine="speech",
    summary="speech server HTTP front: /health, /stt, /tts and the /ctl/v1 control routes",
    provides=("http",), create=lambda ctx, cfg: ServerHttpPlugin(ctx, cfg),
    knobs=(
        Knob("bind", "list[str]", ["127.0.0.1", "100.77.98.89"],
             "Addresses the server listens on; loopback or tailnet only, never a wildcard.",
             min=1, max=4, applies="restart", env="MIRA_SPEECH_BIND", topic="system"),
        Knob("port", "int", 8765, "TCP port of the speech server.", unit="port", min=1024, max=65535,
             applies="restart", env="MIRA_SPEECH_PORT", topic="system"),
        Knob("token_file", "path", "~/.config/mira/speech.token", "Bearer token file of /stt and /tts, read once at start.",
             applies="restart", secret=True, env="MIRA_SPEECH_TOKEN_FILE", topic="system"),
        Knob("base_dir", "path", "~/Library/Application Support/mira-speech", "Install folder; models live in its models folder.",
             applies="restart", env="MIRA_SPEECH_DIR", topic="system"),
        Knob("max_stt_s", "float", 30.0, "Longest clip /stt accepts.", unit="s", min=5, max=60, applies="restart",
             topic="system"),
        Knob("max_tts_chars", "int", 600, "Longest text /tts accepts.", unit="chars", min=100, max=2000,
             topic="system"),
        Knob("tts_body_max", "int", 4096, "Largest /tts request body.", unit="bytes", min=1024, max=65536,
             topic="system"),
    ),
    needs=("stt", "tts", "voices"),
    wants=("control",),
    validate=_validate,
    tests=("wall/tests/speech/test_sp_server_http.py",),
)
