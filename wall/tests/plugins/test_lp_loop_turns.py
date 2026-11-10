"""loop.turns: the timing knobs and the placeholder `loop` service."""
import pytest

import test_lp_common as H
from listener_plugins import loop_turns as lt
from mirakit.plugin import ConfigError

SPEC = lt.SPEC


def test_defaults_equal_the_v1_parser_and_loop_literals():
    ns = H.v1.build_parser().parse_args([])
    k = {x.name: x for x in SPEC.knobs}
    assert (k["wait_speech"].default, k["max_s"].default, k["followup_s"].default) == (ns.wait_speech, ns.max_s, ns.followup_s)
    assert (k["wait_speech"].arg, k["max_s"].arg, k["followup_s"].arg) == ("wait_speech", "max_s", "followup_s")
    # the literals of the v1 loop: deque(maxlen=15), pre[-5:], wait_speech_s=1.0, before[-3:], "Speech is offline"
    src = open(H.WALL / "mira_listen_v1.py", encoding="utf-8").read()
    assert "collections.deque(maxlen=15)" in src and k["preroll_chunks"].default == 15
    assert "list(pre)[-5:]" in src and k["followup_seed_chunks"].default == 5
    assert "wait_speech_s=1.0" in src and k["followup_wait_s"].default == 1.0
    assert "before[-3:]" in src and k["wake_seed_chunks"].default == 3
    assert '"Speech is offline"' in src and k["offline_text"].default == "Speech is offline"
    assert k["events_max"].default == 0 and k["log_text"].default is True and k["sigterm_idle"].default is False


def test_the_facade_arg_names():
    args = {k.name: k.arg for k in SPEC.knobs if k.arg}
    assert args == {"wait_speech": "wait_speech", "max_s": "max_s", "followup_s": "followup_s",
                    "followup_wait_s": "followup_wait_s", "followup_seed_chunks": "followup_seed_chunks",
                    "wake_seed_chunks": "wake_seed_chunks", "preroll_chunks": "preroll_chunks",
                    "offline_text": "offline_text"}


def test_core_slot_and_agent_bounds():
    assert SPEC.core is True and SPEC.provides == ("loop",) and SPEC.required is True
    bounded = {k.name: (k.agent_min, k.agent_max, k.agent_step) for k in SPEC.knobs if k.agent == "bounded"}
    assert bounded == {"wait_speech": (2, 8, 1), "max_s": (6, 15, 2), "followup_s": (4, 15, 2)}
    assert {k.name for k in SPEC.knobs if k.applies == "restart"} == {
        "followup_seed_chunks", "wake_seed_chunks", "preroll_chunks", "events_max", "sigterm_idle"}


def test_start_provides_the_slot_with_every_value():
    plugin, ctx, cfg = H.started("loop.turns", wait_speech=5.0, events_max=2000)
    slot = ctx.get("loop")
    assert slot is plugin.slot and slot.wait_speech == 5.0 and slot.events_max == 2000
    assert (slot.preroll_chunks, slot.wake_seed_chunks, slot.followup_seed_chunks) == (15, 3, 5)


def test_live_knobs_apply():
    plugin, ctx, _ = H.started("loop.turns")
    s = ctx.get("loop")
    H.assert_live_knobs(SPEC, plugin, {
        "wait_speech": (6.0, lambda p: s.wait_speech), "max_s": (12.0, lambda p: s.max_s),
        "followup_s": (10.0, lambda p: s.followup_s), "followup_wait_s": (2.0, lambda p: s.followup_wait_s),
        "offline_text": ("Offline", lambda p: s.offline_text), "log_text": (False, lambda p: s.log_text)})


def _values(**over):
    v = {k.name: k.default for k in SPEC.knobs}
    v.update(over)
    return v


@pytest.mark.parametrize("over,field", [
    ({"preroll_chunks": 3 + 0, "wake_seed_chunks": 3}, "preroll_chunks"),   # must exceed wake_seed_chunks
    ({"preroll_chunks": 5, "wake_seed_chunks": 3, "followup_seed_chunks": 8}, "preroll_chunks"),  # >= followup
])
def test_validate_rejects_bad_preroll(over, field):
    with pytest.raises(ConfigError) as e:
        SPEC.validate(_values(**over))
    assert e.value.field == field


def test_validate_accepts_the_default_and_the_edges():
    SPEC.validate(_values())
    SPEC.validate(_values(preroll_chunks=5, wake_seed_chunks=4, followup_seed_chunks=5))
