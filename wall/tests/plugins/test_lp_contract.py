"""The generic plugin contract (spec 6.4) for every listener spec, plus the provider rules of spec 2.2."""
import os
import sys
import types

import pytest

import test_lp_common as H
from mirakit.plugin import PluginSpec
from mirakit.services import MULTI_SERVICES, REQUIRED_LISTENER
from mirakit.testkit import FakeCore, FakeCtx, run_plugin_contract

SPECS = H.lp.specs()
NAMES = [s.name for s in SPECS]
EXPECTED = ["agc.mic", "brain.hermes", "control.http", "gate.wake", "intent.calendar", "intent.music",
            "intent.printers", "intent.school", "intent.screens", "intent.time", "intent.weather", "loop.turns", "mute.zero",
            "persona.file", "playback.miraaudio", "reporter.agentos", "source.pipewire", "source.wav", "speech.link",
            "stt.remote", "tts.remote", "vad.silero", "wake.kws"]


class PrimedCtx(FakeCtx):
    """release_all() undoes only what the plugin under test provided, not the services primed before it, because
    run_plugin_contract starts the plugin twice and the second start still needs them."""

    def freeze(self):
        self._base_provided, self._base_effects = list(self._provided), list(self._effects)

    def release_all(self):
        base_p, base_e = self._base_provided, self._base_effects
        self._provided = [t for t in self._provided if not any(t is b for b in base_p)]
        self._effects = [e for e in self._effects if not any(e is b for b in base_e)]
        try:
            super().release_all()
        finally:
            self._provided, self._effects = list(base_p), list(base_e)


def _ctx_with_everything():
    """A context where every other listener plugin's config is known and the services other plugins need exist."""
    ctx = PrimedCtx.with_specs(*SPECS)
    ctx.paths = type(ctx.paths)(here=H.WALL, config_dir=ctx.paths.config_dir, home=ctx.paths.home)
    ctx.provide("core", FakeCore(SPECS))
    return ctx


def _prime(name, ctx):
    """Start the plugins this one needs, so its own start() finds them."""
    if name in ("stt.remote", "tts.remote"):
        H.started("speech.link", ctx=ctx)
    if name == "brain.hermes":
        H.started("persona.file", ctx=ctx)
    ctx.freeze()


def _stub_control(monkeypatch):
    import mirakit
    mod = types.ModuleType("mirakit.control")

    class _H:
        address = ("127.0.0.1", 0)

        def stop(self):
            pass

        def set_config(self, cfg):
            pass

    mod.start_server = lambda core, cfg, *, machine, log, bind=(), port=0: _H()
    mod.HttpRemote = lambda base_url, token_file, timeout_s=15.0: object()
    monkeypatch.setitem(sys.modules, "mirakit.control", mod)
    monkeypatch.setattr(mirakit, "control", mod, raising=False)


def test_the_twenty_three_specs_are_unique_and_listed():
    assert sorted(NAMES) == EXPECTED and len(set(NAMES)) == 23
    assert all(isinstance(s, PluginSpec) and s.machine == "listener" for s in SPECS)
    assert all(s.kind == s.name.split(".")[0] for s in SPECS)


@pytest.mark.parametrize("name", NAMES)
def test_plugin_contract(name, tmp_path, monkeypatch):
    spec = H.spec_named(name)
    if name == "source.wav":
        # the default path is "": starting it needs a file, tested in test_lp_source_wav.py
        run_plugin_contract(spec, start=False)
        return
    if name == "source.pipewire":
        H.fake_pw_record(tmp_path, monkeypatch, b"", tail="sleep 30")
    if name == "control.http":
        _stub_control(monkeypatch)
    ctx = _ctx_with_everything()
    _prime(name, ctx)
    run_plugin_contract(spec, make_ctx=lambda: ctx)


@pytest.mark.parametrize("name", NAMES)
def test_every_numeric_knob_has_bounds_and_every_knob_a_short_description(name):
    for k in H.spec_named(name).knobs:
        assert 0 < len(k.description) <= 160, (name, k.name)
        if k.type in ("int", "float"):
            assert k.min is not None and k.max is not None, (name, k.name)
        if k.agent == "bounded" and k.type in ("int", "float"):
            assert k.min <= k.agent_min <= k.agent_max <= k.max and k.agent_step > 0


