"""mirakit.registry: providers and conflicts, needs and PENDING, order, states, effects LIFO, toggle, rebuild."""
from __future__ import annotations

import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from mirakit.plugin import (ConfigError, ConflictError, Knob, Paths, PluginBase, PluginConfig, PluginSpec, State)
from mirakit.registry import CORE_OWNER, RebuildFailed, Registry


class Host:
    machine = "listener"
    paths = Paths(here=Path("/h"), config_dir=Path("/c"), home=Path("/home"))

    def __init__(self):
        self.cfgs = {}
        self.events = []

    def cfg_of(self, plugin):
        return self.cfgs[plugin]

    def now(self):
        return 0.0

    def on(self, event, fn):
        pass

    def emit(self, event, **payload):
        self.events.append((event, payload))


class Rec(PluginBase):
    """Records every lifecycle call into a shared list; provides what its spec says."""
    journal: list = []
    boom_on_start = False

    def __init__(self, ctx, cfg, name, provides, journal, fail=None):
        super().__init__(ctx, cfg)
        self.name, self._provides, self.j, self.fail = name, provides, journal, fail
        self.obj = None

    def start(self):
        self.j.append(("start", self.name))
        if self.fail == "start":
            raise RuntimeError("no model file at /x")
        self.obj = SimpleNamespace(name=self.name, cfg=self.cfg)
        for s in self._provides:
            self.ctx.provide(s, self.obj)
        self.ctx.effect(lambda: self.j.append(("effect-a", self.name)), "a")
        self.ctx.effect(lambda: self.j.append(("effect-b", self.name)), "b")

    def stop(self):
        self.j.append(("stop", self.name))

    def apply(self, changed, cfg):
        self.j.append(("apply", self.name, dict(changed)))
        super().apply(changed, cfg)


def mk(name, provides=(), needs=(), journal=None, order=100, required=True, toggle="restart", enabled=True,
       fail=None, knobs=(), provide_actually=None, cls=None):
    journal = journal if journal is not None else []
    actually = provides if provide_actually is None else provide_actually
    factory = cls or Rec
    return PluginSpec(name=name, kind=name.split(".")[0], version="1", summary="s", machine="listener",
                      provides=tuple(provides), needs=tuple(needs), required=required, toggle=toggle,
                      default_enabled=enabled, order=order, knobs=tuple(knobs),
                      create=lambda ctx, cfg: factory(ctx, cfg, name, actually, journal, fail))


def build(specs, **kw):
    host = Host()
    reg = Registry(specs, host, **kw)
    for s in specs:
        cfg = PluginConfig(s.name, {k.name: k.default for k in s.knobs}, 1)
        host.cfgs[s.name] = cfg
        reg.add(s, cfg, s.default_enabled)
    return reg, host


def states(reg):
    return {n: e.state for n, e in reg.entries.items()}


# ---- providers
def test_two_enabled_exclusive_providers_conflict_naming_both():
    reg, _ = build([mk("source.pipewire", ["source"]), mk("source.wav", ["source"])])
    with pytest.raises(ConfigError) as ei:
        reg.check_providers()
    assert ei.value.code == "provider_conflict"
    assert "two providers for 'source': source.pipewire, source.wav; set enabled=false on one" in ei.value.message


def test_disabling_one_provider_resolves_the_conflict():
    reg, _ = build([mk("source.pipewire", ["source"]), mk("source.wav", ["source"], enabled=False)])
    reg.check_providers()


def test_multi_services_allow_many_and_order_by_priority_then_name():
    reg, host = build([mk("intent.b", ["intent"]), mk("intent.a", ["intent"]), mk("intent.c", ["intent"])])
    reg.check_providers()
    reg.boot_all()
    for name, prio in (("intent.a", 1), ("intent.b", 5), ("intent.c", 5)):
        host.cfgs[name] = PluginConfig(name, {"priority": prio}, 1)
    names = [o.name for o in reg.services("intent")]
    assert names == ["intent.b", "intent.c", "intent.a"]
    assert reg.service("intent").name == "intent.b"


