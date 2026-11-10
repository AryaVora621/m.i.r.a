"""mirakit.policy: the rule table of spec 2.5.5 row by row, rate windows, quiet hours, voice-turn rule."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from mirakit.plugin import Knob, PolicyError
from mirakit.policy import Policy, Settings, owner_command, settings_from

TARGET = Knob("target", "float", 0.05, "d", unit="rms", min=0.005, max=0.3, agent="bounded", agent_min=0.03,
              agent_max=0.10, agent_step=0.01, spoken="microphone boost")
THRESH = Knob("threshold", "float", 0.15, "d", unit="score", min=0.01, max=0.5, spoken="wake sensitivity")
VOICE = Knob("voice", "voice", "a#0", "d", agent="bounded", spoken="my voice")
NAME = Knob("name", "str", "x", "d", min=1, max=10, agent="bounded", spoken="my name")


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    clock = Clock()
    box = {"s": Settings()}
    pol = Policy("listener", lambda: box["s"], clock)
    return pol, box, clock


def item(knob, key, old, new):
    return {"key": key, "knob": knob, "old": old, "new": new}


def write(pol, items, **kw):
    kw.setdefault("in_turn", True)
    return pol.check_write("agent", items, **kw)


def refused(code, fn, *a, **kw):
    with pytest.raises(PolicyError) as ei:
        fn(*a, **kw)
    assert ei.value.code == code
    return ei.value


# ---- row 1: owner
def test_owner_may_set_anything_without_a_turn_or_a_rate_limit(env):
    pol, box, _ = env
    for i in range(50):
        pol.check_write("owner", [item(THRESH, "wake.kws.threshold", 0.1, 0.2)], in_turn=False)
        pol.record_write(["wake.kws.threshold"])
    pol.check_preview("owner", in_turn=False, now_local=datetime(2026, 11, 10, 2, 0))     # no quiet hours for owner
    pol.check_undo_target("owner", {"scope": "agent", "id": "L-1"}, force=True, age_s=10 ** 9)


def test_owner_trial_only_when_asked_and_agent_trial_always(env):
    pol, _, _ = env
    assert pol.trial_for("owner", None) is None
    assert pol.trial_for("owner", 600) == 600
    assert pol.trial_for("owner", 10 ** 6) == 7200
    assert pol.trial_for("agent", None) == 600
    assert pol.trial_for("agent", 5) == 60                     # clamp up to 60
    assert pol.trial_for("agent", 99999) == 1800               # clamp down to trial_max_s


def test_agent_trial_max_follows_the_setting(env):
    pol, box, _ = env
    box["s"] = replace(box["s"], trial_default_s=120.0, trial_max_s=300.0)
    assert pol.trial_for("agent", None) == 120.0 and pol.trial_for("agent", 1000) == 300.0


# ---- row 3: the agent
def test_default_is_deny_with_an_owner_command(env):
    pol, _, _ = env
    err = refused("policy", write, pol, [item(THRESH, "wake.kws.threshold", 0.15, 0.06)])
    assert err.owner_command == "mira set wake.kws.threshold=0.06 --trial 600"
    assert "wake sensitivity" in err.message and err.field == "wake.kws.threshold"
    assert err.allowed is None


def test_owner_command_quotes_text_and_json():
    assert owner_command("tts.remote.voice", "amy") == "mira set tts.remote.voice=amy --trial 600"
    assert owner_command("x.y.z", "two words") == "mira set x.y.z='two words' --trial 600"
    assert owner_command("x.y.l", ["a", "b"]) == """mira set x.y.l='["a", "b"]' --trial 600"""
    assert owner_command("x.y.n", 10.0) == "mira set x.y.n=10 --trial 600"
    assert owner_command("x.y.b", True).startswith("mira set x.y.b=")


def test_bounded_knob_inside_range_and_step_passes(env):
    pol, _, _ = env
    write(pol, [item(TARGET, "agc.mic.target", 0.05, 0.06)])
    write(pol, [item(TARGET, "agc.mic.target", 0.05, 0.04)])


def test_agent_range_refusal_carries_allowed_and_speaks_the_range(env):
    pol, _, _ = env
    err = refused("policy", write, pol, [item(TARGET, "agc.mic.target", 0.05, 0.2)])
    assert err.allowed == {"min": 0.03, "max": 0.10, "step": 0.01}
    assert "microphone boost" in err.message and "0.03" in err.message and "0.1" in err.message
    refused("policy", write, pol, [item(TARGET, "agc.mic.target", 0.05, 0.02)])


def test_agent_step_refusal(env):
    pol, _, _ = env
    err = refused("policy", write, pol, [item(TARGET, "agc.mic.target", 0.04, 0.07)])
    assert "0.01" in err.message and err.allowed["step"] == 0.01
    write(pol, [item(TARGET, "agc.mic.target", 0.04, 0.05)])         # exactly one step is fine (float tolerant)
    write(pol, [item(TARGET, "agc.mic.target", 0.07, 0.08)])


def test_policy_refusal_is_spoken_safe(env):
    """No jargon, no dashes, no markup in anything the skill may read aloud."""
    pol, _, _ = env
    msgs = []
    for it in ([item(THRESH, "w.k.t", 0.1, 0.2)], [item(TARGET, "a.m.t", 0.05, 0.9)], [item(TARGET, "a.m.t", 0.04, 0.07)],
               [item(VOICE, "t.e.v", "a#0", "b#0")]):
        try:
            write(pol, it)
        except PolicyError as exc:
            msgs.append(exc.message)
    assert len(msgs) == 4
    for m in msgs:
        assert chr(0x2014) not in m and chr(0x2013) not in m and "{" not in m and "_" not in m


def test_voice_knob_needs_a_catalog_verdict(env):
    pol, _, _ = env
    refused("policy", write, pol, [item(VOICE, "tts.engine.voice", "a#0", "b#0")])                 # no resolver
    refused("policy", write, pol, [item(VOICE, "tts.engine.voice", "a#0", "b#0")], voice_ok=lambda v: False)
    write(pol, [item(VOICE, "tts.engine.voice", "a#0", "b#0")], voice_ok=lambda v: True)


def test_non_numeric_bounded_knob_has_no_numeric_limits(env):
    pol, _, _ = env
    write(pol, [item(NAME, "x.y.name", "a", "b")])


def test_enable_edit_is_owner_only(env):
    pol, _, _ = env
    err = refused("policy", write, pol, [{"key": "intent.music.enabled", "knob": None, "old": True, "new": False}])
    assert "switch parts" in err.message


def test_system_scope_cannot_write(env):
    pol, _, _ = env
    refused("policy", pol.check_write, "system", [item(TARGET, "a.m.t", 0.05, 0.06)], in_turn=True)


# ---- rule 2: kill switch
def test_agents_locked_refuses_every_agent_write_confirm_start_keep_and_preview(env):
    pol, box, _ = env
    box["s"] = replace(box["s"], agents_locked=True)
    ok = [item(TARGET, "agc.mic.target", 0.05, 0.06)]
    refused("agents_locked", write, pol, ok)
    refused("agents_locked", pol.check_confirm, "agent", in_turn=True)
    refused("agents_locked", pol.check_session, "start", "agent", in_turn=True)
    refused("agents_locked", pol.check_session, "keep", "agent", in_turn=True)
    refused("agents_locked", pol.check_preview, "agent", in_turn=True, now_local=datetime(2026, 11, 10, 12, 0))
    pol.check_session("revert", "agent", in_turn=False)           # moving back toward the owner stays possible
    pol.check_session("end", "agent", in_turn=False)
    pol.check_session("get", "agent", in_turn=False)
    pol.check_undo_target("agent", {"scope": "agent", "id": "L-1"}, force=False, age_s=5)
    pol.check_write("owner", ok, in_turn=False)                   # the owner is unaffected
    pol.check_confirm("owner", in_turn=False)


# ---- rule 3: the voice turn
def test_voice_turn_rule_for_write_confirm_start_keep_and_preview(env):
    pol, _, _ = env
    ok = [item(TARGET, "agc.mic.target", 0.05, 0.06)]
    noon = datetime(2026, 11, 10, 12, 0)
    refused("not_in_voice_turn", write, pol, ok, in_turn=False)
    refused("not_in_voice_turn", pol.check_confirm, "agent", in_turn=False)
    refused("not_in_voice_turn", pol.check_session, "start", "agent", in_turn=False)
    refused("not_in_voice_turn", pol.check_session, "keep", "agent", in_turn=False)
    refused("not_in_voice_turn", pol.check_preview, "agent", in_turn=False, now_local=noon)
    write(pol, ok, in_turn=True)
    pol.check_confirm("agent", in_turn=True)
    pol.check_session("start", "agent", in_turn=True)
    pol.check_session("keep", "agent", in_turn=True)
    pol.check_preview("agent", in_turn=True, now_local=noon)


def test_voice_turn_rule_does_not_apply_to_undo_revert_end_get(env):
    pol, _, _ = env
    pol.check_undo_target("agent", {"scope": "agent", "id": "L-1"}, force=False, age_s=5)
    pol.check_session("revert", "agent", in_turn=False)
    pol.check_session("end", "agent", in_turn=False)
    pol.check_session("get", "agent", in_turn=False)


def test_voice_turn_rule_can_be_switched_off_by_the_owner(env):
    pol, box, _ = env
    box["s"] = replace(box["s"], agent_requires_voice_turn=False)
    write(pol, [item(TARGET, "agc.mic.target", 0.05, 0.06)], in_turn=False)
    pol.check_confirm("agent", in_turn=False)


def test_deny_is_reported_before_the_turn_rule_so_the_skill_can_file_an_owner_task(env):
    pol, _, _ = env
    refused("policy", write, pol, [item(THRESH, "w.k.t", 0.1, 0.2)], in_turn=False)


# ---- rate limits
def test_twelve_writes_per_ten_minutes_with_a_sliding_window(env):
    pol, box, clock = env
    box["s"] = replace(box["s"], agent_writes_per_knob_10min=20)
    ok = [item(TARGET, "agc.mic.target", 0.05, 0.06)]
    for i in range(12):
        write(pol, ok)
        pol.record_write(["agc.mic.target"])
        clock.t += 10
    err = refused("rate_limited", write, pol, ok)
    assert "enough" in err.message
    clock.t += 480                                               # the first write is now 600 s old
    write(pol, ok)


def test_three_writes_per_knob_per_ten_minutes(env):
    pol, _, clock = env
    a, b = [item(TARGET, "agc.mic.target", 0.05, 0.06)], [item(TARGET, "agc.mic.max_gain", 0.05, 0.06)]
    for _ in range(3):
        write(pol, a)
        pol.record_write(["agc.mic.target"])
    err = refused("rate_limited", write, pol, a)
    assert err.field == "agc.mic.target"
    write(pol, b)                                                 # another knob is unaffected
    clock.t += 601
    write(pol, a)


def test_a_batch_counts_once_for_the_total(env):
    pol, box, _ = env
    box["s"] = replace(box["s"], agent_writes_per_10min=1)
    pol.record_write(["a.b.c", "a.b.d", "a.b.e"])
    refused("rate_limited", write, pol, [item(TARGET, "x.y.z", 0.05, 0.06)])


def test_failed_checks_record_nothing(env):
    pol, box, _ = env
    box["s"] = replace(box["s"], agent_writes_per_10min=1)
    for _ in range(5):
        refused("policy", write, pol, [item(TARGET, "agc.mic.target", 0.05, 0.9)])
    write(pol, [item(TARGET, "agc.mic.target", 0.05, 0.06)])


def test_zero_disables_agent_writes(env):
    pol, box, _ = env
    box["s"] = replace(box["s"], agent_writes_per_10min=0)
    refused("rate_limited", write, pol, [item(TARGET, "agc.mic.target", 0.05, 0.06)])


def test_session_cap(env):
    pol, box, _ = env
    ok = [item(TARGET, "agc.mic.target", 0.05, 0.06)]
    write(pol, ok, session_changes=11)
    err = refused("session_cap", write, pol, ok, session_changes=12)
    assert "session" in err.message


def test_undo_confirm_keep_and_revert_are_exempt_from_rate_limits(env):
    pol, box, _ = env
    box["s"] = replace(box["s"], agent_writes_per_10min=1)
    pol.record_write(["a.b.c"])
    pol.check_undo_target("agent", {"scope": "agent", "id": "L-1"}, force=False, age_s=5)
    pol.check_confirm("agent", in_turn=True)
    pol.check_session("keep", "agent", in_turn=True)
    pol.check_session("revert", "agent", in_turn=True)


# ---- undo window and scope
def test_agent_undo_only_agent_changes_younger_than_the_window(env):
    pol, box, _ = env
    pol.check_undo_target("agent", {"scope": "agent", "id": "L-1"}, force=False, age_s=86399)
    refused("policy", pol.check_undo_target, "agent", {"scope": "agent", "id": "L-1"}, force=False, age_s=86401)
    err = refused("policy", pol.check_undo_target, "agent", {"scope": "owner", "id": "L-2"}, force=False, age_s=1)
    assert err.field == "L-2"
    refused("policy", pol.check_undo_target, "agent", {"scope": "system", "id": "L-3"}, force=False, age_s=1)
    box["s"] = replace(box["s"], agent_undo_window_s=600.0)
    refused("policy", pol.check_undo_target, "agent", {"scope": "agent", "id": "L-1"}, force=False, age_s=601)


def test_force_is_owner_only(env):
    pol, _, _ = env
    refused("policy", pol.check_undo_target, "agent", {"scope": "agent", "id": "L-1"}, force=True, age_s=1)
    pol.check_undo_target("owner", {"scope": "owner", "id": "L-1"}, force=True, age_s=1)


# ---- previews and quiet hours
def test_preview_quiet_defaults_to_no_window_and_never_mentions_night(env):
    pol, box, _ = env
    assert box["s"].preview_quiet == ""
    for hour in (0, 2, 5, 23):
        pol.check_preview("agent", in_turn=True, now_local=datetime(2026, 11, 10, hour, 0))


def test_quiet_hours_only_when_a_window_is_set_and_only_for_agents(env):
    pol, box, _ = env
    box["s"] = replace(box["s"], preview_quiet="23:00-06:00")
    err = refused("quiet_hours", pol.check_preview, "agent", in_turn=True, now_local=datetime(2026, 11, 10, 23, 30))
    assert "night" not in err.message.lower() and "quiet hours" in err.message
    refused("quiet_hours", pol.check_preview, "agent", in_turn=True, now_local=datetime(2026, 11, 10, 3, 0))
    pol.check_preview("agent", in_turn=True, now_local=datetime(2026, 11, 10, 12, 0))
    pol.check_preview("owner", in_turn=False, now_local=datetime(2026, 11, 10, 23, 30))


def test_previews_per_ten_minutes(env):
    pol, _, clock = env
    noon = datetime(2026, 11, 10, 12, 0)
    for _ in range(6):
        pol.check_preview("agent", in_turn=True, now_local=noon)
        pol.record_preview()
    refused("rate_limited", pol.check_preview, "agent", in_turn=True, now_local=noon)
    clock.t += 601
    pol.check_preview("agent", in_turn=True, now_local=noon)


# ---- owner-only operations
def test_need_owner(env):
    pol, _, _ = env
    pol.need_owner("owner")
    refused("policy", pol.need_owner, "agent", "restart")
    refused("policy", pol.need_owner, "system")


# ---- settings
def test_settings_defaults_match_the_spec_table():
    s = Settings()
    assert (s.agent_writes_per_10min, s.agent_writes_per_knob_10min, s.agent_undo_window_s) == (12, 3, 86400.0)
    assert (s.trial_default_s, s.trial_max_s) == (600.0, 1800.0)
    assert (s.session_max_s, s.session_max_changes, s.session_followup_s, s.session_idle_s) == (900.0, 12, 15.0, 120.0)
    assert (s.rebuild_quiet_s, s.previews_per_10min, s.preview_max_chars) == (10.0, 6, 200)
    assert s.preview_quiet == "" and s.busy_defer_s == 10.0 and s.history_keep == 50
    assert s.agent_requires_voice_turn and not s.agents_locked


def test_settings_from_ignores_unknown_keys_and_none():
    assert settings_from(None) == Settings()
    s = settings_from({"trial_default_s": 300.0, "port": 8650, "bind": "127.0.0.1"})
    assert s.trial_default_s == 300.0
