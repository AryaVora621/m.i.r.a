
## 5. Migration order, with the test after each step and the rollback

Nothing reaches a machine until M4. M0 to M3 land on `main` in the mira and agentic-os repos (the orchestrator commits; workers never do). OW marks an owner-window step (section 10).

| step | what | test after | rollback |
|---|---|---|---|
| M0 | Wave 1: P1 contracts, P2 legacy freeze, characterization and golden recordings on v1 | four legacy files `65 passed, 8 skipped` unchanged; `tests/test_golden_parity.py` and `tests/test_characterize.py` green against v1 | delete the new files |
| M1 | Wave 2: P3 runtime, P4 listener plugins (copy-only, monolith untouched), P5 speech plugins and shim, P6 control and tools, P7 Hermes surface and knowledge drafts | each package's gates (section 11) | revert that package's files; nothing imports them yet except tests |
| M2 | Wave 3: P8 integration: facade, launcher, shadow runner, generated docs | full ladder rungs 1, 3, 4 on the Mac (section 6.1); `mira check` clean | revert P8; `mira_listen.py` returns to v1 |
| P0 | Pre-flight on the machines, read-only plus scratch runs under /tmp (section 5.1) | `reports/preflight.json` complete; T-pi baseline 73 passed on v1 | nothing to undo |
| M3 | Gate decisions from P0: `wake.kws` R or S (2.3.3); resource gates relative to the baseline | numbers recorded in the vault note draft | schema edit only |
| M4 | Pi staging: `mira deploy pi --stage` adds `releases/<sha>` (files only; nothing running changes) | rung 2 (T-pi on the release, 73 plus new tests), rung 5 (room replay and `kws_report` on the Pi) | `rm -r releases/<sha>` |
| M5 | OW Shadow 24 h (section 6.6): counting-only engine from the release beside the live listener | rung 6 compare script PASS | `systemctl --user stop mira-shadow` (live listener never touched) |
| M6 | OW Mini: deploy speech plugins, adopt the plist env into `speech.json`, engine plugins, kickstart (about 30 s of speech offline) | `/health` fields equal plus additive ones; `/stt` and `/tts` round trip from the Pi with curl (no sound); `voices` lists the catalog; a preview render saved to /tmp (not played); a `jenny` switch with `--trial 120` reverts by itself; one day of normal use | `mira config set core.engine=legacy` on the mini plus kickstart, or `mira deploy speech --rollback` (restores the snapshot and `server.py.bak-plugins-<date>`) plus kickstart |
| M7 | OW Pi cutover, owner awake, weekday (section 7.1) | startup line identical to v1; `mira inventory` all active; owner speaks one wake and reply, one follow-up, one music intent; rung 7 watch for at least 24 h | `mira rollback` (engine legacy, one command, keeps tuned values); auto fallback after 4 unplanned starts in 10 min; `mira deploy pi --rollback`; last resort `rm` the drop-in plus daemon-reload plus restart runs the untouched original file |
| M8 | Control plane: `mira tokens install`, `mira enable control.http` (live), owner canary `mira set agc.mic.target=0.06 --trial 120` reverts by itself, `mira undo` of a permanent change | `/v1/health` ok; change log lines; legacy mirror updated | `mira disable control.http` (live) |
| M9 | Hermes surface: install `/opt/mira-mcp`, register `mira-eval` (mock on 8651) in both profiles, run `dialogue_eval.py`, remove `mira-eval`, register `mira` (real), install skill, MEMORY line; persona line with `persona.file.reread=turn` (Q3) or at the next OW restart | `smoke_test.py` as user agent; `hermes skills list` shows mira-self; dialogue eval PASS; owner speaks the voice script (OW) | remove the `mcp_servers.mira` entries (reconciles without restart); restore `.bak` of skill, MEMORY.md and persona |
| M10 | Soak 7 days on the plugin engine; legacy kept on disk; then the owner decides `sigterm_idle`, the facade cleanup task, and shrinking the mini plist env to `MIRA_SPEECH_CONFIG` only (OW, a kickstart) | daily `mira doctor`; journal counts vs baseline | n/a |

