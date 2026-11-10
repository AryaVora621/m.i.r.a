"""brain.hermes: Hermes copied from v1 with the timeout and title parameters; against a fake SSE server."""
import json
import time
import urllib.error

import pytest

import test_lp_common as H
from conftest import FakeServer, sse
from listener_plugins import brain_hermes as bh
from mirakit.plugin import ConfigError

SPEC = bh.SPEC
REPLY = "You have a physics review at three and nothing else today."


def _hermes_server(reply=REPLY, fail_first_create=False, error_event=False, new_ids=("s1", "s2", "s3")):
    s = FakeServer()
    ids = list(new_ids)
    s.created = 0

    def create(body, h):
        s.created += 1
        if fail_first_create and s.created == 1:
            return 400, b'{"error": "title taken"}', "application/json"
        return 200, json.dumps({"id": ids[min(s.created - 1, len(ids) - 1)]}).encode(), "application/json"

    def stream(body, h):
        if error_event:
            ev = [("error", {"error": "model overloaded"})]
        else:
            ev = [("run.started", {}), ("assistant.delta", {"delta": reply[:5]}),
                  ("assistant.completed", {"content": reply}), ("done", {})]
        return 200, sse(ev), "text/event-stream"

    s.routes[("POST", "/api/sessions")] = create
    for sid in ids:
        s.routes[("POST", f"/api/sessions/{sid}/chat/stream")] = stream
    return s


@pytest.fixture
def key(tmp_path):
    p = tmp_path / "key"
    p.write_text("hermes-key\n")
    return str(p)


def _norm(calls):
    out = []
    for c in calls:
        body = json.loads(c["body"])
        if "title" in body:
            body["title"] = "<title>"
        out.append((c["method"], c["path"], body, c["headers"]["authorization"]))
    return out


def test_requests_replies_and_session_reuse_equal_v1(key):
    a, b = _hermes_server(), _hermes_server()
    try:
        ha, hb = H.v1.Hermes(a.url, key, "persona text"), bh.Hermes(b.url, key, "persona text")
        for now in (10.0, 20.0, 400.0):  # third call is past a 180 s ttl: a new session
            assert ha.ask("what's on my calendar", 180, now) == hb.ask("what's on my calendar", 180, now) == REPLY
            assert (ha.session_id, ha.last_used) == (hb.session_id, hb.last_used)
        assert _norm(a.calls) == _norm(b.calls)
        assert a.created == b.created == 2
        assert json.loads(b.calls[1]["body"]) == {"message": "what's on my calendar", "instructions": "persona text"}
        assert b.calls[1]["headers"]["authorization"] == "Bearer hermes-key"
    finally:
        a.stop(); b.stop()


def test_title_retry_on_400_and_prefix_knob(key):
    a, b = _hermes_server(fail_first_create=True), _hermes_server(fail_first_create=True)
    try:
        H.v1.Hermes(a.url, key, "p").ask("x", 180, 1.0)
        bh.Hermes(b.url, key, "p", title_prefix="Mira test").ask("x", 180, 1.0)
        ta = [json.loads(c["body"])["title"] for c in a.calls if c["path"] == "/api/sessions"]
        tb = [json.loads(c["body"])["title"] for c in b.calls if c["path"] == "/api/sessions"]
        assert len(ta) == len(tb) == 2 and ta[0].startswith("Mira wall ") and tb[0].startswith("Mira test ")
        assert tb[1].startswith("Mira test ") and tb[1].endswith(")") and ta[1].endswith(")")
    finally:
        a.stop(); b.stop()


def test_sse_error_event_raises_like_v1(key):
    a, b = _hermes_server(error_event=True), _hermes_server(error_event=True)
    try:
        with pytest.raises(RuntimeError) as ea:
            H.v1.Hermes(a.url, key, "p").ask("x", 180, 1.0)
        with pytest.raises(RuntimeError) as eb:
            bh.Hermes(b.url, key, "p").ask("x", 180, 1.0)
        assert str(ea.value) == str(eb.value) == "model overloaded"
    finally:
        a.stop(); b.stop()