@pytest.mark.parametrize("name", NAMES)
def test_tests_point_at_files_that_exist(name):
    spec = H.spec_named(name)
    assert spec.tests
    for rel in spec.tests:
        assert (H.WALL.parent / rel).is_file(), rel


def test_provider_rules():
    providers = {}
    for s in SPECS:
        if s.default_enabled:
            for svc in s.provides:
                providers.setdefault(svc, []).append(s.name)
    for svc, names in providers.items():
        if svc not in MULTI_SERVICES:
            assert len(names) == 1, (svc, names)  # two enabled providers of an exclusive service
    assert H.spec_named("source.wav").default_enabled is False and H.spec_named("control.http").default_enabled is False
    # every required listener service has an enabled provider by default
    for svc in REQUIRED_LISTENER:
        assert svc in providers, svc
    assert set(providers["source"]) == {"source.pipewire"}
    assert [s.name for s in SPECS if s.core] == ["loop.turns"]
    # The quick-answer intents are optional too: a failure in one must never take the listener down (exit 78).
    quick = ["intent.calendar", "intent.printers", "intent.school", "intent.time", "intent.weather"]
    # intent.screens is optional too, and the one intent that is on by default: it only acts on a clear screen request.
    assert sorted(s.name for s in SPECS if not s.required) == sorted(["control.http", "intent.screens"] + quick)
    assert all(not H.spec_named(n).default_enabled for n in quick)
    assert H.spec_named("intent.screens").default_enabled is True
    # control.http is restart class: the CLI that would enable it needs the API it starts (stage.verify defect 5)
    assert {s.name for s in SPECS if s.toggle == "live"} == {"intent.music", "intent.screens"} | set(quick)
    assert H.spec_named("control.http").toggle == "restart"


def test_needs_and_reads_name_real_services_and_knobs():
    provided = {svc for s in SPECS for svc in s.provides} | {"core"}
    knobs = {f"{s.name}.{k.name}" for s in SPECS for k in s.knobs}
    for s in SPECS:
        assert set(s.needs) <= provided, (s.name, s.needs)
        assert set(s.reads) <= knobs, (s.name, s.reads)
        assert not (set(s.wants) - provided)


def test_start_order_resolves_without_cycles():
    provided = {}
    for s in SPECS:
        for svc in s.provides:
            provided.setdefault(svc, set()).add(s.name)
    deps = {s.name: {p for n in s.needs for p in provided.get(n, ())} for s in SPECS}
    done, order = set(), []
    while len(done) < len(deps):
        ready = sorted(n for n, d in deps.items() if n not in done and d <= done)
        assert ready, f"cycle among {set(deps) - done}"
        done.update(ready)
        order += ready
    assert order.index("speech.link") < order.index("tts.remote") and order.index("persona.file") < order.index("brain.hermes")


def test_plugin_code_never_reads_the_environment():
    # spec 2.1 rule 2, applied to the plugin classes and specs; the moved v1 classes are allow-listed in
    # LEGACY_CLASSES (MicAGC still reads MIRA_AGC_* when a parameter is None)
    import ast
    pkg = H.WALL / "listener_plugins"
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mod = __import__(f"listener_plugins.{path.stem}", fromlist=["x"]) if path.stem != "__init__" else None
        legacy = set(getattr(mod, "LEGACY_CLASSES", ())) | ({"_envf"} if path.stem == "agc_mic" else set())
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in legacy:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in ("environ", "getenv") and isinstance(sub.value, ast.Name) \
                        and sub.value.id == "os":
                    pytest.fail(f"{path.name}:{sub.lineno} reads the environment outside the legacy classes")


def test_listener_plugin_modules_pass_the_real_source_and_knob_lint():
    from pathlib import Path
    from mirakit import lint
    specs = H.lp.specs()
    found = [f for f in lint.check_knobs(specs) if f.kind == "knob-unit"]
    for s in specs:
        path = lint.module_file(s)
        if path is not None and path.parent.name == "listener_plugins":
            found += lint.scan_source(s, path)
    assert [str(f) for f in found] == []
