"""Integration: the quick-answer intents inside the real facade and core (rig from test_int_facade).

Proves the three promises that matter: off by default the turn is exactly what it was; on, a clear match is answered
without Hermes; on, a fetch that fails hands the same turn to Hermes with nothing left behind.
"""
import json
from datetime import datetime, timezone

import pytest

from golden import fakes
from test_int_facade import CAL_REPLY, CAL_TEXT, cal_audio, rig_factory  # noqa: F401  (the fixture is imported by name)

TOKEN = "quick-int-token"


class QuickServer(fakes.RecordingServer):
    def __init__(self, status=200, events=()):
        self.status, self.events = status, list(events)
        super().__init__()

    def respond(self, method, path, body, headers, index):
        if self.status != 200:
            return self.status, b'{"detail":"no"}', "application/json"
        if path == "/api/hooks/quick/calendar":
            feed = {"updated_at": datetime.now(timezone.utc).isoformat(), "events": self.events}
            return 200, json.dumps(feed).encode(), "application/json"
        return 404, b"{}", "application/json"


@pytest.fixture
def quick(tmp_path):
    token = tmp_path / "quick_token"
    token.write_text(TOKEN + "\n")
    servers = []

    def make(**kw):
        s = QuickServer(**kw)
        servers.append(s)
        return s, str(token)
    yield make
    for s in servers:
        s.stop()


def enabled(srv, token):
    return {"intent.calendar": {"enabled": True, "config": {"url": srv.url, "token_file": token}}}


def replies(r):
    return [d for k, d in r.lis.events if k == "reply"]


def test_default_config_has_only_music_and_the_calendar_question_goes_to_hermes(rig_factory):
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes=[CAL_REPLY], mode="file")
    assert [p.name for p in r.lis.intents] == ["intent.music", "intent.screens"]  # screens is on by default
    r.run()
    assert replies(r) == [CAL_REPLY] and r.marks().count("reply") == 1
    assert not any(k.startswith("quick_") for k in r.marks())
    assert len([c for c in r.hermes.calls if c["path"].endswith("/chat/stream")]) == 1


def test_a_clear_match_is_answered_without_hermes(rig_factory, quick):
    srv, token = quick()
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes=[CAL_REPLY], mode="file", file_plugins=enabled(srv, token))
    assert [p.name for p in r.lis.intents] == ["intent.music", "intent.screens", "intent.calendar"]
    sources = []
    r.core.on("turn.replied", lambda p: sources.append(p["source"]))
    r.run()
    assert replies(r) == ["Nothing else on your calendar today."]
    assert "quick_calendar" in r.marks() and r.hermes.calls == [] and sources == ["intent"]
    assert [c["path"] for c in srv.calls] == ["/api/hooks/quick/calendar"] and srv.calls[0]["has_auth"] is True


@pytest.mark.parametrize("status", [503, 401, 500])
def test_a_failed_fetch_hands_the_whole_turn_to_hermes_as_today(rig_factory, quick, status):
    srv, token = quick(status=status)
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes=[CAL_REPLY], mode="file", file_plugins=enabled(srv, token))
    sources = []
    r.core.on("turn.replied", lambda p: sources.append(p["source"]))
    r.run()
    assert replies(r) == [CAL_REPLY] and sources == ["brain"]
    assert not any(k.startswith("quick_") for k in r.marks())
    assert len([c for c in r.hermes.calls if c["path"].endswith("/chat/stream")]) == 1


def test_an_unreachable_agent_os_falls_through_too(rig_factory, quick):
    srv, token = quick()
    url = srv.url
    srv.stop()
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes=[CAL_REPLY], mode="file",
                    file_plugins={"intent.calendar": {"enabled": True, "config": {"url": url, "token_file": token}}})
    r.run()
    assert replies(r) == [CAL_REPLY]


def test_the_time_needs_no_server_at_all(rig_factory):
    r = rig_factory(cal_audio(), stt=["Mira, what time is it?"], hermes=[CAL_REPLY], mode="file",
                    file_plugins={"intent.time": {"enabled": True, "config": {}}})
    r.run()
    out = replies(r)
    assert len(out) == 1 and out[0].startswith("It is ") and r.hermes.calls == []


def test_music_still_wins_its_own_phrases_when_quick_intents_are_on(rig_factory, quick):
    from golden import scenarios
    import mira_listen as ml
    srv, token = quick()
    r = rig_factory(scenarios.compose(2.0, scenarios.pause(ml), 10.0), stt=["Mira, pause the music."],
                    mode="file", file_plugins=enabled(srv, token))
    r.run()
    assert "music" in r.marks() and not any(k.startswith("quick_") for k in r.marks()) and srv.calls == []
