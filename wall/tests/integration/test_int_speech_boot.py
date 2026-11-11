"""Integration: the speech server's plugin path booted the way the LaunchAgent does it (speech/server.py's
_run_plugins) with the REAL core and plugins, a fake sherpa_onnx and a fake faster_whisper, an ephemeral loopback port
and a fake model tree. Checks /health, /stt, /tts with X-Mira-Voice, an unknown voice, the control plane, and a voice
switch through /ctl/v1 while a /tts request is in flight (the request finishes in the old voice)."""
import http.client
import importlib.util
import io
import json
import os
import signal
import sys
import threading
import time
import types
import wave
from pathlib import Path

import numpy as np
import pytest

WALL = Path(__file__).resolve().parents[2]
for p in (str(WALL), str(WALL / "tests" / "speech")):
    if p not in sys.path:
        sys.path.insert(0, p)

import sp_fakes as fk  # noqa: E402
from sp_fakes import real_config_dir_untouched  # noqa: E402,F401  (autouse guard: the real ~/.config/mira stays untouched)
from golden.fakes import free_port  # noqa: E402

DATA, OWNER, PI = "int-data-token", "int-owner-token", "int-pi-token"
AMY = "vits-piper-en_US-amy-medium#0"
ENV = ("MIRA_SPEECH_DIR", "MIRA_SPEECH_TOKEN_FILE", "MIRA_SPEECH_BIND", "MIRA_SPEECH_PORT", "MIRA_STT", "MIRA_TTS_DIR",
       "MIRA_TTS_VOICE", "MIRA_THREADS", "MIRA_TTS_THREADS", "MIRA_TTS_PEAK", "MIRA_STT_PROMPT", "MIRA_STT_HOTWORDS",
       "MIRA_STT_COMPUTE", "MIRA_STT_BEAM", "MIRA_TTS_LENGTH_SCALE", "MIRA_SPEECH_CONFIG")


def wav16k(seconds=1.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(int(seconds * 16000), dtype="<i2").tobytes())
    return buf.getvalue()


