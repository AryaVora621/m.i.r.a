"""intent.music: Music and music_intent copied from v1 with knob-fed edits; MusicIntent is the service."""
import json

import pytest

import test_lp_common as H
from conftest import FakeServer
from listener_plugins import intent_music as im
from mirakit.plugin import ConfigError

SPEC = im.SPEC

# test_unit.py's table plus regex edge cases
TABLE = im.MUSIC_CASES + [
    ("Please pause", "pause"), ("play it please", "play"), ("what song is this", "status"), ("Skip it", "next"),
    ("louder", "volume_up"), ("quieter", "volume_down"), ("stop", "pause"), ("mute", "pause"), ("resume", "play"),
    ("play Taylor Swift", None), ("", None), ("play the", None), ("volume", None), ("what am I listening to", "status"),
]


@pytest.mark.parametrize("text,intent", TABLE)
def test_music_intent_equals_v1_and_the_table(text, intent):
    assert im.music_intent(text) == H.v1.music_intent(text)
    if (text, intent) in im.MUSIC_CASES:
        assert im.music_intent(text) == intent


def test_default_patterns_are_the_v1_list_in_order():
    assert list(im.DEFAULT_PATTERNS) == ["status", "volume_up", "volume_down", "pause", "next", "play"]
    import re
    src = open(H.WALL / "mira_listen_v1.py", encoding="utf-8").read()
    for name, pat in im.DEFAULT_PATTERNS.items():
        assert f'("{name}", r"{pat}")' in src


def _music_server(volume=50, echo_down=False):
    s = FakeServer()
    state = {"volume": volume}
    status = lambda b, h: (200, json.dumps({"player": {"state": "playing", "volume": state["volume"]}}).encode(), "application/json")
    down = lambda b, h: (409, b"{}", "application/json")
    for p in ("play", "pause", "next", "volume"):
        s.routes[("POST", f"/x/echo/{p}")] = down if (echo_down and p in ("play", "next")) else status
    s.routes[("GET", "/x/echo/status")] = status
    s.state = state
    return s


@pytest.fixture
def auth(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"u": "arya", "t": "tok123", "s": "salt456"}))
    return str(p)


def _calls(s):
    return [(c["method"], c["path"], c["body"], {k: v for k, v in c["headers"].items() if k.startswith("x-music")})
            for c in s.calls]


@pytest.mark.parametrize("intent", ["play", "pause", "next", "status", "volume_up", "volume_down", "nonsense"])
@pytest.mark.parametrize("echo_down", [False, True])
def test_handle_requests_and_replies_equal_v1(auth, intent, echo_down):
    a, b = _music_server(echo_down=echo_down), _music_server(echo_down=echo_down)
    try:
        ra = H.v1.Music(a.url, auth).handle(intent)
        rb = im.Music(b.url, auth).handle(intent)
        assert ra == rb
        assert [(m, p, bd, h) for m, p, bd, h in _calls(a)] == [(m, p, bd, h) for m, p, bd, h in _calls(b)]
    finally:
        a.stop(); b.stop()


@pytest.mark.parametrize("start,intent,end", [(50, "volume_up", 60), (95, "volume_up", 100), (5, "volume_down", 0),
                                              (100, "volume_up", 100), (0, "volume_down", 0)])
def test_volume_math_equals_v1(auth, start, intent, end):
    a, b = _music_server(start), _music_server(start)
    try:
        assert H.v1.Music(a.url, auth).handle(intent) == im.Music(b.url, auth).handle(intent) == f"Volume {end}."
        assert json.loads(b.calls[-1]["body"]) == {"volume": end}
    finally:
        a.stop(); b.stop()


def test_volume_step_and_replies_are_knobs(auth):
    s = _music_server(50)
    try:
        replies = dict(im.DEFAULT_REPLIES, volume="Level {n}.", pause="Stopped.", echo_down="No speaker.")
        m = im.Music(s.url, auth, volume_step=25, replies=replies)
        assert m.handle("volume_up") == "Level 75." and m.handle("pause") == "Stopped."
        assert json.loads(s.calls[1]["body"]) == {"volume": 75}
    finally:
        s.stop()


