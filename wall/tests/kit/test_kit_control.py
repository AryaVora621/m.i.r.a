"""Control API tests (P6): every route with owner, agent and no token, error codes, shapes, proxy, secrets.

The mini is a second FakeCore served by the real control server, and the Pi reaches it through the real
HttpRemote, so the wire path is exercised end to end on ephemeral loopback ports (never 8650, 8651, 8765).
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import control as C  # noqa: E402
from mirakit import plugin as P  # noqa: E402
from mirakit import testkit as TK  # noqa: E402

CONTRACTS = WALL / "docs" / "contracts"
OWNER_TOKEN = "ownerTOKEN-secret-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
AGENT_TOKEN = "agentTOKEN-secret-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
PI_TOKEN = "piTOKEN-secret-cccccccccccccccccccccccccccccccc"
SPEECH_OWNER_TOKEN = "speechOWNER-secret-dddddddddddddddddddddddddd"
ALL_SECRETS = (OWNER_TOKEN, AGENT_TOKEN, PI_TOKEN, SPEECH_OWNER_TOKEN)


# ---------------------------------------------------------------- mini JSON Schema validator (copy of P1's)
_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _is_type(value, name):
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def validate(instance, schema, root, path="$"):
    errs = []
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return validate(instance, node, root, path)
    if "anyOf" in schema:
        if not any(not validate(instance, s, root, path) for s in schema["anyOf"]):
            errs.append(f"{path}: matches none of anyOf")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(instance, t) for t in types):
            return errs + [f"{path}: {instance!r} is not of type {types}"]
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errs.append(f"{path}: missing required {req!r}")
        props = schema.get("properties", {})
        pats = schema.get("patternProperties", {})
        for key, val in instance.items():
            if key in props:
                errs += validate(val, props[key], root, f"{path}.{key}")
                continue
            hit = [s for p, s in pats.items() if re.search(p, key)]
            if hit:
                for s in hit:
                    errs += validate(val, s, root, f"{path}.{key}")
                continue
            extra = schema.get("additionalProperties", True)
            if extra is False:
                errs.append(f"{path}: unexpected key {key!r}")
            elif isinstance(extra, dict):
                errs += validate(val, extra, root, f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errs += validate(item, schema["items"], root, f"{path}[{i}]")
    return errs


@pytest.fixture(scope="module")
def api_schema():
    return json.loads((CONTRACTS / "control-api.schema.json").read_text())


def check(api_schema, name, instance):
    errs = validate(instance, {"$ref": f"#/$defs/{name}"}, api_schema)
    assert errs == [], errs


# ---------------------------------------------------------------- fixtures
def _noop(ctx, cfg):
    return P.PluginBase(ctx, cfg)


def listener_specs():
    agc = P.PluginSpec(
        name="agc.mic", kind="agc", version="1", summary="Mic gain", machine="listener", provides=("agc",),
        create=_noop, knobs=(
            P.Knob("target", "float", 0.05, "Speech level the gain aims for.", unit="rms", min=0.005, max=0.3,
                   agent="bounded", agent_min=0.03, agent_max=0.10, agent_step=0.01, topic="hearing",
                   spoken="microphone boost", effect="hears quiet speech", phrases=("you can't hear me",)),
            P.Knob("rest", "float", 3.0, "Gain in a quiet room.", unit="x", min=1, max=40),
            P.Knob("floor_init", "float", 0.003, "Starting noise floor.", unit="rms", min=0, max=0.1,
                   applies="restart"),
        ))
    wake = P.PluginSpec(
        name="wake.kws", kind="wake", version="1", summary="Wake word", machine="listener", provides=("wake",),
        create=_noop, knobs=(
            P.Knob("threshold", "float", 0.15, "Spotter sensitivity.", unit="score", min=0.01, max=0.5,
                   applies="rebuild", topic="wake", spoken="wake sensitivity"),
        ))
    core = P.PluginSpec(
        name="loop.turns", kind="loop", version="1", summary="Turn loop", machine="listener", provides=("loop",),
        create=_noop, core=True, knobs=(
            P.Knob("followup_s", "float", 8.0, "Follow-up window.", unit="s", min=0, max=30, agent="bounded",
                   agent_min=4, agent_max=15, agent_step=2, topic="timing", spoken="listening after answering"),
        ))
    return [agc, wake, core]


def speech_specs():
    tts = P.PluginSpec(
        name="tts.engine", kind="tts", version="1", summary="Voice", machine="speech", provides=("tts",),
        create=_noop, knobs=(
            P.Knob("voice", "voice", "amy", "Her voice.", agent="bounded", topic="voice", spoken="my voice"),
            P.Knob("speed", "float", 1.0, "How fast she talks.", unit="x", min=0.5, max=1.6, agent="bounded",
                   agent_min=0.8, agent_max=1.3, agent_step=0.1, topic="voice", spoken="how fast I talk",
                   effect="talks faster", phrases=("slow down",)),
            P.Knob("threads", "int", 4, "CPU threads.", unit="threads", min=1, max=8, applies="restart"),
        ))
    return [tts]


class Http:
    """Tiny client; one fresh connection per call unless keep=True."""

    def __init__(self, host, port):
        self.host, self.port = host, port

    def call(self, method, path, body=None, token=None, raw_body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = "Bearer " + token
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        text = resp.read()
        out = (resp.status, json.loads(text) if text else None, dict(resp.getheaders()))
        conn.close()
        return out


def make_cfg(tmp, **over):
    owner, agent = tmp / "owner.token", tmp / "agent.token"
    owner.write_text(OWNER_TOKEN + "\n")
    agent.write_text(AGENT_TOKEN + "\n")
    values = dict(bind="127.0.0.1", port=0, owner_token_file=str(owner), agent_token_file=str(agent),
                  speech_url="", speech_token_file="", agents_locked=False, agent_requires_voice_turn=True,
                  trial_default_s=600.0, trial_max_s=1800.0, wait_s_default=5.0)
    values.update(over)
    return P.PluginConfig("control.http", values)


@pytest.fixture
def logbuf():
    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    log = logging.getLogger("mira.test.control")
    log.setLevel(logging.DEBUG)
    handler = Capture()
    log.addHandler(handler)
    yield log, handler
    log.removeHandler(handler)


@pytest.fixture
def mini(tmp_path, logbuf):
    """The speech machine: a FakeCore behind the real server, accepting the Pi token as its agent scope."""
    log, _ = logbuf
    sdir = tmp_path / "speech"
    sdir.mkdir()
    (sdir / "owner").write_text(SPEECH_OWNER_TOKEN)
    (sdir / "pi").write_text(PI_TOKEN)
    cfg = P.PluginConfig("control.http", dict(bind="127.0.0.1", port=0, owner_token_file=str(sdir / "owner"),
                                              pi_token_file=str(sdir / "pi"), trial_default_s=600.0,
                                              trial_max_s=1800.0, wait_s_default=0.0))
    import time
    core = TK.FakeCore(speech_specs(), machine="speech", clock=time.time())
    handle = C.start_server(core, cfg, machine="speech", log=log)
    yield core, handle, sdir
    handle.stop()


@pytest.fixture
def pi(tmp_path, logbuf, mini):
    log, _ = logbuf
    score, shandle, sdir = mini
    import time
    core = TK.FakeCore(listener_specs(), clock=time.time())
    host, port = shandle.address
    cfg = make_cfg(tmp_path, speech_url=f"http://{host}:{port}", speech_token_file=str(sdir / "pi"))
    core.register_remote("speech", C.HttpRemote(f"http://{host}:{port}", str(sdir / "pi")))
    handle = C.start_server(core, cfg, machine="listener", log=log)
    host, port = handle.address
    yield core, Http(host, port), score
    handle.stop()


# ---------------------------------------------------------------- auth and transport
def test_health_needs_no_token_and_other_routes_do(pi, api_schema):
    core, http, _ = pi
    status, body, _ = http.call("GET", "/v1/health")
    assert status == 200
    check(api_schema, "Health", body)
    assert body["machine"] == "listener"
    for path in ("/v1/status", "/v1/inventory", "/v1/schema", "/v1/config", "/v1/session", "/v1/changes"):
        status, body, hdrs = http.call("GET", path)
        assert status == 401 and body["error"] == "unauthorized", path
        assert "revision" in body
        assert hdrs.get("Connection", "").lower() == "close"
    status, body, _ = http.call("GET", "/v1/status", token="not-a-token")
    assert status == 401


ROUTE_TABLE = [
    ("GET", "/v1/status", None), ("GET", "/v1/inventory", None), ("GET", "/v1/inventory?machine=speech", None),
    ("GET", "/v1/plugins/agc.mic", None), ("GET", "/v1/schema", None), ("GET", "/v1/config", None),
    ("GET", "/v1/config?effective=1", None), ("GET", "/v1/tuning_guide?topic=hearing", None),
    ("GET", "/v1/changes", None), ("GET", "/v1/session", None), ("GET", "/v1/voices", None),
    ("PUT", "/v1/config/agc.mic/target", {"value": 0.06}),
    ("POST", "/v1/config/batch", {"edits": [{"key": "agc.mic.target", "value": 0.06}]}),
    ("POST", "/v1/config/reload", {}), ("POST", "/v1/undo", {}), ("POST", "/v1/confirm", {}),
    ("POST", "/v1/session", {"action": "start"}), ("POST", "/v1/plugins/agc.mic/enable", {}),
    ("POST", "/v1/plugins/agc.mic/disable", {}), ("POST", "/v1/plugins/agc.mic/reload", {}),
    ("POST", "/v1/restart", {}), ("POST", "/v1/preview", {}),
]


@pytest.mark.parametrize("method,path,body", ROUTE_TABLE, ids=[f"{m} {p}" for m, p, _ in ROUTE_TABLE])
def test_every_route_refuses_no_token_and_a_wrong_token_and_changes_nothing(pi, method, path, body):
    core, http, score = pi
    revision, mini_revision = core.revision, score.revision
    for token in (None, "wrong-token-value"):
        status, out, hdrs = http.call(method, path, body, token=token)
        assert status == 401 and out["error"] == "unauthorized" and "revision" in out, (token, status)
        assert hdrs.get("Connection", "").lower() == "close"
    assert core.revision == revision and score.revision == mini_revision and not core.restart_requested


@pytest.mark.parametrize("method,path,body", ROUTE_TABLE, ids=[f"{m} {p}" for m, p, _ in ROUTE_TABLE])
def test_every_route_answers_json_with_the_owner_and_agent_token(pi, method, path, body):
    core, http, score = pi
    for token in (OWNER_TOKEN, AGENT_TOKEN):
        status, out, hdrs = http.call(method, path, body, token=token)
        assert status in (200, 202, 400, 403, 423, 429, 502), (token, status, out)       # an answer, never a crash
        assert status != 500 and hdrs.get("Content-Type") == "application/json"
        if isinstance(out, dict) and status >= 400:
            assert {"error", "message", "revision"} <= set(out)


def test_unauthenticated_body_is_not_read(pi):
    core, http, _ = pi
    conn = socket.create_connection((http.host, http.port), timeout=5)
    # a body is announced but never sent: an implementation that waits for it would hang until the timeout
    conn.sendall(b"POST /v1/restart HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")
    data = conn.recv(4096)
    conn.close()
    assert data.startswith(b"HTTP/1.1 401")
    assert b"close" in data.lower()


def test_oversized_body_is_refused_and_closed(pi):
    core, http, _ = pi
    status, body, hdrs = http.call("POST", "/v1/config/batch", raw_body=b"x" * (C.MAX_BODY + 10), token=OWNER_TOKEN,
                                   headers={"Content-Type": "application/json"})
    assert status == 400 and body["error"] == "invalid"
    assert hdrs.get("Connection", "").lower() == "close"


def test_bad_json_and_non_object_body(pi):
    core, http, _ = pi
    assert http.call("PUT", "/v1/config/agc.mic/target", raw_body=b"{nope", token=OWNER_TOKEN)[0] == 400
    assert http.call("PUT", "/v1/config/agc.mic/target", raw_body=b"[1]", token=OWNER_TOKEN)[0] == 400


def test_scope_comes_from_the_file_that_matched(pi):
    core, http, _ = pi
    # the agent token may not touch an owner-only route; the owner token may
    status, body, _ = http.call("POST", "/v1/restart", {}, token=AGENT_TOKEN)
    assert status == 403 and body["error"] == "policy"
    status, body, _ = http.call("POST", "/v1/restart", {}, token=OWNER_TOKEN)
    assert status == 200 and body["ok"] is True and core.restart_requested


def test_token_files_are_reread_per_request(pi, tmp_path):
    core, http, _ = pi
    assert http.call("GET", "/v1/status", token=OWNER_TOKEN)[0] == 200
    (tmp_path / "owner.token").write_text("rotated-owner-token-xxxxxxxxxxxxxxxxxxxxxxxx")
    assert http.call("GET", "/v1/status", token=OWNER_TOKEN)[0] == 401
    assert http.call("GET", "/v1/status", token="rotated-owner-token-xxxxxxxxxxxxxxxxxxxxxxxx")[0] == 200


def test_same_secret_in_both_files_gets_the_lower_scope(tmp_path):
    (tmp_path / "a").write_text("same-secret-1234567890")
    cfg = P.PluginConfig("control.http", dict(owner_token_file=str(tmp_path / "a"),
                                              agent_token_file=str(tmp_path / "a")))
    assert C.authenticate(cfg, "Bearer same-secret-1234567890") == "agent"
    assert C.authenticate(cfg, "bearer same-secret-1234567890") == "agent"
    assert C.authenticate(cfg, "Basic same-secret-1234567890") is None
    assert C.authenticate(cfg, "") is None
    assert C.authenticate(cfg, "Bearer ") is None


def test_missing_token_files_deny_everything(tmp_path):
    cfg = P.PluginConfig("control.http", dict(owner_token_file=str(tmp_path / "none"),
                                              agent_token_file=str(tmp_path / "none2")))
    assert C.authenticate(cfg, "Bearer anything") is None


def test_bind_rules(tmp_path, logbuf):
    log, _ = logbuf
    core = TK.FakeCore(listener_specs())
    cfg = make_cfg(tmp_path)
    for bad in ("0.0.0.0", "192.168.1.5", "100.77.98.89", "::", "example.com"):
        with pytest.raises(ValueError):
            C.start_server(core, cfg, machine="listener", log=log, bind=[bad])
    with pytest.raises(ValueError):
        C.start_server(core, cfg, machine="speech", log=log, bind=["0.0.0.0"])
    handle = C.start_server(core, cfg, machine="listener", log=log, bind=["127.0.0.1"])
    try:
        assert handle.address[0] == "127.0.0.1" and handle.address[1] not in (0, 8650, 8651, 8765)
    finally:
        handle.stop()
        handle.stop()    # idempotent


def test_stop_releases_the_port(tmp_path, logbuf):
    log, _ = logbuf
    core = TK.FakeCore(listener_specs())
    handle = C.start_server(core, make_cfg(tmp_path), machine="listener", log=log)
    host, port = handle.address
    handle.stop()
    with pytest.raises(OSError):
        socket.create_connection((host, port), timeout=1).close()


# ---------------------------------------------------------------- routes, owner and agent
def test_reads_for_owner_and_agent_validate_against_the_schema(pi, api_schema):
    core, http, _ = pi
    for token in (OWNER_TOKEN, AGENT_TOKEN):
        status, body, _ = http.call("GET", "/v1/status", token=token)
        assert status == 200
        check(api_schema, "Status", body)
        status, rows, _ = http.call("GET", "/v1/inventory", token=token)
        assert status == 200 and isinstance(rows, list)
        for row in rows:
            check(api_schema, "InventoryRow", row)
        status, body, _ = http.call("GET", "/v1/plugins/agc.mic", token=token)
        assert status == 200 and "revision" in body and body["spec"]["name"] == "agc.mic"
        for knob in body["knobs"]:
            check(api_schema, "KnobRow", knob)
        status, body, _ = http.call("GET", "/v1/schema", token=token)
        assert status == 200 and body["machine"] == "listener" and "revision" in body
        status, body, _ = http.call("GET", "/v1/config?effective=1", token=token)
        assert status == 200 and body["plugins"]["agc.mic"]["config"]["target"]["source"]
        status, body, _ = http.call("GET", "/v1/tuning_guide?topic=hearing", token=token)
        assert status == 200
        check(api_schema, "TuningGuide", body)
        status, body, _ = http.call("GET", "/v1/changes", token=token)
        assert status == 200 and body == []
        status, body, _ = http.call("GET", "/v1/session", token=token)
        assert status == 200 and body is None


def test_agent_scope_sees_agent_bounds(pi):
    core, http, _ = pi
    _, owner_view, _ = http.call("GET", "/v1/plugins/agc.mic", token=OWNER_TOKEN)
    _, agent_view, _ = http.call("GET", "/v1/plugins/agc.mic", token=AGENT_TOKEN)
    o = next(k for k in owner_view["knobs"] if k["key"] == "target")
    a = next(k for k in agent_view["knobs"] if k["key"] == "target")
    assert (o["min"], o["max"]) == (0.005, 0.3) and (a["min"], a["max"]) == (0.03, 0.10)


def test_owner_write_roundtrip_with_revision_and_arrays(pi, api_schema):
    core, http, _ = pi
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/target",
                                {"value": 0.06, "reason": "test"}, token=OWNER_TOKEN)
    assert status == 200
    check(api_schema, "Change", body)
    assert body["edits"] == [{"key": "agc.mic.target", "old": 0.05, "new": 0.06}] and body["actor"] == "owner"
    assert body["revision"] == body["rev_after"]
    status, rows, _ = http.call("GET", "/v1/changes", token=OWNER_TOKEN)
    assert isinstance(rows, list) and rows[0]["id"] == body["id"]      # bare array: no revision key
    status, undone, _ = http.call("POST", "/v1/undo", {"change_id": body["id"]}, token=OWNER_TOKEN)
    assert status == 200 and isinstance(undone, list) and undone[0]["undo_of"] == body["id"]
    assert core.values["agc.mic.target"] == 0.05


def test_dry_run_and_stale_revision(pi):
    core, http, _ = pi
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.07, "dry_run": True},
                                token=OWNER_TOKEN)
    assert status == 200 and body["status"] == "dry_run" and core.values["agc.mic.target"] == 0.05
    assert body["revision"] == core.revision                  # a dry run did not move it
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.07, "expect_revision": 999},
                                token=OWNER_TOKEN)
    assert status == 409 and body["error"] == "stale_revision" and body["current"] == core.revision


def test_batch_is_one_change_and_validates_input(pi):
    core, http, _ = pi
    status, body, _ = http.call("POST", "/v1/config/batch", {
        "edits": [{"key": "agc.mic.target", "value": 0.06}, {"key": "agc.mic.rest", "value": 4.0}],
        "reason": "two"}, token=OWNER_TOKEN)
    assert status == 200 and len(body["edits"]) == 2
    for bad in ({}, {"edits": []}, {"edits": "x"}, {"edits": [{"key": "agc.mic.target"}]},
                {"edits": [1]}):
        assert http.call("POST", "/v1/config/batch", bad, token=OWNER_TOKEN)[0] == 400
    assert http.call("PUT", "/v1/config/agc.mic/target", {}, token=OWNER_TOKEN)[0] == 400
    assert http.call("PUT", "/v1/config/agc.mic", {"value": 1}, token=OWNER_TOKEN)[0] == 400


def test_numeric_request_fields_are_type_checked(pi):
    core, http, _ = pi
    for field, value in (("trial_s", "ten"), ("expect_revision", 1.5), ("wait_s", True)):
        status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06, field: value},
                                    token=OWNER_TOKEN)
        assert status == 400 and body["field"] == field


def test_wait_s_default_comes_from_the_knob(tmp_path, logbuf):
    log, _ = logbuf

    class Spy(TK.FakeCore):
        seen = None

        def submit(self, edits, **kw):
            Spy.seen = kw
            return super().submit(edits, **kw)

    core = Spy(listener_specs())
    handle = C.start_server(core, make_cfg(tmp_path, wait_s_default=7.0), machine="listener", log=log)
    try:
        http = Http(*handle.address)
        http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06}, token=OWNER_TOKEN)
        assert Spy.seen["wait_s"] == 7.0
        http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.07, "wait_s": 99}, token=OWNER_TOKEN)
        assert Spy.seen["wait_s"] == 30.0                      # clamped
        http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.08, "reason": "x" * 500}, token=OWNER_TOKEN)
        assert len(Spy.seen["reason"]) == 120
    finally:
        handle.stop()


def test_queued_change_answers_202(tmp_path, logbuf):
    log, _ = logbuf

    class Queued(TK.FakeCore):
        def submit(self, edits, **kw):
            change = super().submit(edits, **kw)
            change.status = "queued"
            return change

    handle = C.start_server(Queued(listener_specs()), make_cfg(tmp_path), machine="listener", log=log)
    try:
        status, body, _ = Http(*handle.address).call("PUT", "/v1/config/agc.mic/target", {"value": 0.06},
                                                     token=OWNER_TOKEN)
        assert status == 202 and body["status"] == "queued"
    finally:
        handle.stop()


def test_restart_class_edit_reports_pending_restart(pi):
    core, http, _ = pi
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/floor_init", {"value": 0.004}, token=OWNER_TOKEN)
    assert status == 200 and body["status"] == "pending_restart" and body["applies"] == "restart"
    _, st, _ = http.call("GET", "/v1/status", token=OWNER_TOKEN)
    assert "agc.mic.floor_init" in st["pending_restart"]


def test_agent_policy_errors_map_to_http(pi):
    core, http, _ = pi
    # deny knob: 403 policy with an owner command
    status, body, _ = http.call("PUT", "/v1/config/wake.kws/threshold", {"value": 0.06}, token=AGENT_TOKEN)
    assert status == 403 and body["error"] == "policy" and body["owner_command"].startswith("mira set wake.kws.threshold")
    # out of range: allowed range given
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.2}, token=AGENT_TOKEN)
    assert status == 403 and body["allowed"] == {"min": 0.03, "max": 0.10, "step": 0.01}
    # outside a voice turn
    core.voice_turn = False
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06}, token=AGENT_TOKEN)
    assert status == 403 and body["error"] == "not_in_voice_turn"
    core.voice_turn = True
    core.agents_locked = True
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06}, token=AGENT_TOKEN)
    assert status == 423 and body["error"] == "agents_locked"
    core.agents_locked = False
    # reads still work while locked
    core.agents_locked = True
    assert http.call("GET", "/v1/status", token=AGENT_TOKEN)[0] == 200


def test_unknown_key_plugin_and_bad_value_are_400(pi):
    core, http, _ = pi
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/targt", {"value": 0.06}, token=OWNER_TOKEN)
    assert status == 400 and body["error"] == "unknown_key" and "target" in body["message"]
    status, body, _ = http.call("GET", "/v1/plugins/agc.mik", token=OWNER_TOKEN)
    assert status == 400 and body["error"] in ("unknown_plugin",)
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": "loud"}, token=OWNER_TOKEN)
    assert status == 400 and body["error"] == "invalid"
    status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 9.0}, token=OWNER_TOKEN)
    assert status == 400 and body["error"] == "invalid"


def test_session_flow_over_http(pi, api_schema):
    core, http, _ = pi
    status, sess, _ = http.call("POST", "/v1/session", {"action": "start", "topic": "hearing"}, token=AGENT_TOKEN)
    assert status == 200
    check(api_schema, "Session", sess)
    assert http.call("GET", "/v1/session", token=OWNER_TOKEN)[1]["id"] == sess["id"]
    http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06}, token=AGENT_TOKEN)
    status, closed, _ = http.call("POST", "/v1/session", {"action": "revert"}, token=AGENT_TOKEN)
    assert status == 200 and closed["state"] == "reverted" and core.values["agc.mic.target"] == 0.05
    assert http.call("POST", "/v1/session", {"action": "bogus"}, token=AGENT_TOKEN)[0] == 400
    # no session open: keep answers null (a bare null carries no revision)
    status, body, _ = http.call("POST", "/v1/session", {"action": "end"}, token=AGENT_TOKEN)
    assert status == 200 and body is None


def test_confirm_and_undo_by_agent(pi):
    core, http, _ = pi
    status, ch, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06}, token=AGENT_TOKEN)
    assert status == 200 and ch["trial_expires"]
    status, out, _ = http.call("POST", "/v1/confirm", {"change_id": ch["id"]}, token=AGENT_TOKEN)
    assert status == 200 and out[0]["status"] == "confirmed"
    status, out, _ = http.call("POST", "/v1/undo", {"change_id": ch["id"], "force": True}, token=AGENT_TOKEN)
    assert status in (200, 403, 409)       # an agent may never force; the core decides the rest
    assert http.call("POST", "/v1/undo", {"change_id": 5}, token=AGENT_TOKEN)[0] == 400


def test_owner_only_routes(pi):
    core, http, _ = pi
    for path in ("/v1/config/reload", "/v1/restart", "/v1/plugins/agc.mic/enable", "/v1/plugins/agc.mic/disable",
                 "/v1/plugins/agc.mic/reload"):
        status, body, _ = http.call("POST", path, {}, token=AGENT_TOKEN)
        assert status == 403 and body["error"] == "policy", path
        status, body, _ = http.call("POST", path, {}, token=OWNER_TOKEN)
        assert status == 200 and body["revision"] >= 1, path
    assert core.reloads == ["<file>", "agc.mic"]
    assert core.enabled["agc.mic"] is False
    status, body, _ = http.call("POST", "/v1/plugins/nope/enable", {}, token=OWNER_TOKEN)
    assert status == 400 and body["error"] == "unknown_plugin"


def test_preview_route(pi):
    core, http, _ = pi
    status, body, _ = http.call("POST", "/v1/preview", {"text": "hello", "voice": "jenny", "speed": 1.1},
                                token=OWNER_TOKEN)
    assert status == 200 and body["played"] is True and "revision" in body
    assert core.previews[-1]["speed"] == 1.1 and core.previews[-1]["voice"] == "jenny"
    assert http.call("POST", "/v1/preview", {"text": 5}, token=OWNER_TOKEN)[0] == 400
    core.voice_turn = False
    assert http.call("POST", "/v1/preview", {}, token=AGENT_TOKEN)[1]["error"] == "not_in_voice_turn"


def test_unknown_routes_and_methods(pi):
    core, http, _ = pi
    assert http.call("GET", "/v1/nope", token=OWNER_TOKEN)[0] == 404
    assert http.call("GET", "/nope", token=OWNER_TOKEN)[0] == 404
    assert http.call("POST", "/v1/status", {}, token=OWNER_TOKEN)[0] == 404
    assert http.call("GET", "/v1/tuning_guide", token=OWNER_TOKEN)[0] == 400   # topic required
    assert http.call("GET", "/v1/changes?limit=abc", token=OWNER_TOKEN)[0] == 400
    # the /ctl prefix is accepted on every server
    assert http.call("GET", "/ctl/v1/status", token=OWNER_TOKEN)[0] == 200


# ---------------------------------------------------------------- error table
@pytest.mark.parametrize("exc,status,code", [
    (P.ConfigError("m", "f", "invalid"), 400, "invalid"),
    (P.ConfigError("m", "f", "unknown_key"), 400, "unknown_key"),
    (P.ConfigError("m", "f", "unknown_plugin"), 400, "unknown_plugin"),
    (P.ConfigError("m", "f", "unknown_voice"), 400, "unknown_voice"),
    (P.ConfigError("m", "f", "provider_conflict"), 409, "provider_conflict"),
    (P.ConfigError("m", "f", "missing_service"), 400, "invalid"),
    (P.PolicyError("policy", "m"), 403, "policy"),
    (P.PolicyError("not_in_voice_turn", "m"), 403, "not_in_voice_turn"),
    (P.PolicyError("quiet_hours", "m"), 403, "quiet_hours"),
    (P.PolicyError("session_cap", "m"), 403, "session_cap"),
    (P.PolicyError("agents_locked", "m"), 423, "agents_locked"),
    (P.PolicyError("rate_limited", "m"), 429, "rate_limited"),
    (P.PolicyError("busy", "m"), 429, "busy"),
    (P.ConflictError("stale_revision", "m", revision=3), 409, "stale_revision"),
    (P.ConflictError("undo_conflict", "m", current=1), 409, "undo_conflict"),
    (P.ConflictError("file_changed", "m"), 409, "file_changed"),
    (P.ConflictError("provider_conflict", "m"), 409, "provider_conflict"),
    (C.RemoteError("speech_unreachable", "x", 502, connection=True), 502, "speech_unreachable"),
    (C.RemoteError("policy", "no", 403, {"error": "policy", "message": "no", "revision": 9}), 403, "policy"),
    (RuntimeError("the owner said hunter2"), 500, "internal"),
])
def test_error_table(api_schema, exc, status, code):
    got_status, body = C.error_response(exc, 7, logging.getLogger("t"))
    assert (got_status, body["error"]) == (status, code)
    assert "revision" in body
    check(api_schema, "Error", body)
    assert "hunter2" not in json.dumps(body)


def test_unknown_voice_carries_near_and_effective():
    exc = P.ConfigError("no such voice", "voice", "unknown_voice")
    exc.near = ["jenny", "amy", "kristin", "extra"]
    exc.effective = "vits-piper-en_US-amy-medium#0"
    status, body = C.error_response(exc, 1)
    assert body["near"] == ["jenny", "amy", "kristin"] and body["effective"].endswith("#0")


def test_internal_error_hides_request_text_and_logs_only_the_class(tmp_path, logbuf):
    log, cap = logbuf

    class Boom(TK.FakeCore):
        def submit(self, *a, **kw):
            raise RuntimeError("SECRET-USER-TEXT " + OWNER_TOKEN)

    handle = C.start_server(Boom(listener_specs()), make_cfg(tmp_path), machine="listener", log=log)
    try:
        status, body, _ = Http(*handle.address).call("PUT", "/v1/config/agc.mic/target", {"value": 0.06},
                                                      token=OWNER_TOKEN)
    finally:
        handle.stop()
    assert status == 500 and body["error"] == "internal"
    blob = json.dumps(body) + "\n".join(cap.lines)
    assert "SECRET-USER-TEXT" not in blob and OWNER_TOKEN not in blob


# ---------------------------------------------------------------- no secret anywhere
def test_no_token_in_any_response_or_log_line(pi, logbuf):
    core, http, score = pi
    log, cap = logbuf
    blob = []
    calls = [("GET", "/v1/status"), ("GET", "/v1/inventory"), ("GET", "/v1/config?effective=1"),
             ("GET", "/v1/plugins/agc.mic"), ("GET", "/v1/schema"), ("GET", "/v1/changes?machine=all"),
             ("PUT", "/v1/config/agc.mic/target"), ("PUT", "/v1/config/tts.engine/speed"),
             ("GET", "/v1/inventory?machine=speech"), ("GET", "/v1/config?machine=speech")]
    for token in (OWNER_TOKEN, AGENT_TOKEN, "wrong-" + OWNER_TOKEN):
        for method, path in calls:
            body = {"value": 0.06 if "target" in path else 1.1, "reason": "x"} if method == "PUT" else None
            status, out, hdrs = http.call(method, path, body, token=token)
            blob.append(json.dumps(out) + json.dumps(hdrs))
    text = "\n".join(blob) + "\n".join(cap.lines)
    for secret in ALL_SECRETS:
        assert secret not in text
    assert "Bearer" not in text
    assert any("GET /v1/status" in line for line in cap.lines)       # request lines are logged
    assert not any(re.search(r"\{.*value", line) for line in cap.lines)  # never a body


# ---------------------------------------------------------------- Pi proxy
def test_inventory_config_changes_voices_proxy_to_the_mini(pi, api_schema):
    core, http, score = pi
    status, rows, _ = http.call("GET", "/v1/inventory?machine=speech", token=OWNER_TOKEN)
    assert status == 200 and [r["name"] for r in rows] == ["tts.engine"] and rows[0]["machine"] == "speech"
    status, cfg, _ = http.call("GET", "/v1/config?machine=speech&effective=1", token=AGENT_TOKEN)
    assert status == 200 and "tts.engine" in cfg["plugins"]
    status, desc, _ = http.call("GET", "/v1/plugins/tts.engine", token=AGENT_TOKEN)
    assert status == 200 and desc["spec"]["machine"] == "speech"         # unknown here, found on the mini
    status, sch, _ = http.call("GET", "/v1/schema?machine=speech", token=AGENT_TOKEN)
    assert status == 200 and sch["machine"] == "speech"
    status, rows, _ = http.call("GET", "/v1/inventory?machine=all", token=OWNER_TOKEN)
    assert {r["machine"] for r in rows} == {"listener", "speech"}
    # the mini has no voice catalog service in a FakeCore: its answer is passed through unchanged
    status, body, _ = http.call("GET", "/v1/voices", token=AGENT_TOKEN)
    assert status == 400 and body["error"] == "invalid"


def test_changes_merge_when_machine_is_all(pi):
    core, http, score = pi
    http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06}, token=OWNER_TOKEN)
    score.advance(5)
    http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1}, token=OWNER_TOKEN)
    status, rows, _ = http.call("GET", "/v1/changes?machine=all&limit=10", token=OWNER_TOKEN)
    assert {r["machine"] for r in rows} == {"listener", "speech"}
    assert rows == sorted(rows, key=lambda c: (c["ts"], c["id"]), reverse=True)
    status, rows, _ = http.call("GET", "/v1/changes?machine=speech", token=OWNER_TOKEN)
    assert {r["machine"] for r in rows} == {"speech"}


def test_agent_write_to_the_mini_is_checked_on_the_pi_then_forwarded(pi):
    core, http, score = pi
    seen = {}
    real = score.submit

    def spy(edits, **kw):
        seen.update(kw)
        return real(edits, **kw)

    score.submit = spy
    status, ch, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1, "reason": "talks slower"},
                              token=AGENT_TOKEN)
    assert status == 200 and ch["machine"] == "speech" and ch["edits"][0]["new"] == 1.1
    assert seen["actor"] == "hermes" and seen["scope"] == "agent"
    assert seen["session"] and re.fullmatch(r"V-\d+", seen["session"])
    # the mini trial outlives the session (2.5.4): default 600 plus the seconds left, capped at trial_max_s
    assert 600 < seen["trial_s"] <= 1800
    # a session was opened on the Pi and the mini change attached to it
    assert core.remote_changes and core.remote_changes[0]["id"] == ch["id"]
    assert core.session("get", actor="x", scope="owner")["changes"] == [ch["id"]]
    # then the Pi can confirm the speech change by its S- id through the same API
    status, out, _ = http.call("POST", "/v1/confirm", {"change_id": ch["id"]}, token=AGENT_TOKEN)
    assert status == 200 and out[0]["status"] == "confirmed"


def test_agent_dry_run_of_a_speech_write_leaves_no_state(pi):
    core, http, score = pi
    seen = []
    real = score.submit
    score.submit = lambda edits, **kw: (seen.append(dict(kw)), real(edits, **kw))[1]
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1, "dry_run": True},
                                token=AGENT_TOKEN)
    assert status == 200 and body["status"] == "dry_run"
    assert core.session("get", actor="x", scope="owner") is None
    assert not core.remote_changes and score.values["tts.engine.speed"] == 1.0
    assert seen and not seen[0].get("session") and seen[0].get("trial_s") is None


def test_owner_speech_write_is_not_a_session_trial_unless_asked(pi):
    core, http, score = pi
    status, _, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1}, token=AGENT_TOKEN)
    assert status == 200
    sid = core.session("get", actor="x", scope="owner")["id"]
    seen = []
    real = score.submit
    score.submit = lambda edits, **kw: (seen.append(dict(kw)), real(edits, **kw))[1]
    n = len(core.remote_changes)
    status, ch, _ = http.call("PUT", "/v1/config/tts.engine/voice", {"value": "amy"}, token=OWNER_TOKEN)
    assert status == 200
    assert not seen[0].get("session") and seen[0].get("trial_s") is None
    assert len(core.remote_changes) == n and ch["id"] not in core.session("get", actor="x", scope="owner")["changes"]
    assert core.session("get", actor="x", scope="owner")["id"] == sid


def test_pi_refuses_before_forwarding(pi):
    core, http, score = pi
    calls = []
    real = score.submit
    score.submit = lambda *a, **kw: (calls.append(1), real(*a, **kw))[1]
    # deny knob on the mini
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/threads", {"value": 2}, token=AGENT_TOKEN)
    assert status == 403 and body["error"] == "policy" and body["owner_command"].startswith("mira set tts.engine.threads=2")
    # out of the agent range
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 2.0}, token=AGENT_TOKEN)
    assert status == 403 and body["allowed"]["max"] == 1.3
    # more than one step
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.3}, token=AGENT_TOKEN)
    assert status == 403 and body["allowed"]["step"] == 0.1
    # unknown knob
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/pitch", {"value": 1}, token=AGENT_TOKEN)
    assert status == 400 and body["error"] == "unknown_key"
    # not in a voice turn, then locked
    core.voice_turn = False
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1}, token=AGENT_TOKEN)
    assert status == 403 and body["error"] == "not_in_voice_turn"
    core.voice_turn, core.agents_locked = True, True
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1}, token=AGENT_TOKEN)
    assert status == 423
    assert calls == [] and score.values["tts.engine.speed"] == 1.0       # nothing reached the mini


def test_owner_write_to_the_mini_goes_with_the_pi_token_so_the_mini_applies_agent_policy(pi):
    """The Pi token is the mini's agent scope (2.7.1). An owner request through the Pi is therefore bounded by
    the agent rules; owner-only speech knobs are changed on the mini with its own owner token."""
    core, http, score = pi
    status, ch, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1}, token=OWNER_TOKEN)
    assert status == 200 and score.values["tts.engine.speed"] == 1.1 and ch["actor"] == "owner"
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/threads", {"value": 2}, token=OWNER_TOKEN)
    assert status == 403 and body["error"] == "policy" and score.values["tts.engine.threads"] == 4
    status, ch, _ = http.call("POST", "/v1/config/batch", {"edits": [
        {"key": "tts.engine.speed", "value": 1.0}, {"key": "tts.engine.voice", "value": "jenny"}]},
        token=OWNER_TOKEN)
    assert status == 200 and score.values["tts.engine.voice"] == "jenny"
    # one batch may not span machines
    status, body, _ = http.call("POST", "/v1/config/batch", {"edits": [
        {"key": "tts.engine.speed", "value": 1.0}, {"key": "agc.mic.target", "value": 0.06}]}, token=OWNER_TOKEN)
    assert status == 400 and body["error"] == "invalid"
    assert core.values["agc.mic.target"] == 0.05


def test_mini_errors_pass_through_with_their_own_status(pi):
    core, http, score = pi
    score.agents_locked = True      # the mini says no even though the Pi checks passed
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1}, token=AGENT_TOKEN)
    assert status == 423 and body["error"] == "agents_locked"
    score.agents_locked = False
    status, body, _ = http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1, "expect_revision": 99},
                                token=OWNER_TOKEN)
    assert status == 409 and body["error"] == "stale_revision"


def test_speech_unreachable_is_502_and_nothing_changes(tmp_path, logbuf):
    log, _ = logbuf
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    (tmp_path / "pi").write_text(PI_TOKEN)
    core = TK.FakeCore(listener_specs())
    core.register_remote("speech", C.HttpRemote(f"http://127.0.0.1:{dead_port}", str(tmp_path / "pi"), 2.0))
    handle = C.start_server(core, make_cfg(tmp_path), machine="listener", log=log)
    try:
        http = Http(*handle.address)
        for method, path in (("GET", "/v1/inventory?machine=speech"), ("GET", "/v1/voices"),
                             ("PUT", "/v1/config/tts.engine/speed")):
            status, body, _ = http.call(method, path, {"value": 1.1} if method == "PUT" else None,
                                        token=OWNER_TOKEN)
            assert status == 502 and body["error"] == "speech_unreachable", path
        # local routes keep working, and machine=all degrades to the local half
        assert http.call("GET", "/v1/inventory?machine=all", token=OWNER_TOKEN)[0] == 200
        assert http.call("GET", "/v1/changes?machine=all", token=OWNER_TOKEN)[0] == 200
        assert http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06}, token=OWNER_TOKEN)[0] == 200
    finally:
        handle.stop()


def test_only_allow_listed_paths_are_forwarded(pi):
    core, http, score = pi
    # restart, enable and preview never leave the Pi even with machine=speech
    for method, path in (("POST", "/v1/restart"), ("POST", "/v1/plugins/tts.engine/enable"),
                         ("POST", "/v1/config/reload")):
        status, body, _ = http.call(method, path + "?machine=speech", {}, token=OWNER_TOKEN)
        assert (method, path) and not score.restart_requested
        assert score.reloads == [] and score.enabled["tts.engine"] is True


def test_schema_cache_refreshes_when_the_mini_revision_moves(pi):
    core, http, score = pi
    http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.1}, token=OWNER_TOKEN)
    # observe the effect through the wire: a new plugin appearing on the mini after a write is visible to the next write on the Pi.
    extra = P.PluginSpec(name="tts.piper", kind="tts", version="1", summary="p", machine="speech",
                         provides=(), create=_noop,
                         knobs=(P.Knob("length_scale", "float", 1.0, "Pace.", unit="x", min=0.5, max=2.0,
                                       agent="bounded", agent_min=0.8, agent_max=1.3, agent_step=0.2),))
    score.specs["tts.piper"] = extra
    score._knobs["tts.piper.length_scale"] = (extra, extra.knobs[0])
    score.values["tts.piper.length_scale"] = 1.0
    score.enabled["tts.piper"] = True
    http.call("PUT", "/v1/config/tts.engine/speed", {"value": 1.2}, token=OWNER_TOKEN)   # revision moved
    status, ch, _ = http.call("PUT", "/v1/config/tts.piper/length_scale", {"value": 1.1}, token=OWNER_TOKEN)
    assert status == 200 and ch["machine"] == "speech"


# ---------------------------------------------------------------- the mini side
def test_mini_routes_use_ctl_prefix_pi_token_and_body_actor(mini, api_schema):
    core, handle, sdir = mini
    http = Http(*handle.address)
    status, body, _ = http.call("GET", "/ctl/v1/status", token=PI_TOKEN)
    assert status == 200 and body["machine"] == "speech"
    assert http.call("GET", "/ctl/v1/status", token="speech-data-plane-token")[0] == 401   # data token not accepted
    status, ch, _ = http.call("PUT", "/ctl/v1/config/tts.engine/speed",
                              {"value": 1.1, "actor": "hermes", "session": "V-0007", "trial_s": 900},
                              token=PI_TOKEN)
    assert status == 200 and ch["actor"] == "hermes" and ch["scope"] == "agent"
    assert ch["id"].startswith("S-")
    status, ch, _ = http.call("PUT", "/ctl/v1/config/tts.engine/speed", {"value": 1.2, "actor": "BAD ACTOR!"},
                              token=PI_TOKEN)
    assert ch["actor"] == "pi"
    # the owner token on the mini is the owner scope
    status, ch, _ = http.call("PUT", "/ctl/v1/config/tts.engine/threads", {"value": 2}, token=SPEECH_OWNER_TOKEN)
    assert status == 200 and ch["scope"] == "owner" and ch["actor"] == "owner"
    status, body, _ = http.call("PUT", "/ctl/v1/config/tts.engine/threads", {"value": 2}, token=PI_TOKEN)
    assert status == 403


def test_handle_serves_the_speech_servers_own_handler(tmp_path):
    """P5's server.http calls handle(core, handler, path, scope) after its own token check."""
    (tmp_path / "pi").write_text(PI_TOKEN)
    (tmp_path / "ow").write_text(SPEECH_OWNER_TOKEN)
    cfg = P.PluginConfig("control.http", dict(owner_token_file=str(tmp_path / "ow"), pi_token_file=str(tmp_path / "pi")))
    core = TK.FakeCore(speech_specs(), machine="speech")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/ctl/v1/"):
                C.handle(core, self, self.path, C.authenticate(cfg, self.headers.get("Authorization", ""), "speech"))
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        do_PUT = do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        http = Http(*server.server_address[:2])
        assert http.call("GET", "/ctl/v1/status")[0] == 401
        status, body, _ = http.call("GET", "/ctl/v1/status", token=PI_TOKEN)
        assert status == 200 and body["machine"] == "speech"
        status, ch, _ = http.call("PUT", "/ctl/v1/config/tts.engine/speed", {"value": 1.1}, token=PI_TOKEN)
        assert status == 200 and ch["machine"] == "speech"
        assert http.call("GET", "/health")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_mini_voices_come_from_the_voices_service(tmp_path, logbuf):
    log, _ = logbuf
    entries = [
        {"id": "m#0", "alias": "amy", "label": "Amy", "lang": "en_US", "featured": True, "current": True,
         "speakers": 1, "speaker": "0", "engine": "piper", "model_dir": "m"},
        {"id": "k#af_heart", "alias": "heart", "label": "Heart", "lang": "en_US", "featured": False,
         "current": False, "speakers": 11, "speaker": "af_heart", "engine": "kokoro", "model_dir": "k"},
        {"id": "l#0", "alias": "libritts", "label": "LibriTTS", "lang": "en_US", "featured": False, "current": False,
         "speakers": 904, "speaker": "0", "engine": "piper", "model_dir": "l"},
        {"id": "j#0", "alias": "jenny", "label": "Jenny", "lang": "en_GB", "featured": True, "current": False,
         "speakers": 1, "speaker": "0", "engine": "piper", "model_dir": "j"},
        {"id": "f#0", "alias": "siwis", "label": "Siwis", "lang": "fr_FR", "featured": False, "current": False,
         "speakers": 1, "speaker": "0", "engine": "piper", "model_dir": "f"},
    ]

    class Catalog:
        def list(self, lang="", include_unusable=False):
            return entries

    class MiniCore(TK.FakeCore):
        def service(self, name, default=None):
            return Catalog() if name == "voices" else default

    handle = C.start_server(MiniCore(speech_specs(), machine="speech"), make_cfg(tmp_path, pi_token_file=""),
                            machine="speech", log=log)
    try:
        (tmp_path / "pi").write_text(PI_TOKEN)
        handle.set_config(make_cfg(tmp_path, pi_token_file=str(tmp_path / "pi")))
        http = Http(*handle.address)
        status, body, _ = http.call("GET", "/ctl/v1/voices?lang=en&limit=3", token=PI_TOKEN)
        assert status == 200 and body["current"]["alias"] == "amy"
        assert [v["alias"] for v in body["voices"]] == ["amy", "jenny", "heart"]
        assert body["families"] == [{"model_dir": "l", "speakers": 904, "alias_prefix": "libritts"}]
        assert "revision" in body
    finally:
        handle.stop()


