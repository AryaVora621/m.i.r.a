"""`mira check` lint rules (P6): each rule of spec 2.11 catches a bad synthetic plugin and passes a good one."""
from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import docsgen as G  # noqa: E402
from mirakit import lint as L  # noqa: E402
from mirakit import plugin as P  # noqa: E402

DASH = chr(0x2013), chr(0x2014)


def make_module(tmp: Path, name: str, source: str):
    path = tmp / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, path


GOOD = '''
    """A good plugin.

    FIXED: 16000 sample rate
    """
    import os
    from mirakit.plugin import Knob, PluginBase, PluginSpec

    LEGACY_CLASSES = ("OldThing",)
    RATE = 16000


    class OldThing:
        def __init__(self):
            self.t = os.environ.get("MIRA_OLD", "x")
            self.f = open("/etc/hostname")
            self.n = 0.25


    class Good(PluginBase):
        def start(self):
            self.ctx.provide("agc", self)
            self.window = 16000
            self.small = 1


    SPEC = PluginSpec(name="agc.good", kind="agc", version="1", summary="s", machine="listener", provides=("agc",),
                      create=lambda ctx, cfg: Good(ctx, cfg), tests=("wall/tests/plugins/test_lp_agc_good.py",),
                      knobs=(Knob("target", "float", 0.25, "Level.", unit="rms", min=0.01, max=1.0,
                                  agent="bounded", agent_min=0.1, agent_max=0.5, agent_step=0.05),))
'''


def rules(findings):
    return sorted({f.rule for f in findings})


@pytest.fixture
def tmp_wall(tmp_path):
    root = tmp_path / "wall"
    (root / "tests" / "plugins").mkdir(parents=True)
    (root / "tests" / "plugins" / "test_lp_agc_good.py").write_text("")
    return root


def test_a_good_plugin_is_clean(tmp_wall):
    module, _ = make_module(tmp_wall, "lint_good", GOOD)
    findings = L.lint([module.SPEC], tmp_wall, fast=True) + L.check_tests_exist([module.SPEC], tmp_wall)
    assert findings == [], [str(f) for f in findings]


def knob(**kw):
    base = dict(name="x", type="float", default=2.5, description="d", unit="s", min=0, max=9)
    base.update(kw)
    return P.Knob(**base)


def spec_with(*knobs, name="a.b", **kw):
    return P.PluginSpec(name=name, kind=name.split(".")[0], version="1", summary="s", machine=kw.pop("machine", "listener"),
                        provides=kw.pop("provides", ()), create=lambda c, g: None, knobs=tuple(knobs), **kw)


def test_knob_metadata_rules():
    cases = {
        "knob-description": knob(description=" "),
        "knob-unit": knob(unit=""),
        "knob-bounds": knob(max=None),
        "knob-applies": knob(applies="sometimes"),
        "description-length": knob(description="x" * 161),
    }
    for rule, k in cases.items():
        found = L.check_knobs([spec_with(k)])
        assert rule in rules(found), rule
        assert all("a.b.x" in f.where for f in found)
        assert "a.b.x" in str(found[0])


def test_agent_rules_use_a_corrupted_knob_because_the_dataclass_blocks_bad_ones():
    k = knob(agent="bounded", agent_min=1, agent_max=3, agent_step=1)
    object.__setattr__(k, "agent_step", None)
    assert rules(L.check_knobs([spec_with(k)])) == ["knob-agent"]
    k = knob(agent="bounded", agent_min=1, agent_max=3, agent_step=1)
    object.__setattr__(k, "agent_max", 99)
    assert "outside the hard bounds" in L.check_knobs([spec_with(k)])[0].message
    k = knob(agent="bounded", agent_min=1, agent_max=3, agent_step=1)
    object.__setattr__(k, "agent_step", 5)
    assert "does not fit" in L.check_knobs([spec_with(k)])[0].message
    # a bounded listener knob must be live (2.4.2: none is rebuild or restart on the Pi)
    k = knob(agent="bounded", agent_min=1, agent_max=3, agent_step=1, applies="rebuild")
    assert rules(L.check_knobs([spec_with(k)])) == ["knob-agent"]
    assert L.check_knobs([spec_with(k, machine="speech")]) == []
    # non numeric bounded knobs (a voice) need no numbers
    assert L.check_knobs([spec_with(knob(name="v", type="voice", default="amy", unit="", min=None, max=None,
                                         agent="bounded"))]) == []


