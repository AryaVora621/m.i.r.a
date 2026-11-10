"""voices.catalog: scan, ids, int8 folding, aliases and their collisions, resolution, near matches, rtf, listing."""
import json
import sys
import types
from pathlib import Path

import pytest

import sp_fakes as fk
from sp_fakes import real_config_dir_untouched  # noqa: F401  (autouse guard)
from mirakit.plugin import ConfigError
from mirakit.testkit import FakeCtx, cfg_for
from speech_plugins import server_http, voices_catalog as vc


@pytest.fixture
def cat(tmp_path):
    models = fk.make_models(tmp_path)
    clock = fk.FakeClock()
    return vc.VoiceCatalog(lambda: models, featured=lambda: ["amy", "jenny", "lessac", "kristin", "hfc_female"],
                           rtf_file=lambda: tmp_path / "rtf.json", clock=clock), clock, models, tmp_path


def by_alias(c, alias):
    return c.resolve(alias)


def test_ids_and_single_speaker_entries(cat):
    c = cat[0]
    amy = c.resolve("amy")
    assert amy["id"] == "vits-piper-en_US-amy-medium#0"
    assert amy["engine"] == "piper" and amy["lang"] == "en_US" and amy["gender"] == "" and amy["speakers"] == 1
    assert amy["usable"] and amy["reason"] == ""
    assert set(amy) >= {"id", "alias", "aliases", "label", "lang", "gender", "engine", "model_dir", "speaker", "speakers",
                        "variants", "usable", "reason", "loaded", "current", "featured", "size_mb", "rtf"}
    assert amy["rtf"] is None and amy["size_mb"] >= 0


def test_int8_folds_into_the_fp32_entry_and_lessac_is_int8_only(cat):
    c = cat[0]
    amy = c.resolve("amy")
    assert amy["model_dir"] == "vits-piper-en_US-amy-medium"
    assert amy["variants"] == ["vits-piper-en_US-amy-medium-int8"]
    listed = [e["model_dir"] for e in c.list()]
    assert "vits-piper-en_US-amy-medium-int8" not in listed
    lessac = c.resolve("lessac")
    assert lessac["model_dir"] == "vits-piper-en_US-lessac-medium-int8" and lessac["variants"] == []
    # the int8 id still resolves, to the fp32 entry
    assert c.resolve("vits-piper-en_US-amy-medium-int8#0")["id"] == amy["id"]
    # Kokoro int8 of v0_19 folds into the fp32 v0_19 dir, the multi-lang int8 dir stays an entry
    af = c.resolve("af")
    assert af["model_dir"] == "kokoro-en-v0_19" and af["variants"] == ["kokoro-int8-en-v0_19"]
    assert c.resolve("heart")["model_dir"] == "kokoro-int8-multi-lang-v1_0"


def test_aliases_short_alias_and_normalisation(cat):
    c = cat[0]
    jenny = c.resolve("jenny")
    assert jenny["alias"] == "jenny_dioco" and jenny["aliases"][:2] == ["jenny_dioco", "jenny"]
    assert c.resolve("Jenny Dioco")["id"] == jenny["id"]       # case, space and underscore are equal
    assert c.resolve("jenny-dioco")["id"] == jenny["id"]
    assert c.resolve("HFC female")["model_dir"] == "vits-piper-en_US-hfc_female-medium"
    assert c.resolve("amy")["alias"] == "amy"
    assert c.resolve("vits-piper-en_US-AMY-medium#0")["alias"] == "amy"      # ids compare case-insensitively


def test_kokoro_aliases_gender_and_lang(cat):
    c = cat[0]
    heart = c.resolve("heart")
    assert heart["id"] == "kokoro-int8-multi-lang-v1_0#af_heart"
    assert heart["aliases"] == ["heart", "af_heart"] and heart["alias"] == "heart"
    assert heart["lang"] == "en_US" and heart["gender"] == "f" and heart["engine"] == "kokoro"
    assert c.resolve("af_heart")["id"] == heart["id"]
    assert c.resolve("bm_george")["gender"] == "m" and c.resolve("bm_george")["lang"] == "en_GB"
    langs = {e["id"].split("#")[1]: e["lang"] for e in c.list(include_unusable=False) if e["engine"] == "kokoro"}
    assert langs["ef_dora"] == "es" and langs["ff_siwis"] == "fr" and langs["hf_alpha"] == "hi"
    assert langs["if_sara"] == "it" and langs["jf_alpha"] == "ja" and langs["pf_dora"] == "pt_BR"
    assert langs["zf_xiaobei"] == "zh"


