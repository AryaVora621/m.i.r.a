"""persona.file: the identity text, read once or when its mtime changes."""
import os

import pytest

import test_lp_common as H
from listener_plugins import persona_file as pf

SPEC = pf.SPEC


def _v1_persona(path):
    from pathlib import Path
    return Path(path).read_text() if Path(path).exists() else "You are Mira."


def test_text_equals_what_v1_read(tmp_path):
    p = tmp_path / "id.md"
    p.write_text("You are Mira, the wall.\nBe brief.\n")
    assert pf.Persona(str(p)).text() == _v1_persona(p)
    assert pf.Persona(str(tmp_path / "missing.md")).text() == _v1_persona(tmp_path / "missing.md") == "You are Mira."


def test_start_reads_once_and_turn_rereads_on_change(tmp_path):
    p = tmp_path / "id.md"
    p.write_text("one")
    once = pf.Persona(str(p), reread="start")
    turn = pf.Persona(str(p), reread="turn")
    p.write_text("two")
    os.utime(p, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    assert once.text() == "one" and turn.text() == "two"
    assert turn.text() == "two"


def test_turn_mode_uses_the_mtime_cache(tmp_path, monkeypatch):
    p = tmp_path / "id.md"
    p.write_text("a")
    turn = pf.Persona(str(p), reread="turn")
    reads = []
    real = pf.Path.read_text
    monkeypatch.setattr(pf.Path, "read_text", lambda self, *a, **k: (reads.append(1), real(self, *a, **k))[1])
    for _ in range(5):
        turn.text()
    assert reads == []  # unchanged mtime: no reread
    p.write_text("bb")
    os.utime(p, ns=(3_000_000_000_000_000_000, 3_000_000_000_000_000_000))
    assert turn.text() == "bb" and len(reads) == 1


def test_turn_mode_survives_a_file_that_disappears_and_returns(tmp_path):
    p = tmp_path / "id.md"
    p.write_text("a")
    turn = pf.Persona(str(p), fallback="fb", reread="turn")
    p.unlink()
    assert turn.text() == "fb"
    p.write_text("back")
    assert turn.text() == "back"


def test_plugin_provides_persona_with_expansion(tmp_path):
    ctx = H.make_ctx()
    (ctx.paths.here / "MIRA_WALL_IDENTITY.md").write_text("From HERE.")
    plugin, ctx, _ = H.started("persona.file", ctx=ctx)
    assert ctx.get("persona").text() == "From HERE."
    plugin2, ctx2, _ = H.started("persona.file", path="${HOME}/missing.md", fallback="You are Mira.")
    assert ctx2.get("persona").text() == "You are Mira."


def test_live_knobs_apply(tmp_path):
    p = tmp_path / "id.md"
    plugin, ctx, _ = H.started("persona.file", path=str(p))
    per = ctx.get("persona")
    H.assert_live_knobs(SPEC, plugin, {"fallback": ("Other.", lambda x: per.fallback),
                                      "reread": ("turn", lambda x: per.reread)})
    plugin.apply({"fallback": "New fallback."}, plugin.cfg.replace(fallback="New fallback."))
    assert per.text() == "New fallback."  # still on the fallback: the new one shows
    assert {k.name for k in SPEC.knobs if k.applies == "restart"} == {"path"}
