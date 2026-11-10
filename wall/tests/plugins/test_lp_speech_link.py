"""speech.link: Speech copied from v1 plus the timeout parameters, the tts additions and X-Mira-Voice."""
import json
import time

import pytest

import test_lp_common as H
from listener_plugins import speech_link as sl
from mirakit.plugin import ConfigError

SPEC = sl.SPEC
WAV = b"RIFFfakewavbytes"


@pytest.fixture
def token(tmp_path):
    p = tmp_path / "speech.token"
    p.write_text("tok-one\n")
    return p


def _server(voice_header=None, code=200):
    def handler(method, path, body, headers):
        if path == "/stt":
            return code, json.dumps({"text": "hello there"}).encode(), "application/json", None
        extra = {"X-Mira-Voice": voice_header} if voice_header else None
        return code, WAV, "audio/wav", extra
    return H.Srv(handler)


def test_stt_request_and_text_equal_v1(token):
    srv = _server()
    try:
        a = H.v1.Speech(srv.url, str(token))
        b = sl.Speech(srv.url, str(token))
        assert a.stt(b"abc") == b.stt(b"abc") == "hello there"
        x, y = srv.calls
        assert x["body"] == y["body"] == b"abc"
        for k in ("authorization", "content-type"):
            assert x["headers"][k] == y["headers"][k]
        assert y["headers"]["authorization"] == "Bearer tok-one"
    finally:
        srv.stop()


@pytest.mark.parametrize("voice", ["", "bella"])
def test_tts_body_is_identical_to_v1_when_no_header_comes_back(token, voice):
    srv = _server()
    try:
        a = H.v1.Speech(srv.url, str(token), voice)
        b = sl.Speech(srv.url, str(token), voice)
        assert a.tts("Good morning.") == b.tts("Good morning.") == WAV
        x, y = srv.calls
        assert x["body"] == y["body"]
        assert json.loads(y["body"]) == ({"text": "Good morning."} | ({"voice": voice} if voice else {}))
        assert x["headers"]["content-type"] == y["headers"]["content-type"] == "application/json"
        assert b.reply_voice == ""
    finally:
        srv.stop()


def test_reply_voice_is_recorded_from_unpinned_and_pinned_chunks_send_it(token):
    srv = _server(voice_header="vits-piper-en_US-amy-medium#0")
    try:
        s = sl.Speech(srv.url, str(token))
        s.tts("First chunk.")
        assert s.reply_voice == "vits-piper-en_US-amy-medium#0"
        s.tts("Second chunk.", pin=True)
        s.tts("Third chunk.", pin=True)
        bodies = [json.loads(c["body"]) for c in srv.calls]
        assert bodies[0] == {"text": "First chunk."}
        assert bodies[1] == {"text": "Second chunk.", "voice": "vits-piper-en_US-amy-medium#0"}
        assert bodies[2]["voice"] == "vits-piper-en_US-amy-medium#0"
        s.tts("A new reply.")  # unpinned again: no stale voice, the header is read afresh
        assert "voice" not in json.loads(srv.calls[3]["body"])
    finally:
        srv.stop()


def test_pin_without_a_known_voice_falls_back_to_the_configured_one(token):
    srv = _server()
    try:
        s = sl.Speech(srv.url, str(token), "bella")
        s.tts("Chunk two.", pin=True)
        assert json.loads(srv.calls[0]["body"]) == {"text": "Chunk two.", "voice": "bella"}
    finally:
        srv.stop()


def test_stale_reply_voice_resets_against_a_server_without_the_header(token):
    with_header, without = _server("amy#0"), _server()
    try:
        s = sl.Speech(with_header.url, str(token))
        s.tts("x")
        assert s.reply_voice == "amy#0"
        s.url = without.url
        s.tts("y")
        assert s.reply_voice == ""
    finally:
        with_header.stop(); without.stop()