def test_collision_loser_keeps_only_the_qualified_alias(cat):
    c = cat[0]
    winner = c.resolve("af_bella")
    assert winner["model_dir"] == "kokoro-int8-multi-lang-v1_0"          # newer Kokoro claims first
    loser = c.resolve("af_bella@v0_19")
    assert loser["model_dir"] == "kokoro-en-v0_19" and loser["id"] == "kokoro-en-v0_19#af_bella"
    assert loser["aliases"] == ["af_bella@v0_19"]
    assert c.resolve("bella")["id"] == winner["id"]
    # a name only the old model has keeps its plain alias
    assert c.resolve("af")["model_dir"] == "kokoro-en-v0_19"


def test_piper_claims_before_kokoro(tmp_path):
    models = fk.make_models(tmp_path)
    fk.make_piper(models, "vits-piper-en_US-heart-medium")
    c = vc.VoiceCatalog(lambda: models)
    assert c.resolve("heart")["engine"] == "piper"
    assert c.resolve("af_heart")["engine"] == "kokoro"          # its full name was free


def test_a_bare_model_directory_resolves_to_its_first_voice(cat):
    c = cat[0]
    assert c.resolve("vits-piper-en_US-amy-medium")["id"] == "vits-piper-en_US-amy-medium#0"
    assert c.resolve("vits-piper-en_US-amy-medium-int8")["id"] == "vits-piper-en_US-amy-medium#0"
    assert c.resolve("vits-piper-en_US-lessac-medium-int8")["alias"] == "lessac"
    assert c.resolve("kokoro-int8-multi-lang-v1_0")["id"] == "kokoro-int8-multi-lang-v1_0#af_alloy"
    assert c.resolve("vits-piper-en_US-libritts_r-medium")["speaker"] == "0"
    with pytest.raises(vc.UnknownVoice):
        c.resolve("vits-piper-en_US-nothing-medium")


def test_resolution_order_and_unknown_voice_with_near(cat):
    c = cat[0]
    with pytest.raises(vc.UnknownVoice) as e:
        c.resolve("amey")
    assert e.value.code == "unknown_voice" and isinstance(e.value, ConfigError)
    assert "amy" in e.value.near and len(e.value.near) <= 3
    with pytest.raises(vc.UnknownVoice):
        c.resolve("")
    with pytest.raises(vc.UnknownVoice):
        c.resolve("nothing like any voice zzzz")


def test_multi_speaker_piper_is_one_family_row_never_one_row_per_speaker(cat):
    c = cat[0]
    rows = [e for e in c.list() if "libritts" in e["model_dir"]]
    assert len(rows) == 1 and rows[0]["speakers"] == 904 and rows[0]["alias"] == "libritts" and rows[0]["engine"] == "piper"
    listing = c.listing()
    assert not [v for v in listing["voices"] if "libritts" in v["model_dir"]]
    assert listing["families"] == [{"model_dir": "vits-piper-en_US-libritts_r-medium", "speakers": 904, "alias_prefix": "libritts"}]
    e = c.resolve("libritts:123")
    assert e["id"] == "vits-piper-en_US-libritts_r-medium#123" and e["speaker"] == "123" and e["usable"]
    assert c.locate(e)[1] == 123
    assert c.resolve("vits-piper-en_US-libritts_r-medium#903")["speaker"] == "903"
    assert c.resolve("libritts")["speaker"] == "0"
    with pytest.raises(vc.UnknownVoice):
        c.resolve("libritts:5000")
    assert c.default_for("vits-piper-en_US-libritts_r-medium")["speaker"] == "0"
    assert [f["alias_prefix"] for f in c.listing("es")["families"]] == []


def test_the_real_control_route_builds_the_same_response_from_list(cat):
    control = pytest.importorskip("mirakit.control")
    c = cat[0]
    mine = c.listing("en", limit=100)
    theirs = control.voices_listing(c, None, "en", 100, False)
    assert theirs["families"] == mine["families"]
    assert [v["id"] for v in theirs["voices"]] == [v["id"] for v in mine["voices"]]
    assert theirs["current"] == mine["current"]