def test_a_second_provide_of_an_exclusive_service_is_refused_at_runtime():
    reg, _ = build([mk("a.one", ["x"])])
    reg.boot_all()
    with pytest.raises(ConflictError) as ei:
        reg.provide("b.two", "x", object())
    assert ei.value.code == "provider_conflict"
    reg.provide("a.one", "x", object())               # the owner may replace its own object (rebuild)


def test_service_lookup_default_and_keyerror():
    reg, _ = build([])
    with pytest.raises(KeyError):
        reg.service("nothing")
    assert reg.service("nothing", None) is None
    assert reg.services("nothing") == [] and not reg.has_service("nothing")


def test_a_service_provided_with_none_still_exists():
    """agc.mic with on=false: the service exists, its value is None."""
    class NoneProvider(PluginBase):
        def start(self):
            self.ctx.provide("agc", None)

    spec = PluginSpec(name="agc.mic", kind="agc", version="1", summary="s", machine="listener", provides=("agc",),
                      create=lambda ctx, cfg: NoneProvider(ctx, cfg))
    reg, _ = build([spec])
    reg.boot_all()
    assert reg.has_service("agc") and reg.service("agc") is None and states(reg)["agc.mic"] == State.ACTIVE


# ---- order and needs
def test_start_order_is_needs_first_then_order_then_name_and_stop_is_reverse():
    j = []
    specs = [mk("z.last", ["zz"], needs=["yy"], journal=j), mk("y.mid", ["yy"], needs=["xx"], journal=j),
             mk("x.first", ["xx"], journal=j, order=200), mk("a.free", ["aa"], journal=j, order=10)]
    reg, _ = build(specs)
    assert reg.order() == ["a.free", "x.first", "y.mid", "z.last"]
    reg.boot_all()
    assert [e[1] for e in j if e[0] == "start"] == ["a.free", "x.first", "y.mid", "z.last"]
    j.clear()
    reg.stop_all()
    assert [e[1] for e in j if e[0] == "stop"] == ["z.last", "y.mid", "x.first", "a.free"]
    assert set(states(reg).values()) == {State.STOPPED}


def test_order_tie_break_uses_the_order_field_then_the_name():
    reg, _ = build([mk("b.two", ["b"], order=50), mk("a.one", ["a"], order=50), mk("c.three", ["c"], order=10)])
    assert reg.order() == ["c.three", "a.one", "b.two"]


def test_pending_lists_the_missing_service_and_starts_when_it_appears():
    j = []
    reg, _ = build([mk("tts.remote", ["tts"], needs=["speech"], journal=j)])
    reg.boot_all()
    e = reg.entry("tts.remote")
    assert e.state == State.PENDING and e.missing == ["speech"] and ("start", "tts.remote") not in j
    row = reg.row(e)
    assert row["state"] == "pending" and row["missing"] == ["speech"]
    reg.provide("speech.link", "speech", object())
    reg.boot_all()
    assert e.state == State.ACTIVE and e.missing == []


def test_pending_when_the_provider_failed():
    reg, _ = build([mk("speech.link", ["speech"], fail="start"), mk("tts.remote", ["tts"], needs=["speech"])])
    reg.boot_all()
    assert states(reg) == {"speech.link": State.FAILED, "tts.remote": State.PENDING}
    assert reg.entry("tts.remote").missing == ["speech"]


def test_a_dependency_cycle_leaves_both_pending_without_hanging():
    reg, _ = build([mk("a.x", ["ax"], needs=["by"]), mk("b.y", ["by"], needs=["ax"])])
    reg.boot_all()
    assert set(states(reg).values()) == {State.PENDING}


def test_the_core_service_satisfies_needs():
    reg, _ = build([mk("control.http", ["control"], needs=["core"])])
    reg.provide(CORE_OWNER, "core", object())
    reg.boot_all()
    assert states(reg)["control.http"] == State.ACTIVE


# ---- states, errors
def test_failed_start_sets_last_error_releases_effects_and_unprovides():
    j = []
    spec = mk("brain.hermes", ["brain"], journal=j, fail="start")
    reg, _ = build([spec])
    reg.boot_all()
    e = reg.entry("brain.hermes")
    assert e.state == State.FAILED and e.last_error == "RuntimeError: no model file at /x"
    assert not reg.has_service("brain")
    assert reg.row(e)["last_error"] == e.last_error


