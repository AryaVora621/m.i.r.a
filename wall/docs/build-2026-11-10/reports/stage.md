# M4 stage report (2026-11-10, ~20:30 to 21:15)

## Result
Release 71dcc7c34598 is staged on the Pi at /home/mira/mira-listen/releases/71dcc7c34598 (files only). Tests pass on the Pi with two caveats below. Nothing live changed.

## 1. What --dry-run and --stage do (read before running)
- `mira deploy pi --stage`: collects the manifest files from git HEAD (git archive, refuses a dirty wall/), then on the Pi runs `rm -rf releases/<sha> && mkdir -p && tar -x` into the release folder only, then `mira check --fast` in that folder using the live venv python read-only (-B). It writes no symlink, no unit, no env file, no planned marker. Safe; ran it.
- `deploy pi --dry-run`: diffs, then plans stage plus activate; every mutating command is logged and skipped (it would write state/planned, flip `current`, write releases/.previous, restart the unit). Nothing executed; confirmed `current` and ~/.config/mira/state do not exist afterwards.
- `deploy speech --dry-run`: diff against the mini install, then logs the snapshot, server.py backup and tar extract as planned, skips them.
- ssh target overridden with MIRA_DEPLOY_PI_SSH=mira@192.168.1.179 and MIRA_DEPLOY_SPEECH_SSH=aryavora@192.168.1.174 (manifest aliases pi and mini do not resolve here).

## 2. Stage
Clean detached worktree of HEAD 71dcc7c (worktree and its folder removed afterwards). Note: the repo checkout is on branch feat/mira-voice-loop at 71dcc7c, not named main; I used HEAD as it is.
Dry run listed 43 files as "A" (all new). Stage output: `staged 71dcc7c34598 in /home/mira/mira-listen/releases/71dcc7c34598`; check --fast passed.
Copied: mira_launch.py, mira_listen.py (facade), mira_listen_v1.py, mira_shadow.py, mirakit/ (19 files), listener_plugins/ (19 files), RELEASE.json ({"built": 1794360665, "machine": "pi", "release": "71dcc7c34598"}); 43 files, no __pycache__. The manifest ships code only: models, kws, assets, venv and the persona stay in the install root.

## 3. Pi tests (rung 2)
Method: private 0700 scratch /home/mira/.mira-stage-test.XXXX (deleted), staged release bytes copied from the Pi release folder, overlaid with tests/, speech/, speech_plugins/, models/, assets/, docs/, deploy/, bin/, persona and requirements from the same commit; pytest 9.x via pip --target in scratch (live venv untouched); scope CPUQuota=40% MemoryMax=400M nice 10; HOME in scratch; MIRA_SPEECH_TOKEN_FILE set (e2e uses the live mini STT and TTS, no sound, no mic, fake mira-audio); symlink scratch voice/models/kws to the live kws dir (read-only).
- Run 1 (scratch lacked the kws path and bin/): 86 failed, 1320 passed, 22 skipped, 2 errors. All causes were scratch setup (kws dir missing, wall/bin/mira missing, pytest not importable in a subprocess), not product.
- Run 2 (full, fixed setup): `2 failed, 1410 passed, 18 skipped in 1928.22s (0:32:08)`, so the legacy files plus all new suites pass except:
  1. tests/kit/test_kit_scaffold.py::test_generated_test_file_runs_green_on_its_own: the test overrides PYTHONPATH with WALL only, so the subprocess cannot find my --target pytest. Environmental. After copying pytest into the scratch wall, the file passed (33 passed, 2 of 3 reruns).
  2. tests/speech/test_sp_server_http.py::test_tts_returns_wav_at_the_model_rate_with_the_voice_header: REAL flaky race. server_http.py calls plugin.note_busy() in the `finally` after the response is written, and the test asserts `core.busy == 1` right after the client gets the reply; under the throttled Pi it sees 0. Reproduced in isolation: the sibling test_stt_transcribes_a_wav_and_notes_busy failed once in 3 reruns (line 147) with the same cause. Fix (orchestrator or a builder): call note_busy before writing the response, or make the tests poll briefly. Not changed by me.
- Skipped (18): skip markers in the suites themselves (8 legacy skips as in the spec plus 10 others); I did not audit each.
- Rung 5 (room replay, kws_report) not run: no owner recordings (preflight blocker).

## 4. Mini dry run (changes nothing)
Would: snapshot server.py, server_v1.py, mirakit, speech_plugins into snapshots/<stamp>.tar (none exists yet), copy server.py to server.py.bak-plugins-2026-11-10, then extract: `M server.py` (the live script gets overwritten), `A server_v1.py`, `A mirakit/*` (19), `A speech_plugins/*` (17 incl. voices json). No restart; hint: launchctl kickstart -k gui/501/org.aryavora.mira-speech. Mini dir listing was 13 entries before and after.

## 5. Nothing live changed (before and after snapshots, compared with preflight.json)
- Listener mira-listen: PID 64358, start Tue Nov 10 19:04:17, NRestarts 0, unchanged. mira_listen.py md5 8990842340a5ce68aa11515d0432f4ed, listen.env 77909fedcf8c1939fb2354d48ac79fb3, persona cf5d457ff61e387169f8fbb1b5bc675f, unit md5 9773a69a58b43c51c1d5f06f48e51e43 and mtime 2026-11-09 17:00:31: all equal to preflight.
- Splitter: PID 31452, start 17:55:52, NRestarts 0, proxy.py c12203606e83c38915da5f28032369d0, speech-split.env 63dcea6ad906d44b5632f1953ae27da0, unit 16210691e50b8c42f70c78097f8a09f4 (mtime 17:55:50): equal.
- Mini speech server: PID 22675, started Nov 10 01:05:55, server.py md5 ffe5ae0ee3881249f1102b27dd1755c4, plist mtime Nov 10 01:05:21: equal.
- Only difference in the Pi listing: the new `releases` folder under /home/mira/mira-listen. No current symlink, no state/planned.
Rollback of this step: `rm -r /home/mira/mira-listen/releases/71dcc7c34598`.

## For the orchestrator
- Fix the note_busy race (above) before relying on the speech suite as a gate.
- The Pi test copy needs voice/models/kws and bin/ in the wall layout; the spec 6.2 command lacks both (and pytest on PYTHONPATH breaks the scaffold subprocess test). Worth updating the ladder text.
- A full Pi run takes about 32 min throttled (9.5 min for the first, mostly failing, run).
- Evidence files: _stage_before.txt, _stage_after.txt, _stage_dry_pi.txt, _stage_dry_mini.txt in this folder.