def test_headers_come_from_the_auth_file_on_every_call(tmp_path):
    s = _music_server()
    try:
        p = tmp_path / "a.json"
        p.write_text(json.dumps({"u": "u1", "t": "t1", "s": "s1"}))
        m = im.Music(s.url, str(p))
        m.handle("pause")
        p.write_text(json.dumps({"u": "u2", "t": "t2", "s": "s2"}))
        m.handle("pause")
        assert [c["headers"]["x-music-user"] for c in s.calls] == ["u1", "u2"]
    finally:
        s.stop()


def test_unreachable_music_api_raises_like_v1(auth):
    url = H.refused_url()
    for cls_ in (H.v1.Music, im.Music):
        with pytest.raises(Exception) as e:
            cls_(url, auth).handle("pause")
        assert type(e.value).__name__ == "URLError"


def test_intent_service_attributes_and_dispatch(auth):
    plugin, ctx, cfg = H.started("intent.music", auth_file=auth, priority=250)
    i = ctx.get("intent")
    assert (i.priority, i.mark, i.log_name, i.done_reply, i.fail_reply) == (250, "music", "music-api", "Done.",
                                                                           "I could not reach the music player.")
    assert i.match("Play the music.") == "play" and i.match("play Taylor Swift") is None
    assert isinstance(i.music, im.Music) and ctx.all("intent") == [i]


def test_live_knobs_apply(auth):
    plugin, ctx, cfg = H.started("intent.music")
    i = ctx.get("intent")
    pats = dict(im.DEFAULT_PATTERNS, play="(play|go)")
    reps = dict(im.DEFAULT_REPLIES, done="OK.")
    H.assert_live_knobs(SPEC, plugin, {
        "priority": (7, lambda p: i.priority), "url": ("http://127.0.0.1:1/", lambda p: i.music.url, "http://127.0.0.1:1"),
        "auth_file": ("/a.json", lambda p: i.music.auth_file), "timeout_s": (3.0, lambda p: i.music.timeout_s),
        "volume_step": (20, lambda p: i.music.volume_step), "patterns": (pats, lambda p: i.patterns),
        "replies": (reps, lambda p: i.music.replies)})
    plugin.apply({"patterns": pats}, cfg.replace(patterns=pats))
    assert i.match("go") == "play"
    plugin.apply({"replies": reps}, cfg.replace(replies=reps))
    assert i.done_reply == "OK."


def _values(**over):
    v = {k.name: (dict(k.default) if isinstance(k.default, dict) else k.default) for k in SPEC.knobs}
    v.update(over)
    return v


def test_validate_patterns_and_replies():
    SPEC.validate(_values())
    with pytest.raises(ConfigError):
        SPEC.validate(_values(patterns={"play": "play"}))
    with pytest.raises(ConfigError):
        SPEC.validate(_values(patterns=dict(im.DEFAULT_PATTERNS, play="(unclosed")))
    with pytest.raises(ConfigError) as e:  # a greedy play pattern would swallow "play Taylor Swift"
        SPEC.validate(_values(patterns=dict(im.DEFAULT_PATTERNS, play=r"play.*")))
    assert "Taylor" in e.value.message
    with pytest.raises(ConfigError):
        SPEC.validate(_values(replies={"play": "x"}))
    with pytest.raises(ConfigError):
        SPEC.validate(_values(replies=dict(im.DEFAULT_REPLIES, volume="Volume.")))


def test_spec_shape():
    assert SPEC.toggle == "live" and SPEC.provides == ("intent",) and im.LEGACY_CLASSES == ("Music",)
    assert [k.name for k in SPEC.knobs if k.agent != "deny"] == []