def test_start_that_forgets_to_provide_fails_loudly():
    reg, _ = build([mk("a.one", ["x", "y"], provide_actually=["x"])])
    reg.boot_all()
    e = reg.entry("a.one")
    assert e.state == State.FAILED and "'y'" in e.last_error
    assert not reg.has_service("x")                  # the half-start was disposed


def test_disabled_plugins_are_not_created():
    j = []
    reg, _ = build([mk("a.off", ["x"], journal=j, enabled=False)])
    reg.boot_all()
    assert reg.entry("a.off").state == State.DISABLED and j == []


def test_state_observer_hears_every_change():
    seen = []
    host = Host()
    reg = Registry([mk("a.one", ["x"])], host, on_state=lambda n, s: seen.append((n, s)))
    reg.add(reg.specs["a.one"], PluginConfig("a.one", {}, 1), True)
    host.cfgs["a.one"] = PluginConfig("a.one", {}, 1)
    reg.boot_all()
    assert seen == [("a.one", State.LOADING), ("a.one", State.ACTIVE)]


def test_effects_are_released_lifo_after_stop_and_services_are_unprovided():
    j = []
    reg, _ = build([mk("a.one", ["x"], journal=j)])
    reg.boot_all()
    assert reg.has_service("x")
    j.clear()
    reg.stop_entry(reg.entry("a.one"))
    assert j == [("stop", "a.one"), ("effect-b", "a.one"), ("effect-a", "a.one")]
    assert not reg.has_service("x")


def test_a_failing_effect_does_not_stop_the_others():
    j = []

    class Bad(Rec):
        def start(self):
            super().start()
            self.ctx.effect(lambda: 1 / 0, "bad")

    reg, _ = build([mk("a.one", ["x"], journal=j, cls=Bad)])
    reg.boot_all()
    j.clear()
    reg.stop_entry(reg.entry("a.one"))
    assert ("effect-a", "a.one") in j and ("effect-b", "a.one") in j


def test_stop_all_survives_a_plugin_that_raises_in_stop():
    class Boom(Rec):
        def stop(self):
            raise RuntimeError("x")

    j = []
    reg, _ = build([mk("a.one", ["x"], journal=j, cls=Boom), mk("b.two", ["y"], journal=j)])
    reg.boot_all()
    reg.stop_all()
    assert ("stop", "b.two") in j and not reg.has_service("x")


def test_ctx_exposes_the_documented_interface():
    seen = {}

    class Probe(PluginBase):
        def start(self):
            c = self.ctx
            seen.update(machine=c.machine, log=c.log.name, here=c.paths.here, cfg=c.cfg_of("a.one")["k"],
                        now=c.now(), has=c.get("nothing", "dflt"), all=c.all("intent"))
            c.on("x", lambda p: None)
            c.emit("y", a=1)
            self.ctx.provide("x", object())

    spec = PluginSpec(name="a.one", kind="a", version="1", summary="s", machine="listener", provides=("x",),
                      knobs=(Knob("k", "int", 4, "d"),), create=lambda ctx, cfg: Probe(ctx, cfg))
    reg, host = build([spec])
    reg.boot_all()
    assert seen == {"machine": "listener", "log": "mira.a.one", "here": Path("/h"), "cfg": 4, "now": 0.0,
                    "has": "dflt", "all": []}
    assert host.events == [("y", {"a": 1})]


# ---- required
def test_required_service_and_plugin_reports():
    reg, _ = build([mk("a.one", ["x"], fail="start"), mk("b.two", ["y"], required=False, fail="start")],
                   required_services=("x", "y", "z"))
    reg.boot_all()
    assert reg.missing_required_services() == ["x", "y", "z"]
    failed = dict(reg.failed_required())
    assert list(failed) == ["a.one"] and "no model file" in failed["a.one"]


def test_pending_required_plugin_counts_as_failed_with_what_it_waits_for():
    reg, _ = build([mk("tts.remote", ["tts"], needs=["speech"])])
    reg.boot_all()
    assert reg.failed_required() == [("tts.remote", "waiting for speech")]