BAD_SOURCES = {
    "environ": ('import os\nx = os.environ.get("A")\n', "environ"),
    "getenv": ('import os\nx = os.getenv("A")\n', "environ"),
    "open-literal": ('def f():\n    return open("/etc/passwd")\n', "open-literal"),
    "literal-default": ("def f():\n    return 2.5 * 3\n", "literal-default"),
}


@pytest.mark.parametrize("name", sorted(BAD_SOURCES))
def test_source_scan_rules(tmp_path, name):
    source, rule = BAD_SOURCES[name]
    module, path = make_module(tmp_path, f"lint_bad_{name.replace('-', '_')}", source + """
from mirakit.plugin import Knob, PluginSpec
SPEC = PluginSpec(name="a.b", kind="a", version="1", summary="s", machine="listener", provides=(),
                  create=lambda c, g: None, knobs=(Knob("x", "float", 2.5, "d", unit="s", min=0, max=9),))
""")
    found = L.scan_source(module.SPEC, path)
    assert rules(found) == [rule], [str(f) for f in found]
    assert path.name in found[0].where and found[0].where.split(":")[1].isdigit()


def test_literal_rule_exemptions(tmp_path):
    module, path = make_module(tmp_path, "lint_exempt", '''
        """Doc.

        FIXED: 2.5 the protocol value
        """
        from mirakit.plugin import Knob, PluginSpec
        LIMIT = 9.75
        class Old:
            LEGACY = 9.75
        LEGACY_CLASSES = ("Old",)
        def f():
            return 2.5, 0, 1, True
        SPEC = PluginSpec(name="a.b", kind="a", version="1", summary="s", machine="listener", provides=(),
                          create=lambda c, g: None, knobs=(Knob("x", "float", 9.75, "d", unit="s", min=0, max=9),))
        ''')
    assert L.scan_source(module.SPEC, path) == []          # FIXED, upper-case constant, legacy class, trivial values
    module, path = make_module(tmp_path, "lint_not_exempt", '''
        from mirakit.plugin import Knob, PluginSpec
        def f():
            return 9.75
        SPEC = PluginSpec(name="a.b", kind="a", version="1", summary="s", machine="listener", provides=(),
                          create=lambda c, g: None, knobs=(Knob("x", "float", 9.75, "d", unit="s", min=0, max=9),))
        ''')
    assert rules(L.scan_source(module.SPEC, path)) == ["literal-default"]


def test_signature_defaults_and_legacy_functions_are_exempt(tmp_path):
    module, path = make_module(tmp_path, "lint_legacy_fn", '''
        import os
        from mirakit.plugin import Knob, PluginSpec
        LEGACY_FUNCTIONS = ("_envf",)
        def _envf(name, default):
            return float(os.environ.get(name, default)) + open("/etc/hostname").read().count("x") + 2.5
        def chunks(text, limit=2.5, *, other=lambda: 2.5):
            return text[: int(limit)]
        class Svc:
            def __init__(self, a=2.5, b=2.5):
                self.a = a
        SPEC = PluginSpec(name="a.b", kind="a", version="1", summary="s", machine="listener", provides=(),
                          create=lambda c, g: None, knobs=(Knob("x", "float", 2.5, "d", unit="s", min=0, max=9),))
        ''')
    assert L.scan_source(module.SPEC, path) == []
    module, path = make_module(tmp_path, "lint_legacy_fn_off", '''
        import os
        from mirakit.plugin import Knob, PluginSpec
        def _envf(name, default):
            return float(os.environ.get(name, default))
        SPEC = PluginSpec(name="a.b", kind="a", version="1", summary="s", machine="listener", provides=(),
                          create=lambda c, g: None, knobs=(Knob("x", "float", 2.5, "d", unit="s", min=0, max=9),))
        ''')
    assert rules(L.scan_source(module.SPEC, path)) == ["environ"]