### 5.1 Pre-flight P0 (orchestrator; no restarts, no sound, no recording)
1. md5 of the Pi file against `mira_listen_v1.py` (must be `8990842340a5...`); md5 of the mini `server.py` against `speech/server_v1.py`. Stop if either differs.
2. Pi: Python and venv versions; whether `pytest` is in `~/mira-listen/venv` (do not install into it); `ss -ltn` shows 8650 and 8651 free.
3. Baseline telemetry from `journalctl --user -u mira-listen --since -7d` (counts and timings only, no transcript copied): per day `wake spotter hit`, `wake_accepted`, `wake_rejected`, `no_speech`, `followup_capture`, `offline`, `mic muted`, `mic live`; latency from a `wake` line to its `reply` line; the AGC gain range from `agc N.Nx`; idle CPU from `CPUUsageNSec` over a quiet 10 minutes; `MemoryCurrent`; `NRestarts`. Mini: `/health`, request timings from `server.log`. Written to `reports/preflight.json`.
4. Rebuild and cold-start measurement (2.3.3) on the Pi, as user mira under `systemd-run --user --scope -p CPUQuota=40% -p MemoryMax=200M -p Nice=10`, against a scratch copy in `/tmp/mira-p0/` with `--wav` and a fake `mira-audio` script: spotter build ms, VmHWM after build, after dispose plus rebuild, VAD build ms, cold start to first chunk.
5. T-pi baseline: the four legacy test files on the Pi from a scratch copy (section 6.2): expect `73 passed`.
6. Hermes: confirm which platforms see MCP tools (F11), and that `owner-tasks` `assign_task` exists.
7. Locate the owner's room recordings for `room_eval.py` (ask the owner if none are on the Pi or the Mac).

## 6. Test plan and the parity proof

### 6.1 Evidence ladder (all rungs pass, in order, before the owner is asked to cut over)
1. Mac: the four legacy files give `65 passed, 8 skipped`, and `git diff --exit-code` on them, `conftest.py` and `room_eval.py` is empty. New tests gate on exit code 0.
2. Pi (T-pi): the same four files plus every new test from the staged release: `73 passed` for the legacy files (8 e2e included), new files exit 0. Run at a quiet hour under `systemd-run --user --scope -p CPUQuota=40% -p MemoryMax=400M -p Nice=10`.
3. Golden digests (P2): for every scenario, recorded once from `mira_listen_v1.py` and replayed against `mira_listen.py`: per-chunk sha256 of the AGC output (float32 bytes), raw chunk count, spotter hit times (source clock), capture bounds (start clock, end clock, samples), `events` with clock, hook state sequence (state, heard, reply, mic, level rounded to 3 dp), Hermes request bodies, sha256 of every WAV sent to `/stt`, `/tts` request bodies, the `mira-audio` call sequence. Byte-equal canonical JSON.
4. Stage and trace equivalence: the characterization suite (P2) runs parametrised over both modules (`mira_listen_v1`, `mira_listen`); plus stage equivalence tests in P4 (each moved class fed the same inputs as its v1 twin: AGC to 1e-7 per sample, mute verdicts, reporter queue items, gate and music tables, `clean_for_speech`, `speech_chunks`, `wav_envelope`).
5. Real-room evidence on the Pi: `room_eval.py` over the owner's recordings and `kws_report.py`, run for v1 and for the release; counts equal (wake hits within one per take, accepted equal).
6. 24 h shadow (6.6) PASS.
7. After cutover, at least 24 h of watching (the orchestrator tails the journal with the `sv08watch` pattern: wake every 30 min and on any `FAULT`-like line): daily counts within the baseline's day-to-day spread, no unplanned restart, idle CPU within 15 percent of the baseline, `MemoryCurrent` under 150 MB.

