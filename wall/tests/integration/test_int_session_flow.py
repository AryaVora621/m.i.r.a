"""Integration: voice sessions (spec 2.5.4) and the tuning dialogue's API sequence (2.12.4) against the real Pi core.

The calls are made the way the Hermes tools make them: through the control layer's dispatcher with the agent scope,
from the fake Hermes' HTTP thread while the loop thread is inside Brain.ask. The mini is an in-memory RemoteClient
with its own change log; a real listener runs the WAV, so turn boundaries, safe points and the follow-up overlay are
the production ones."""
import json
import re

from test_int_facade import CAL_REPLY, CAL_TEXT, Rig, ml, rig_factory  # noqa: F401
from golden import scenarios
from mirakit import control as C
from mirakit.plugin import Edit

from test_int_facade import AudioClock  # noqa: F401


class FakeMini:
    """The speech machine as the Pi sees it: /v1 over RemoteClient.request, with its own S- change log."""

    VOICES = ["amy", "jenny", "hfc_female", "kristin"]

    def __init__(self):
        self.state = {"tts.engine.voice": "amy", "tts.engine.speed": 1.0}
        self.rev = 1
        self.changes = []
        self.requests = []

    def request(self, method, path, body=None):
        self.requests.append((method, path, dict(body or {})))
        path = path.split("?")[0]
        if method == "GET" and path == "/v1/schema":
            return {"revision": self.rev, "plugins": [{"name": "tts.engine", "knobs": []}, {"name": "stt.engine", "knobs": []}]}
        if method == "GET" and path == "/v1/config":
            return {"revision": self.rev}
        if method == "GET" and path == "/v1/status":
            return {"engine": "plugins", "speech": {"stt": "tiny.en", "voice": self.state["tts.engine.voice"]}}
        if method == "GET" and path == "/v1/plugins/tts.engine":
            return {"name": "tts.engine", "revision": self.rev, "knobs": [
                {"key": "voice", "type": "str", "agent": "bounded", "value": self.state["tts.engine.voice"], "spoken": "voice"},
                {"key": "speed", "type": "float", "agent": "bounded", "agent_min": 0.7, "agent_max": 1.3, "agent_step": 0.1,
                 "value": self.state["tts.engine.speed"], "spoken": "speaking speed"}]}
        if method == "GET" and path == "/v1/voices":
            return {"voices": [{"id": v, "alias": v.title()} for v in self.VOICES]}
        if method == "PUT" and path.startswith("/v1/config/"):
            key = path[len("/v1/config/"):].replace("/", ".")
            old, new = self.state[key], body["value"]
            self.state[key] = new
            self.rev += 1
            ch = {"id": f"S-{len(self.changes) + 1:06d}", "machine": "speech", "actor": body.get("actor"), "scope": "agent",
                  "session": body.get("session"), "edits": [{"key": key, "old": old, "new": new}], "status": "applied",
                  "applies": "live", "revision": self.rev, "trial_s": body.get("trial_s")}
            self.changes.append(ch)
            return dict(ch)
        if method == "POST" and path in ("/v1/confirm", "/v1/undo"):
            cid = body.get("change_id")
            for ch in self.changes:
                if ch["id"] == cid:
                    if path.endswith("undo"):
                        for e in ch["edits"]:
                            self.state[e["key"]] = e["old"]
                    ch["status"] = "confirmed" if path.endswith("confirm") else "reverted"
            self.rev += 1
            return [{"id": cid}]
        raise AssertionError(f"unexpected mini request {method} {path}")


class Dialogue:
    """The agent's side: every call goes through the real dispatcher with scope agent."""

    def __init__(self, core):
        self.core = core
        self.api = C._api_for(core, "listener")
        self.trace = []

    def call(self, method, path, body=None, scope="agent"):
        try:
            status, payload = self.api.dispatch(method, path, body, scope)
        except Exception as exc:
            status, payload = C.error_response(exc, 0)
        self.trace.append((method, path, status))
        return status, payload


def two_turns():
    return scenarios.compose(2.0, scenarios.cal(ml), 1.5, scenarios.nowake(ml), 14.0)


def setup(rig_factory, mini):
    r = rig_factory(two_turns(), stt=[CAL_TEXT, "Keep it."], hermes=[CAL_REPLY, "Kept."])
    r.core.register_remote("speech", mini)
    return r, Dialogue(r.core)


def follow_gaps(r):
    """followup_until - now at each 'followup' mark: the window she opened after that reply."""
    gaps = []
    orig = r.lis._mark

    def mark(kind, detail=""):
        orig(kind, detail)
        if kind == "followup":
            gaps.append(round(r.lis.followup_until - r.src.clock(), 3))
    r.lis._mark = mark
    return gaps