def test_unusable_directory_is_listed_with_a_reason(cat):
    c = cat[0]
    assert all(e["usable"] for e in c.list())
    rows = c.list(include_unusable=True)
    ghost = [e for e in rows if e["model_dir"] == "vits-piper-en_US-ghost-medium"]
    assert len(ghost) == 1 and ghost[0]["usable"] is False and "tokens.txt" in ghost[0]["reason"]
    assert c.resolve("ghost")["usable"] is False


def test_kokoro_dir_without_a_table_is_unusable(tmp_path):
    models = fk.make_models(tmp_path, with_broken=False)
    fk.make_kokoro(models, "kokoro-multi-lang-v9_9")
    c = vc.VoiceCatalog(lambda: models)
    bad = [e for e in c.list(include_unusable=True) if e["model_dir"] == "kokoro-multi-lang-v9_9"]
    assert bad and not bad[0]["usable"] and "mira voices index" in bad[0]["reason"]


def test_lang_filter_keeps_both_english_variants(cat):
    c = cat[0]
    en = {e["lang"] for e in c.list("en")}
    assert en == {"en_US", "en_GB"}
    assert {e["lang"] for e in c.list("en_GB")} == {"en_GB"}
    assert "es" in {e["lang"] for e in c.list("")}
    assert {e["lang"] for e in c.list("es")} == {"es"}


def test_listing_order_current_then_featured_then_lang_and_alias(tmp_path):
    models = fk.make_models(tmp_path)
    tts = types.SimpleNamespace(voice_id="vits-piper-en_US-kristin-medium#0",
                                loaded=lambda: [{"model_dir": "vits-piper-en_US-kristin-medium", "role": "active"}])
    c = vc.VoiceCatalog(lambda: models, featured=lambda: ["amy", "jenny"], tts=lambda: tts)
    listing = c.listing("en")
    ids = [v["id"] for v in listing["voices"]]
    assert ids[0] == "vits-piper-en_US-kristin-medium#0"
    assert listing["current"]["alias"] == "kristin" and listing["current"]["id"] == ids[0]
    assert listing["voices"][0]["current"] and listing["voices"][0]["loaded"]
    featured = [v["alias"] for v in listing["voices"] if v["featured"]]
    assert featured == ["amy", "jenny_dioco"] or set(featured) == {"amy", "jenny_dioco"}
    first_nonfeatured = next(i for i, v in enumerate(listing["voices"]) if i and not v["featured"])
    assert all(v["featured"] for v in listing["voices"][1:first_nonfeatured])
    assert len(c.listing("en", limit=3)["voices"]) == 3


def test_agent_allowed_rule(cat):
    c, clock, models, tmp = cat
    assert c.agent_allowed(c.resolve("amy"))                       # featured, en_US
    assert c.agent_allowed(c.resolve("jenny"))                     # featured, en_GB
    heart = c.resolve("heart")
    assert not c.agent_allowed(heart)                              # not featured, never measured
    c.record_rtf(heart["id"], 0.3)
    assert c.agent_allowed(c.resolve("heart"))
    c.record_rtf(heart["id"], 5.0)                                 # moving average climbs past 0.5
    c.record_rtf(heart["id"], 5.0)
    assert not c.agent_allowed(c.resolve("heart"))
    assert not c.agent_allowed(c.resolve("ef_dora"))               # not en_*, even when fast
    c.record_rtf(c.resolve("ef_dora")["id"], 0.1)
    assert not c.agent_allowed(c.resolve("ef_dora"))
    assert not c.agent_allowed(c.resolve("ghost"))                 # unusable
    assert not c.agent_allowed(c.resolve("libritts:1"))


def test_rtf_moving_average_is_persisted_and_reloaded(cat):
    c, clock, models, tmp = cat
    key = c.resolve("amy")["id"]
    c.record_rtf(key, 0.2)
    c.record_rtf(key, 0.4)
    assert 0.2 < c.rtf(key) < 0.4
    c.flush_rtf()
    data = json.loads((tmp / "rtf.json").read_text())
    assert key.lower() in data["rtf"]
    c2 = vc.VoiceCatalog(lambda: models, rtf_file=lambda: tmp / "rtf.json")
    assert c2.resolve("amy")["rtf"] == pytest.approx(c.rtf(key), abs=1e-3)
    assert not (tmp / "rtf.json.tmp").exists()


