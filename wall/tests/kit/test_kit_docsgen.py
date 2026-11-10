"""Documentation generator (P6): deterministic output, content, references, staleness."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import docsgen as G  # noqa: E402
from mirakit import plugin as P  # noqa: E402
from mirakit import testkit as TK  # noqa: E402

DASH = chr(0x2013), chr(0x2014)


def specs():
    def mk(name, machine, knobs, **kw):
        return P.PluginSpec(name=name, kind=name.split(".")[0], version="2", summary=f"{name} summary", machine=machine,
                            provides=kw.pop("provides", (name.split(".")[0],)), create=lambda c, g: None,
                            knobs=tuple(knobs), **kw)

    agc = mk("agc.mic", "listener", [
        P.Knob("target", "float", 0.05, "Speech level the gain aims for.", unit="rms", min=0.005, max=0.3,
               agent="bounded", agent_min=0.03, agent_max=0.10, agent_step=0.01, topic="hearing",
               spoken="microphone boost", effect="hears quiet speech from farther away",
               phrases=("you can't hear me", "you don't hear me from my desk"), env="MIRA_AGC_TARGET", arg="agc_target"),
        P.Knob("floor_init", "float", 0.003, "Starting noise floor | with a pipe.", unit="rms", min=0, max=0.1,
               applies="restart", env="MIRA_AGC_FLOOR"),
        P.Knob("on", "bool", True, "Software gain stage on the mic.", applies="restart", arg="agc"),
    ], needs=("source",), reads=("reporter.agentos.level_scale",), toggle="live")
    wake = mk("wake.kws", "listener", [
        P.Knob("threshold", "float", 0.15, "Spotter sensitivity.", unit="score", min=0.01, max=0.5, applies="rebuild",
               topic="wake", spoken="wake sensitivity", effect="fires less often",
               phrases=("you wake up too much",)),
        P.Knob("token_file", "path", "", "Token path.", secret=True),
    ])
    tts = mk("tts.engine", "speech", [
        P.Knob("voice", "voice", "vits-piper-en_US-amy-medium#0", "Her voice.", agent="bounded", topic="voice",
               spoken="my voice", phrases=("change your voice",)),
        P.Knob("speed", "float", 1.0, "How fast she talks.", unit="x", min=0.5, max=1.6, agent="bounded",
               agent_min=0.8, agent_max=1.3, agent_step=0.1, topic="voice", spoken="how fast I talk",
               effect="talks faster", phrases=("slow down",)),
        P.Knob("names", "list[str]", ["a", "b"], "Names.", min=0, max=5),
    ])
    return [tts, wake, agc]          # deliberately unsorted


def test_output_is_deterministic_and_independent_of_input_order(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    G.write_all(a, specs(), skill_dir=a / "skill", vault_dir=a / "vault")
    G.write_all(b, list(reversed(specs())), skill_dir=b / "skill", vault_dir=b / "vault")
    files_a = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    assert files_a == sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    for rel in files_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel
    again = tmp_path / "c"
    G.write_all(again, specs(), skill_dir=again / "skill", vault_dir=again / "vault")
    for rel in files_a:
        assert (a / rel).read_bytes() == (again / rel).read_bytes()


def test_plugins_md_content():
    text = G.render_plugins_md(specs())
    assert text.index("## Machine: listener") < text.index("## Machine: speech")
    assert text.index("### agc.mic") < text.index("### wake.kws")                    # sorted inside a machine
    assert "needs: source" in text and "reads: reporter.agentos.level_scale" in text and "toggle live" in text
    assert "| target | float | 0.05 | 0.005..0.3 | rms | live | 0.03..0.1 step 0.01 |" in text
    assert "Starting noise floor \\| with a pipe." in text                          # table cells escape pipes
    assert "| floor_init | float | 0.003 | 0..0.1 | rms | restart | deny |" in text
    assert "| names | list[str] | [\"a\", \"b\"] | 0..5 items |" in text
    assert "(secret path)" in text


def test_knobs_md_has_the_2_4_6_columns_and_legacy_info():
    text = G.render_knobs_md(specs(), {"agc.mic.floor_init": "L225"})
    header = text.splitlines()[4]
    assert header == ("| plugin | key | type | default | bounds | unit | applies | agent | legacy env | legacy flag | "
                      "v1 source |")
    # a knob with both an env var and a flag shows both (spec 2.4.2: "MIRA_AGC_TARGET, --agc-target")
    assert "| agc.mic | target | float | 0.05 | 0.005..0.3 | rms | live | 0.03..0.1 step 0.01 | MIRA_AGC_TARGET | --agc-target |  |" in text
    assert "| agc.mic | on | bool | true |  |  | restart | deny |  | --agc |  |" in text
    only_real = G.render_knobs_md(specs(), None, frozenset({"--agc"}))
    assert "| MIRA_AGC_TARGET |  |  |" in only_real and "| --agc |  |" in only_real     # --agc-target is not a real flag here
    assert "MIRA_AGC_FLOOR |  | L225 |" in text
    assert "| wake.kws | threshold | float | 0.15 | 0.01..0.5 | score | rebuild | deny |  |  | literal |" in text


def test_discover_legacy_flags_reads_literals_and_expands_loops(tmp_path):
    assert G.discover_legacy_flags(tmp_path) is None
    (tmp_path / "mira_listen_v1.py").write_text(
        "import argparse\n"
        "def build_parser():\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--kws-dir')\n"
        "    p.add_argument('--agc', action=argparse.BooleanOptionalAction)\n"
        "    for knob in ('target', 'max-gain'):\n"
        "        p.add_argument(f'--agc-{knob}', type=float)\n"
        "    p.add_argument('positional')\n"
        "    return p\n")
    assert G.discover_legacy_flags(tmp_path) == frozenset({"--kws-dir", "--agc", "--agc-target", "--agc-max-gain"})
    (tmp_path / "mira_listen_v1.py").write_text("def (:\n")
    assert G.discover_legacy_flags(tmp_path) is None


def test_discover_on_the_real_frozen_listener_matches_its_argparse_parser():
    root = WALL
    flags = G.discover_legacy_flags(root)
    if flags is None:
        pytest.skip("mira_listen_v1.py is not present")
    import importlib
    v1 = importlib.import_module("mira_listen_v1")
    real = {o for a in v1.build_parser()._actions for o in a.option_strings if o.startswith("--")} - {"--help"}
    assert real - {"--no-agc"} <= flags and flags <= real | {"--no-agc"}
    assert "--agc-target" in flags and "--followup-wait-s" not in flags and "--preroll-chunks" not in flags


def test_schema_json_roundtrips_and_is_stable():
    text = G.render_schema_json(specs())
    data = json.loads(text)
    assert data["contract_version"] == 1 and sorted(data["machines"]) == ["listener", "speech"]
    names = [p["name"] for p in data["machines"]["listener"]]
    assert names == ["agc.mic", "wake.kws"]
    knob = data["machines"]["listener"][0]["knobs"][0]
    assert knob["name"] == "target" and knob["agent_step"] == 0.01 and knob["phrases"] == [
        "you can't hear me", "you don't hear me from my desk"] and knob["env"] == "MIRA_AGC_TARGET"
    assert text.endswith("\n") and text == G.render_schema_json(specs())


def test_skill_references_come_from_the_knobs():
    knobs = G.render_skill_knobs(specs())
    assert "## hearing" in knobs and "## voice" in knobs and "## wake" in knobs and "## brain" not in knobs
    assert "| agc.mic.target | microphone boost | yes | 0.03..0.1 rms | 0.01 | 0.03 0.04 0.05 0.06 0.07 0.08 0.09 0.1 |" in knobs
    assert "| wake.kws.threshold | wake sensitivity | owner only |" in knobs
    assert "| tts.engine.voice | my voice | yes | any usable |" in knobs
    gloss = G.render_skill_glossary(specs())
    assert '| "you can\'t hear me" | agc.mic.target | microphone boost | hears quiet speech from farther away | 0.03..0.1 step 0.01 |' in gloss
    assert '| "you wake up too much" | wake.kws.threshold | wake sensitivity | fires less often | owner only |' in gloss
    assert '| "change your voice" | tts.engine.voice | my voice |  | yes |' in gloss
    assert G.render_skill_glossary(specs()) == G.render_skill_glossary(list(reversed(specs())))


def test_vault_reference_body():
    text = G.render_vault_reference(specs())
    assert text.startswith("# Mira Plugin Reference") and "### agc.mic (v2)" in text
    assert "- `target` (float, default 0.05, rms, 0.005..0.3; live; agent 0.03..0.1 step 0.01): Speech level" in text
    assert "mira set" in text


def test_tuning_guide_data_matches_the_api_shape_of_fakecore():
    ss = specs()
    core = TK.FakeCore(ss, machine="speech")
    for topic in ("voice", "hearing", "wake"):
        mine = G.tuning_guide_data(ss, topic)
        theirs = core.tuning_guide(topic, scope="agent")
        assert set(mine) == set(theirs) == {"topic", "knobs", "owner_only"}
        mine_keys = [k["key"] for k in mine["knobs"]]
        assert mine_keys == [k["key"] for k in theirs["knobs"]]
        for a, b in zip(mine["knobs"], theirs["knobs"]):
            assert set(a) == set(b)
            assert a["ladder"] == b["ladder"] and a["agent_step"] == b["agent_step"]
        assert [o["key"] for o in mine["owner_only"]] == [o["key"] for o in theirs["owner_only"]]
        for o in mine["owner_only"]:
            assert set(o) == {"key", "spoken", "why", "owner_command"} and o["owner_command"].startswith("mira set ")
    assert G.tuning_guide_data(ss, "voice")["knobs"][1]["ladder"] == [0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
    assert G.tuning_guide_data(ss, "nothing") == {"topic": "nothing", "knobs": [], "owner_only": []}


def test_ladder_edge_cases():
    assert G.ladder(P.Knob("n", "int", 6, "d", unit="x", min=1, max=20, agent="bounded", agent_min=4, agent_max=14,
                           agent_step=2)) == [4, 6, 8, 10, 12, 14]
    assert G.ladder(P.Knob("n", "int", 6, "d", unit="x", min=1, max=20)) == []


def test_stale_detection(tmp_path):
    ss = specs()
    assert sorted(G.stale_files(tmp_path, ss)) == sorted(G.GENERATED_PATHS)
    G.write_all(tmp_path, ss)
    assert G.stale_files(tmp_path, ss) == []
    (tmp_path / G.PLUGINS_MD).write_text((tmp_path / G.PLUGINS_MD).read_text() + "x")
    assert G.stale_files(tmp_path, ss) == [G.PLUGINS_MD]
    changed = specs()
    changed[2] = P.PluginSpec(**{**changed[2].__dict__, "version": "3"})
    assert G.PLUGINS_MD in G.stale_files(tmp_path, changed) and G.SCHEMA_JSON in G.stale_files(tmp_path, changed)


def test_write_all_returns_paths_and_creates_directories(tmp_path):
    written = G.write_all(tmp_path, specs(), skill_dir=tmp_path / "s", vault_dir=tmp_path / "v")
    rels = sorted(p.relative_to(tmp_path).as_posix() for p in written)
    assert rels == ["docs/KNOBS.md", "docs/PLUGINS.md", "docs/contracts/schema.generated.json",
                    "s/references/glossary.md", "s/references/knobs.md", "v/Mira Plugin Reference.md"]


def test_generated_text_never_contains_en_or_em_dashes(tmp_path):
    G.write_all(tmp_path, specs(), skill_dir=tmp_path / "s", vault_dir=tmp_path / "v")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert not any(c in path.read_text(encoding="utf-8") for c in DASH), path
    assert not any(c in (WALL / "mirakit" / "docsgen.py").read_text(encoding="utf-8") for c in DASH)


@pytest.fixture
def isolated_plugin_packages():
    """The real plugin packages may already be imported by other tests; load_specs must see only tmp ones."""
    names = ("listener_plugins", "speech_plugins")
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in names}
    for k in saved:
        del sys.modules[k]
    yield
    for k in [k for k in sys.modules if k.split(".")[0] in names]:
        del sys.modules[k]
    sys.modules.update(saved)


def test_load_specs_reports_missing_packages_instead_of_crashing(tmp_path, isolated_plugin_packages, monkeypatch):
    # hide the real wall/ from the import path so only the temp root can answer
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p or ".").resolve() != WALL])
    loaded, warnings = G.load_specs(tmp_path)
    assert loaded == [] and len(warnings) == 2 and "not importable" in warnings[0]
    (tmp_path / "listener_plugins").mkdir()
    (tmp_path / "listener_plugins" / "__init__.py").write_text("def specs():\n    return []\n")
    loaded, warnings = G.load_specs(tmp_path)
    assert loaded == [] and len(warnings) == 1 and "speech_plugins" in warnings[0]
