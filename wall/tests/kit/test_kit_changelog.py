"""mirakit.changelog: append, status merging, ids, rotation, trials, boot replay and boot guard planning."""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from mirakit.changelog import ChangeLog, fmt_ts, parse_ts, plan_reverts
from mirakit.plugin import Change

NOW = 1_800_000_000.0


def change(ident="L-000001", *, scope="owner", actor="owner", edits=None, status="queued", trial=None,
           undo_of=None, session=None, age=0.0, applies="live", ts=None):
    return Change(id=ident, ts=ts or fmt_ts(NOW - age), machine="listener", actor=actor, scope=scope,
                  session=session, reason="r", edits=edits or [{"key": "a.b.c", "old": 1, "new": 2}],
                  applies=applies, status=status, rev_before=1, rev_after=2,
                  trial_expires=fmt_ts(trial) if trial is not None else None, undo_of=undo_of, error=None,
                  announce="x")


@pytest.fixture
def path(tmp_path):
    return tmp_path / "state" / "changes.jsonl"


def test_timestamps_round_trip_at_one_second_resolution():
    assert parse_ts(fmt_ts(NOW)) == NOW
    assert parse_ts(None) is None and parse_ts("garbage") is None
    assert parse_ts("2026-11-10T13:42:19-0500") is not None


