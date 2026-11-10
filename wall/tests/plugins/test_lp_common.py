"""Tests for listener_plugins.common (spec 4.4), and the helpers the other test_lp_* files import.

The other plugin tests do `import test_lp_common as H`: pytest puts this folder on sys.path (no __init__.py), and
every test_*.py basename is unique across the repo, so the module is imported once under this name.
"""
from __future__ import annotations

import io
import os
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

import mira_listen_v1 as v1  # noqa: E402
import listener_plugins as lp  # noqa: E402
from listener_plugins import common  # noqa: E402
from mirakit.plugin import PluginConfig  # noqa: E402
from mirakit.testkit import FakeCtx, cfg_for  # noqa: E402

FIXTURES = WALL / "tests" / "fixtures"
CALENDAR_WAV = FIXTURES / "mira_what_s_on_my_calendar_today.wav"
PAUSE_WAV = FIXTURES / "mira_pause_the_music.wav"


# ------------------------------------------------------------------------------------------ helpers

def spec_named(name):
    for s in lp.specs():
        if s.name == name:
            return s
    raise KeyError(name)


def make_ctx(**cfg_overrides):
    """A FakeCtx whose cfg_of knows every listener spec's defaults; overrides are {"plugin.name": {knob: value}}."""
    ctx = FakeCtx.with_specs(*lp.specs())
    for plugin, over in cfg_overrides.items():
        ctx.cfgs[plugin.replace("__", ".")] = cfg_for(spec_named(plugin.replace("__", ".")), **over)
    return ctx


def started(spec_name, ctx=None, **overrides):
    """(plugin, ctx, cfg) after start()."""
    spec = spec_named(spec_name)
    ctx = ctx or make_ctx()
    cfg = cfg_for(spec, **overrides)
    ctx.cfgs[spec.name] = cfg
    plugin = spec.create(ctx, cfg)
    plugin.start()
    return plugin, ctx, cfg


def chunks_of(x, n=common.CHUNK):
    return [x[i:i + n] for i in range(0, len(x), n)]


def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MIRA_"):
            monkeypatch.delenv(k)


def assert_live_knobs(spec, plugin, checks):
    """Every live knob changes the object through apply(): checks = {knob: (new_value, getter[, expected])}."""
    live = {k.name for k in spec.knobs if k.applies == "live"}
    missing = live - set(checks)
    assert not missing, f"{spec.name}: no apply check for live knobs {sorted(missing)}"
    for name in sorted(live):
        entry = checks[name]
        value, getter = entry[0], entry[1]
        expected = entry[2] if len(entry) > 2 else value
        base = plugin.cfg
        plugin.apply({name: value}, base.replace(**{name: value}))
        got = getter(plugin)
        assert got == expected, f"{spec.name}.{name}: attribute is {got!r}, wanted {expected!r}"
        plugin.apply({name: base[name]}, base)


