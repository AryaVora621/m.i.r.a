"""The shared parts of the quick-answer intents (listener_plugins.quick_common), the five specs as a set, and the
helpers the five test_lp_intent_* files import (`import test_lp_quick_common as Q`)."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import test_lp_common as H
from listener_plugins import (intent_calendar, intent_music, intent_printers, intent_school, intent_time,
                              intent_weather, quick_common as qc)
from mirakit.plugin import ConfigError
from mirakit.testkit import cfg_for

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 10, 19, 42, tzinfo=TZ)   # Tuesday evening, EST
MODS = (intent_time, intent_weather, intent_school, intent_calendar, intent_printers)
NAMES = ["intent.time", "intent.weather", "intent.school", "intent.calendar", "intent.printers"]
TOKEN = "quick-token-for-tests"


def cfg(mod, **over):
    return cfg_for(mod.SPEC, **over)


def values(mod, **over):
    v = {k.name: (dict(k.default) if isinstance(k.default, dict) else k.default) for k in mod.SPEC.knobs}
    v.update(over)
    return v


def words(text):
    return len(text.split())


def token_file(tmp_path, token=TOKEN):
    p = tmp_path / "quick_token"
    p.write_text(token + "\n")
    return str(p)


def quick_server(payloads, status=200, delay=0.0):
    """A recording server for GET /api/hooks/quick/<topic>; it checks the bearer token like the real route does."""
    import time as _t

    def handler(method, path, body, headers):
        if delay:
            _t.sleep(delay)
        if headers.get("authorization") != f"Bearer {TOKEN}":
            return 401, b'{"detail":"Invalid quick token"}', "application/json", {}
        topic = path.rsplit("/", 1)[-1]
        if status != 200 or topic not in payloads:
            return (status if status != 200 else 404), b'{"detail":"no"}', "application/json", {}
        return 200, json.dumps(payloads[topic]).encode(), "application/json", {}
    return H.Srv(handler)


# ---------------------------------------------------------------------------------------------- phrase matching

PAT = {"a": r"what time is it( now)?", "b": r"what'?s the date"}


@pytest.mark.parametrize("text,want", [
    ("what time is it", "a"), ("What time is it?", "a"), ("WHAT TIME IS IT NOW!", "a"), ("please what time is it", "a"),
    ("what time is it please", "a"), ("what time is it for me", "a"), ("tell me what time is it", "a"),
    ("can you tell me what time is it", "a"), ("could you please tell me what time is it", "a"),
    ("do you know what time is it", "a"), ("what’s the date", "b"), ("whats the date", "b"), ("what's the date", "b"),
    ("what time is it in tokyo", None), ("what time is it now and then", None), ("now what time is it", None),
    ("", None), ("   ", None), ("?!", None), ("tell me", None), ("please", None),
])
def test_phrase_match_is_a_whole_phrase_match_inside_a_polite_frame(text, want):
    assert qc.phrase_match(text, PAT) == want


def test_phrase_match_tries_patterns_in_order_and_takes_pairs():
    assert qc.phrase_match("go", [("first", "go"), ("second", "go")]) == "first"
    assert qc.phrase_match("go", {"x": "stop", "y": "go"}) == "y"


def test_a_pattern_that_eats_the_lead_in_still_matches_whole():
    assert qc.phrase_match("tell me the time", {"t": r"tell me the time"}) == "t"


# -------------------------------------------------------------------------------------------------- spoken words

@pytest.mark.parametrize("h,m,want", [(0, 0, "midnight"), (12, 0, "noon"), (15, 0, "3 pm"), (15, 30, "3:30 pm"),
                                      (8, 5, "8:05 am"), (23, 59, "11:59 pm"), (0, 15, "12:15 am"), (12, 45, "12:45 pm"),
                                      (1, 0, "1 am")])
def test_clock_words(h, m, want):
    assert qc.clock_words(datetime(2026, 11, 10, h, m, tzinfo=TZ)) == want


@pytest.mark.parametrize("sec,want", [(0, "under a minute"), (20, "under a minute"), (60, "1 minute"), (300, "5 minutes"),
                                      (4200, "1 hour and 10 minutes"), (7200, "2 hours"), (3600, "1 hour"),
                                      (3 * 3600 + 170, "3 hours and 5 minutes"), (2700, "45 minutes"), (-5, "under a minute"),
                                      (612, "10 minutes")])
def test_duration_words(sec, want):
    assert qc.duration_words(sec) == want


def test_number_and_ordinal_words():
    assert [qc.number_word(n) for n in (0, 1, 3, 12, 20, 21)] == ["zero", "one", "three", "twelve", "twenty", "21"]
    assert [qc.ordinal_word(d) for d in (1, 2, 3, 10, 11, 21, 22, 30, 31)] == [
        "first", "second", "third", "tenth", "eleventh", "twenty first", "twenty second", "thirtieth", "thirty first"]
    assert all(qc.ordinal_word(d) for d in range(1, 32))


@pytest.mark.parametrize("title,n,want", [
    ("Projectiles Review Sheets", 6, "Projectiles Review Sheets"),
    ("Essay draft on the causes of the first world war", 6, "Essay draft on the causes"),
    ("HW 3.2 (due Friday) Kinematics", 6, "HW 3.2 Kinematics"), ("Lab *** report", 6, "Lab report"),
    ("one two three four five six seven eight", 3, "one two three"), ("Read the", 1, "Read"), ("", 6, ""),
    ("The Great Gatsby ch 1-3 questions to", 5, "The Great Gatsby ch 1-3"),
])
def test_short_title(title, n, want):
    assert qc.short_title(title, n) == want


def test_join_and_and_parse_iso():
    assert [qc.join_and(x) for x in ([], ["a"], ["a", "b"], ["a", "b", "c"])] == ["", "a", "a and b", "a, b, and c"]
    assert qc.parse_iso("2026-11-10T20:00:00Z") == datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
    assert qc.parse_iso("2026-11-10T20:00:00.000Z").tzinfo is not None
    assert qc.parse_iso("2026-11-10T14:00:00-05:00") == datetime(2026, 11, 10, 19, tzinfo=timezone.utc)
    assert qc.parse_iso("2026-11-10").tzinfo is not None
    assert [qc.parse_iso(x) for x in ("soon", "", None, 5)] == [None] * 4


# ------------------------------------------------------------------------------------------------ the client

def test_client_sends_the_bearer_token_to_the_quick_route_and_reads_json(tmp_path):
    s = quick_server({"school": {"items": []}})
    try:
        c = qc.QuickClient(s.url + "/", token_file(tmp_path), 2.0)
        assert c.get("school") == {"items": []}
        call = s.calls[0]
        assert (call["method"], call["path"]) == ("GET", "/api/hooks/quick/school")
        assert call["headers"]["authorization"] == f"Bearer {TOKEN}" and call["body"] == b""
    finally:
        s.stop()


def test_client_reads_the_token_on_every_call(tmp_path):
    s = quick_server({"school": {"ok": 1}})
    try:
        p = token_file(tmp_path, "wrong")
        c = qc.QuickClient(s.url, p, 2.0)
        assert c.get("school") is None
        token_file(tmp_path)
        assert c.get("school") == {"ok": 1}
    finally:
        s.stop()


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_client_answers_none_for_any_error_status(tmp_path, status):
    s = quick_server({"school": {"ok": 1}}, status=status)
    try:
        assert qc.QuickClient(s.url, token_file(tmp_path), 2.0).get("school") is None
    finally:
        s.stop()


def test_client_without_a_token_makes_no_request(tmp_path):
    s = quick_server({"school": {"ok": 1}})
    try:
        for path in ("", str(tmp_path / "missing"), token_file(tmp_path, "")):
            assert qc.QuickClient(s.url, path, 2.0).get("school") is None
        assert s.calls == []
    finally:
        s.stop()


def test_client_refused_connection_timeout_and_junk_are_none(tmp_path):
    assert qc.QuickClient(H.refused_url(), token_file(tmp_path), 1.0).get("school") is None
    s = quick_server({"school": {"ok": 1}}, delay=1.5)
    try:
        assert qc.QuickClient(s.url, token_file(tmp_path), 0.5).get("school") is None
    finally:
        s.stop()
    for body in (b"not json", b"[1, 2]", b'"text"', b"x" * (qc.MAX_BODY + 10)):
        j = H.Srv(lambda m, p, b, h, body=body: (200, body, "application/json", {}))
        try:
            assert qc.QuickClient(j.url, token_file(tmp_path), 2.0).get("school") is None
        finally:
            j.stop()


def test_client_ignores_proxy_variables(tmp_path, monkeypatch):
    s = quick_server({"school": {"ok": 1}})
    try:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        assert qc.QuickClient(s.url, token_file(tmp_path), 2.0).get("school") == {"ok": 1}
    finally:
        s.stop()


# ---------------------------------------------------------------------------------------------- the service

class BoomClient:
    def get(self, topic):
        raise RuntimeError("boom")


def _service(compose, client=None, topic="x", patterns=None, **over):
    spec_cfg = cfg(intent_time, patterns=patterns or {"time": "what time is it", "date": "what day is it"}, **over)
    import logging
    svc = qc.QuickIntent("intent.t", "quick_t", topic, compose, spec_cfg, client, logging.getLogger("t"))
    svc.clock = lambda: NOW.astimezone(timezone.utc)
    return svc


def test_match_returns_the_key_only_with_a_finished_answer_and_handle_gives_it_once():
    svc = _service(lambda key, data, now, cfg: f"{key} {now:%H:%M}", topic="")
    assert svc.match("what time is it") == "time"
    assert svc.handle("time") == "time 19:42"
    assert svc.handle("time") is None     # one answer per match
    assert (svc.hits, svc.misses) == (1, 0)


def test_the_clock_is_localised_to_the_timezone_knob():
    svc = _service(lambda key, data, now, cfg: now.strftime("%H %Z"), topic="", timezone="Asia/Tokyo")
    svc.match("what time is it")
    assert svc.handle("time") == "09 JST"


@pytest.mark.parametrize("compose", [lambda *a: None, lambda *a: "", lambda *a: 1 / 0])
def test_no_answer_or_a_compose_bug_is_a_miss_not_a_crash(compose):
    svc = _service(compose, topic="")
    assert svc.match("what time is it") is None and svc.hits == 0
    assert svc.handle("time") is None


def test_a_failing_fetch_is_a_miss_and_a_client_exception_never_escapes():
    class NoneClient:
        def get(self, topic):
            return None
    for client in (NoneClient(), BoomClient()):
        svc = _service(lambda *a: "never", client)
        assert svc.match("what time is it") is None and svc.hits == 0
        assert svc.handle("time") is None
    assert _service(lambda *a: "never", NoneClient()).match("what time is it") is None


def test_a_bad_timezone_value_cannot_crash_match():
    svc = _service(lambda *a: "x", topic="", timezone="Nowhere/Land")
    assert svc.match("what time is it") is None


def test_unmatched_phrases_do_not_touch_the_network():
    calls = []

    class Spy:
        def get(self, topic):
            calls.append(topic)
            return {}
    svc = _service(lambda *a: "x", Spy())
    assert svc.match("play some music") is None and calls == []


def test_service_attributes_follow_the_intent_contract():
    svc = _service(lambda *a: "x", topic="")
    assert (svc.mark, svc.log_name, svc.done_reply, svc.fail_reply) == ("quick_t", "agent-os", qc.FALLBACK_REPLY, qc.FALLBACK_REPLY)
    assert svc.priority == 90 and svc.name == "intent.t"


# --------------------------------------------------------------------------- the five specs as a set

def test_five_specs_are_listed_and_off_by_default_without_failing_boot():
    by = {s.name: s for s in H.lp.specs()}
    for n in NAMES:
        s = by[n]
        assert (s.kind, s.machine, s.provides, s.default_enabled, s.required, s.toggle) == (
            "intent", "listener", ("intent",), False, False, "live"), n
        assert s.needs == () and s.core is False


def test_marks_are_unique_and_do_not_clash_with_loop_event_kinds():
    marks = []
    for mod in MODS:
        plugin, ctx, _ = H.started(mod.SPEC.name, url="http://127.0.0.1:1") if mod is not intent_time else H.started(mod.SPEC.name)
        marks.append(ctx.get("intent").mark)
        assert not hasattr(ctx.get("intent"), "music")       # the facade's `music` property probes for this
    assert len(set(marks)) == 5 and not set(marks) & {"music", "heard", "reply", "wake", "followup", "empty", "offline", "mic"}


def test_start_does_no_network_and_provides_one_intent_that_stop_releases():
    for mod in MODS:
        kw = {"url": H.refused_url()} if mod is not intent_time else {}
        plugin, ctx, _ = H.started(mod.SPEC.name, **kw)
        assert len(ctx.all("intent")) == 1 and ctx.all("intent")[0].priority == 90


def test_priorities_sit_below_music_and_all_are_distinct_services():
    assert intent_music.SPEC.knobs[0].default == 100
    assert all(k.default == 90 for mod in MODS for k in mod.SPEC.knobs if k.name == "priority")


def test_every_knob_has_a_short_description_and_no_agent_write_access():
    for mod in MODS:
        for k in mod.SPEC.knobs:
            assert len(k.description) <= 160 and k.agent == "deny" and k.applies == "live", (mod.SPEC.name, k.name)


def test_shared_knobs_exist_on_every_fetching_spec():
    for mod in MODS[1:]:
        names = {k.name for k in mod.SPEC.knobs}
        assert {"priority", "url", "token_file", "timeout_s", "timezone", "patterns"} <= names, mod.SPEC.name
        assert ("max_words" in names) is (mod is not intent_weather), mod.SPEC.name
        tok = next(k for k in mod.SPEC.knobs if k.name == "token_file")
        assert tok.secret is True and tok.default == "~/.config/mira/quick_token"
    assert {k.name for k in intent_time.SPEC.knobs} == {"priority", "timezone", "patterns"}


@pytest.mark.parametrize("mod", MODS, ids=NAMES)
def test_validate_accepts_defaults_and_refuses_bad_values(mod):
    mod.SPEC.validate(values(mod))
    with pytest.raises(ConfigError):
        mod.SPEC.validate(values(mod, patterns={"only": "x"}))
    first = next(iter(mod.DEFAULT_PATTERNS))
    with pytest.raises(ConfigError):
        mod.SPEC.validate(values(mod, patterns=dict(mod.DEFAULT_PATTERNS, **{first: "(unclosed"})))
    with pytest.raises(ConfigError) as e:    # a greedy pattern would swallow phrases that belong to Hermes
        mod.SPEC.validate(values(mod, patterns=dict(mod.DEFAULT_PATTERNS, **{first: ".*"})))
    assert "must map" in e.value.message
    with pytest.raises(ConfigError):
        mod.SPEC.validate(values(mod, timezone="Nowhere/Land"))
    if "url" in values(mod):
        for bad in ("http://192.168.1.10:3456", "https://os.arya-vora.org", "http://100.77.98.89:3456"):
            with pytest.raises(ConfigError):
                mod.SPEC.validate(values(mod, url=bad))
        mod.SPEC.validate(values(mod, url="http://localhost:3456"))


@pytest.mark.parametrize("mod", MODS, ids=NAMES)
def test_the_case_tables_hold_for_the_defaults(mod):
    for text, want in mod.CASES:
        assert qc.phrase_match(text, mod.DEFAULT_PATTERNS) == want, text


def test_no_phrase_is_claimed_by_two_quick_intents_or_by_music():
    """Every must-match phrase of one intent is a near-miss for the other four and for music, so priority order never
    decides between them."""
    music = {t for t, w in intent_music.MUSIC_CASES if w} | {"pause", "play", "next", "louder", "what's playing"}
    owned = {t: m.SPEC.name for m in MODS for t, w in m.CASES if w}
    for mod in MODS:
        for text, want in mod.CASES:
            if not want and text in owned:      # a near-miss here is another intent's phrase
                continue
            hits = [m.SPEC.name for m in MODS if qc.phrase_match(text, m.DEFAULT_PATTERNS)]
            assert hits == ([mod.SPEC.name] if want else []), (text, hits)
            if want:
                assert intent_music.music_intent(text) is None, text
    for text in music:
        assert not any(qc.phrase_match(text, m.DEFAULT_PATTERNS) for m in MODS), text


def test_fallthrough_phrases_for_hermes_stay_unmatched():
    for text in ("play Taylor Swift", "turn the volume up", "remind me to call mom at five", "who won the game",
                 "what's the capital of france", "tell me a joke", "how do i print a benchy", "add milk to my list",
                 "what's on my calendar next friday", "what time is the football game", "weather in tokyo"):
        assert not any(qc.phrase_match(text, m.DEFAULT_PATTERNS) for m in MODS), text


def test_patterns_have_no_dash_characters():
    for mod in MODS:
        assert not re.search("[" + chr(0x2013) + chr(0x2014) + "]", json.dumps(mod.DEFAULT_PATTERNS) + open(mod.__file__, encoding="utf-8").read())
