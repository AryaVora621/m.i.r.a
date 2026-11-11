"""A release must carry every file the CLI reads when it runs on a machine (cold verifier, 2026-11-10: `mira unit
render` failed inside a staged release because the drop-in template was not in the manifest).

The static test walks the code's own string literals instead of a hand-written list, so a new reference to a
repo file that the manifest does not ship fails here. The behavioural test builds a release folder from the
manifest alone and runs the CLI from it.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import deploy as D  # noqa: E402

# Run from a checkout on the Mac, never on a machine: they read tests, docs and the manifest itself.
DEV_ONLY_MODULES = {"deploy.py", "docsgen.py", "lint.py", "scaffold.py", "testkit.py"}
NOT_SHIPPED_DIRS = {"tests", "docs", "__pycache__", "venv", ".git", "assets", "models", "kws"}
# Install-root data (persona) and names of units that live under HOME on the machine: the manifest ships code only,
# the machine already has these (spec 7.1), and the CLI reads the installed copy, not the repo's.
INSTALLED_ON_THE_MACHINE = {"MIRA_WALL_IDENTITY.md", "mira-listen.service", "org.aryavora.mira-speech.plist"}
DATA_SUFFIXES = (".tmpl", ".json", ".md", ".service", ".example", ".txt", ".sh", ".env", ".plist", ".wav", ".onnx")


def shipped(machine: str) -> set[str]:
    manifest = D.load_manifest(WALL)
    files = D.collect_from_tree(WALL, D.manifest_entries(manifest[machine]))
    return set(files)


def repo_data_files() -> dict[str, str]:
    """basename and relative path of every non-code file outside the dev-only folders, to repo-relative path."""
    found: dict[str, str] = {}
    for base, dirs, names in os.walk(WALL):
        dirs[:] = [d for d in dirs if d not in NOT_SHIPPED_DIRS]
        for name in names:
            rel = (Path(base) / name).relative_to(WALL).as_posix()
            if name in INSTALLED_ON_THE_MACHINE:
                continue
            if name.endswith(DATA_SUFFIXES) or rel == "bin/mira":
                found[rel] = rel
                found.setdefault(name, rel)
    return found


def runtime_sources() -> list[Path]:
    out = [p for p in (WALL / "mirakit").glob("*.py") if p.name not in DEV_ONLY_MODULES]
    out += list((WALL / "listener_plugins").glob("*.py")) + [WALL / "mira_launch.py", WALL / "mira_listen.py"]
    return out


def literal_references(path: Path) -> set[str]:
    refs: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and 0 < len(node.value) < 120 \
                and " " not in node.value:
            refs.add(node.value)
    return refs


def test_every_repo_file_the_runtime_code_names_is_in_the_pi_manifest():
    data = repo_data_files()
    pi = shipped("pi")
    missing = []
    for path in runtime_sources():
        for ref in literal_references(path):
            rel = data.get(ref)
            if rel and rel not in pi and not any(rel.startswith(p + "/") for p in pi):
                missing.append(f"{path.relative_to(WALL).as_posix()} names {ref!r} ({rel}), which no release carries")
    assert not missing, "\n".join(sorted(set(missing)))


def test_the_walk_sees_the_dropin_template():
    # guards the walker itself: it must find the one reference that was missed, or the test above proves nothing
    refs = literal_references(WALL / "mirakit" / "unitgen.py")
    assert "mira-listen.dropin.tmpl" in refs
    assert repo_data_files()["mira-listen.dropin.tmpl"] == "deploy/mira-listen.dropin.tmpl"


def test_unit_render_runs_from_a_release_built_from_the_manifest_alone(tmp_path):
    release = tmp_path / "release"
    for rel, data in D.collect_from_tree(WALL, D.manifest_entries(D.load_manifest(WALL)["pi"])).items():
        target = release / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "MIRA_MACHINE": "listener",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(release)}
    res = subprocess.run([sys.executable, "-B", "-m", "mirakit.cli", "unit", "render"], cwd=release, env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "MemoryMax=200M" in res.stdout and "ExecStart=%h/mira-listen" in res.stdout