### 6.2 Commands
Mac (from `wall/`):
```
PY=/private/tmp/claude-501/-Users-aryavora-Desktop-Personal-Projects-agentic-os/a699da48-f6d2-4aec-8907-3db40e3fb372/scratchpad/mira-plugins/venv/bin/python
cd "/Users/aryavora/Desktop/Personal Projects/mira/wall"
PYTHONDONTWRITEBYTECODE=1 MIRA_SPEECH_TOKEN_FILE=/nonexistent "$PY" -B -m pytest -q -p no:cacheprovider \
  tests/test_unit.py tests/test_agc_reporter.py tests/test_e2e.py tests/stt_eval_test.py      # pass line: 65 passed, 8 skipped
git -C .. diff --exit-code -- wall/tests/test_unit.py wall/tests/test_agc_reporter.py wall/tests/test_e2e.py \
  wall/tests/stt_eval_test.py wall/tests/conftest.py wall/tests/room_eval.py                   # exit 0
```
Pi (T-pi; orchestrator only):
```
ssh ... mira 'sudo -n -u mira bash -lc "rm -rf /tmp/mira-test && mkdir -p /tmp/mira-test && tar -C /tmp/mira-test -xf -"' < wall-and-kws.tar
ssh ... mira 'sudo -n -u mira bash -lc "/home/mira/mira-listen/venv/bin/python -m pip install -q --target /tmp/mira-test/site pytest"'
ssh ... mira 'sudo -n -u mira XDG_RUNTIME_DIR=/run/user/1000 systemd-run --user --scope -p CPUQuota=40% -p MemoryMax=400M -p Nice=10 \
  bash -lc "cd /tmp/mira-test/wall && PYTHONPATH=/tmp/mira-test/site PYTHONDONTWRITEBYTECODE=1 \
  MIRA_SPEECH_TOKEN_FILE=/home/mira/.config/mira/speech.token /home/mira/mira-listen/venv/bin/python -B -m pytest -q -p no:cacheprovider tests"'
```
The e2e tests use the live mini speech server (STT and TTS only; `mira-audio` is faked, nothing plays).

### 6.3 Characterization scenarios (P2; written against v1 first, parametrised over both modules)
S1 calendar round trip (wake, Hermes, one-chunk reply, follow-up window expiry with no speech); S2 music intent stays local (pause), plus volume up and down math with a fake music-api (status volume 50 to 60, 95 to 100, 5 to 0); S3 follow-up reuses the Hermes session without the wake word; S4 STT offline (connection refused): chime, error trail `idle, listening, thinking, error, idle`; S5 STT HTTP 500 counts as offline; S6 Hermes failure: `error` posted, then the fail line spoken, the speaking post queued behind the 4 s hold; S7 SSE `error` event; S8 multi-chunk reply capped at 3 chunks of 280; S9 session TTL expiry starts a new session (`--session-ttl 2` with WAV time); S10 empty transcript is a reject and opens no window; S11 look-alike transcript ("mirror on the wall") rejected; S12 bare "Mira" opens the follow-up window; S13 silence only; S14 digital zeros for 12 s then signal: `mic muted` then `mic live` posts; S15 `--no-agc` level path; S16 an STT reply with invalid JSON ends `run()` with the `ValueError` propagating (crash and restart policy); S17 TTS fails on chunk 2: playback ends silently after chunk 1. `test_pwsource.py`: `PwSource` against a fake `pw-record` script that writes s16 to stdout (block sizes, drop-oldest at a small queue, EOF ends `read()` with None, `flush`, `close` twice). All with fake servers from `tests/golden/fakes.py` (stdlib HTTP, recording) and a fake `mira-audio` shell script; STT replies scripted by call order.