# ---- toggle
def test_enable_and_disable_a_live_toggle_plugin():
    j = []
    reg, _ = build([mk("intent.music", ["intent"], journal=j, required=False, toggle="live", enabled=False)])
    reg.boot_all()
    assert reg.entry("intent.music").state == State.DISABLED
    assert reg.enable("intent.music") and reg.entry("intent.music").state == State.ACTIVE
    assert reg.has_service("intent")
    reg.disable("intent.music")
    assert reg.entry("intent.music").state == State.DISABLED and not reg.has_service("intent")
    assert not reg.entry("intent.music").enabled


def test_enable_that_would_create_a_conflict_is_refused():
    reg, _ = build([mk("source.pipewire", ["source"]), mk("source.wav", ["source"], enabled=False)])
    reg.boot_all()
    with pytest.raises(ConflictError) as ei:
        reg.enable("source.wav")
    assert ei.value.code == "provider_conflict" and "source.pipewire" in ei.value.message
    assert not reg.entry("source.wav").enabled


def test_disabling_the_only_provider_of_a_required_service_is_refused():
    reg, _ = build([mk("source.pipewire", ["source"])], required_services=("source",))
    reg.boot_all()
    with pytest.raises(ConfigError) as ei:
        reg.disable("source.pipewire")
    assert ei.value.code == "missing_service" and "source" in ei.value.message
    assert reg.entry("source.pipewire").state == State.ACTIVE


def test_disabling_a_provider_another_active_plugin_needs_is_refused():
    reg, _ = build([mk("speech.link", ["speech"], toggle="live", required=False),
                    mk("tts.remote", ["tts"], needs=["speech"])])
    reg.boot_all()
    with pytest.raises(ConfigError) as ei:
        reg.disable("speech.link")
    assert "tts.remote" in ei.value.message


def test_disabling_one_of_two_providers_of_a_required_service_is_fine():
    reg, _ = build([mk("source.pipewire", ["source"]), mk("source.wav", ["source"], enabled=False)],
                   required_services=("source",))
    reg.check_providers()
    reg.boot_all()
    reg.entry("source.wav").enabled = True       # pretend both are present, one gets disabled
    reg.entry("source.pipewire").enabled = True
    reg.check_disable("source.wav")


# ---- rebuild
def test_default_rebuild_disposes_first_then_builds_and_replaces_effects():
    j = []
    reg, host = build([mk("wake.kws", ["wake"], journal=j, knobs=[Knob("t", "float", 0.15, "d", applies="rebuild")])])
    reg.boot_all()
    old_obj = reg.service("wake")
    j.clear()
    new = PluginConfig("wake.kws", {"t": 0.05}, 2)
    assert reg.rebuild("wake.kws", new, host.cfgs["wake.kws"]) is None
    assert j == [("stop", "wake.kws"), ("effect-b", "wake.kws"), ("effect-a", "wake.kws"), ("start", "wake.kws")]
    assert reg.service("wake") is not old_obj and reg.service("wake").cfg["t"] == 0.05
    assert reg.entry("wake.kws").cfg is new and reg.entry("wake.kws").last_error is None
    assert reg.entry("wake.kws").rebuild_ms is not None
    assert len(reg.entry("wake.kws").effects) == 2          # not doubled


def test_rebuild_frees_the_old_object_before_the_new_one_is_built():
    """Never two spotters in MemoryMax=200M: the old object must be collectable when start() runs."""
    refs = []
    alive_at_start = []

    class Heavy:
        pass

    class P(PluginBase):
        def start(self):
            if refs:
                import gc
                gc.collect()
                alive_at_start.append(refs[-1]() is not None)
            obj = Heavy()
            refs.append(weakref.ref(obj))
            self.ctx.provide("wake", obj)

    spec = PluginSpec(name="wake.kws", kind="wake", version="1", summary="s", machine="listener",
                      provides=("wake",), create=lambda ctx, cfg: P(ctx, cfg))
    reg, host = build([spec])
    reg.boot_all()
    assert reg.rebuild("wake.kws", PluginConfig("wake.kws", {}, 2), host.cfgs["wake.kws"]) is None
    assert alive_at_start == [False]


