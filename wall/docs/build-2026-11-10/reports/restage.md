# Restage report (2026-11-10, about 22:25 to 23:10)

## Result
Release 73de1dad0ad0 (main at 73de1da, the repair commit) is staged on the Pi at /home/mira/mira-listen/releases/73de1dad0ad0, files only. Release 71dcc7c34598 is still there. Nothing live changed. Pi tests: 1 failed, 1428 passed, 26 skipped (one wall-clock microbenchmark, details below), so the clean-run gate is NOT strictly met.

## 1. Stage
- Clean detached worktree of main (73de1da) under $TMPDIR; wall/ clean. Removed afterwards (git worktree list shows only the main checkout).
- bin/mira is now a shell wrapper, so the CLI is run as `python -B -m mirakit.cli` (the venv python on this Mac).
- `deploy pi --dry-run` (ssh target overridden to mira@192.168.1.179): 45 files all "A" (adds bin/mira and deploy/mira-listen.dropin.tmpl versus the old release). Planned commands: tar extract into releases/73de1dad0ad0 only; link ~/.local/bin/mira (follows current/bin/mira, falls back to the release's bin/mira; refuses to replace a non-link or a link to another tool); write state/planned; flip `current`; restart the unit. The last three are activate steps and were skipped in the dry run.
- `deploy pi --stage`: "staged 73de1dad0ad0". It ran the extract and the wrapper link only. New on the Pi: the release folder (45 files, RELEASE.json {"built": 1794367667, "machine": "pi", "release": "73de1dad0ad0"}) and ~/.local/bin/mira -> releases/73de1dad0ad0/bin/mira. `current` does not exist; ~/.config/mira/state does not exist; no unit, env or persona touched. `mira --help` through the link works.
- Release id: 73de1dad0ad0.

## 2. Pi tests
Method: private 0700 scratch /home/mira/.mira-restage-test.XXXXXX, wall tree from the worktree plus the staged release bytes overlaid, a throwaway venv (live venv python as base, live site-packages added read-only through a .pth, pytest 9.2.0 installed into the throwaway venv only), HOME and TMPDIR in scratch, kws dir symlinked to the live one read-only, MIRA_SPEECH_TOKEN_FILE=/nonexistent (so the e2e tests that need the live mini speech server skip; no live servers, no sound, no mic), under systemd-run --user --scope CPUQuota=40% MemoryMax=500M and nice 10.
- Run A: collection error from macOS AppleDouble `._*` files in my tar (my slip). Deleted them.
- Run B: 84 failed, 2 errors, all "voice/models/kws/tokens.txt does not exist" (my scratch lacked the voice/models/kws symlink that the code defaults to). Setup slip, not product.
- Run C (correct setup), last lines:
  `FAILED tests/kit/test_kit_core.py::test_safe_point_with_a_long_due_timer_stays_on_the_fast_path`
  `1 failed, 1428 passed, 26 skipped in 1737.27s (0:28:57)`
  The failure is `assert perf_counter delta < 0.2` for 100000 safe_point calls: measured 0.26 s while the full suite ran throttled and the Pi was busy (an ssh connection also timed out once during that window). Re-run alone under the same throttle, 3 of 3 passed in about 4.4 s. The earlier busy-note race (test_sp_server_http) passed; the scaffold test passed. Skips: 26 (the 8 legacy skips, the e2e tests that need the live speech server because of the fake-only rule, and suite-internal markers).
- Suggestion: loosen that threshold or measure with a ratio, since it flakes on a loaded Pi. A full rerun costs about 29 minutes, which would not fit the budget.
- Cleanup: scratch folder (including pytest basetemp), setup script and path file deleted on the Pi; nothing left under /tmp from this job (the one systemd-private entry there belongs to mira-bt-watchdog); Mac tar and scripts deleted.

## 3. Mini dry run (nothing changed)
`deploy speech --dry-run` (target aryavora@192.168.1.174) would: snapshot server.py, server_v1.py, bin/mira, mirakit, speech_plugins into snapshots/<stamp>.tar; copy server.py to server.py.bak-plugins-2026-11-10; extract: `M server.py`, `A server_v1.py`, `A bin/mira`, `A mirakit/*`, `A speech_plugins/*`; link ~/.local/bin/mira -> the install's bin/mira. No restart (hint: launchctl kickstart -k gui/501/org.aryavora.mira-speech). Afterwards: install dir still 12 entries, no snapshots dir, no ~/.local/bin/mira, server.py unchanged.

## 4. Nothing live changed (looked at after the tests and cleanup, against preflight.md)
- Listener: PID 64358, ActiveEnter Tue 2026-11-10 19:04:17, NRestarts 0. mira_listen.py 8990842340a5ce68aa11515d0432f4ed, listen.env 77909fedcf8c1939fb2354d48ac79fb3, persona cf5d457ff61e387169f8fbb1b5bc675f, unit 9773a69a58b43c51c1d5f06f48e51e43: all equal.
- Splitter: PID 31452, ActiveEnter 17:55:52, NRestarts 0, proxy.py c12203606e83c38915da5f28032369d0, speech-split.env 63dcea6ad906d44b5632f1953ae27da0: equal.
- Mini speech server: PID 22675, server.py ffe5ae0ee3881249f1102b27dd1755c4: equal.
- Only differences on the Pi: the new release folder and ~/.local/bin/mira link. No `current`, no state/planned.
Rollback of this step: rm -r /home/mira/mira-listen/releases/73de1dad0ad0 and rm ~/.local/bin/mira (a link).