def test_append_writes_one_json_line_with_mode_0600_and_fsync(path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    log = ChangeLog(path, "listener")
    log.append(change())
    lines = path.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == "L-000001"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert synced, "an append must fsync"


def test_status_lines_merge_by_id_and_survive_reload(path):
    log = ChangeLog(path, "listener")
    log.append(change())
    log.status("L-000001", "applied", at=NOW)
    log.status("L-000001", "reverted", trial_expires=None, at=NOW)
    assert log.get("L-000001")["status"] == "reverted"
    fresh = ChangeLog(path, "listener")
    rec = fresh.get("L-000001")
    assert rec["status"] == "reverted" and rec["edits"] == [{"key": "a.b.c", "old": 1, "new": 2}]
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert lines[1] == {"id": "L-000001", "status": "applied", "at": fmt_ts(NOW), "error": None}


def test_status_for_an_unknown_id_is_ignored(path):
    log = ChangeLog(path, "listener")
    log.status("L-999999", "applied")
    assert log.all() == [] and ChangeLog(path, "listener").all() == []


def test_a_torn_last_line_is_skipped(path):
    log = ChangeLog(path, "listener")
    log.append(change())
    with open(path, "ab") as fh:
        fh.write(b'{"id": "L-000002", "edi')
    assert [r["id"] for r in ChangeLog(path, "listener").all()] == ["L-000001"]


def test_ids_continue_from_the_max_in_the_log(path):
    log = ChangeLog(path, "listener")
    assert log.next_id() == "L-000001"
    log.append(change("L-000007"))
    log.append(change("L-000003", session="V-0012"))
    fresh = ChangeLog(path, "listener")
    assert fresh.next_id() == "L-000008" and fresh.next_id() == "L-000009"
    assert fresh.next_id("V") == "V-0013"
    assert fresh.peek_id() == "L-000010"
    assert fresh.peek_id() == "L-000010"              # peeking consumes nothing


def test_speech_machine_uses_s_ids(path):
    log = ChangeLog(path, "speech")
    assert log.next_id() == "S-000001"
    assert log.peek_id("L") == "L-000001"


def test_rotation_keeps_one_old_generation_and_ids_stay_monotonic(path):
    log = ChangeLog(path, "listener", max_bytes=600)
    for i in range(1, 9):
        log.append(change(f"L-{i:06d}"))
    rotated = Path(str(path) + ".1")
    assert rotated.exists() and stat.S_IMODE(os.stat(rotated).st_mode) == 0o600
    ids = [r["id"] for r in ChangeLog(path, "listener", max_bytes=600).all()]
    assert ids[-1] == "L-000008" and ids == sorted(ids)
    assert log.next_id() == "L-000009"
    assert ChangeLog(path, "listener", max_bytes=600).next_id() == "L-000009"   # max over both files


def test_a_trial_in_the_rotated_file_is_still_found_and_a_status_in_the_new_file_merges(path):
    probe = ChangeLog(path.with_name("probe.jsonl"), "listener")
    probe.append(change("L-000001", trial=NOW + 60, status="applied"))
    line = len(path.with_name("probe.jsonl").read_bytes())
    log = ChangeLog(path, "listener", max_bytes=int(line * 2.5))
    log.append(change("L-000001", trial=NOW + 60, status="applied"))
    log.append(change("L-000002", status="reverted"))
    log.append(change("L-000003", status="reverted"))
    assert not Path(str(path) + ".1").exists()
    log.append(change("L-000004", status="reverted"))              # the live file is over the limit: rotate first
    assert Path(str(path) + ".1").exists()
    assert [r["id"] for r in ChangeLog(path, "listener", max_bytes=10 ** 6).trials()] == ["L-000001"]
    log.status("L-000001", "reverted", trial_expires=None)         # lands in the NEW file, the change is in .1
    fresh = ChangeLog(path, "listener", max_bytes=10 ** 6)
    assert fresh.get("L-000001")["status"] == "reverted" and fresh.get("L-000001")["trial_expires"] is None
    assert fresh.trials() == []
    assert fresh.next_id() == "L-000005"


def test_in_memory_mode_writes_nothing(tmp_path):
    log = ChangeLog(None, "listener")
    log.append(change())
    log.status("L-000001", "applied")
    assert log.get("L-000001")["status"] == "applied"
    assert list(tmp_path.iterdir()) == []


def test_trials_lists_only_running_trials(path):
    log = ChangeLog(path, "listener")
    log.append(change("L-000001", trial=NOW + 60, status="applied"))
    log.append(change("L-000002", trial=NOW + 60, status="queued"))
    log.append(change("L-000003", trial=NOW + 60, status="confirmed"))
    log.append(change("L-000004", trial=NOW + 60, status="reverted"))
    log.append(change("L-000005", trial=NOW + 60, status="applied", undo_of="L-000001"))
    log.append(change("L-000006", status="applied"))
    assert [r["id"] for r in log.trials()] == ["L-000001", "L-000002"]


def test_position_and_session_changes(path):
    log = ChangeLog(path, "listener")
    for i, s in enumerate([None, "V-0001", "V-0001", "V-0002"], 1):
        log.append(change(f"L-{i:06d}", session=s))
    assert log.position("L-000003") == 2 and log.position("L-000099") is None
    assert [r["id"] for r in log.session_changes("V-0001")] == ["L-000002", "L-000003"]


def test_no_text_beyond_the_reason_is_stored(path):
    log = ChangeLog(path, "listener")
    log.append(change())
    rec = json.loads(path.read_text().splitlines()[0])
    assert set(rec) == {"id", "ts", "machine", "actor", "scope", "session", "reason", "edits", "applies", "status",
                        "rev_before", "rev_after", "trial_expires", "undo_of", "error", "announce"}


# ---- boot replay and guard planning
def values(**kv):
    return lambda key: kv[key]


def test_expired_trial_is_planned_and_an_unexpired_one_is_not():
    recs = [change("L-000001", trial=NOW - 5, status="applied").to_dict(),
            change("L-000002", edits=[{"key": "x.y.z", "old": 1, "new": 2}], trial=NOW + 500,
                   status="applied").to_dict()]
    plans = plan_reverts(recs, values(**{"a.b.c": 2, "x.y.z": 2}), NOW)
    assert [p.change["id"] for p in plans] == ["L-000001"]
    assert plans[0].reason == "expiry"
    assert plans[0].edits == [{"key": "a.b.c", "old": 2, "new": 1}]


def test_replay_skips_a_key_that_moved_on_and_reports_it():
    recs = [change("L-000001", trial=NOW - 5, status="applied").to_dict()]
    plans = plan_reverts(recs, values(**{"a.b.c": 7}), NOW)
    assert plans[0].edits == [] and plans[0].skipped == ["a.b.c"]


def test_replay_unwinds_a_chain_newest_first():
    recs = [change("L-000001", edits=[{"key": "a.b.c", "old": 1, "new": 2}], trial=NOW - 50, status="applied").to_dict(),
            change("L-000002", edits=[{"key": "a.b.c", "old": 2, "new": 3}], trial=NOW - 40, status="applied").to_dict()]
    plans = plan_reverts(recs, values(**{"a.b.c": 3}), NOW)
    assert [p.change["id"] for p in plans] == ["L-000002", "L-000001"]
    assert plans[0].edits[0]["new"] == 2 and plans[1].edits[0]["new"] == 1 and not any(p.skipped for p in plans)


def test_replay_ignores_undo_records_and_finished_changes():
    recs = [change("L-000001", trial=NOW - 5, status="reverted").to_dict(),
            change("L-000002", trial=NOW - 5, status="confirmed").to_dict(),
            change("L-000003", trial=NOW - 5, status="applied", undo_of="L-000001").to_dict(),
            change("L-000004", trial=NOW - 5, status="failed").to_dict()]
    assert plan_reverts(recs, values(**{"a.b.c": 2}), NOW) == []


def test_boot_guard_reverts_recent_agent_changes_including_confirmed_ones():
    recs = [change("L-000001", scope="agent", actor="hermes", status="confirmed", age=100).to_dict(),
            change("L-000002", scope="agent", actor="hermes", edits=[{"key": "x.y.z", "old": 5, "new": 6}],
                   status="applied", age=50, trial=NOW + 900).to_dict(),
            change("L-000003", scope="agent", actor="hermes", status="applied", age=4000).to_dict(),   # too old
            change("L-000004", scope="owner", status="applied", age=10).to_dict()]                       # owner
    plans = plan_reverts(recs, values(**{"a.b.c": 2, "x.y.z": 6}), NOW, guard_window_s=1800, expire=False)
    assert [p.change["id"] for p in plans] == ["L-000002", "L-000001"]
    assert all(p.reason == "boot_guard" for p in plans)


def test_without_the_guard_agent_changes_are_left_alone():
    recs = [change("L-000001", scope="agent", status="applied", age=50).to_dict()]
    assert plan_reverts(recs, values(**{"a.b.c": 2}), NOW) == []


def test_guard_covers_a_queued_agent_change_persisted_before_a_crash():
    recs = [change("L-000001", scope="agent", status="queued", age=5).to_dict()]
    plans = plan_reverts(recs, values(**{"a.b.c": 2}), NOW, guard_window_s=1800, expire=False)
    assert len(plans) == 1


def test_a_real_clock_log_round_trip(path):
    now = time.time()
    log = ChangeLog(path, "listener")
    c = change("L-000001", trial=now - 1, status="applied", ts=fmt_ts(now - 30))
    log.append(c)
    recs = ChangeLog(path, "listener").all()
    assert plan_reverts(recs, values(**{"a.b.c": 2}), now)[0].edits[0]["new"] == 1