def test_a_literal_that_equals_another_plugins_default_is_not_flagged(tmp_path):
    module, path = make_module(tmp_path, "lint_other", '''
        from mirakit.plugin import Knob, PluginSpec
        def f():
            return 7.5
        SPEC = PluginSpec(name="a.b", kind="a", version="1", summary="s", machine="listener", provides=(),
                          create=lambda c, g: None, knobs=(Knob("x", "float", 2.5, "d", unit="s", min=0, max=9),))
        ''')
    assert L.scan_source(module.SPEC, path) == []


def test_unparsable_source_is_a_finding(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("def (:\n")
    found = L.scan_source(spec_with(), path)
    assert found[0].rule == "source"


def test_tests_must_exist(tmp_wall):
    spec = spec_with(tests=("wall/tests/plugins/test_lp_nope.py",))
    found = L.check_tests_exist([spec], tmp_wall)
    assert rules(found) == ["tests"] and "test_lp_nope.py" in found[0].message
    assert rules(L.check_tests_exist([spec_with()], tmp_wall)) == ["tests"]            # declares none
    assert L.check_tests_exist([spec_with(tests=("wall/tests/plugins/test_lp_agc_good.py",))], tmp_wall) == []
    assert L.check_tests_exist([spec_with(tests=("tests/plugins/test_lp_agc_good.py",))], tmp_wall) == []


def test_duplicate_names_and_exclusive_providers():
    a, b = spec_with(name="x.one"), spec_with(name="x.one")
    assert rules(L.check_unique_names([a, b])) == ["duplicate-name"]
    assert L.check_unique_names([a, spec_with(name="x.one", machine="speech")]) == []    # control.http on both machines
    p1 = spec_with(name="source.pipewire", provides=("source",))
    p2 = spec_with(name="source.wav", provides=("source",))
    found = L.check_exclusive_providers([p1, p2])
    assert rules(found) == ["exclusive-provider"] and "source.pipewire, source.wav" in found[0].message
    off = spec_with(name="source.wav", provides=("source",), default_enabled=False)
    assert L.check_exclusive_providers([p1, off]) == []
    m1, m2 = spec_with(name="intent.a", provides=("intent",)), spec_with(name="intent.b", provides=("intent",))
    assert L.check_exclusive_providers([m1, m2]) == []                                  # intent is multi
    sp = spec_with(name="stt.engine", provides=("stt",), machine="speech")
    assert L.check_exclusive_providers([p1, sp]) == []                                  # other machine, other service


def example_specs():
    return [
        spec_with(P.Knob("threshold", "float", 0.15, "d", unit="s", min=0.01, max=0.5),
                  P.Knob("when", "time_window", "", "d"), P.Knob("kind", "enum", "a", "d", choices=("a", "b")),
                  P.Knob("names", "list[str]", ["a"], "d", min=0, max=3), P.Knob("n", "int", 3, "d", unit="x", min=1, max=5),
                  name="wake.kws"),
        spec_with(P.Knob("path", "path", "~/x", "d"), name="persona.file", default_enabled=False),
    ]


def good_example():
    return {"format": 1, "machine": "listener", "revision": 1, "$comment": "c",
            "plugins": {"wake.kws": {"enabled": True, "config": {"threshold": 0.05, "when": "", "kind": "a",
                                                                  "names": ["a"], "n": 3}},
                        "persona.file": {"enabled": False, "config": {"path": "/home/mira/x"}}}}


def example_findings(tmp_path, data):
    (tmp_path / "deploy").mkdir(exist_ok=True)
    (tmp_path / "deploy" / "mira.example.json").write_text(json.dumps(data))
    return L.check_examples(example_specs(), tmp_path)


def test_example_rules(tmp_path):
    assert example_findings(tmp_path, good_example()) == []     # tilde expands to the Pi home, threshold is documented
    cases = {
        "not a knob": lambda d: d["plugins"]["wake.kws"]["config"].update(bogus=1),
        "unknown plugin": lambda d: d["plugins"].update(ghost={"enabled": True, "config": {}}),
        "is missing": lambda d: d["plugins"].pop("persona.file"),
        "outside": lambda d: d["plugins"]["wake.kws"]["config"].update(threshold=9),
        "not a documented deployment value": lambda d: d["plugins"]["wake.kws"]["config"].update(n=4),
        "enabled is True but the spec default is False": lambda d: d["plugins"]["persona.file"].update(enabled=True),
        "not HH:MM-HH:MM": lambda d: d["plugins"]["wake.kws"]["config"].update(when="late"),
        "not one of": lambda d: d["plugins"]["wake.kws"]["config"].update(kind="z"),
        "format": lambda d: d.update(format=2),
        "machine": lambda d: d.update(machine="speech"),
    }
    for needle, mutate in cases.items():
        data = good_example()
        mutate(data)
        found = example_findings(tmp_path, data)
        assert found and any(needle in str(f) for f in found), (needle, [str(f) for f in found])
    (tmp_path / "deploy" / "mira.example.json").write_text("{nope")
    assert "not valid JSON" in str(L.check_examples(example_specs(), tmp_path)[0])


def test_documented_deployment_values_may_differ(tmp_path):
    data = good_example()
    data["plugins"]["wake.kws"]["config"]["threshold"] = 0.4           # documented (2.4.5), in bounds
    assert example_findings(tmp_path, data) == []
    assert "playback.miraaudio.night_window" in L.DEPLOYMENT_DIFFS["listener"]     # owner decision 2026-11-10


def test_stale_generated_files_are_found_and_cleared(tmp_path):
    specs = example_specs()
    found = L.check_generated(specs, tmp_path)
    assert {f.where for f in found} == set(G.GENERATED_PATHS) and all(f.rule == "stale-generated" for f in found)
    G.write_all(tmp_path, specs)
    assert L.check_generated(specs, tmp_path) == []
    (tmp_path / G.KNOBS_MD).write_text("hand edited\n")
    assert [f.where for f in L.check_generated(specs, tmp_path)] == [G.KNOBS_MD]


def test_dash_scan_covers_text_files_and_skips_models(tmp_path):
    (tmp_path / "a.py").write_text("x = 1  # a " + DASH[1] + " b\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("ok\nbad " + DASH[0] + "\n", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "mira").write_text("# " + DASH[1] + "\n", encoding="utf-8")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "c.md").write_text(DASH[0], encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff" + DASH[0].encode())
    found = L.check_dashes(tmp_path)
    assert sorted(f.where for f in found) == ["a.py:1", "bin/mira:1", "sub/b.md:2"]


def test_fast_mode_skips_tests_examples_docs_and_dashes(tmp_wall):
    module, _ = make_module(tmp_wall, "lint_fast", GOOD)
    spec = module.SPEC
    (tmp_wall / "x.md").write_text(DASH[0], encoding="utf-8")
    assert L.lint([spec], tmp_wall, fast=True) == []
    full = L.lint([spec], tmp_wall, fast=False)
    assert {"stale-generated", "dash"} <= set(rules(full))


def test_lint_reports_everything_at_once(tmp_path):
    module, path = make_module(tmp_path, "lint_multi", "import os\nx = os.environ\n"
                               "from mirakit.plugin import Knob, PluginSpec\n"
                               "SPEC = PluginSpec(name='q.r', kind='q', version='1', summary='s', machine='listener',"
                               " provides=(), create=lambda c, g: None, knobs=(Knob('k', 'int', 4, 'd'),))\n")
    found = L.lint([module.SPEC, module.SPEC], tmp_path, fast=True)
    assert {"duplicate-name", "knob-unit", "knob-bounds", "environ"} <= set(rules(found))
    assert all(isinstance(str(f), str) and f.where for f in found)


def test_lint_module_has_no_literal_dash_characters():
    text = (WALL / "mirakit" / "lint.py").read_text(encoding="utf-8")
    assert not any(c in text for c in DASH)