def test_rescan_on_folder_change_and_on_timer(tmp_path):
    models = fk.make_models(tmp_path)
    clock = fk.FakeClock()
    c = vc.VoiceCatalog(lambda: models, rescan_s=lambda: 300.0, clock=clock)
    c.list()
    n = c.scans
    c.list()
    assert c.scans == n                                            # nothing changed: no rescan
    clock.advance(301)
    c.list()
    assert c.scans == n + 1                                        # timer
    fk.make_piper(models, "vits-piper-en_US-newbie-medium")
    c.list()
    assert c.scans == n + 2 and c.resolve("newbie")["usable"]      # folder mtime changed


def test_kokoro_tables_match_their_counts_and_the_fixture():
    fixture = json.loads((fk.FIX / "kokoro_speakers.json").read_text())
    tables = sorted(p.stem for p in (fk.WALL / "speech_plugins" / "voices").glob("*.json"))
    assert tables == sorted(fixture)
    for name in tables:
        table = json.loads((fk.WALL / "speech_plugins" / "voices" / f"{name}.json").read_text())
        assert len(table["speaker_names"]) == table["n_speakers"], name
        assert table["speaker_names"] == fixture[name]["speaker_names"]
    assert fixture["kokoro-int8-multi-lang-v1_0"]["n_speakers"] == 54
    assert fixture["kokoro-en-v0_19"]["n_speakers"] == 11


def test_index_main_reads_onnx_metadata(tmp_path, monkeypatch):
    models = tmp_path / "models"
    for name, names in (("kokoro-x-en-v1_0", ["af", "am"]), ("kokoro-y-en-v1_0", ["bf"])):
        d = models / name
        d.mkdir(parents=True)
        (d / "m.onnx").write_bytes(b"x")
    meta = {"kokoro-x-en-v1_0/m.onnx": {"speaker_names": "af,am", "n_speakers": "2", "sample_rate": "24000"},
            "kokoro-y-en-v1_0/m.onnx": {"speaker_names": json.dumps(["bf"]), "n_speakers": "1"}}

    class Sess:
        def __init__(self, path, providers=None):
            self.key = "/".join(Path(path).parts[-2:])

        def get_modelmeta(self):
            return types.SimpleNamespace(custom_metadata_map=meta[self.key])

    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(InferenceSession=Sess))
    out = tmp_path / "out"
    assert vc.index_main(models, out) == {"kokoro-x-en-v1_0": 2, "kokoro-y-en-v1_0": 1}
    table = json.loads((out / "kokoro-x-en-v1_0.json").read_text())
    assert table["speaker_names"] == ["af", "am"] and table["n_speakers"] == 2 and table["sample_rate"] == 24000
    meta["kokoro-y-en-v1_0/m.onnx"]["n_speakers"] = "3"
    with pytest.raises(ValueError):
        vc.index_main(models, out)


def test_bench_main_writes_the_rtf_file(tmp_path):
    models = fk.make_models(tmp_path, with_broken=False)
    ticks = iter(float(i) for i in range(10000))
    backend = fk.FakeTtsBackend()
    rtf_file = tmp_path / "bench.json"
    result = vc.bench_main(models, rtf_file, loader=lambda path, n: backend.load(path, n, {}),
                           clock=lambda: next(ticks))
    assert "vits-piper-en_US-amy-medium#0" in result and "kokoro-int8-multi-lang-v1_0#af_heart" in result
    assert all(v > 0 for v in result.values())
    data = json.loads(rtf_file.read_text())["rtf"]
    assert set(data) == {k.lower() for k in result}
    only = vc.bench_main(models, tmp_path / "one.json", only="amy", loader=lambda p, n: backend.load(p, n, {}),
                         clock=lambda: next(ticks))
    assert list(only) == ["vits-piper-en_US-amy-medium#0"]


def test_plugin_provides_the_service_and_reads_base_dir_through_paths(tmp_path):
    base = tmp_path / "base"
    fk.make_models(base)
    ctx = FakeCtx("speech")
    ctx.cfgs["server.http"] = cfg_for(server_http.SPEC, base_dir=str(base))
    cfg = cfg_for(vc.SPEC)                                         # default rtf_file starts with ~
    ctx.cfgs[vc.SPEC.name] = cfg
    p = vc.SPEC.create(ctx, cfg)
    p.start()
    assert ctx.get("voices") is p.catalog and p.catalog.resolve("amy")["usable"]
    p.catalog.record_rtf("vits-piper-en_US-amy-medium#0", 0.2)
    p.stop()
    ctx.release_all()                                              # flushes the rtf file under the FAKE home
    assert (ctx.paths.home / ".config/mira/voices.rtf.json").exists()
    ctx.cleanup()
