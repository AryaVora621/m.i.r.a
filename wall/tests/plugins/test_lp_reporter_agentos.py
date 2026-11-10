"""reporter.agentos: VoiceReporter copied from v1 with the five knob-fed parameters."""
import json
import time

import pytest

import test_lp_common as H
from conftest import FakeServer
from listener_plugins import reporter_agentos as ra
from mirakit.plugin import ConfigError

SPEC = ra.SPEC


@pytest.fixture
def hook():
    s = FakeServer()
    s.routes[("POST", "/h")] = lambda b, h: (200, b"{}", "application/json")
    yield s
    s.stop()


@pytest.fixture
def token(tmp_path):
    p = tmp_path / "tok"
    p.write_text("voice-token\n")
    return str(p)


def _posts(server):
    out = []
    for c in server.calls:
        d = json.loads(c["body"])
        d.pop("at")
        out.append(d)
    return out


SCRIPT = [("idle", "", ""), ("listening", "", ""), ("thinking", "", ""), ("thinking", "what time is it", ""),
          ("speaking", "what time is it", "It is three."), ("idle", "what time is it", "It is three.")]


def _run_script(rep):
    rep.set_level(0.0)
    for st, heard, reply in SCRIPT:
        rep.update(st, heard, reply)
    rep.set_mic("muted")
    rep.update("idle")
    rep.close()


def test_queue_items_and_post_order_equal_v1(hook, token):
    other = FakeServer()
    other.routes[("POST", "/h")] = hook.routes[("POST", "/h")]
    try:
        a = H.v1.VoiceReporter(hook.url + "/h", token, error_hold=0.0)
        b = ra.VoiceReporter(other.url + "/h", token, error_hold=0.0)
        t0 = time.monotonic()
        _run_script(a)
        _run_script(b)
        assert time.monotonic() - t0 < 12
        pa, pb = _posts(hook), _posts(other)
        assert pa == pb and len(pa) >= 6
        assert [p["state"] for p in pb][:3] == ["idle", "listening", "thinking"]
        assert pb[-1].get("mic") == "muted" or pb[-2].get("mic") == "muted"
        assert all(c["headers"]["authorization"] == "Bearer voice-token" for c in other.calls)
        assert a.last == b.last
    finally:
        other.stop()


def test_defaults_and_class_constants_equal_v1(token):
    r = ra.VoiceReporter("", token)
    try:
        assert (ra.VoiceReporter.MIN_GAP, ra.VoiceReporter.LEVEL_GAP, ra.VoiceReporter.LEVEL_STEP) == (0.5, 0.2, 0.03)
        assert (r.MIN_GAP, r.LEVEL_GAP, r.LEVEL_STEP, r.post_timeout_s, r.close_wait_s) == (0.5, 0.2, 0.03, 3.0, 20.0)
        assert r.LEVEL_STATES == ("listening", "speaking")
    finally:
        r.close()


def test_min_gap_is_an_instance_value(hook, token):
    r = ra.VoiceReporter(hook.url + "/h", token, error_hold=0.0, min_gap=0.1)
    r.update("idle"); r.update("listening"); r.update("thinking"); r.update("speaking")
    t0 = time.monotonic()
    r.close()
    assert time.monotonic() - t0 < 1.0 and len(hook.calls) == 4
    assert ra.VoiceReporter.MIN_GAP == 0.5  # the class constant is untouched


def test_error_hold_delays_the_next_state(hook, token):
    r = ra.VoiceReporter(hook.url + "/h", token, error_hold=0.8, min_gap=0.1)
    r.update("error", "q", "r")
    r.update("idle")
    r.close()
    times = [c for c in hook.calls]
    assert [json.loads(c["body"])["state"] for c in times] == ["error", "idle"]


def test_level_posts_follow_a_speaking_state(hook, token):
    r = ra.VoiceReporter(hook.url + "/h", token, min_gap=0.1, level_gap=0.05, level_step=0.05)
    r.update("speaking")
    time.sleep(0.3)
    r.set_level(0.8)
    deadline = time.time() + 3
    while time.time() < deadline and not any(json.loads(c["body"])["level"] == 0.8 for c in hook.calls):
        time.sleep(0.02)
    r.close()
    assert any(json.loads(c["body"])["level"] == 0.8 for c in hook.calls)


def test_empty_url_posts_nothing_and_close_is_safe_twice(hook, token):
    r = ra.VoiceReporter("", token, min_gap=0.1)
    r.update("idle")
    r.close()
    r.close()
    r.close(wait=False)
    assert hook.calls == []


def test_post_timeout_bounds_a_stuck_hook(token):
    srv = H.Srv(lambda m, p, b, h: (time.sleep(1.2), (200, b"{}", "application/json", None))[1])
    try:
        r = ra.VoiceReporter(srv.url + "/h", token, post_timeout_s=0.3)
        r.update("idle")
        t0 = time.monotonic()
        r.close()
        assert time.monotonic() - t0 < 1.0
    finally:
        srv.stop()


def test_close_wait_bounds_the_join(token):
    srv = H.Srv(lambda m, p, b, h: (time.sleep(1.5), (200, b"{}", "application/json", None))[1])
    try:
        r = ra.VoiceReporter(srv.url + "/h", token, post_timeout_s=5.0, close_wait_s=0.2)
        r.update("idle")
        time.sleep(0.1)
        t0 = time.monotonic()
        r.close()
        assert time.monotonic() - t0 < 1.0
    finally:
        srv.stop()


def test_plugin_provides_reporter_and_live_knobs_apply(hook, token):
    plugin, ctx, _ = H.started("reporter.agentos", url=hook.url + "/h", token_file=token)
    r = ctx.get("reporter")
    assert r is plugin.rep and r.url.endswith("/h")
    H.assert_live_knobs(SPEC, plugin, {
        "url": ("http://127.0.0.1:1/x", lambda p: r.url), "token_file": ("/t", lambda p: r.token_file),
        "error_hold": (2.0, lambda p: r.error_hold), "min_gap": (0.3, lambda p: r.MIN_GAP),
        "level_gap": (0.4, lambda p: r.LEVEL_GAP), "level_step": (0.1, lambda p: r.LEVEL_STEP),
        "post_timeout_s": (5.0, lambda p: r.post_timeout_s), "level_scale": (4.0, lambda p: r.level_scale)})
    assert {k.name for k in SPEC.knobs if k.applies == "restart"} == {"close_wait_s"}
    plugin.stop()
    ctx.release_all()
    ctx.release_all()  # effect list is empty the second time; close() already ran


@pytest.mark.parametrize("url,ok", [("http://127.0.0.1:3456/api/hooks/voice", True), ("", True),
                                    ("http://localhost:3456/x", True), ("http://100.77.98.89:3456/x", False),
                                    ("http://example.com/x", False)])
def test_validate_loopback_or_empty(url, ok):
    values = {k.name: k.default for k in SPEC.knobs}
    if ok:
        SPEC.validate(dict(values, url=url))
    else:
        with pytest.raises(ConfigError):
            SPEC.validate(dict(values, url=url))
