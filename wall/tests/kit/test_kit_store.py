"""mirakit.store: layers, argv precedence, atomic write, history, file_changed, last-good, migrations."""
from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from mirakit import store as store_mod
from mirakit.plugin import ConfigError, ConflictError, Knob, PluginBase, PluginSpec
from mirakit.store import ConfigStore, StoreError


def spec(name, *knobs, default_enabled=True, validate=None):
    return PluginSpec(name=name, kind=name.split(".")[0], version="1", summary="s", machine="listener",
                      provides=(), create=lambda ctx, cfg: PluginBase(ctx, cfg), knobs=tuple(knobs),
                      default_enabled=default_enabled, validate=validate)


def specs():
    return [
        spec("agc.mic",
             Knob("target", "float", 0.05, "d", unit="rms", min=0.005, max=0.3, arg="agc_target", env="MIRA_AGC_TARGET"),
             Knob("max_gain", "float", 10.0, "d", unit="x", min=1, max=40)),
        spec("wake.kws", Knob("threshold", "float", 0.15, "d", unit="score", min=0.01, max=0.5, applies="rebuild")),
        spec("source.wav", Knob("path", "path", "", "d"), default_enabled=False),
        spec("demo.rule", Knob("a", "float", 1.0, "d", unit="s", min=0, max=10),
             Knob("b", "float", 2.0, "d", unit="s", min=0, max=10), validate=_a_le_b),
    ]


def _a_le_b(v):
    if v["a"] > v["b"]:
        raise ConfigError("a must not exceed b", "demo.rule.a")


def write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def doc(**plugins):
    return {"format": 1, "machine": "listener", "revision": 3, "core": {}, "plugins": plugins}


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "cfg" / "mira.json"


def open_store(cfg, d, **kw):
    cfg.parent.mkdir(parents=True, exist_ok=True)
    write(cfg, d)
    st = ConfigStore(specs(), "listener", path=cfg, **kw)
    st.load()
    return st


# ---- layers
def test_layer_precedence_table(cfg):
    st = open_store(cfg, doc(**{"agc.mic": {"enabled": True, "config": {"target": 0.06}}}),
                    argv={"wake.kws.threshold": 0.07}, env={"agc.mic.target": 0.09, "agc.mic.max_gain": 12.0,
                                                           "wake.kws.threshold": 0.4})
    assert st.get("wake.kws.threshold") == (0.07, "argv")            # argv beats env and file
    assert st.get("agc.mic.target") == (0.06, "file")                  # file beats env
    assert st.get("agc.mic.max_gain") == (12.0, "env")                 # env beats default while fallback
    assert st.get("demo.rule.a") == (1.0, "default")


def test_env_ignored_once_legacy_env_is_ignore(cfg):
    d = doc()
    d["core"] = {"legacy_env": "ignore"}
    st = open_store(cfg, d, env={"agc.mic.max_gain": 12.0})
    assert st.get("agc.mic.max_gain") == (10.0, "default")


def test_sparse_file_is_valid_and_defaults_fill(cfg):
    st = open_store(cfg, {"format": 1, "machine": "listener"})
    assert st.values("agc.mic") == {"target": 0.05, "max_gain": 10.0}
    assert st.enabled("agc.mic") and not st.enabled("source.wav")


def test_enabled_flag_and_override(cfg):
    st = open_store(cfg, doc(**{"agc.mic": {"enabled": False}}), enabled_override={"source.wav": True})
    assert not st.enabled("agc.mic") and st.enabled("source.wav")
    assert st.enabled("core")


def test_missing_file_is_an_empty_sparse_file(tmp_path):
    st = ConfigStore(specs(), "listener", path=tmp_path / "none.json")
    st.load()
    assert st.revision == 0 and st.get("agc.mic.target")[1] == "default"


def test_dollar_keys_are_ignored_and_preserved(cfg):
    d = doc(**{"agc.mic": {"enabled": True, "$comment": "x", "config": {"target": 0.06, "$note": 1}}})
    d["$comment"] = "header"
    st = open_store(cfg, d)
    assert st.get("agc.mic.target")[0] == 0.06
    st.write({"agc.mic.max_gain": 11.0})
    disk = json.loads(cfg.read_text())
    assert disk["$comment"] == "header" and disk["plugins"]["agc.mic"]["config"]["$note"] == 1


def test_get_returns_copies_of_mutable_values(cfg):
    sp = [spec("x.y", Knob("l", "list[str]", ["a"], "d"))]
    st = ConfigStore(sp, "listener")
    st.value("x.y.l").append("z")
    assert st.value("x.y.l") == ["a"]