# ---------------------------------------------------------------- HttpRemote
def test_http_remote_maps_paths_and_errors(mini):
    core, handle, sdir = mini
    host, port = handle.address
    remote = C.HttpRemote(f"http://{host}:{port}/", str(sdir / "pi"))
    assert remote.request("GET", "/v1/status")["machine"] == "speech"
    assert remote.request("GET", "/ctl/v1/status")["machine"] == "speech"
    assert isinstance(remote.request("GET", "/v1/inventory"), list)            # lists are returned as parsed
    with pytest.raises(C.RemoteError) as info:
        remote.request("PUT", "/v1/config/tts.engine/threads", {"value": 2})   # the Pi token is agent scope
    assert info.value.status == 403 and info.value.code == "policy" and not info.value.connection
    bad = C.HttpRemote(f"http://{host}:{port}", str(sdir / "owner"))
    with pytest.raises(C.RemoteError) as info:
        C.HttpRemote(f"http://{host}:{port}", str(sdir / "none")).request("GET", "/v1/status")
    assert info.value.connection and info.value.code == "speech_unreachable"
    # a wrong token is a 401 from the server, not a connection failure
    (sdir / "wrong").write_text("not-the-token")
    with pytest.raises(C.RemoteError) as info:
        C.HttpRemote(f"http://{host}:{port}", str(sdir / "wrong")).request("GET", "/v1/status")
    assert info.value.status == 401 and not info.value.connection