def wav_bytes_16(x, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(x.astype("<i2").tobytes())
    return buf.getvalue()


class Srv:
    """A recording stdlib server that can set response headers. handler(method, path, body, headers) ->
    (code, bytes, content_type, extra_headers). Binds 127.0.0.1 port 0 only."""

    def __init__(self, handler):
        import json as _json  # noqa: F401
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                outer.calls.append({"method": self.command, "path": self.path, "body": body, "headers": hdrs})
                code, data, ctype, extra = handler(self.command, self.path, body, hdrs)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def refused_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def fake_pw_record(tmp_path, monkeypatch, pcm=None, name="pw-record", tail=""):
    """A `pw-record` stand-in on PATH: logs its argv, writes PCM to stdout, then `tail` (a shell line such as
    `sleep 30`). Returns the argv log path."""
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    data = tmp_path / "pcm.raw"
    data.write_bytes(pcm or b"")
    log = tmp_path / "argv.log"
    script = d / name
    script.write_text(f'#!/bin/sh\necho "$@" > "{log}"\ncat "{data}"\n{tail}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    return log


# ------------------------------------------------------------------------------------------- tests

def test_constants_and_verbatim_helpers_match_v1(tmp_path):
    assert (common.RATE, common.CHUNK) == (v1.RATE, v1.CHUNK) == (16000, 1600)
    x = np.sin(np.linspace(0, 200, 8000)).astype(np.float32)
    for r_in, r_out in ((16000, 16000), (22050, 16000), (8000, 16000)):
        assert np.array_equal(common.resample(x, r_in, r_out), v1.resample(x, r_in, r_out))
    assert np.array_equal(common.read_wav_float(CALENDAR_WAV), v1.read_wav_float(CALENDAR_WAV))
    raw = CALENDAR_WAV.read_bytes()
    assert np.array_equal(common.read_wav_float_bytes(raw), v1.read_wav_float_bytes(raw))
    y = common.read_wav_float(CALENDAR_WAV)[:20000]
    assert common.pcm16_wav_bytes(y) == v1.pcm16_wav_bytes(y)
    tok = tmp_path / "t"
    tok.write_text(" secret \n")
    assert common.read_secret(str(tok)) == v1.read_secret(str(tok)) == "secret"
    assert common.read_secret("") == "" and common.read_secret(str(tmp_path / "none")) == ""


def test_http_helper_round_trip():
    from conftest import FakeServer
    s = FakeServer()
    s.routes[("POST", "/x")] = lambda b, h: (200, b'{"ok": 1}', "application/json")
    try:
        status, body = common.http("POST", s.url + "/x", b"{}", {"X-A": "1"}, timeout=5)
        assert (status, body) == (200, b'{"ok": 1}')
    finally:
        s.stop()


@pytest.mark.parametrize("value,expected", [
    ("${HERE}/models/x.onnx", "/inst/models/x.onnx"), ("${HOME}/a", "/home/u/a"), ("~/a/b", "/home/u/a/b"),
    ("~", "/home/u"), ("/etc/x", "/etc/x"), ("", ""), ("${HERE}/../voice", "/inst/../voice"),
])
def test_expand_path(value, expected):
    from mirakit.plugin import Paths
    paths = Paths(here=Path("/inst"), config_dir=Path("/home/u/.config/mira"), home=Path("/home/u"))
    assert common.expand_path(value, paths) == expected
    assert common.expand_path(common.expand_path(value, paths), paths) == expected  # idempotent


@pytest.mark.parametrize("url,loop,tail", [
    ("http://127.0.0.1:8642", True, True), ("http://localhost:3456/x", True, True), ("http://[::1]:1/", True, True),
    ("http://100.77.98.89:8765", False, True), ("http://100.64.0.1", False, True), ("http://100.128.0.1", False, False),
    ("http://192.168.1.5:80", False, False), ("http://example.com", False, False), ("http://10.0.0.1", False, False),
    ("", False, False),
])
def test_host_rules(url, loop, tail):
    assert common.host_is_loopback(url) is loop
    assert common.host_is_loopback_or_tailnet(url) is tail


@pytest.mark.parametrize("window,minute,inside", [
    ("23:00-06:00", 23 * 60, True), ("23:00-06:00", 23 * 60 + 59, True), ("23:00-06:00", 0, True),
    ("23:00-06:00", 5 * 60 + 59, True), ("23:00-06:00", 6 * 60, False), ("23:00-06:00", 12 * 60, False),
    ("23:00-06:00", 22 * 60 + 59, False), ("09:00-17:00", 9 * 60, True), ("09:00-17:00", 17 * 60, False),
    ("09:00-17:00", 3 * 60, False), ("", 100, False), ("10:00-10:00", 10 * 60, False),
])
def test_in_window(window, minute, inside):
    assert common.in_window(window, minute) is inside


def test_common_has_no_plugin_imports():
    src = (WALL / "listener_plugins" / "common.py").read_text()
    assert "mira_listen" not in src.replace("mira_listen_v1.py", "")