# ---- validation of the file
def test_unknown_plugin_and_key_name_the_closest(cfg):
    cfg.parent.mkdir()
    write(cfg, doc(**{"agc.mik": {"config": {}}}))
    st = ConfigStore(specs(), "listener", path=cfg)
    with pytest.raises(StoreError) as ei:
        st.load()
    assert "agc.mic" in ei.value.message
    write(cfg, doc(**{"agc.mic": {"config": {"targt": 0.1}}}))
    with pytest.raises(StoreError) as ei:
        ConfigStore(specs(), "listener", path=cfg).load()
    assert "agc.mic.target" in ei.value.message


def test_check_doc_rejects_bad_shapes(cfg):
    st = ConfigStore(specs(), "listener", path=cfg)
    for bad in ([], {"format": 2}, {"machine": "speech"}, {"revision": "x"}, {"plugins": []},
                {"plugins": {"agc.mic": {"enabled": "yes"}}}, {"plugins": {"agc.mic": {"config": {"target": 9}}}},
                {"plugins": {"demo.rule": {"config": {"a": 5, "b": 1}}}}):
        with pytest.raises(ConfigError):
            st.check_doc(bad)


def test_values_are_coerced_on_load(cfg):
    st = open_store(cfg, doc(**{"agc.mic": {"config": {"target": "0.06"}}}))
    assert st.get("agc.mic.target")[0] == 0.06


# ---- atomic write, history, revision
def test_write_is_atomic_bumps_revision_and_keeps_history(cfg):
    st = open_store(cfg, doc())
    before, after = st.write({"agc.mic.target": 0.08})
    assert (before, after) == (3, 4) and st.revision == 4
    disk = json.loads(cfg.read_text())
    assert disk["revision"] == 4 and disk["plugins"]["agc.mic"]["config"]["target"] == 0.08
    assert disk["plugins"]["agc.mic"]["enabled"] is True                 # a new section records enabled
    assert (cfg.parent / "history" / "listener.3.json").exists()
    assert json.loads((cfg.parent / "history" / "listener.3.json").read_text())["revision"] == 3
    assert not (cfg.parent / "mira.json.tmp").exists()
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600
    assert st.get("agc.mic.target") == (0.08, "file")


def test_history_is_pruned_to_history_keep(cfg):
    st = open_store(cfg, doc())
    for i in range(8):
        st.write({"agc.mic.max_gain": 10.0 + i}, history_keep=5)
    kept = sorted(int(p.name.split(".")[1]) for p in (cfg.parent / "history").iterdir())
    assert kept == [6, 7, 8, 9, 10]


def test_enabled_edits_and_core_keys_are_written(cfg):
    st = open_store(cfg, doc())
    st.write({"core.engine": "plugins"}, {"source.wav": True})
    disk = json.loads(cfg.read_text())
    assert disk["core"]["engine"] == "plugins" and disk["plugins"]["source.wav"]["enabled"] is True
    assert st.enabled("source.wav") and st.get("core.engine") == ("plugins", "file")


def test_file_changed_refuses_and_leaves_the_file_alone(cfg):
    st = open_store(cfg, doc())
    d = json.loads(cfg.read_text())
    d["plugins"] = {"agc.mic": {"enabled": True, "config": {"target": 0.2}}}
    write(cfg, d)                                         # someone edited the file behind the process
    with pytest.raises(ConflictError) as ei:
        st.write({"agc.mic.max_gain": 12.0})
    assert ei.value.code == "file_changed" and "reload" in ei.value.message
    assert json.loads(cfg.read_text())["plugins"]["agc.mic"]["config"] == {"target": 0.2}
    assert st.revision == 3                                # nothing moved in memory either


def test_reload_candidate_then_adopt_lets_writes_through(cfg):
    st = open_store(cfg, doc())
    d = json.loads(cfg.read_text())
    d["plugins"] = {"agc.mic": {"enabled": True, "config": {"target": 0.2}}}
    write(cfg, d)
    cand, digest = st.read_candidate()
    changes, enabled = st.diff_with(cand)
    assert changes == {"agc.mic.target": (0.05, 0.2)} and enabled == {}
    st.adopt_disk(cand, digest)
    st.write({})
    assert st.get("agc.mic.target")[0] == 0.2 and st.revision == 4
    assert (cfg.parent / "history" / "listener.3.json").exists()


def test_diff_with_ignores_keys_held_by_a_flag(cfg):
    st = open_store(cfg, doc(), argv={"agc.mic.target": 0.07})
    cand = st.check_doc({"plugins": {"agc.mic": {"config": {"target": 0.2}}}})
    assert st.diff_with(cand)[0] == {}


