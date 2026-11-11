"""server.http: /health, /stt, /tts, /ctl/v1 delegation, auth, limits, binds (spec 2.9.1, 2.7.1)."""
import http.client
import io
import json
import logging
import socket
import sys
import threading
import time
import types
import wave

import numpy as np
import pytest

import sp_fakes as fk
from sp_fakes import real_config_dir_untouched  # noqa: F401  (autouse guard)
from mirakit.plugin import ConfigError
from mirakit.testkit import FakeCtx, cfg_for
from speech_plugins import control_http, server_http as sh

DATA, OWNER, PI = "data-plane-token", "owner-token", "pi-token"
AMY = "vits-piper-en_US-amy-medium#0"
SECRET_TEXT = "zebra marmalade confidential sentence"


class FakeStt:
    kind = "faster-whisper:tiny.en"

    def __init__(self):
        self.seen = []

    def transcribe(self, samples):
        self.seen.append(len(samples))
        return SECRET_TEXT, 7


class FakeCore:
    def __init__(self):
        self.busy = 0

    def note_busy(self):
        self.busy += 1

    def status(self):
        return {"engine": "plugins", "revision": 12}


@pytest.fixture
def ctl_stub(monkeypatch):
    mod = types.ModuleType("mirakit.control")
    mod.calls = []

    def handle(core, handler, path, scope):
        mod.calls.append((core, handler.command, path, scope))
        if path.endswith("/boom"):
            raise RuntimeError("explode")
        body = json.dumps({"scope": scope, "path": path, "method": handler.command}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    mod.handle = handle
    monkeypatch.setitem(sys.modules, "mirakit.control", mod)
    return mod


@pytest.fixture
def srv(tmp_path, ctl_stub):
    clock = fk.FakeClock()
    backend = fk.FakeTtsBackend()
    backend.gates["vits-piper-en_US-kristin-medium"] = threading.Event()
    ctx, tts_plugin, cat, backend = fk.build_tts_stack(tmp_path, clock=clock, backend=backend,
                                                       tts_cfg={"warm": False})
    backend.gates["vits-piper-en_US-kristin-medium"].set()
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    fk.token_file(tokens / "speech.token", DATA)
    fk.token_file(tokens / "owner", OWNER)
    fk.token_file(tokens / "pi", PI)
    stt, core = FakeStt(), FakeCore()
    ctx.provide("stt", stt)
    ctx.provide("core", core)
    ctx.cfgs["server.http"] = cfg_for(sh.SPEC, bind=["127.0.0.1"], port=0, token_file=str(tokens / "speech.token"),
                                      base_dir=str(tmp_path / "base"))
    ctx.cfgs["control.http"] = cfg_for(control_http.SPEC, owner_token_file=str(tokens / "owner"),
                                       pi_token_file=str(tokens / "pi"))
    control = control_http.SPEC.create(ctx, ctx.cfgs["control.http"])
    control.start()
    plugin = sh.SPEC.create(ctx, ctx.cfgs["server.http"])
    plugin.start()
    host, port = plugin.addresses[0]
    ns = types.SimpleNamespace(ctx=ctx, plugin=plugin, host=host, port=port, stt=stt, core=core, backend=backend,
                               tts=tts_plugin, control=control, tokens=tokens, ctl=ctl_stub, tmp=tmp_path, clock=clock)
    yield ns
    plugin.stop()
    tts_plugin.stop()
    ctx.release_all()
    ctx.cleanup()


def call(s, method, path, body=None, token=DATA, headers=None, raw=False):
    conn = http.client.HTTPConnection(s.host, s.port, timeout=10)
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    payload = r.read()
    out = (r.status, payload if raw else (json.loads(payload) if payload and r.getheader("Content-Type") == "application/json" else payload),
           {k.lower(): v for k, v in r.getheaders()})
    conn.close()
    return out


def wav16k(seconds=1.0, rate=16000, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.zeros(int(seconds * rate) * channels, dtype="<i2").tobytes())
    return buf.getvalue()


def test_health_needs_no_auth_and_has_the_v1_fields_plus_the_additive_ones(srv):
    code, body, _ = call(srv, "GET", "/health", token=None)
    assert code == 200
    assert body["ok"] is True and body["stt"] == "faster-whisper:tiny.en" and body["tts"] == "vits-piper-en_US-amy-medium"
    assert body["voice"] == "amy" and isinstance(body["uptime_s"], float)
    assert body["engine"] == "plugins" and body["revision"] == 12 and body["voice_id"] == AMY
    assert set(body) == {"ok", "stt", "tts", "voice", "uptime_s", "engine", "revision", "voice_id"}


def test_health_survives_a_core_that_cannot_report(srv):
    srv.core.status = lambda: (_ for _ in ()).throw(RuntimeError("no"))
    code, body, _ = call(srv, "GET", "/health", token=None)
    assert code == 200 and body["engine"] == "plugins" and body["revision"] == 0


def test_stt_transcribes_a_wav_and_notes_busy(srv):
    code, body, _ = call(srv, "POST", "/stt", wav16k(1.5))
    assert code == 200 and body == {"text": SECRET_TEXT, "ms": 7}
    assert srv.stt.seen == [24000] and srv.core.busy == 1


@pytest.mark.parametrize("path,payload,raw", [("/stt", None, False), ("/tts", {"text": "hello"}, True)])
def test_the_busy_note_is_made_before_the_reply_leaves(srv, path, payload, raw):
    # A slow note_busy (a loaded machine) used to run after the reply was written, so a client that acted on the
    # reply saw the engine idle. With the note before the flush the client cannot get the reply until it is made.
    import time

    def slow_note():
        time.sleep(0.4)
        srv.core.busy += 1

    srv.core.note_busy = slow_note
    code = (call(srv, "POST", path, wav16k(0.5) if payload is None else payload, raw=raw))[0]
    assert code == 200 and srv.core.busy == 1


def test_a_failed_request_is_still_noted_busy_before_its_error_reply(srv):
    assert call(srv, "POST", "/stt", b"not a wav")[0] == 400
    assert srv.core.busy == 1


def test_stt_rejects_bad_audio_and_oversized_bodies_with_the_v1_answers(srv):
    assert call(srv, "POST", "/stt", wav16k(1.0, rate=8000))[:2] == (400, {"error": "need 16 kHz mono 16-bit WAV"})
    assert call(srv, "POST", "/stt", wav16k(1.0, channels=2))[0] == 400
    assert call(srv, "POST", "/stt", wav16k(30.03))[:2] == (400, {"error": "clip longer than 30 s"})
    # only the headers are sent: the server must answer 413 from Content-Length alone, as v1 did
    sock = socket.create_connection((srv.host, srv.port), timeout=5)
    sock.sendall(f"POST /stt HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {DATA}\r\n"
                 f"Content-Length: {30 * 16000 * 2 + 4097}\r\n\r\n".encode())
    reply = b""
    while not reply.endswith(b"}"):
        chunk = sock.recv(4096)
        assert chunk
        reply += chunk
    sock.close()
    assert reply.startswith(b"HTTP/1.1 413") and b"bad_or_oversized_body" in reply
    assert call(srv, "POST", "/stt", b"", headers={"Content-Length": "0"})[0] == 413
    assert call(srv, "POST", "/stt", b"not a wav")[0] == 400


def test_auth_failures_return_401_and_close_the_connection_without_reading(srv):
    for tok in (None, "wrong", OWNER):
        code, body, _ = call(srv, "POST", "/stt", wav16k(), token=tok)
        assert (code, body) == (401, {"error": "unauthorized"})
    conn = http.client.HTTPConnection(srv.host, srv.port, timeout=5)
    conn.request("POST", "/tts", body=b"{}")
    assert conn.getresponse().status == 401
    with pytest.raises((http.client.HTTPException, OSError)):
        conn.request("POST", "/tts", body=b"{}")
        conn.getresponse().read()
    assert srv.stt.seen == [] and srv.core.busy == 0               # nothing was processed


def test_unknown_paths_404_and_unknown_post_paths_404_before_auth(srv):
    assert call(srv, "GET", "/nope", token=None)[:2] == (404, {"error": "not_found"})
    assert call(srv, "POST", "/nope", b"x", token=None)[:2] == (404, {"error": "not_found"})
    assert call(srv, "PUT", "/health", b"x", token=None)[0] == 404
    assert call(srv, "GET", "/stt", token=None)[0] == 404


def test_tts_returns_wav_at_the_model_rate_with_the_voice_header(srv):
    code, wav, h = call(srv, "POST", "/tts", {"text": "hello there"}, raw=True)
    assert code == 200 and h["content-type"] == "audio/wav" and h["x-mira-voice"] == AMY
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)
    assert srv.core.busy == 1


