"""intent.screens: wall screens by voice through Agent OS's screen hook, with a fake Agent OS that refuses what the real
one refuses (bad bearer 401, unknown template or source or lifetime 422, data that does not fit the template 422, a
full stack is not modelled)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import test_lp_common as H
from listener_plugins import intent_screens as isc

SPEC = isc.SPEC
TOKEN = "screen-token-value"
# agentic-os pi/server/screens.py: the hook route's template enum, sources and lifetime limits.
HOOK_TEMPLATES = {"morning", "timer", "alarm", "focus", "school", "week", "exams", "homelab", "recap", "board",
                  "print-studio", "cad", "keepsake", "answer", "list", "freeform"}
HOOK_SOURCES = {"mira", "hermes", "schedule", "owner"}
REQUIRED_DATA = {"print-studio": {"title", "jobs"}, "school": {"title", "items"}, "week": {"title", "days"},
                 "homelab": {"title", "services"}, "morning": {"note"}, "recap": {"note"}, "exams": {"title", "items"},
                 "board": {"title", "columns"}}


class FakeAgentOS:
    def __init__(self, status=None):
        self.calls, self.stack, self.status, self.n = [], {}, status, 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.calls.append({"method": self.command, "path": self.path, "body": body,
                                    "auth": self.headers.get("Authorization", "")})
                if outer.status:
                    return self._send(outer.status, {"detail": "down"})
                if self.headers.get("Authorization", "") != f"Bearer {TOKEN}":
                    return self._send(401, {"detail": "Invalid hook token"})
                if self.command == "POST" and self.path == "/api/hooks/screen":
                    try:
                        spec = json.loads(body)
                    except ValueError:
                        return self._send(422, {"detail": "bad json"})
                    tpl, data = spec.get("template"), spec.get("data")
                    ttl = spec.get("ttl_s")
                    bad = (tpl not in HOOK_TEMPLATES or spec.get("source") not in HOOK_SOURCES
                           or spec.get("priority") not in ("ambient", "info", "alert")
                           or not isinstance(ttl, int) or not 5 <= ttl <= 14400
                           or not isinstance(data, dict) or not REQUIRED_DATA.get(tpl, set()) <= set(data))
                    if bad:
                        return self._send(422, {"detail": "invalid screen"})
                    outer.n += 1
                    sid = f"id{outer.n:04d}"
                    outer.stack[sid] = spec
                    return self._send(200, {"id": sid})
                if self.command == "DELETE" and self.path.startswith("/api/hooks/screen/"):
                    sid = self.path.rsplit("/", 1)[1]
                    return self._send(200, {"ok": outer.stack.pop(sid, None) is not None})
                return self._send(404, {"detail": "no such route"})

            do_GET = do_POST = do_DELETE = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def token(tmp_path):
    p = tmp_path / "screen_token"
    p.write_text(TOKEN + "\n")
    return str(p)


@pytest.fixture
def agent_os():
    s = FakeAgentOS()
    yield s
    s.stop()


def _intent(token_file, url, **over):
    plugin, ctx, cfg = H.started("intent.screens", url=url, token_file=token_file, **over)
    return plugin, ctx.get("intent")


TABLE = isc.parse_aliases(isc.DEFAULT_ALIASES)


@pytest.mark.parametrize("text,want", isc.CASES, ids=[c[0] for c in isc.CASES])
def test_matching_table(text, want):
    assert isc.screens_intent(text, TABLE) == want


@pytest.mark.parametrize("heard,template", [
    ("bring up my 3 D printers", "print-studio"), ("bring up my three d printers", "print-studio"),
    ("bring up my 3-D printers", "print-studio"), ("bring up the printer", "print-studio"),
    ("bring up my prints", "print-studio"), ("show me my printerz", "print-studio"),
    ("pull up school", "school"), ("pull up the assignments", "school"), ("pull up my scool", "school"),
    ("show homework", "school"), ("show my week", "week"), ("put up the calender", "week"),
    ("bring up homelab", "homelab"), ("bring up the home lab", "homelab"), ("show me the severs", "homelab"),
    ("bring up the brief", "morning"), ("bring up the morning brief", "morning"), ("bring up recap", "recap"),
    ("show exams", "exams"), ("bring up the build board", "board"),
])
def test_likely_mishearings_resolve(heard, template):
    assert isc.screens_intent(heard, TABLE) == f"show:{template}"


@pytest.mark.parametrize("heard", [
    "bring up my journey, Hermes.", "bring up my journey", "show me the weather", "show me a recipe",
    "pull up the printed circuit board", "bring up my schedule of classes next quarter", "open the printer door",
    "show me what is for dinner", "bring me a coffee", "put up the shelves", "bring up the school grades",
])
def test_unclear_requests_fall_through(heard):
    assert isc.screens_intent(heard, TABLE) is None


def test_does_not_steal_music_or_quick_phrases():
    from listener_plugins.intent_music import music_intent, DEFAULT_PATTERNS
    for text in ("turn it down", "turn it up", "stop", "pause", "play", "next", "volume down", "quieter", "what's playing"):
        assert isc.screens_intent(text, TABLE) is None, text
    # and the music intent is untouched by the screen phrases
    for text in ("bring up my printers", "close it", "take it down", "clear the wall"):
        assert music_intent(text, DEFAULT_PATTERNS) is None, text


def test_show_pushes_the_agent_route_equivalent_through_the_hook(token, agent_os):
    plugin, i = _intent(token, agent_os.url)
    assert i.match("Bring up my 3D printers.") == "show:print-studio"
    assert i.handle("show:print-studio") == "Printers are up."
    assert len(agent_os.calls) == 1
    call = agent_os.calls[0]
    assert (call["method"], call["path"], call["auth"]) == ("POST", "/api/hooks/screen", f"Bearer {TOKEN}")
    assert json.loads(call["body"]) == {"template": "print-studio", "data": {"title": "Print Studio", "jobs": []},
                                        "ttl_s": 300, "priority": "info", "source": "mira"}
    assert len(agent_os.stack) == 1


@pytest.mark.parametrize("template,spoken", [
    ("school", "bring up school"), ("week", "bring up my week"), ("homelab", "bring up the homelab"),
    ("morning", "bring up the morning brief"), ("recap", "bring up the recap"), ("exams", "bring up my exams"),
    ("board", "bring up the build board")])
def test_every_template_is_accepted_by_the_hook(token, agent_os, template, spoken):
    plugin, i = _intent(token, agent_os.url)
    assert i.match(spoken) == f"show:{template}"
    assert list(agent_os.stack.values())[0]["template"] == template


def test_replies_are_five_words_or_fewer():
    for key, text in isc.DEFAULT_REPLIES.items():
        assert 1 <= len(text.split()) <= 5, key


def test_a_second_push_of_the_same_screen_replaces_mine_instead_of_stacking(token, agent_os):
    plugin, i = _intent(token, agent_os.url)
    assert i.match("bring up the printers")
    assert i.match("show me the printers")
    assert len(agent_os.stack) == 1
    assert [c["method"] for c in agent_os.calls] == ["POST", "DELETE", "POST"]
    assert i.match("bring up my week")
    assert len(agent_os.stack) == 2


def test_close_it_removes_only_the_last_screen_this_listener_pushed(token, agent_os):
    plugin, i = _intent(token, agent_os.url)
    agent_os.stack["owner1"] = {"template": "timer", "source": "owner"}   # not mine: must survive everything
    i.match("bring up my week")
    i.match("bring up the printers")
    assert i.match("close it") == "clear:last"
    assert i.handle("clear:last") == "Cleared."
    assert {s["template"] for s in agent_os.stack.values()} == {"week", "timer"}
    assert i.match("take it down") == "clear:last"
    assert set(agent_os.stack) == {"owner1"}
    assert i.match("close it") is None  # nothing of mine is left: Hermes decides


def test_clear_the_wall_removes_only_my_screens_never_by_template_or_all(token, agent_os):
    plugin, i = _intent(token, agent_os.url)
    agent_os.stack["sched1"] = {"template": "school", "source": "schedule"}
    i.match("bring up my week")
    i.match("bring up the printers")
    assert i.match("clear the wall") == "clear:all"
    assert i.handle("clear:all") == "Cleared."
    assert set(agent_os.stack) == {"sched1"}
    deletes = [c["path"] for c in agent_os.calls if c["method"] == "DELETE"]
    assert deletes and all(p.startswith("/api/hooks/screen/") and p != "/api/hooks/screen/" for p in deletes)
    assert not any("?" in c["path"] or c["path"] == "/api/hooks/screen" for c in agent_os.calls if c["method"] == "DELETE")


def test_close_with_nothing_of_mine_goes_to_hermes_without_a_call(token, agent_os):
    plugin, i = _intent(token, agent_os.url)
    assert i.match("close it") is None and i.match("clear the wall") is None
    assert agent_os.calls == []


def test_an_expired_screen_is_not_deleted_and_not_claimed(token, agent_os):
    plugin, i = _intent(token, agent_os.url)
    now = [1000.0]
    i.clock = lambda: now[0]
    i.match("bring up the printers")
    now[0] += 301
    assert i.match("close it") is None
    assert [c["method"] for c in agent_os.calls] == ["POST"]


def test_a_screen_that_agent_os_already_dropped_is_not_claimed_cleared(token, agent_os):
    plugin, i = _intent(token, agent_os.url)
    i.match("bring up the printers")
    agent_os.stack.clear()
    assert i.match("close it") is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_agent_os_errors_hand_the_request_to_hermes(token, status):
    s = FakeAgentOS(status=status)
    try:
        plugin, i = _intent(token, s.url)
        assert i.match("bring up the printers") is None
        assert i.misses == 1 and i.handle("show:print-studio") is None
    finally:
        s.stop()


def test_wrong_token_is_refused_like_the_real_route(tmp_path, agent_os):
    bad = tmp_path / "t"
    bad.write_text("not-the-token\n")
    plugin, i = _intent(str(bad), agent_os.url)
    assert i.match("bring up the printers") is None
    assert agent_os.calls[0]["auth"] == "Bearer not-the-token" and agent_os.stack == {}


def test_missing_or_empty_token_file_never_reaches_the_network(tmp_path, agent_os):
    plugin, i = _intent(str(tmp_path / "nope"), agent_os.url)
    assert i.match("bring up the printers") is None
    empty = tmp_path / "empty"
    empty.write_text("\n")
    plugin, i = _intent(str(empty), agent_os.url)
    assert i.match("bring up the printers") is None
    assert agent_os.calls == []


def test_unreachable_agent_os_falls_through(token):
    plugin, i = _intent(token, H.refused_url())
    assert i.match("bring up the printers") is None


def test_a_slow_agent_os_costs_at_most_the_timeout(token):
    import time
    blocker = threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            blocker.wait(5)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        plugin, i = _intent(token, f"http://127.0.0.1:{httpd.server_address[1]}", timeout_s=0.5)
        t0 = time.monotonic()
        assert i.match("bring up the printers") is None
        assert time.monotonic() - t0 < 2.0
    finally:
        blocker.set()
        httpd.shutdown()
        httpd.server_close()


def test_junk_ids_from_agent_os_are_refused(token):
    class Junk(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            data = b'{"id": "../../etc/passwd"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Junk)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        plugin, i = _intent(token, f"http://127.0.0.1:{httpd.server_address[1]}")
        assert i.match("bring up the printers") is None and i.shown == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_minutes_knob_sets_the_lifetime(token, agent_os):
    plugin, i = _intent(token, agent_os.url, minutes=2.0)
    i.match("bring up my week")
    assert json.loads(agent_os.calls[0]["body"])["ttl_s"] == 120


def test_not_a_screen_phrase_makes_no_call(token, agent_os):
    plugin, i = _intent(token, agent_os.url)
    assert i.match("what's the weather") is None and i.match("bring up my journey, Hermes.") is None
    assert agent_os.calls == [] and i.misses == 0


def test_live_knobs_apply():
    plugin, ctx, cfg = H.started("intent.screens")
    i = ctx.get("intent")
    aliases = dict(isc.DEFAULT_ALIASES, week="agenda")
    replies = dict(isc.DEFAULT_REPLIES, cleared="Gone.")
    H.assert_live_knobs(SPEC, plugin, {
        "priority": (7, lambda p: i.priority), "url": ("http://127.0.0.1:1/", lambda p: i.client.url, "http://127.0.0.1:1"),
        "token_file": ("/t/screen", lambda p: i.client.token_file), "timeout_s": (1.5, lambda p: i.client.timeout_s),
        "minutes": (3.0, lambda p: i.minutes), "aliases": (aliases, lambda p: i.table["week"], {"agenda"}),
        "replies": (replies, lambda p: i.replies)})


def test_validate_refuses_bad_values():
    values = {k.name: k.default for k in SPEC.knobs}
    SPEC.validate(values)
    from mirakit.plugin import ConfigError
    for over, field in [({"url": "http://100.77.98.89:3456"}, "url"),
                        ({"aliases": dict(isc.DEFAULT_ALIASES, week="")}, "aliases"),
                        ({"aliases": {"week": "week"}}, "aliases"),
                        ({"aliases": dict(isc.DEFAULT_ALIASES, week="agenda")}, "aliases"),   # breaks "bring up my week"
                        ({"replies": dict(isc.DEFAULT_REPLIES, cleared="That is all gone now for you.")}, "replies"),
                        ({"replies": {"cleared": "Cleared."}}, "replies")]:
        with pytest.raises(ConfigError) as e:
            SPEC.validate(dict(values, **over))
        assert e.value.field == field, over


def test_spec_shape():
    assert SPEC.name == "intent.screens" and SPEC.provides == ("intent",) and SPEC.required is False
    assert SPEC.default_enabled is True and SPEC.toggle == "live"
    assert [k.name for k in SPEC.knobs if k.secret] == ["token_file"]
    assert isc.LEGACY_CLASSES == ()
