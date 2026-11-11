# Stage verification (cold verifier), 2026-11-10 ~21:20 to 21:45

Method: read-only on both machines; my own commands against the Pi (192.168.1.179) and the mini (192.168.1.174); one suite re-run in a private 0700 scratch on the Pi (deleted). Verdict: the staging itself held (nothing live changed, release is intact), but the verification found problems, so ok is false.

## What checked out (with evidence)

- Pi listener `mira-listen`: MainPID 64358, ActiveEnter 2026-11-10 19:04:17, NRestarts 0, active (21:18). Same as preflight.json.
- Splitter `mira-speech-split`: PID 31452, ActiveEnter 17:55:52, NRestarts 0. Same.
- Live files byte-identical to preflight: `mira_listen.py` 8990842340a5ce68aa11515d0432f4ed, `listen.env` 77909fedcf8c1939fb2354d48ac79fb3, persona `MIRA_WALL_IDENTITY.md` cf5d457ff61e387169f8fbb1b5bc675f, `proxy.py` c12203606e83c38915da5f28032369d0, `speech-split.env` 63dcea6ad906d44b5632f1953ae27da0, unit files 9773a69a... and 16210691.... No `current` symlink, no `mira-listen.service.d`, no `~/.config/mira/state` or `mira.json`; `/home/mira/mira-listen` differs from before only by the `releases` folder (mtime 20:31).
- Mini speech server: launchd pid 22675, started Nov 10 01:05:55, `server.py` md5 ffe5ae0ee3881249f1102b27dd1755c4, plist mtime Nov 10 01:05:21, `/health` ok (uptime 73181 s at check time). Mini `/private/tmp` holds nothing from the stage.
- Agent OS PID 21389 (start 17:33:59, listening 3456), Hermes gateway PID 84433 (start 19:51:08, listening 8642), kiosk PID 77714 (start 19:37:11, the accepted orchestrator restart). preflight.json records no PIDs for agent-os or Hermes, so "unchanged since preflight" cannot be proven by PID; both started before preflight finished at 19:58, which is consistent with no change since.
- Staged release `/home/mira/mira-listen/releases/71dcc7c34598`: 43 files (42 plus RELEASE.json), owner mira, dirs 775, no `__pycache__`. I took md5 of all 42 files at commit 71dcc7c (`git show`) and of the Pi folder: identical (diff empty). RELEASE.json `{"built": 1794360665, "machine": "pi", "release": "71dcc7c34598"}`. Nothing references it: no process cmdline or environ, no unit, no config file mentions `releases/71dc`.
- Re-ran a suite on the Pi myself (scratch `/home/mira/.verify.XXXXXX`, 0700, pytest via `pip --target`, `--basetemp` inside the scratch, `CPUQuota=40% MemoryMax=400M nice 10`, release bytes copied from the Pi folder): `tests/speech/test_sp_server_http.py` gave `26 passed in 6.64s`. Scratch deleted and confirmed gone.
- The stage report's two failures are real as described: the race is visible in code (`speech_plugins/server_http.py` line 173, `plugin.note_busy()` in the `finally` of `do_POST`, after the response is written; test asserts `core.busy == 1` at lines 147 and 194). My single run passed, consistent with an intermittent failure, not a contradiction.

## Problems found

1. **Leftover in shared /tmp on the Pi.** `/tmp/pytest-of-mira/` (pytest-4, pytest-5, pytest-6 and the `pytest-current` link, modified 21:15 and 21:16, 35 test folders each) was left by the stage's test reruns. It violates "nothing left in shared temp folders". Harmless, mira-owned 0700; the runbook removes it (step 0.1 i). I did not delete it (read-only job). Other /tmp files by user mira dated 17:13 to 19:56 (`m_*.py`, `*.wav`, `cpu.py`, `web-brain2-20261110.tgz`, `extra_school.txt`, `measure_hermes.py`) predate the stage (started 20:30) and are not attributable to it.
2. **Code is not on main.** The staged commit 71dcc7c is on `feat/mira-voice-loop` (also on origin). `main` is at 7a0bb0e and does not contain it (`merge-base --is-ancestor` false; HEAD is 4 commits ahead). The brief said main. `deploy` ships HEAD of whatever is checked out, so this is a landing decision, not a staging fault. The stage report did mention the branch.
3. **Tests not green, as the stage said.** One real flaky race (see above, with the code location) and one scratch-setup artefact. Rung 5 (room replay and kws_report) not run. So the stage's own `ok: false` stands; the full 1410-passed result was not re-run by me (32 minutes), only one suite.
4. **Tooling defects that the staging did not surface** (each reproduced on the Mac with the real code; details in the runbook, section 6):
   - `mira unit render|apply` fails in a staged release: `deploy/mira-listen.dropin.tmpl` is not in the manifest, so the release lacks it (`FileNotFoundError`). Blocks M7 step 3.
   - The `mira` wrapper (`wall/bin/mira`) is not shipped and not installed on the Pi (`~/.local/bin` has only `claude`) or the mini. Blocks every `mira ...` command.
   - Online CLI commands (`status`, `inventory`, `restart`, `rollback`, `enable`, `set`) stop with "owner token file not readable" until `mira tokens install` has run, so spec 7.1 steps 4 to 6 fail as written; bare `mira rollback` fails when it is needed most. `--offline` works.
   - `legacy.args` regains six flags the v1 listener does not know after any `mira set` (16 flags known after adopt, 22 after one set, 6 unknown); the launcher's crash fallback would feed them to v1 and loop. Only `mira rollback` cleans the file.
   - `mira enable control.http` cannot be used to turn the control plane on (no process answers); it needs a file edit and a second restart.
5. **Minor, no action:** the stage's mini directory count (13) differs from my `ls | wc -l` (12 non-hidden entries); `server.py` md5 and plist mtime are identical to preflight, so I treat it as a counting difference.

## Not verified
- The full Pi suite result (1410 passed) beyond the one file I re-ran.
- Hermes gateway and Agent OS against preflight PIDs (not recorded there).
- Anything about the plugin engine running on the real machines (not started, by design).
