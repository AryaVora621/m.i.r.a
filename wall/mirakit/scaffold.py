"""`mira plugin new <kind> <variant>`: a disabled plugin module and its test file, from fixed templates. Stdlib only.

The output passes `run_plugin_contract` and `mira check` as it stands; the owner of the plugin then replaces the
example knob and the service object with the real ones.
"""
from __future__ import annotations

import re
from pathlib import Path

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")

MODULE = '''"""{kind} {variant} plugin (scaffold: replace this docstring with what the plugin does and why).

FIXED: none yet (list protocol constants that must not become knobs, for example sample rates)
"""
from __future__ import annotations

from typing import Mapping

from mirakit.plugin import Knob, PluginBase, PluginConfig, PluginContext, PluginSpec


class {cls}(PluginBase):
    def start(self) -> None:
        self.example_s = self.cfg.example_s
        self.ctx.provide("{kind}", self)

    def stop(self) -> None:
        self.example_s = 0.0

    def apply(self, changed: Mapping[str, object], cfg: PluginConfig) -> None:
        self.cfg = cfg
        self.example_s = cfg.example_s

    def status(self) -> dict:
        return {{"example_s": self.example_s}}


def _create(ctx: PluginContext, cfg: PluginConfig) -> PluginBase:
    return {cls}(ctx, cfg)


SPEC = PluginSpec(
    name="{kind}.{variant}", kind="{kind}", version="1", summary="{kind} {variant} (scaffold, off by default).",
    machine="{machine}", provides=("{kind}",), create=_create, default_enabled=False,
    knobs=(
        Knob("example_s", "float", 1.0, "Example delay; replace this knob with a real one.", unit="s", min=0.1,
             max=10.0, applies="live"),
    ),
    tests=("wall/tests/{testdir}/{testfile}",),
)
'''

TEST = '''"""Tests for the {kind}.{variant} plugin (scaffold: keep the contract test, replace the behaviour test)."""
from __future__ import annotations

import sys
from pathlib import Path

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit.testkit import FakeCtx, run_plugin_contract  # noqa: E402
from {package}.{module} import SPEC  # noqa: E402


def test_contract():
    run_plugin_contract(SPEC)


def test_apply_changes_the_service_the_plugin_provides():
    ctx = FakeCtx.with_specs(SPEC)
    cfg = ctx.cfg_of(SPEC.name)
    plugin = SPEC.create(ctx, cfg)
    plugin.start()
    try:
        assert ctx.get("{kind}") is plugin
        plugin.apply({{"example_s": 2.0}}, cfg.replace(example_s=2.0))
        assert plugin.status() == {{"example_s": 2.0}}
    finally:
        plugin.stop()
        ctx.release_all()
'''


class ScaffoldError(ValueError):
    pass


def new_plugin(root: Path, kind: str, variant: str, machine: str = "listener") -> tuple[list[Path], str]:
    """Write the module and test under `root` (the wall/ directory). Returns (paths written, the line to add to
    specs()). Refuses to overwrite anything."""
    if machine not in ("listener", "speech"):
        raise ScaffoldError("machine must be listener or speech")
    for label, value in (("kind", kind), ("variant", variant)):
        if not NAME.match(value):
            raise ScaffoldError(f"{label} {value!r} must match [a-z][a-z0-9_]*")
    package = f"{machine}_plugins"
    module = f"{kind}_{variant}"
    prefix, testdir = ("test_lp_", "plugins") if machine == "listener" else ("test_sp_", "speech")
    testfile = f"{prefix}{module}.py"
    mod_path = root / package / f"{module}.py"
    test_path = root / "tests" / testdir / testfile
    clash = [p for p in (mod_path, test_path) if p.exists()]
    if clash:
        raise ScaffoldError("refusing to overwrite: " + ", ".join(str(p) for p in clash))
    cls = "".join(part.capitalize() for part in (kind + "_" + variant).split("_"))
    fields = dict(kind=kind, variant=variant, machine=machine, cls=cls, module=module, package=package,
                  testdir=testdir, testfile=testfile)
    written = []
    for path, template in ((mod_path, MODULE), (test_path, TEST)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(**fields), encoding="utf-8")
        written.append(path)
    line = f"    {module}.SPEC,   # in {package}/__init__.py: import {module} and add this to the list returned by specs()"
    return written, line