### 6.4 New tests per layer (owned by the package that owns the code)
- Contract suite (`tests/plugins/test_lp_contract.py`, `tests/speech/test_sp_contract.py`, using `testkit.run_plugin_contract(spec)`): unique names; defaults inside bounds; agent bounds inside hard bounds with step; descriptions under 160 chars; `start` then `stop` leaves no new thread and releases effects; a second `start` after `stop` works; every live knob applies through `apply()` and the service attribute changes; `rebuild()` frees the old object (weakref dies before the new one is built on the listener).
- Knob coverage (`tests/test_knob_coverage.py`): section 2.4.6.
- Core (`tests/kit/`): layering precedence table incl. argv-explicit detection; atomic write and history; stale revision; file_changed; invalid file at boot uses lastgood, none exits 78; undo, undo conflict, force; trials with a fake clock, boot replay, session deferral; sessions (start, implicit start, keep, revert, idle end, cap, follow-up overlay restored); policy table row by row; voice-turn rule; agents_locked; rate limits with undo exempt; provider conflicts; PENDING with missing; failed rebuild restores; legacy mirror content; boot guard; `Core.from_args` touches no file (run with `HOME` pointed at an empty temp dir and assert it stays empty).
- Control and CLI (`tests/kit/`): every route with both tokens and no token; error codes and HTTP status; secrets never in output; Pi proxy allowlist and agent re-check with a fake mini; CLI exit codes; `--json` shapes validate against `docs/contracts/control-api.schema.json`.
- Integration (`tests/integration/`): a WAV-driven listener with a change submitted from another thread mid-turn is applied only after the turn (live) or at the next ARMED quiet safe point (rebuild), and the next chunk uses it; a `wake.kws` rebuild mid-ARMED creates a new stream, clears pre-roll and flushes; a failing rebuild reverts; an inline preview during the brain phase plays before the reply and the next safe point flushes; a queued owner preview plays only when armed; trial expiry inside an open session waits; the follow-up overlay gives 15 s in a session and 8 s after; launcher engine selection, crash counting, planned markers, ImportError fallback; speech host boot with the real core and fake sherpa (`/health` shape, `/tts` header, unknown voice 400, swap with a request in flight); end-to-end tuning flow against the real Core with fake playback.
- Speech (`tests/speech/`): fake `sherpa_onnx` and `faster_whisper` (the `stt_eval_test.py` pattern): swap atomicity with a request in flight, failed load keeps the old model, `max_loaded`, candidate slot release, preview changes nothing, persistence round trip, unknown voice 400, `X-Mira-Voice`, catalog from fixture dirs (aliases, collisions, int8 folding, libritts family, Kokoro tables count equals `n_speakers`), `stt_eval_test.py` unchanged and green.
- MCP (`agentic-os:pi/mira-mcp/`): `smoke_test.py` (every tool against `mock_control.py`, error passthrough, no secret in any output), `dialogue_test.py` (2.12.5).

### 6.5 Residual risk, stated plainly
The fixture WAVs are Piper renderings. They do not exercise the JV603 hiss, noise drift over hours, mute transitions over a night, or the real wake accept rate. The Mac cannot reach the mini speech server, so the 8 e2e tests and the real STT path run only on the Pi. Parity is therefore proven for the code paths and the fixtures; for the real room it rests on rungs 5 to 7 (room replay, shadow, post-cutover watch). The spec does not claim more.

### 6.6 Shadow run (M5)
- Runner `mira_shadow.py` from the staged release (builds its core with `mirror=False`, no change log, no control plane, `HOME` and `cfgdir` under `/tmp/mira-shadow/`): `source.pipewire` (its own `pw-record`), `mute.zero`, `agc.mic`, `wake.kws` with the Pi's adopted values (from `mira adopt` dry-run output written to `/tmp/mira-shadow/mira.json`); no VAD capture, no STT, no posts, no sound, no Hermes. It logs one line per spotter hit (`shadow hit t=<epoch> agc=<gain>`), per mute verdict, and every 10 min CPU and VmHWM.
- Its own transient unit as user mira: `systemd-run --user --unit=mira-shadow -p CPUQuota=25% -p MemoryMax=150M -p Nice=10 /home/mira/mira-listen/venv/bin/python -B /home/mira/mira-listen/releases/<sha>/mira_shadow.py --config /tmp/mira-shadow/mira.json`.
- Abort rules, checked every 5 min by the orchestrator: live `NRestarts` changed; live CPU rate more than 15 percent above the P0 baseline; `MemAvailable` under 400 MB; SoC temperature over 80 C; any live `offline` line that the baseline rate does not explain. Abort = `systemctl --user stop mira-shadow`.
- Compare (`tests/tools/shadow_compare.py` over the live journal and the shadow log): only periods when the live listener was ARMED count (busy windows derived from the live journal: from a `wake` line to its closing `followup`, `wake_rejected`, `no_speech`, `offline` or `empty` line, plus `followup_s`). PASS when at least 95 percent of live spotter hits have a shadow hit within 1.0 s, shadow-only hits are at most 5 percent of live hits (or at most 3 when there are fewer than 60), mute transitions match within 2 s, and the AGC gain at matched hits differs by at most 0.5x.