def test_tts_speed_peak_and_voice_overrides_reach_the_engine(srv):
    code, wav, h = call(srv, "POST", "/tts", {"text": "hi", "voice": "kristin", "speed": 1.2, "peak": 0.5}, raw=True)
    assert code == 200 and h["x-mira-voice"] == "vits-piper-en_US-kristin-medium#0"
    model = srv.backend.models["vits-piper-en_US-kristin-medium"][0]
    assert model.calls[-1] == ("hi", 0, 1.2)
    with wave.open(io.BytesIO(wav), "rb") as w:
        peak = np.max(np.abs(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"))) / 32767
    assert peak == pytest.approx(0.5, abs=0.01)
    assert srv.ctx.get("tts").voice_id == AMY                      # a request never changes the stored voice
    assert call(srv, "POST", "/tts", {"text": "hi", "voice": AMY, "speed": 0, "peak": 0}, raw=True)[0] == 200


def test_tts_unknown_voice_is_a_400_with_effective_and_near(srv):
    code, body, h = call(srv, "POST", "/tts", {"text": "hi", "voice": "amey"})
    assert code == 400 and body["error"] == "unknown_voice" and body["effective"] == AMY
    assert "amy" in body["near"] and len(body["near"]) <= 3 and h["x-mira-voice"] == AMY
    code, body, _ = call(srv, "POST", "/tts", {"text": "hi", "voice": "ghost"})
    assert code == 400 and body["error"] == "invalid" and "tokens.txt" in body["message"]


def test_tts_request_validation_matches_v1(srv):
    assert call(srv, "POST", "/tts", {"text": ""})[:2] == (400, {"error": "text must be 1..600 chars"})
    assert call(srv, "POST", "/tts", {"text": "x" * 601})[0] == 400
    assert call(srv, "POST", "/tts", {"text": "x" * 600})[0] == 200
    assert call(srv, "POST", "/tts", {"nope": 1})[:2] == (400, {"error": "need JSON {text, voice?}"})
    assert call(srv, "POST", "/tts", b"{bad json")[:2] == (400, {"error": "need JSON {text, voice?}"})
    assert call(srv, "POST", "/tts", b"[1]")[0] == 400
    assert call(srv, "POST", "/tts", {"text": "hi", "speed": "fast"})[0] == 400
    assert call(srv, "POST", "/tts", {"text": "hi", "peak": True})[0] == 400
    big = json.dumps({"text": "hi", "pad": "y" * 5000}).encode()
    assert call(srv, "POST", "/tts", big)[:2] == (413, {"error": "bad_or_oversized_body"})


def test_live_limits_follow_the_config(srv):
    srv.plugin.apply({"max_tts_chars": 100}, srv.plugin.cfg.replace(max_tts_chars=100))
    assert call(srv, "POST", "/tts", {"text": "x" * 101})[0] == 400
    assert call(srv, "POST", "/tts", {"text": "x" * 100})[0] == 200


def test_a_second_preview_gets_429_busy_while_replies_still_work(srv):
    gate = srv.backend.gates["vits-piper-en_US-kristin-medium"]
    gate.clear()
    first = {}
    t = threading.Thread(target=lambda: first.setdefault("r", call(srv, "POST", "/tts", {"text": "a", "voice": "kristin"}, raw=True)))
    t.start()
    for _ in range(500):
        models = srv.backend.models.get("vits-piper-en_US-kristin-medium")
        if models and models[0].entered.is_set():
            break
        time.sleep(0.01)
    code, body, h = call(srv, "POST", "/tts", {"text": "b", "voice": "jenny"})
    assert code == 429 and body["error"] == "busy" and h["x-mira-voice"] == AMY
    assert call(srv, "POST", "/tts", {"text": "reply"}, raw=True)[0] == 200
    gate.set()
    t.join(5)
    assert first["r"][0] == 200


def test_tts_failure_is_a_500_without_request_text(srv, caplog):
    srv.backend.models["vits-piper-en_US-amy-medium"][0].generate = lambda *a: (_ for _ in ()).throw(RuntimeError("model died"))
    with caplog.at_level(logging.DEBUG):
        code, body, _ = call(srv, "POST", "/tts", {"text": SECRET_TEXT})
    assert (code, body) == (500, {"error": "internal"})
    assert SECRET_TEXT not in caplog.text


def test_request_text_is_never_logged(srv, caplog):
    with caplog.at_level(logging.DEBUG):
        call(srv, "POST", "/tts", {"text": SECRET_TEXT})
        call(srv, "POST", "/stt", wav16k())
        call(srv, "POST", "/tts", {"text": SECRET_TEXT, "voice": "nonesuch"})
    assert SECRET_TEXT not in caplog.text and "zebra" not in caplog.text
    assert any("POST /tts" in r.getMessage() for r in caplog.records)           # request lines are logged


def test_ctl_routes_hand_over_with_the_scope_of_the_matching_control_token(srv):
    code, body, _ = call(srv, "GET", "/ctl/v1/inventory?machine=speech", token=OWNER)
    assert code == 200 and body == {"scope": "owner", "path": "/ctl/v1/inventory?machine=speech", "method": "GET"}
    assert call(srv, "PUT", "/ctl/v1/config/tts.engine/speed", {"value": 1.1}, token=PI)[1]["scope"] == "agent"
    assert call(srv, "POST", "/ctl/v1/undo", {}, token=PI)[1]["method"] == "POST"
    core, method, path, scope = srv.ctl.calls[-1]
    assert core is srv.core and (method, path, scope) == ("POST", "/ctl/v1/undo", "agent")
    assert srv.core.busy == 0                                       # control calls are not speech traffic


def test_ctl_refuses_the_data_plane_token_and_unknown_tokens_with_401_and_close(srv):
    for tok in (DATA, "wrong", None):
        code, body, _ = call(srv, "GET", "/ctl/v1/status", token=tok)
        assert (code, body) == (401, {"error": "unauthorized"})
    assert srv.ctl.calls == []
    # the control tokens do not open the data plane either
    assert call(srv, "POST", "/stt", wav16k(), token=OWNER)[0] == 401


def test_ctl_tokens_are_reread_on_every_request(srv):
    fk.token_file(srv.tokens / "owner", "rotated-owner")
    assert call(srv, "GET", "/ctl/v1/status", token=OWNER)[0] == 401
    assert call(srv, "GET", "/ctl/v1/status", token="rotated-owner")[1]["scope"] == "owner"


def test_ctl_is_404_when_the_control_plugin_is_not_running(srv):
    srv.ctx.release_all()                  # drops every service this FakeCtx holds, including "control"
    srv.ctx.provide("stt", srv.stt)
    assert call(srv, "GET", "/ctl/v1/status", token=OWNER)[:2] == (404, {"error": "not_found"})


def test_a_failing_control_handler_is_a_500_internal(srv):
    code, body, _ = call(srv, "GET", "/ctl/v1/boom", token=OWNER)
    assert (code, body) == (500, {"error": "internal"})


def test_wildcard_and_foreign_binds_are_refused(srv):
    for bad in ("0.0.0.0", "::", "192.168.1.5", "8.8.8.8", "localhost", ""):
        with pytest.raises(ConfigError) as e:
            sh.SPEC.validate({"bind": ["127.0.0.1", bad]})
        assert e.value.field == "bind"
    for ok in ("127.0.0.1", "100.77.98.89", "::1", "100.127.255.254"):
        sh.SPEC.validate({"bind": [ok]})
    p = sh.SPEC.create(srv.ctx, srv.ctx.cfgs["server.http"].replace(bind=["0.0.0.0"]))
    with pytest.raises(ConfigError):
        p.start()


def test_missing_token_file_fails_the_start(tmp_path):
    ctx = FakeCtx("speech")
    cfg = cfg_for(sh.SPEC, bind=["127.0.0.1"], port=0, token_file=str(tmp_path / "absent"))
    with pytest.raises(RuntimeError, match="no token in"):
        sh.SPEC.create(ctx, cfg).start()
    (tmp_path / "empty").write_text("\n")
    with pytest.raises(RuntimeError, match="no token in"):
        sh.SPEC.create(ctx, cfg.replace(token_file=str(tmp_path / "empty"))).start()


def test_the_default_config_points_at_the_fake_home_not_the_real_one(tmp_path):
    ctx = FakeCtx("speech")
    p = sh.SPEC.create(ctx, cfg_for(sh.SPEC, bind=["127.0.0.1"], port=0))
    with pytest.raises(RuntimeError) as e:
        p.start()
    assert str(ctx.paths.home) in str(e.value)                      # "~" expanded through ctx.paths, never expanduser


def test_server_bind_does_not_reverse_resolve(srv, monkeypatch):
    def no(*a, **k):
        raise AssertionError("getfqdn must not be called")
    monkeypatch.setattr(socket, "getfqdn", no)
    s = sh.SPEC.create(srv.ctx, srv.ctx.cfgs["server.http"])
    s.token = b"x"
    srv2 = sh._Server(("127.0.0.1", 0), sh.make_handler(s))
    srv2.server_close()


def test_bind_is_retried_until_the_address_is_free(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "BIND_RETRY_S", 0.05)
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    ctx = FakeCtx("speech")
    fk.token_file(tmp_path / "t", "x")
    p = sh.SPEC.create(ctx, cfg_for(sh.SPEC, bind=["127.0.0.1"], port=port, token_file=str(tmp_path / "t")))
    p.start()
    try:
        assert p.addresses == []
        holder.close()
        for _ in range(200):
            if p.addresses:
                break
            time.sleep(0.02)
        assert p.addresses == [("127.0.0.1", port)]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        assert conn.getresponse().status == 200
    finally:
        p.stop()
        ctx.cleanup()


def test_stop_releases_the_port_and_a_restart_works(srv):
    port = srv.port
    srv.plugin.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    srv.plugin.start()
    host, port2 = srv.plugin.addresses[0]
    srv.host, srv.port = host, port2
    assert call(srv, "GET", "/health", token=None)[0] == 200


def test_health_is_not_ok_while_a_service_is_missing(tmp_path):
    ctx = FakeCtx("speech")
    fk.token_file(tmp_path / "t", "x")
    p = sh.SPEC.create(ctx, cfg_for(sh.SPEC, bind=["127.0.0.1"], port=0, token_file=str(tmp_path / "t")))
    p.start()
    try:
        host, port = p.addresses[0]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/health")
        body = json.loads(conn.getresponse().read())
        assert body["ok"] is False and body["voice_id"] == ""
    finally:
        p.stop()
        ctx.cleanup()