class Box:
    """The running server plus a tiny HTTP client."""

    def __init__(self):
        self.port = 0

    def call(self, method, path, body=None, token=DATA, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = "Bearer " + token
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        payload = r.read()
        out = (r.status, payload, {k.lower(): v for k, v in r.getheaders()})
        conn.close()
        return out

    def json(self, *a, **kw):
        status, payload, hdrs = self.call(*a, **kw)
        return status, (json.loads(payload) if payload and hdrs.get("content-type", "").startswith("application/json") else payload), hdrs


@pytest.fixture
def box(tmp_path, monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    cfgdir = home / ".config" / "mira"
    cfgdir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    base = tmp_path / "base"
    fk.make_models(base)
    for name, tok in (("speech.token", DATA), ("control.owner.token", OWNER), ("control.pi.token", PI)):
        fk.token_file(cfgdir / name, tok)
        (cfgdir / name).chmod(0o600)
    port = free_port()
    cfg = cfgdir / "speech.json"
    cfg.write_text(json.dumps({
        "format": 1, "machine": "speech", "revision": 1, "core": {"engine": "plugins"},
        "plugins": {
            "server.http": {"enabled": True, "config": {"bind": ["127.0.0.1"], "port": port, "base_dir": str(base),
                                                        "token_file": str(cfgdir / "speech.token")}},
            "control.http": {"enabled": True, "config": {"owner_token_file": str(cfgdir / "control.owner.token"),
                                                         "pi_token_file": str(cfgdir / "control.pi.token")}},
            "stt.engine": {"enabled": True, "config": {"kind": "faster-whisper:tiny.en", "warm": False}},
            "tts.engine": {"enabled": True, "config": {"voice": AMY, "warm": False}},
            "voices.catalog": {"enabled": True, "config": {"rtf_file": str(cfgdir / "voices.rtf.json")}},
        }}))
    monkeypatch.setenv("MIRA_SPEECH_CONFIG", str(cfg))

    # fake sherpa_onnx: the first /tts whose text starts with "slow" blocks inside generate until released
    sherpa = fk.FakeSherpa()
    box = Box()
    box.gate, box.started = threading.Event(), threading.Event()
    real_generate = sherpa.OfflineTts.generate

    def generate(self, text, sid=0, speed=1.0):
        if text.startswith("slow"):
            box.started.set()
            assert box.gate.wait(20), "the test never released the gate"
        return real_generate(self, text, sid, speed)
    sherpa.OfflineTts.generate = generate
    monkeypatch.setitem(sys.modules, "sherpa_onnx", sherpa)
    fw = types.ModuleType("faster_whisper")

    class Seg:
        text = " Mira, open Onshape "

    class WhisperModel:
        def __init__(self, name, **kw):
            pass

        def transcribe(self, samples, **kw):
            return [Seg()], None
    fw.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)

    spec = importlib.util.spec_from_file_location("int_speech_server", WALL / "speech" / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    handlers = []
    monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.append(h))   # _run_plugins is not on the main thread here
    box.server, box.cfg, box.cfgdir, box.port, box.sherpa = server, cfg, cfgdir, port, sherpa
    assert server.choose_engine(cfg)[0] == "plugins"
    server.record_start(cfg)
    err = []
    th = threading.Thread(target=lambda: _guard(lambda: server._run_plugins(cfg), err), daemon=True)
    th.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if box.call("GET", "/health", token=None)[0] == 200:
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise AssertionError(f"speech server never came up: {err}")
    yield box
    box.gate.set()
    for h in handlers:
        h(signal.SIGTERM, None)
    th.join(15)
    assert not th.is_alive() and not err, err


def _guard(fn, err):
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001 - reported by the fixture
        err.append(repr(exc))


def test_launcher_chose_the_plugin_path_and_counted_the_start(box):
    starts = (box.cfgdir / "state" / "starts").read_text().split()
    assert starts[1] == "unplanned"


def test_health_shape(box):
    status, body, _ = box.json("GET", "/health", token=None)
    assert status == 200 and body["ok"] is True
    assert body["stt"] == "faster-whisper:tiny.en" and body["voice"] == "amy" and body["voice_id"] == AMY
    assert body["engine"] == "plugins" and isinstance(body["revision"], int) and body["uptime_s"] >= 0
    assert {"ok", "stt", "tts", "voice", "uptime_s"} <= set(body)           # the v1 fields stay


def test_tts_returns_a_wav_with_the_voice_header(box):
    status, wav, hdrs = box.call("POST", "/tts", {"text": "hello there"})
    assert status == 200 and hdrs["content-type"] == "audio/wav" and hdrs["x-mira-voice"] == AMY
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getnframes() > 0
    assert box.call("POST", "/tts", {"text": "hello"}, token="nope")[0] == 401


def test_unknown_voice_is_a_400_and_still_names_the_effective_voice(box):
    status, body, hdrs = box.json("POST", "/tts", {"text": "hello", "voice": "nobody-here"})
    assert status == 400 and hdrs["x-mira-voice"] == AMY
    assert body["error"] == "unknown_voice" and body["effective"] == AMY and "nobody-here" in body["message"]


def test_stt_goes_through_the_fake_whisper(box):
    status, body, _ = box.json("POST", "/stt", raw=wav16k(1.0), headers={"Content-Type": "audio/wav"})
    assert status == 200 and body["text"].strip() == "Mira, open Onshape"


def test_control_plane_answers_the_owner_and_refuses_the_data_token(box):
    status, body, _ = box.json("GET", "/ctl/v1/status", token=OWNER)
    assert status == 200 and body["machine"] == "speech"
    assert box.call("GET", "/ctl/v1/status", token=DATA)[0] == 401
    status, inv, _ = box.json("GET", "/ctl/v1/inventory", token=OWNER)
    assert status == 200 and {"server.http", "tts.engine", "stt.engine", "voices.catalog"} <= {r["name"] for r in inv}


def test_a_voice_switch_through_ctl_waits_for_the_request_in_flight(box):
    kristin = "vits-piper-en_US-kristin-medium#0"
    slow = {}

    def request():
        slow["r"] = box.call("POST", "/tts", {"text": "slow reply that is still being rendered"})
    th = threading.Thread(target=request, daemon=True)
    th.start()
    assert box.started.wait(10)                                   # the request is inside the model now
    status, change, _ = box.json("PUT", "/ctl/v1/config/tts.engine/voice", {"value": "kristin", "wait_s": 0.5}, token=OWNER)
    assert status in (200, 202) and change["edits"][0]["key"] == "tts.engine.voice" and change["applies"] == "rebuild"
    assert th.is_alive()                                          # nothing cut the request short
    box.gate.set()
    th.join(20)
    code, wav, hdrs = slow["r"]
    assert code == 200 and hdrs["x-mira-voice"] == AMY            # finished on the model it started with
    deadline = time.time() + 15
    voice = ""
    while time.time() < deadline:
        voice = box.json("GET", "/health", token=None)[1]["voice"]
        if voice == "kristin":
            break
        time.sleep(0.1)
    assert voice == "kristin"
    code, _, hdrs = box.call("POST", "/tts", {"text": "after the switch"})
    assert code == 200 and hdrs["x-mira-voice"].startswith("vits-piper-en_US-kristin-medium")
    ch = box.json("GET", "/ctl/v1/changes?limit=1", token=OWNER)[1][0]
    assert ch["status"] == "applied" and ch["machine"] == "speech"
    assert (box.cfgdir / "changes.jsonl").exists()
    assert json.loads(box.cfg.read_text())["plugins"]["tts.engine"]["config"]["voice"] == kristin
