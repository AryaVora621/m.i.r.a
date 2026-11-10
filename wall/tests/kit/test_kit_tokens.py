"""Token installation (P6): files, modes, ACL commands, and no value ever printed or put on a command line."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WALL))

from mirakit import deploy as D  # noqa: E402
from mirakit import tokens as T  # noqa: E402


class SudoRunner(D.Runner):
    """Plays sudo: records the command, and for `install SRC DEST` copies the file so the test can read modes."""

    def __init__(self, etc: Path, fail_on: str = ""):
        super().__init__(None)
        self.calls: list[list[str]] = []
        self.etc, self.fail_on = etc, fail_on
        self.copied_sources: list[Path] = []

    def _exec(self, argv, input, timeout):
        self.calls.append(list(argv))
        if argv[:2] == ["sudo", "-n"]:
            cmd = argv[2:]
            if self.fail_on and cmd[0] == self.fail_on:
                return D.Result(1, b"", b"sudo: a password is required")
            if cmd[0] == "test":
                return D.Result(0 if Path(cmd[2]).exists() else 1)
            if cmd[0] == "install" and cmd[1] == "-d":
                Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
                return D.Result(0)
            if cmd[0] == "install":
                src, dest = Path(cmd[-2]), Path(cmd[-1])
                self.copied_sources.append(src)
                dest.write_bytes(src.read_bytes())
                os.chmod(dest, int(cmd[cmd.index("-m") + 1], 8))
                return D.Result(0)
            return D.Result(0)
        return D.Result(0)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_mini_creates_owner_and_pi_tokens_0600(tmp_path):
    reports = T.install("speech", tmp_path, SudoRunner(tmp_path))
    cfg = tmp_path / ".config" / "mira"
    assert [r.state for r in reports] == ["created", "created"]
    for name in ("control.owner.token", "control.pi.token"):
        assert mode(cfg / name) == 0o600
        text = (cfg / name).read_text()
        assert len(text.strip()) >= 43 and text.endswith("\n")          # token_urlsafe(32) is 43 characters
    assert (cfg / "control.owner.token").read_text() != (cfg / "control.pi.token").read_text()
    assert mode(cfg) == 0o700


def test_existing_tokens_are_never_rewritten_and_loose_modes_are_fixed(tmp_path):
    T.install("speech", tmp_path, SudoRunner(tmp_path))
    cfg = tmp_path / ".config" / "mira"
    before = (cfg / "control.owner.token").read_text()
    os.chmod(cfg / "control.pi.token", 0o644)
    reports = {r.name: r for r in T.install("speech", tmp_path, SudoRunner(tmp_path))}
    assert reports["owner"].state == "exists" and reports["pi"].state == "fixed_mode"
    assert (cfg / "control.owner.token").read_text() == before and mode(cfg / "control.pi.token") == 0o600


def test_pi_agent_token_goes_through_sudo_with_paths_only(tmp_path):
    etc = tmp_path / "etc-mira"
    runner = SudoRunner(etc)
    reports = {r.name: r for r in T.install("listener", tmp_path, runner, etc_dir=str(etc))}
    assert reports["owner"].state == "created" and reports["agent"].state == "created"
    assert reports["speech"].state == "missing_copy_from_mini"
    dest = etc / "control.agent.token"
    assert dest.exists() and mode(dest) == 0o640
    flat = [" ".join(c) for c in runner.calls]
    assert any(c.startswith("sudo -n install -o root -g mira -m 640") and c.endswith(str(dest)) for c in flat)
    assert any(c == f"sudo -n setfacl -m u:agent:r {dest}" for c in flat)
    # the staging file is gone and its contents appear in no command
    for src in runner.copied_sources:
        assert not src.exists()
    secret = dest.read_text().strip()
    assert secret and not any(secret in c for c in flat)
    assert not any(secret in repr(r) for r in reports.values())


def test_existing_agent_token_only_refreshes_the_acl(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "control.agent.token").write_text("keep-me\n")
    runner = SudoRunner(etc)
    reports = {r.name: r for r in T.install("listener", tmp_path, runner, etc_dir=str(etc))}
    assert reports["agent"].state == "exists"
    assert (etc / "control.agent.token").read_text() == "keep-me\n"
    assert not any(c[2:4] == ["install", "-o"] for c in runner.calls)
    assert any(c[2] == "setfacl" for c in runner.calls)


def test_sudo_failure_is_reported_not_raised_and_leaves_no_temp_file(tmp_path):
    etc = tmp_path / "etc"
    runner = SudoRunner(etc, fail_on="install")
    reports = {r.name: r for r in T.install("listener", tmp_path, runner, etc_dir=str(etc))}
    assert reports["agent"].state == "failed" and "password" in reports["agent"].detail
    assert not (tmp_path / ".config" / "mira" / ".agent-token.tmp").exists()


def test_reports_carry_only_paths_and_modes(tmp_path):
    etc = tmp_path / "etc"
    reports = T.install("listener", tmp_path, SudoRunner(etc), etc_dir=str(etc))
    blob = repr(reports)
    for p in (tmp_path / ".config" / "mira").glob("control.owner.token"):
        assert p.read_text().strip() not in blob
    assert all(r.mode in ("", "0600", "0640", "0400") for r in reports)


def test_files_have_no_en_or_em_dash():
    bad = chr(0x2013), chr(0x2014)
    assert not any(c in (WALL / "mirakit" / "tokens.py").read_text(encoding="utf-8") for c in bad)