def test_the_tuning_dialogue_to_keep_it(rig_factory):
    mini = FakeMini()
    r, d = setup(rig_factory, mini)
    gaps, out = follow_gaps(r), {}

    def turn(i):
        if i == 0:
            out["status"] = d.call("GET", "/v1/status")
            out["guide"] = d.call("GET", "/v1/tuning_guide?topic=voice")
            out["voices"] = d.call("GET", "/v1/voices?limit=4")
            out["start"] = d.call("POST", "/v1/session", {"action": "start", "topic": "voice"})
            out["preview"] = d.call("POST", "/v1/preview", {"voice": "jenny", "wait_s": 5})
            out["voice"] = d.call("PUT", "/v1/config/tts.engine/voice", {"value": "jenny", "reason": "he liked it"})
            out["speed"] = d.call("PUT", "/v1/config/tts.engine/speed", {"value": 0.9, "reason": "a bit slower"})
            out["hearing"] = d.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06, "reason": "hear me", "wait_s": 0})
            out["wake"] = d.call("PUT", "/v1/config/wake.kws/threshold", {"value": 0.1})
            out["fast"] = d.call("PUT", "/v1/config/tts.engine/speed", {"value": 2.0})
            out["session"] = d.call("GET", "/v1/session")
            out["state_mid_turn"] = dict(mini.state)
        else:
            out["keep"] = d.call("POST", "/v1/session", {"action": "keep"})
    r.hermes.on_chat = turn
    r.lis.run()

    assert [out[k][0] for k in ("status", "guide", "voices", "start", "preview", "voice", "speed")] == [200] * 7
    assert out["hearing"][0] == 202 and out["hearing"][1]["status"] == "queued"     # the loop is in the brain phase
    sid = out["start"][1]["id"]
    assert re.fullmatch(r"V-\d+", sid) and out["start"][1]["followup_s"] == 15.0
    assert out["preview"][1]["played"] is True
    assert out["guide"][1]["topic"] == "voice" and "owner_only" in out["guide"][1]
    # refusals: owner-only wake threshold, and a speed outside the agent range with the range in the answer
    assert out["wake"][0] == 403 and out["wake"][1]["error"] == "policy" and "owner_command" in out["wake"][1]
    assert out["fast"][0] == 403 and out["fast"][1]["allowed"]["max"] == 1.3
    assert out["state_mid_turn"] == {"tts.engine.voice": "jenny", "tts.engine.speed": 0.9}
    # the mini writes carry the session, the actor, and a trial that outlives the session
    puts = [b for m, p, b in mini.requests if m == "PUT"]
    assert [b["value"] for b in puts] == ["jenny", 0.9] and all(b["session"] == sid and b["actor"] == "hermes" for b in puts)
    assert all(b["trial_s"] > 600 for b in puts)
    # session overlay: 15 s after the first reply, back to 8 s once she kept it
    assert gaps == [15.0, 8.0]
    assert out["keep"][0] == 200 and out["keep"][1]["state"] == "kept"
    # both change logs and the final state
    pi = {c["id"]: c for c in r.core.changes(limit=50)}
    local = [c for c in pi.values() if c["edits"][0]["key"] == "agc.mic.target" and not c.get("undo_of")]
    assert len(local) == 1 and local[0]["status"] == "confirmed" and local[0]["session"] == sid
    assert [c["status"] for c in mini.changes] == ["confirmed", "confirmed"]
    assert mini.state == {"tts.engine.voice": "jenny", "tts.engine.speed": 0.9}
    assert r.lis.agc.target == 0.06 and r.args.followup_s == 8.0
    assert r.core.session("get", actor="owner", scope="owner") is None
    assert not any(m == "PUT" and "wake" in p for m, p, _ in mini.requests)     # a refused write never left the Pi


def test_the_tuning_dialogue_to_go_back(rig_factory):
    mini = FakeMini()
    r, d = setup(rig_factory, mini)
    out = {}

    def turn(i):
        if i == 0:
            d.call("POST", "/v1/session", {"action": "start", "topic": "voice"})
            d.call("PUT", "/v1/config/tts.engine/voice", {"value": "jenny", "reason": "try it"})
            d.call("PUT", "/v1/config/agc.mic/target", {"value": 0.06, "reason": "try it", "wait_s": 0})
        else:
            out["revert"] = d.call("POST", "/v1/session", {"action": "revert"})
    r.hermes.on_chat = turn
    r.lis.run()
    assert out["revert"][0] == 200 and out["revert"][1]["state"] == "reverted"
    assert mini.state["tts.engine.voice"] == "amy" and [c["status"] for c in mini.changes] == ["reverted"]
    assert r.lis.agc.target == 0.05 and r.args.agc_target == 0.05           # reverted on the Pi, applied at turn end
    statuses = {c["id"]: c["status"] for c in r.core.changes(limit=50) if c["edits"][0]["key"] == "agc.mic.target"}
    assert "reverted" in statuses.values()


def test_a_trial_inside_an_open_session_waits_for_the_session_to_end(rig_factory):
    r = rig_factory(scenarios.compose(2.0, scenarios.cal(ml), 20.0), stt=[CAL_TEXT], hermes=[CAL_REPLY])
    obs = []

    def turn(i):
        ch = r.core.submit([Edit("agc.mic.target", 0.06)], actor="hermes", scope="agent", trial_s=60)
        obs.append(("submitted", ch.status, ch.session))
    r.hermes.on_chat = turn
    real_sp = r.core.safe_point
    stage = {"n": 0, "clock0": None}

    def sp(armed):
        flags = real_sp(armed)
        t = r.src.clock()
        if obs and stage["clock0"] is None and t > 4:
            stage["clock0"] = t
            r.clock.bias += 90                    # 90 s later: the 60 s trial is due, the 120 s idle limit is not
        elif stage["clock0"] is not None and t > stage["clock0"] + 0.5 and stage["n"] == 0:
            obs.append(("session open, trial due", r.lis.agc.target, r.core.session("get", actor="o", scope="owner") is not None))
            r.core.session("end", actor="owner", scope="owner")
            stage["n"] = 1
        elif stage["n"] == 1 and t > stage["clock0"] + 1.5:
            obs.append(("after the session", r.lis.agc.target))
            stage["n"] = 2
        return flags
    r.core.safe_point = sp
    r.lis.run()
    assert obs[0][0] == "submitted" and obs[0][2] is not None                 # joined an implicit session
    assert obs[1] == ("session open, trial due", 0.06, True)                  # not reverted while the session is open
    assert obs[2] == ("after the session", 0.05)                              # the trial's own clock ran out: reverted
    done = [c for c in r.core.changes(limit=10) if c["actor"] == "expiry"]
    assert len(done) == 1 and done[0]["scope"] == "system"
