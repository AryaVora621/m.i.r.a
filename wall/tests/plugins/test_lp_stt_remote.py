"""stt.remote: provides `stt` as the same object speech.link provides."""
import json

import pytest

import test_lp_common as H
from listener_plugins import stt_remote as sr

SPEC = sr.SPEC


def test_stt_is_the_speech_object_and_transcribes(tmp_path):
    srv = H.Srv(lambda m, p, b, h: (200, json.dumps({"text": "what time is it"}).encode(), "application/json", None))
    try:
        tok = tmp_path / "t"
        tok.write_text("abc")
        _, ctx, _ = H.started("speech.link", url=srv.url, token_file=str(tok))
        plugin, ctx, _ = H.started("stt.remote", ctx=ctx)
        assert ctx.get("stt") is ctx.get("speech")
        assert ctx.get("stt").stt(b"wav") == "what time is it"
    finally:
        srv.stop()


def test_needs_speech_and_has_no_knobs():
    assert SPEC.needs == ("speech",) and SPEC.provides == ("stt",) and SPEC.knobs == ()


def test_start_without_speech_fails_loud():
    ctx = H.make_ctx()
    with pytest.raises(KeyError):
        H.started("stt.remote", ctx=ctx)