def test_a_plugin_with_its_own_rebuild_is_called_directly():
    calls = []

    class Own(Rec):
        def rebuild(self, cfg):
            calls.append(cfg["t"])
            self.cfg = cfg

    reg, host = build([mk("wake.kws", ["wake"], cls=Own, knobs=[Knob("t", "float", 0.15, "d")])])
    reg.boot_all()
    assert reg.rebuild("wake.kws", PluginConfig("wake.kws", {"t": 0.05}, 2), host.cfgs["wake.kws"]) is None
    assert calls == [0.05]


def test_failed_rebuild_restores_the_previous_config_and_reports_the_error():
    j = []

    class Picky(Rec):
        def start(self):
            if self.cfg["t"] > 0.3:
                raise RuntimeError("model rejected")
            super().start()

    reg, host = build([mk("wake.kws", ["wake"], journal=j, cls=Picky, knobs=[Knob("t", "float", 0.15, "d")])])
    reg.boot_all()
    prev = host.cfgs["wake.kws"]
    err = reg.rebuild("wake.kws", PluginConfig("wake.kws", {"t": 0.4}, 2), prev)
    assert err == "RuntimeError: model rejected"
    e = reg.entry("wake.kws")
    assert e.state == State.ACTIVE and e.cfg is prev and reg.has_service("wake") and reg.service("wake").cfg["t"] == 0.15
    assert e.last_error == err


def test_failed_rebuild_whose_restore_fails_raises_rebuild_failed():
    class Doomed(Rec):
        starts = 0

        def start(self):
            Doomed.starts += 1
            if Doomed.starts > 1:
                raise RuntimeError("gone")
            super().start()

    Doomed.starts = 0
    reg, host = build([mk("wake.kws", ["wake"], cls=Doomed, knobs=[Knob("t", "float", 0.15, "d")])])
    reg.boot_all()
    with pytest.raises(RebuildFailed):
        reg.rebuild("wake.kws", PluginConfig("wake.kws", {"t": 0.4}, 2), host.cfgs["wake.kws"])
    assert reg.entry("wake.kws").state == State.FAILED


def test_rebuild_of_a_failed_plugin_tries_to_start_it_again():
    class Flaky(Rec):
        n = 0

        def start(self):
            Flaky.n += 1
            if Flaky.n == 1:
                raise RuntimeError("first time")
            super().start()

    Flaky.n = 0
    reg, host = build([mk("a.one", ["x"], cls=Flaky)])
    reg.boot_all()
    assert reg.entry("a.one").state == State.FAILED
    assert reg.rebuild("a.one", host.cfgs["a.one"], host.cfgs["a.one"]) is None
    assert reg.entry("a.one").state == State.ACTIVE


def test_inventory_row_shape():
    reg, _ = build([mk("a.one", ["x"], required=False, toggle="live")])
    reg.boot_all()
    row = reg.row(reg.entry("a.one"), ["k"])
    assert set(row) == {"name", "kind", "version", "machine", "state", "enabled", "required", "core", "provides",
                        "needs", "missing", "toggle", "rev", "pending_restart", "last_error", "status"}
    assert row["state"] == "active" and row["toggle"] == "live" and row["required"] is False
    assert row["pending_restart"] == ["k"] and row["rev"] == 1


def test_a_status_method_that_raises_does_not_break_the_row():
    class BadStatus(Rec):
        def status(self):
            raise ValueError("x")

    reg, _ = build([mk("a.one", ["x"], cls=BadStatus)])
    reg.boot_all()
    assert reg.row(reg.entry("a.one"))["status"].get("status_error") == "ValueError"


def test_duplicate_spec_names_are_refused():
    with pytest.raises(ConfigError):
        Registry([mk("a.one"), mk("a.one")], Host())


def test_start_stop_leaves_no_thread():
    class Threaded(Rec):
        def start(self):
            super().start()
            self.t = threading.Thread(target=self.stopper.wait, daemon=True)
            self.stopper = threading.Event()
            self.t = threading.Thread(target=self.stopper.wait, daemon=True)
            self.t.start()
            self.ctx.effect(self.stopper.set, "thread")

    before = threading.active_count()
    reg, _ = build([mk("a.one", ["x"], cls=Threaded)])
    reg.boot_all()
    reg.stop_all()
    import time
    time.sleep(0.05)
    assert threading.active_count() == before