## 7. Deployment (exact; orchestrator runs, owner windows marked)

### 7.1 Pi cutover (M7, OW)
1. `mira deploy pi --dry-run`, then `--stage` (already done in M4 for this sha).
2. On the Pi as mira: `mira adopt` (dry run). Check every value against the journal startup line and `listen.env`; `mira adopt --write` writes `mira.json` (engine still `legacy`), `listen.env` keeps its `.bak-adopt-<date>`, `legacy.args` written.
3. `mira unit render --check` (prints the drop-in that will be installed), then `mira unit apply`: writes `mira-listen.service.d/mira.conf` (`ExecStart=` cleared, `ExecStart=%h/mira-listen/venv/bin/python -B %h/mira-listen/current/mira_launch.py --config %h/.config/mira/mira.json`, `MemoryMax=200M`, `CPUQuota=40%`, `Nice=5`, `Restart=always`, `RestartSec=3`), `ln -sfn releases/<sha> current`, `systemctl --user daemon-reload`.
4. `mira set core.engine=plugins --offline` (the listener is still the old process), `mira restart` (planned marker plus restart).
5. Verify within 60 s: `systemctl --user is-active mira-listen`; the journal startup line equals v1's; `mira status` and `mira inventory`; then the owner speaks (one wake and reply, one follow-up, one music intent); the wall shows `idle, listening, thinking, speaking, idle`.
6. Rollback at any point: `mira rollback`.

### 7.2 Mini (M6, OW, about 30 s offline)
1. `mira deploy speech --dry-run` from the Mac (diff against the install dir), then `mira deploy speech`: tar snapshot of the current code files to `snapshots/<ts>.tar`, `cp -p server.py server.py.bak-plugins-<date>`, copy `server.py`, `server_v1.py`, `mirakit/`, `speech_plugins/`; nothing restarts.
2. `mira adopt --from-plist ~/Library/LaunchAgents/org.aryavora.mira-speech.plist` (dry run, then `--write`); `mira tokens install --machine speech` (owner and Pi control tokens, 0600, values never printed); copy the Pi token to the Pi without printing: `ssh mini 'cat ~/.config/mira/control.pi.token' | ssh pi 'sudo -n install -o mira -g mira -m 400 /dev/stdin /home/mira/.config/mira/speech.control.token'`.
3. `mira set core.engine=plugins --offline` then `launchctl kickstart -k gui/501/org.aryavora.mira-speech`.
4. Verify: `/health` (same fields plus `engine`, `revision`, `voice_id`); from the Pi `curl` `/stt` with a fixture WAV and `/tts` with a short text using the token file (`-H @<header file>` built by a script that never echoes the token); `mira voices`; `mira voices index` was run before deploy and its JSON committed.
5. Rollback: `mira set core.engine=legacy --offline` plus kickstart; or `mira deploy speech --rollback` (restore the snapshot and the `.bak`) plus kickstart. This is the one step whose undo is not automatic: the owner schedules it.

### 7.3 Hermes surface (M9)
Follow the Hermes map steps: `install -d -o root -g root -m 755 /opt/mira-mcp`; copy `server.py`, `requirements.txt`, `smoke_test.py`, `mock_control.py`, `dialogue_eval.py` (0644 root); `python3 -m venv /opt/mira-mcp/venv && /opt/mira-mcp/venv/bin/pip install -r /opt/mira-mcp/requirements.txt`; `sudo -n -u agent /opt/mira-mcp/venv/bin/python /opt/mira-mcp/smoke_test.py`; back up both Hermes configs (`.bak-mira-<date>`); add `mcp_servers.mira-eval` (env `MIRA_CONTROL_URL=http://127.0.0.1:8651`, `MIRA_CONTROL_TOKEN_FILE` pointing at a throwaway mock token file) to both; wait for `MCP servers reconciled ... added=['mira-eval']`; start the mock on 8651 with `--voice-turn always`; run `dialogue_eval.py`; remove `mira-eval`; add `mcp_servers.mira` (real URL and `/etc/mira/control.agent.token`); install the skill files as agent (`install -o agent -g agent -m 664`), `hermes skills list`; MEMORY.md line (backup first); persona file (backup first).
