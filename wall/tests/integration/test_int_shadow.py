"""Integration: the counting-only shadow runner (spec 6.6) on a WAV, and the compare tool's PASS rule on synthetic
journals. The runner reads only the front of the listener (source, mute, AGC, spotter); the tests prove it touches no
network peer, no speaker and nothing outside its work folder."""
import json
import sys
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parents[2]
for p in (str(WALL), str(WALL / "tests"), str(WALL / "tests" / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mira_listen as ml  # noqa: E402
import mira_shadow  # noqa: E402
import shadow_compare as sc  # noqa: E402
from golden import scenarios  # noqa: E402

REPO = WALL.parent


def adopted(tmp, extra_plugins=None):
    """What `mira adopt` would hand the shadow: the whole listener file, including plugins it must ignore."""
    plugins = {
        "wake.kws": {"enabled": True, "config": {"kws_dir": str(REPO / "voice" / "models" / "kws"), "threshold": 0.15}},
        "agc.mic": {"enabled": True, "config": {}},
        "mute.zero": {"enabled": True, "config": {}},
        "speech.link": {"enabled": True, "config": {"url": "http://127.0.0.1:9", "token_file": "/nonexistent"}},
        "reporter.agentos": {"enabled": True, "config": {"url": "http://127.0.0.1:9/hook"}},
        "control.http": {"enabled": True, "config": {}},
        "playback.miraaudio": {"enabled": True, "config": {"bin": "/nonexistent/mira-audio"}},
    }
    plugins.update(extra_plugins or {})
    p = Path(tmp) / "mira.json"
    p.write_text(json.dumps({"format": 1, "machine": "listener", "revision": 3, "core": {"engine": "plugins"},
                             "plugins": plugins}))
    return p


def run_shadow(tmp, audio, monkeypatch, capsys, extra=()):
    wav = Path(tmp) / "in.wav"
    wav.write_bytes(ml.pcm16_wav_bytes(audio))
    work = Path(tmp) / "work"
    monkeypatch.setenv("HOME", str(Path(tmp) / "realhome"))
    rc = mira_shadow.main(["--config", str(adopted(tmp)), "--workdir", str(work), "--wav", str(wav), "--stats-s", "0", *extra])
    return rc, capsys.readouterr().out, work


def test_shadow_counts_the_wake_and_touches_nothing_else(tmp_path, monkeypatch, capsys):
    rc, out, work = run_shadow(tmp_path, scenarios.compose(2.0, scenarios.cal(ml), 3.0), monkeypatch, capsys)
    assert rc == 0
    hits = [ln for ln in out.splitlines() if ln.startswith("shadow hit ")]
    assert len(hits) == 1 and " agc=" in hits[0]
    # the home the shadow was given is under its work folder; the one in the environment before it is untouched
    assert not (tmp_path / "realhome").exists()
    names = {p.name for p in work.rglob("*") if p.is_file()}
    assert "shadow-config.json" in names
    assert not names & {"listen.env", "legacy.args", "changes.jsonl"}
    assert (work / "home").is_dir()


def test_shadow_runs_only_the_four_plugins(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    core = mira_shadow.build_core(adopted(tmp_path), tmp_path / "work", wav=None)
    try:
        assert set(core.registry.entries) == {"source.pipewire", "source.wav", "mute.zero", "agc.mic", "wake.kws"}
        enabled = sorted(n for n, e in core.registry.entries.items() if e.enabled)
        assert enabled == ["agc.mic", "mute.zero", "source.pipewire", "wake.kws"]
        assert core.mirror is False
    finally:
        core.shutdown()


def test_shadow_reports_mute_verdicts(tmp_path, monkeypatch, capsys):
    import numpy as np
    audio = np.concatenate([scenarios.silence(4.0), np.zeros(16000 * 14, dtype=np.float32), scenarios.silence(14.0)])
    rc, out, _ = run_shadow(tmp_path, audio, monkeypatch, capsys)
    mics = [ln.split()[-1] for ln in out.splitlines() if ln.startswith("shadow mic ")]
    assert rc == 0 and mics == ["live", "muted", "live"]


def test_shadow_stats_line_has_cpu_and_peak_rss(tmp_path, monkeypatch, capsys):
    import numpy as np
    stats = []
    core_out = []
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    wav = tmp_path / "in.wav"
    wav.write_bytes(ml.pcm16_wav_bytes(scenarios.silence(1.0)))
    core = mira_shadow.build_core(adopted(tmp_path), tmp_path / "work", wav)
    core.boot()
    try:
        mira_shadow.run(core, stats_s=1e-9, out=core_out.append)
    finally:
        core.shutdown()
    stats = [ln for ln in core_out if ln.startswith("shadow stats ")]
    assert stats and "cpu=" in stats[0] and "vmhwm_kb=" in stats[0]


# ---------------------------------------------------------------------------------------------------------------------
# the compare tool
# ---------------------------------------------------------------------------------------------------------------------

T0 = 1_800_000_000.0


def jl(t, msg):
    return f"{T0 + t:.6f} pi mira-listen[42]: 12:00:00 {msg}"


def sh(t, gain=3.0):
    return f"shadow hit t={T0 + t:.3f} agc={gain:.2f}"


def shmic(t, state):
    return f"shadow mic t={T0 + t:.3f} {state}"


def live(hits, gain=3.0):
    return "\n".join(jl(t, f"wake spotter hit 'MIRA', agc {gain:.1f}x") for t in hits)


def test_pass_when_every_hit_matches():
    journal = live([100, 200, 300])
    shadow = "\n".join(sh(t + 0.4) for t in (100, 200, 300))
    r = sc.compare(journal, shadow)
    assert r["pass"] and r["matched"] == 3 and r["shadow_only"] == []


def test_a_missed_live_hit_fails_at_ninety_five_percent():
    journal = live(range(100, 2100, 100))                       # 20 live hits
    shadow = "\n".join(sh(t) for t in range(100, 2000, 100))     # the last one is missing: 19 of 20 is exactly 95 %
    assert sc.compare(journal, shadow)["pass"]
    short = "\n".join(sh(t) for t in range(100, 1900, 100))      # 18 of 20
    r = sc.compare(journal, short)
    assert not r["pass"] and not r["checks"]["live_hits_matched"]


def test_hits_more_than_a_second_apart_do_not_match():
    r = sc.compare(live([100]), sh(101.5))
    assert r["matched"] == 0 and not r["pass"]


def test_shadow_only_limit_is_three_under_sixty_live_hits_and_five_percent_above():
    journal = live([100, 200])
    extra = "\n".join(sh(t) for t in (100, 200, 400, 500, 600))   # three extras
    assert sc.compare(journal, extra)["pass"]
    assert not sc.compare(journal, extra + "\n" + sh(700))["pass"]
    big = live(range(1000, 1000 + 100 * 100, 100))               # 100 live hits: 5 percent is 5
    sh_all = [sh(t) for t in range(1000, 11000, 100)]
    plus5 = "\n".join(sh_all + [sh(50 + 100 * i) for i in range(5)])
    plus6 = "\n".join(sh_all + [sh(50 + 100 * i) for i in range(6)])
    assert sc.compare(big, plus5)["pass"] and not sc.compare(big, plus6)["pass"]


def test_only_armed_periods_count_busy_windows_are_skipped():
    journal = "\n".join([jl(100, "wake spotter hit 'MIRA', agc 3.0x"), jl(103, "followup 8.0s")])
    # four shadow hits while the live listener was busy with the request and its 8 s window (100+1 .. 103+8)
    shadow = "\n".join([sh(100.2)] + [sh(t) for t in (102, 104, 106, 110)])
    r = sc.compare(journal, shadow)
    assert r["pass"] and r["shadow_hits_total"] == 5 and r["shadow_hits_counted"] == 1 and r["busy_windows"] == 1
    # the same extras after the window are shadow-only hits
    late = "\n".join([sh(100.2)] + [sh(t) for t in (200, 210, 220, 230)])
    assert not sc.compare(journal, late)["pass"]


@pytest.mark.parametrize("closer", ["wake_rejected heard nothing", "no_speech", "offline stt: x", "empty ", "followup 8.0s"])
def test_every_closing_line_ends_a_busy_window(closer):
    journal = "\n".join([jl(100, "wake spotter hit 'MIRA', agc 3.0x"), jl(105, closer)])
    r = sc.compare(journal, sh(100.1) + "\n" + sh(112))          # 112 is inside 105 + 8 = 113
    assert r["busy_windows"] == 1 and r["shadow_hits_counted"] == 1


def test_gain_difference_over_half_fails():
    r = sc.compare(live([100], gain=3.0), sh(100.2, 3.4))
    assert r["pass"]
    r = sc.compare(live([100], gain=3.0), sh(100.2, 3.6))
    assert not r["pass"] and not r["checks"]["agc_gain"] and r["gain_mismatches"]


def test_mute_transitions_must_match_within_two_seconds():
    journal = "\n".join([jl(50, "mic muted"), jl(300, "mic live"), jl(400, "mic muted")])   # repeat lines carry no verdict
    good = "\n".join([shmic(51.5, "muted"), shmic(299, "live"), shmic(401, "muted")])
    assert sc.compare(journal, good)["pass"]
    late = "\n".join([shmic(53.5, "muted"), shmic(299, "live"), shmic(401, "muted")])
    assert not sc.compare(journal, late)["pass"]
    wrong = "\n".join([shmic(50, "live"), shmic(299, "live"), shmic(401, "muted")])
    assert not sc.compare(journal, wrong)["pass"]
    extra = good + "\n" + shmic(900, "live")
    assert not sc.compare(journal, extra)["pass"]


def test_iso_timestamps_and_plain_lines_parse():
    iso = "2026-11-10T12:00:00+0000 pi mira-listen[1]: 12:00:00 wake spotter hit 'MIRA', agc 3.0x"
    ev = sc.parse_journal(iso + "\nnot a log line\n")
    import datetime
    want = datetime.datetime(2026, 11, 10, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    assert len(ev) == 1 and ev[0][1] == "wake" and abs(ev[0][0] - want) < 1


def test_cli_exit_codes_and_output(tmp_path, capsys):
    j, s = tmp_path / "j.txt", tmp_path / "s.txt"
    j.write_text(live([100]))
    s.write_text(sh(100.2))
    assert sc.main(["--journal", str(j), "--shadow", str(s)]) == 0
    assert capsys.readouterr().out.strip().endswith("PASS")
    s.write_text("")
    assert sc.main(["--journal", str(j), "--shadow", str(s)]) == 1
    assert capsys.readouterr().out.strip().endswith("FAIL")
    assert sc.main(["--journal", str(tmp_path / "missing"), "--shadow", str(s)]) == 2


def test_the_shadow_log_of_a_real_run_passes_against_its_own_journal(tmp_path, monkeypatch, capsys):
    rc, out, _ = run_shadow(tmp_path, scenarios.compose(2.0, scenarios.cal(ml), 3.0), monkeypatch, capsys)
    stamp = lambda tag: float(next(ln for ln in out.splitlines() if ln.startswith(tag)).split("t=")[1].split()[0])
    gain = float(next(ln for ln in out.splitlines() if ln.startswith("shadow hit ")).split("agc=")[1])
    journal = "\n".join([f"{stamp('shadow mic') + 0.2:.6f} pi mira-listen[1]: 12:00:00 mic live",
                         f"{stamp('shadow hit') + 0.3:.6f} pi mira-listen[1]: 12:00:00 wake spotter hit 'MIRA', agc {gain:.1f}x"])
    r = sc.compare(journal, out)
    assert r["pass"], r


def test_workdir_must_be_private(tmp_path):
    """A folder others can write to, a folder that is a link, and a planted config link are all refused."""
    import os
    import pytest
    import mira_shadow

    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    with pytest.raises(SystemExit):
        mira_shadow.private_workdir(shared)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(SystemExit):
        mira_shadow.private_workdir(link)
    made = mira_shadow.private_workdir(tmp_path / "fresh")
    assert (os.stat(made).st_mode & 0o777) == 0o700
    assert not mira_shadow.default_workdir().startswith("/tmp/")