def test_timeouts_are_knobs(key):
    def slow(method, path, body, headers):
        time.sleep(1.2)
        return 200, b'{"id": "s1"}', "application/json", None
    srv = H.Srv(slow)
    try:
        h = bh.Hermes(srv.url, key, "p", session_timeout_s=0.3)
        t0 = time.monotonic()
        with pytest.raises(Exception):
            h.ask("x", 180, 1.0)
        assert time.monotonic() - t0 < 1.0
    finally:
        srv.stop()
    srv2 = H.Srv(lambda m, p, b, h: (200, b'{"id": "s1"}', "application/json", None) if p == "/api/sessions"
                 else (time.sleep(1.2), (200, b"", "text/event-stream", None))[1])
    try:
        h = bh.Hermes(srv2.url, key, "p", stream_timeout_s=0.3)
        t0 = time.monotonic()
        with pytest.raises(Exception):
            h.ask("x", 180, 1.0)
        assert time.monotonic() - t0 < 1.0
    finally:
        srv2.stop()


def test_defaults_and_attributes_equal_v1(key):
    h = bh.Hermes("http://x/", key, "p")
    assert (h.url, h.session_timeout_s, h.stream_timeout_s, h.title_prefix) == ("http://x", 15, 150, "Mira wall")
    assert (h.fail_reply, h.empty_reply) == ("I cannot reach Hermes right now.", "I have nothing to add.")
    assert h.session_id is None and h.last_used == 0.0


def _with_persona(text="You are Mira. Be brief."):
    ctx = H.make_ctx()
    ctx.paths.here.joinpath("MIRA_WALL_IDENTITY.md").write_text(text)
    H.started("persona.file", ctx=ctx)
    return ctx


def test_plugin_takes_the_persona_text_and_provides_brain(key):
    ctx = _with_persona()
    plugin, ctx, _ = H.started("brain.hermes", ctx=ctx, key_file=key, fail_reply="Hermes is away.", session_ttl=90.0)
    h = ctx.get("brain")
    assert h.persona == "You are Mira. Be brief." and h.fail_reply == "Hermes is away." and h.session_ttl == 90.0
    assert h.empty_reply == "I have nothing to add."


def test_live_knobs_apply(key):
    ctx = _with_persona()
    plugin, ctx, _ = H.started("brain.hermes", ctx=ctx)
    h = ctx.get("brain")
    H.assert_live_knobs(SPEC, plugin, {
        "url": ("http://127.0.0.1:9/", lambda p: h.url, "http://127.0.0.1:9"), "key_file": ("/k", lambda p: h.key_file),
        "session_ttl": (300.0, lambda p: h.session_ttl), "session_timeout_s": (20.0, lambda p: h.session_timeout_s),
        "stream_timeout_s": (200.0, lambda p: h.stream_timeout_s), "title_prefix": ("T", lambda p: h.title_prefix),
        "fail_reply": ("nope", lambda p: h.fail_reply), "empty_reply": ("none", lambda p: h.empty_reply)})


def test_needs_persona_and_validate_loopback():
    assert SPEC.needs == ("persona",)
    values = {k.name: k.default for k in SPEC.knobs}
    SPEC.validate(values)
    with pytest.raises(ConfigError) as e:
        SPEC.validate(dict(values, url="http://100.77.98.89:8642"))
    assert e.value.field == "url"


def test_agent_policy_is_session_ttl_only():
    k = {k.name: k for k in SPEC.knobs}
    assert [n for n, x in k.items() if x.agent == "bounded"] == ["session_ttl"]
    assert (k["session_ttl"].agent_min, k["session_ttl"].agent_max, k["session_ttl"].agent_step) == (60, 600, 60)
