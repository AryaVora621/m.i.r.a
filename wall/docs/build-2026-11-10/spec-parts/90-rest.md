
## 8. Out of scope (chosen not to do, and why)
- No Cordis port, no event bus with dispatch modes, no wrap hooks, no async rewrite, no barge-in: the single blocking thread is why she never hears herself.
- No reorderable audio stages, no change of chunk size, no second pipeline.
- No in-process hot reload of Python code on the Pi or the mini; code ships through git, tests and `mira deploy`. `reload` rebuilds a plugin from code already on disk.
- No agent-authored code path at runtime. Agents change settings through the API; plugins change through commits the owner lands.
- No isolate-on-exception mode; no SIGTERM handler by default; the `HTTPError`-as-offline mapping stays. Each is a later, owner-approved change.
- No settings web UI (a page over `/v1/schema` and `/v1/config` can come later).
- No propose queue (refusals carry `owner_command`, filed through `owner-tasks`).
- No new always-on process on the mini; no sherpa-onnx bump; no new third-party dependency on the Pi or the mini (`mcp` only inside `/opt/mira-mcp`).
- No rotation of the mini's `server.log` (launchd holds the descriptor); a later task.
- No change to the wall hook payload, `mira-audio` commands, the speech `/health` fields (additive only), binds, or token handling.
- Out of this project: removing the facade re-exports (tracked task after the soak), the `sigterm_idle` decision, changing the start-anchored wake gate (Q1).

## 9. Risks
| risk | mitigation |
|---|---|
| Behaviour drift in the move | services are the original objects; code copied, not rewritten; the exact edit list of 4.2; golden digests and characterization on both modules |
| A rebuild leaves her deaf or over budget | owner-only, ARMED and quiet only, dispose first, flush and new stream after, measured in P0.5 with a fallback to `restart` |
| Cross-thread state | control thread never applies; one documented inline-preview exception during the brain phase |
| A false wake or a TV line changes settings | ten bounded live knobs only, step limit, trial, rate limit, session cap, voice-turn rule, kill switch; nothing rebuilds the spotter, touches the gate, URLs, tokens or enables |
| Another Hermes session calls the tools during a wall turn's brain phase | accepted residual (seconds long, still bounded and trial); `agents_locked` if it ever happens |
| A bad edit crashes the process later | crash counting with no healthy reset: boot guard at 3, legacy at 4 unplanned starts in 10 min |
| Rollback loses tuning | legacy mirror after every change; `mira rollback` lists what legacy cannot represent |
| Pi copy drifts from the repo | releases are tarballs of a clean sha; the original file is never touched; `mira deploy --dry-run` diffs |
| Mini restart takes speech offline | once, in an owner window; afterwards voice and STT swap without restart; snapshot plus `.bak` for rollback |
| Preview contention on the old Intel mini | one preview at a time, candidate slot, `max_loaded` 2, previews only after this turn's STT |
| The facade becomes permanent | tracked cleanup task with a named trigger (tests and `room_eval` ported) |
| The synthetic fixtures miss real-room behaviour | rungs 5 to 7; stated as residual risk (6.5) |
| Hermes latency stretches a dialogue past the window | session follow-up overlay of 15 s; one step per turn; short replies |

## 10. Owner-window steps (no worker may do these)
| step | reversible? | undo |
|---|---|---|
| OW-M5 start and stop the shadow (reads the mic, plays and records nothing) | yes | stop the transient unit |
| OW-M6 mini speech server restart into the plugin engine (about 30 s offline) | yes, by hand (the only one without an automatic undo) | engine legacy plus kickstart, or restore snapshot and `server.py.bak-plugins-<date>` plus kickstart |
| OW-M7 Pi cutover: drop-in, `current`, adopt, engine plugins, restart | yes | `mira rollback`; auto fallback; release rollback; remove the drop-in |
| OW-M8 any owner `mira preview` (plays sound) | yes | n/a |
| OW-M9 the first spoken tuning dialogue | yes | "go back", `mira undo --session` |
| OW any later `mira restart`, `mira unit apply`, `launchctl kickstart`, plist change | yes | as for M7 and M6 |
| OW any spoken test of any kind | n/a | n/a |
Workers never restart the live listener or the speech server, never record from a microphone, never play sound, never install into a live venv, never write the vault, never commit.