def test_http_remote_connection_refused_and_timeout(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    (tmp_path / "t").write_text("tok")
    with pytest.raises(C.RemoteError) as info:
        C.HttpRemote(f"http://127.0.0.1:{port}", str(tmp_path / "t"), 1.0).request("GET", "/v1/status")
    assert info.value.connection
    # a listener that accepts but never answers: timeout is a connection failure too
    s.listen(1)
    try:
        with pytest.raises(C.RemoteError) as info:
            C.HttpRemote(f"http://127.0.0.1:{port}", str(tmp_path / "t"), 0.3).request("GET", "/v1/status")
        assert info.value.connection
    finally:
        s.close()


def test_http_remote_never_echoes_the_token(mini):
    core, handle, sdir = mini
    host, port = handle.address
    remote = C.HttpRemote(f"http://{host}:{port}", str(sdir / "pi"))
    assert PI_TOKEN not in repr(vars(remote)) and PI_TOKEN not in repr(remote)


def test_json_serialisable_core_values_only(pi):
    core, http, _ = pi
    status, body, _ = http.call("GET", "/v1/config", token=OWNER_TOKEN)
    assert status == 200
    json.dumps(body)


# ---------------------------------------------------------------- the real Core behind the real server
def test_real_core_roundtrip_through_the_api(tmp_path, logbuf, api_schema, monkeypatch):
    """Smoke against P3's Core in its in-memory test mode: the dispatcher's calls match the real CoreAPI."""
    import argparse
    core_mod = pytest.importorskip("mirakit.core")
    from mirakit.docsgen import load_specs
    monkeypatch.setenv("HOME", str(tmp_path))
    specs, _ = load_specs(WALL)
    specs = [s for s in specs if s.machine == "listener"]
    if not specs:
        pytest.skip("listener plugins are not importable")
    log, cap = logbuf
    core = core_mod.Core.from_args(argparse.Namespace(), specs=specs)
    try:
        handle = C.start_server(core, make_cfg(tmp_path, wait_s_default=0.0), machine="listener", log=log)
        http = Http(*handle.address)
        try:
            status, body, _ = http.call("GET", "/v1/status", token=OWNER_TOKEN)
            assert status == 200
            check(api_schema, "Status", body)
            status, rows, _ = http.call("GET", "/v1/inventory", token=OWNER_TOKEN)
            assert status == 200 and rows
            for row in rows:
                check(api_schema, "InventoryRow", row)
            status, desc, _ = http.call("GET", "/v1/plugins/agc.mic", token=AGENT_TOKEN)
            assert status == 200
            for knob in desc["knobs"]:
                check(api_schema, "KnobRow", knob)
            status, ch, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06}, token=OWNER_TOKEN)
            assert status == 200 and ch["edits"][0]["new"] == 0.06
            check(api_schema, "Change", ch)
            # outside a voice turn the agent is refused; inside the brain phase the same call becomes a trial
            status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.07}, token=AGENT_TOKEN)
            assert status == 403 and body["error"] == "not_in_voice_turn"
            core.turn_begin()
            status, ch, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 0.07}, token=AGENT_TOKEN)
            assert status == 200 and ch["trial_expires"] and ch["scope"] == "agent"
            # mira set sends text: the core coerces "0.06" to a float, and the agent range answers before the hard bound
            status, ch, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": "0.08"}, token=AGENT_TOKEN)
            assert status == 200 and ch["edits"][0]["new"] == 0.08
            status, body, _ = http.call("PUT", "/v1/config/agc.mic/target", {"value": 2.0}, token=AGENT_TOKEN)
            real_order = (status, body["error"])
            assert real_order in ((403, "policy"), (400, "invalid"))          # recorded in the P6 report
            if real_order == (403, "policy"):
                assert body["allowed"] == {"min": 0.03, "max": 0.1, "step": 0.01}
            status, guide, _ = http.call("GET", "/v1/tuning_guide?topic=hearing", token=AGENT_TOKEN)
            assert status == 200
            check(api_schema, "TuningGuide", guide)
            status, body, _ = http.call("PUT", "/v1/config/wake.kws/threshold", {"value": 0.07}, token=AGENT_TOKEN)
            assert status == 403 and body["error"] == "policy" and body["owner_command"].startswith("mira set wake.kws.threshold")
            status, body, _ = http.call("PUT", "/v1/config/agc.mic/targt", {"value": 0.07}, token=OWNER_TOKEN)
            assert status == 400 and body["error"] == "unknown_key"
        finally:
            handle.stop()
    finally:
        core.shutdown()