def test_speed_and_peak_only_when_set_and_render_keeps_no_state(token):
    srv = _server(voice_header="amy#0")
    try:
        s = sl.Speech(srv.url, str(token))
        s.tts("a", speed=1.2, peak=0.8)
        s.tts("b", voice="kristin#0", speed=0.0, peak=0.0)
        before = s.reply_voice
        s.render("c", voice="jenny#0", speed=1.1, peak=0.9)
        assert s.reply_voice == before
        bodies = [json.loads(c["body"]) for c in srv.calls]
        assert bodies == [{"text": "a", "speed": 1.2, "peak": 0.8}, {"text": "b", "voice": "kristin#0"},
                          {"text": "c", "voice": "jenny#0", "speed": 1.1, "peak": 0.9}]
    finally:
        srv.stop()


def test_token_is_read_on_every_call(token):
    srv = _server()
    try:
        s = sl.Speech(srv.url, str(token))
        s.stt(b"1")
        token.write_text("tok-two\n")
        s.stt(b"2")
        s.tts("x")
        assert [c["headers"]["authorization"] for c in srv.calls] == ["Bearer tok-one", "Bearer tok-two", "Bearer tok-two"]
    finally:
        srv.stop()


@pytest.mark.parametrize("make", ["refused", "http500", "timeout"])
def test_offline_mapping_matches_v1(token, make):
    if make == "refused":
        url, srv, kw = H.refused_url(), None, {}
    elif make == "http500":
        srv = _server(code=500); url, kw = srv.url, {}
    else:
        def slow(method, path, body, headers):
            time.sleep(1.5)
            return 200, b"{}", "application/json", None
        srv = H.Srv(slow); url, kw = srv.url, {"stt_timeout_s": 0.3, "tts_timeout_s": 0.3}
    try:
        for cls_, extra in ((H.v1.Speech, {}), (sl.Speech, kw)):
            s = cls_(url, str(token), **extra)
            if make == "timeout" and cls_ is H.v1.Speech:
                continue  # v1's timeouts are fixed at 30 and 45 s
            exc = H.v1.SpeechOffline if cls_ is H.v1.Speech else sl.SpeechOffline
            with pytest.raises(exc):
                s.stt(b"x")
            with pytest.raises(exc):
                s.tts("x")
        assert issubclass(sl.SpeechOffline, Exception) and sl.SpeechOffline is not H.v1.SpeechOffline
    finally:
        if srv:
            srv.stop()


def test_defaults_equal_v1_literals(token):
    s = sl.Speech("http://x/", str(token))
    assert (s.url, s.stt_timeout_s, s.tts_timeout_s, s.voice, s.reply_voice) == ("http://x", 30, 45, "", "")


def test_plugin_provides_speech_and_live_knobs_apply(tmp_path):
    plugin, ctx, cfg = H.started("speech.link", url="http://127.0.0.1:9/", token_file="${HOME}/tok")
    s = ctx.get("speech")
    assert s.url == "http://127.0.0.1:9" and s.token_file == str(ctx.paths.home / "tok")
    H.assert_live_knobs(SPEC, plugin, {
        "url": ("http://100.77.98.89:1/", lambda p: s.url, "http://100.77.98.89:1"),
        "token_file": ("/x/tok", lambda p: s.token_file), "stt_timeout_s": (50.0, lambda p: s.stt_timeout_s),
        "tts_timeout_s": (60.0, lambda p: s.tts_timeout_s)})
    ctx.release_all()


@pytest.mark.parametrize("url,ok", [("http://100.77.98.89:8765", True), ("http://127.0.0.1:8765", True),
                                    ("http://192.168.1.9:8765", False), ("http://example.com", False)])
def test_validate_host_rule(url, ok):
    values = {k.name: k.default for k in SPEC.knobs}
    if ok:
        SPEC.validate(dict(values, url=url))
    else:
        with pytest.raises(ConfigError) as e:
            SPEC.validate(dict(values, url=url))
        assert e.value.field == "url"