def test_concurrent_writers_serialize_on_the_flock(cfg):
    """Two stores on one file: the loser gets file_changed, never a torn or merged file."""
    a = open_store(cfg, doc())
    b = ConfigStore(specs(), "listener", path=cfg)
    b.load()
    errors = []

    def go(st, v):
        try:
            st.write({"agc.mic.max_gain": v})
        except ConflictError as exc:
            errors.append(exc.code)

    t1, t2 = threading.Thread(target=go, args=(a, 11.0)), threading.Thread(target=go, args=(b, 12.0))
    t1.start(); t1.join(); t2.start(); t2.join()
    assert errors == ["file_changed"]
    assert json.loads(cfg.read_text())["plugins"]["agc.mic"]["config"]["max_gain"] == 11.0


def test_in_memory_store_never_touches_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    st = ConfigStore(specs(), "listener", values={"agc.mic.target": 0.07})
    assert st.in_memory and st.revision == 1 and st.cfgdir is None
    assert st.write({"agc.mic.max_gain": 12.0}) == (1, 2)
    assert st.get("agc.mic.target") == (0.07, "file")
    assert list(tmp_path.iterdir()) == []


# ---- last good
def test_mark_good_copies_the_file_with_mode_0600(cfg):
    st = open_store(cfg, doc(**{"agc.mic": {"config": {"target": 0.06}}}))
    st.mark_good()
    good = Path(str(cfg) + ".lastgood")
    assert json.loads(good.read_text())["plugins"]["agc.mic"]["config"]["target"] == 0.06
    assert stat.S_IMODE(os.stat(good).st_mode) == 0o600


def test_invalid_file_falls_back_to_lastgood_with_config_error(cfg):
    st = open_store(cfg, doc(**{"agc.mic": {"config": {"target": 0.06}}}))
    st.mark_good()
    cfg.write_text("{ not json")
    st2 = ConfigStore(specs(), "listener", path=cfg)
    st2.load()
    assert st2.get("agc.mic.target")[0] == 0.06
    assert st2.loaded_from_lastgood and "not valid JSON" in st2.config_error
    st2.mark_good()                                   # must not overwrite last-good with itself while broken
    assert json.loads(Path(str(cfg) + ".lastgood").read_text())["plugins"]["agc.mic"]["config"]["target"] == 0.06
    st2.write({"agc.mic.max_gain": 11.0})             # a write over the broken file is allowed, history keeps it
    assert (cfg.parent / "history").exists()


def test_invalid_file_without_lastgood_raises(cfg):
    cfg.parent.mkdir()
    cfg.write_text(json.dumps(doc(**{"agc.mic": {"config": {"target": 99}}})))
    with pytest.raises(StoreError):
        ConfigStore(specs(), "listener", path=cfg).load()


def test_invalid_lastgood_too_raises(cfg):
    cfg.parent.mkdir()
    cfg.write_text("{")
    Path(str(cfg) + ".lastgood").write_text("[]")
    with pytest.raises(StoreError):
        ConfigStore(specs(), "listener", path=cfg).load()


def test_use_lastgood_for_a_later_check(cfg):
    st = open_store(cfg, doc(**{"agc.mic": {"config": {"target": 0.06}}}))
    st.mark_good()
    d = json.loads(cfg.read_text())
    d["plugins"]["agc.mic"]["config"]["target"] = 0.2
    write(cfg, d)
    st2 = ConfigStore(specs(), "listener", path=cfg)
    st2.load()
    assert st2.get("agc.mic.target")[0] == 0.2
    assert st2.use_lastgood("conflict") and st2.get("agc.mic.target")[0] == 0.06
    assert st2.config_error == "conflict"
    assert not st2.use_lastgood("again")


# ---- migrations
def test_migrations_rename_keys_on_load(cfg, monkeypatch):
    monkeypatch.setattr(store_mod, "MIGRATIONS", {(1, "agc.mic.old_gain"): "agc.mic.max_gain"})
    st = open_store(cfg, doc(**{"agc.mic": {"config": {"old_gain": 12.0}}}))
    assert st.get("agc.mic.max_gain")[0] == 12.0


def test_migrations_are_empty_in_format_one():
    assert store_mod.MIGRATIONS == {}


def test_siblings_live_next_to_the_file_in_use(cfg):
    st = open_store(cfg, doc())
    assert st.cfgdir == cfg.parent
    assert st.sibling("changes.jsonl") == cfg.parent / "changes.jsonl"
    st.write({"agc.mic.max_gain": 11.0})
    assert (cfg.parent / "mira.json.lock").exists()
    assert sorted(p.name for p in cfg.parent.iterdir() if p.is_file()) == ["mira.json", "mira.json.lock"]