## 11. Work packages
| id | title | wave | depends on | brief |
|---|---|---|---|---|
| P1 | Contracts, testkit, mock control server | 1 | | briefs/P1.md |
| P2 | Legacy freeze, characterization and golden parity harness | 1 | | briefs/P2.md |
| P3 | mirakit runtime: schema, store, change log, policy, registry, core | 2 | P1 | briefs/P3.md |
| P4 | Listener plugins (copy-only from v1) and knob coverage | 2 | P1, P2 | briefs/P4.md |
| P5 | Speech server plugins, catalog, swaps, compat shim and launcher | 2 | P1, P2 | briefs/P5.md |
| P6 | Control API, CLI, docs generator, lint, scaffold, deploy, units, tokens | 2 | P1 | briefs/P6.md |
| P7 | Hermes surface (mira MCP, dialogue tests) and the knowledge texts | 2 | P1 | briefs/P7.md |
| P8 | Listener integration: facade, launcher, shadow, integration tests, generated docs | 3 | P2, P3, P4, P5, P6, P7 | briefs/P8.md |
Packages in one wave own disjoint files (checked by script). Workers do not deploy and do not commit; the orchestrator does both, and runs P0 and every machine step.

## 12. Open questions
- Q1 (owner): the wake gate only accepts her name first ("Mira, let's tune your voice model now"). Is that acceptable, or should a later change also accept the name at the end of a short request? Out of this refactor either way.
- Q2 (owner): approve the cutover values that differ from today: `loop.turns.events_max=2000` (the trail was unbounded) and `tts.remote.pin_voice=true`.
- Q3 (owner): may `persona.file.reread` be set to `turn` at cutover, so persona edits (the new self-tuning line) need no restart?
- Q4 (owner): approve the ten voice-tunable knobs and their agent ranges (2.4.2), and that wake sensitivity stays owner-only.
- Q5 (owner): quiet hours for previews `23:00-06:00`, and the shadow run reading the mic for 24 h.
- Q6 (orchestrator): where are the room recordings for `room_eval.py` (P0.7)?
- Q7 (orchestrator): the `-I` probe wrote a git-ignored `wall/__pycache__/mira_listen.cpython-313.pyc` (F13); delete it or leave it.

## 13. Judge requirements traceability
| judge item | where |
|---|---|
| J1.1 threading rule as a contract | 2.3.1 |
| J1.2 one rebuild and apply gate, trial expiry and undo deferral | 2.3.2, 2.5.3 |
| J1.3 deaf window and backlog policy, measure first | 2.3.2 (flush), 2.3.3, 5.1 item 4 |
| J1.4 rollback fidelity and a runtime crash-loop rule | 2.5.6 |
| J1.5 pre-flight facts | 0.1, 5.1 |
| J1.6 evidence ladder | 6.1 |
| J1.7 synthetic-fixture gap | 6.5 |
| J1.8 dialogue and loop concurrency, preview order, voice per reply, mini preview caps | 2.6 |
| J1.9 mini steps, swap without restart, trials on the mini, tokens | 2.9.2, 2.5.3, 2.5.4, 7.2 |
| J1.10 freeze list | 1.1 |
| J1.11 owner-window steps | 10 |
| J1.12 bounded events, .bak text changes, facade removal task | 0.2, 7.3, 4.2 |
| J2.1 one writer-class rule table | 2.5.5 |
| J2.2 voice session object | 2.5.4 |
| J2.3 spoken safety enforced server-side | 2.5.5 rule 3, 2.3.4 |
| J2.4 prompt-injection rule and test | 2.5.5 rule 6, 2.12.1 item 6, 2.12.5 (f) |
| J2.5 bounds from one source, owner-only widening | 2.1 rule 5, 2.11, 2.12.2 |
| J2.6 no agent writes to rebuild knobs | 0.2, 2.4.2 |
| J2.7 voice catalog contract | 2.9.3 |
| J2.8 config format and edit rule | 2.4.1 |
| J2.9 end-to-end dialogue test | 2.12.5 |
| J2.10 fresh-agent contract, worked example, escape hatch first | P7 brief (wall/AGENTS.md), 8 |
| J2.11 change-log retention and privacy | 2.5.1 |
| J2.12 mini control routes, separate token, Pi re-check, Hermes holds no mini token | 2.7.1, 2.7.2, 2.5.5 rule 4 |
| J3.1 what is not a plugin | 1.2 |
| J3.2 provider selection and conflicts | 2.2 provider rules |
| J3.3 one contract set before workers start | P1, docs/contracts |
| J3.4 file ownership with no overlap | 3, 11, WORKPLAN.json check |
| J3.5 observe-only events | 2.10 |
| J3.6 parity gate for every package | 6.1, each brief's gates |
| J3.7 legacy flag, env and constant table | 2.4.6 |
| J3.8 small agent policy | 2.5.5 |
| J3.9 tuning dialogue as a test | 2.12.5 |
| J3.10 rollback at every step | 5 |
| J3.11 shared pieces named once | 2.4.2 (speech.link, reporter.agentos.level_scale, core.unit_*) |
