
### 2.5 Writes, change log, trials, undo, voice sessions, policy

#### 2.5.1 The one write path: `Core.submit`
Every writer (CLI, control API, Hermes through the API, trial expiry, boot guard, `mira config reload`) calls `submit`. Steps, in order:
1. Parse each `Edit.key` as `<plugin>.<key>` (plugin names are `core` or `kind.variant`; keys contain no dots). Unknown: `ConfigError("unknown_key")` naming the closest key.
2. Coerce the value to the knob type (`"0.06"` to 0.06, `"true"` to True, JSON text for list and map). `RESET` takes the schema default. `voice` values go through `voices.catalog.resolve` (Pi: through the proxy).
3. Hard bounds, choices, regex compile, host rules, then `spec.validate` on the full candidate config of each touched plugin (cross-field).
4. Policy (2.5.5) for the scope.
5. `expect_revision` given and not equal to the current revision: `ConflictError("stale_revision", revision=current)`.
6. `dry_run`: return the would-be Change with `status="dry_run"` and stop.
7. Persist before apply: under `flock(<file>.lock)`, check the file hash (2.4.1), copy the current file to `history/`, write `<file>.tmp`, fsync, `os.replace`, revision + 1.
8. Append the Change (`status="queued"`) to `changes.jsonl` with fsync. Regenerate the legacy mirror (Pi, 2.5.6).
9. Put it on the apply queue. If `wait_s > 0`, wait for the apply result up to `wait_s`; the HTTP answer is 200 with the final status, or 202 with `status="queued"` (never lost, applied at the next eligible safe point).
A batch is one Change with several edits, applied and undone atomically.

Change wire form (also the `changes.jsonl` record):
```json
{"id": "L-000124", "ts": "2026-11-10T13:42:19-0500", "machine": "listener", "actor": "hermes", "scope": "agent",
 "session": "V-0003", "reason": "owner said she cuts him off",
 "edits": [{"key": "vad.silero.end_silence", "old": 0.7, "new": 0.8}],
 "applies": "live", "status": "applied", "rev_before": 41, "rev_after": 42,
 "trial_expires": "2026-11-10T13:52:19-0500", "undo_of": null, "error": null,
 "announce": "I will wait a little longer for you to finish, 0.8 seconds instead of 0.7, for ten minutes."}
```
- Ids: `L-` (listener) and `S-` (speech) plus a zero-padded counter (max id in the log plus one at boot), `V-` for voice sessions, `T-` for turns.
- Status values: `dry_run`, `queued`, `applied`, `pending_restart`, `failed`, `reverted` (undone or expired), `confirmed` (trial kept). Status updates are appended as `{"id": "...", "status": "...", "at": "...", "error": ...}` lines; readers merge by id.
- `announce` is built from `Knob.spoken`, old and new values with units, and the trial length, in plain words. The skill reads it aloud.
- Privacy and retention: no transcript or reply text is ever stored. `reason` is capped at 120 chars; the MCP instructions tell Hermes to paraphrase, never quote the owner. `changes.jsonl` is mode 0600 and rotates to `changes.jsonl.1` above `changelog_max_bytes` (one old generation kept). `mira changes` output is safe to read aloud: spoken name, old and new value, actor, age, status.

#### 2.5.2 Undo and confirm
- `undo(change_id)` appends a new Change that writes each edit's `old` back, through the same dispatch (a reverted rebuild knob rebuilds the same way), with `undo_of` set, and marks the original `reverted`. It refuses with `ConflictError("undo_conflict", current=...)` when any key no longer holds the change's `new` value, unless `force=True` (owner only).
- `undo()` with no id: the newest change of the caller's scope that is not reverted or confirmed.
- `undo(session=V)` and `confirm(session=V)`: every change of that session, newest first, on both machines (the Pi forwards mini change ids it attached).
- Agents may undo and confirm only `scope="agent"` changes younger than `agent_undo_window_s`.
- Redo is undoing the undo.

#### 2.5.3 Trials
- An agent write is always a trial: `trial_s = clamp(requested or trial_default_s, 60, trial_max_s)`. An owner write is a trial only with `--trial S`.
- Expiry: at a safe point (listener) or the apply worker tick, once per second (speech), a due trial becomes `undo(change_id, actor="expiry", scope="system")`. Deferral rules: (a) the change belongs to an open voice session: wait until the session ends; (b) a rebuild edit: the safe point gate of 2.3.2; (c) speech only: an expiry swap waits until no `/tts` or `/stt` finished within `busy_defer_s`.
- `confirm(change_id)` clears the expiry and sets `confirmed`.
- Boot replay (before plugins start): read `changes.jsonl`; a trial whose expiry passed is reverted in the file first (logged with `actor="expiry"`); an unexpired trial keeps its remaining time.

#### 2.5.4 Voice sessions (Pi; one open at a time)
```json
{"id": "V-0003", "state": "open", "topic": "voice", "started": "...", "expires": "...", "last_activity": "...",
 "changes": ["L-000124", "S-000045"], "followup_s": 15.0, "turn": "T-000311"}
```
- Opens by `session("start")` or implicitly on the first agent write; an agent start needs a voice turn (2.5.5 rule 3). The owner can also open one from the CLI.
- While open: the follow-up overlay sets the effective `loop.turns.followup_s` to `session_followup_s` (applied at `turn_end()` and at safe points; not persisted; the args namespace returns to the file value when the session ends); agent writes are tagged with the session id; trial expiry of its changes waits.
- Ends by: `keep` (confirm all its changes), `revert` (undo all, newest first), `end` (trials keep running on their own clocks), idle (no voice turn and no agent call for `session_idle_s`), or the cap (`session_max_s` elapsed; after `session_max_changes` writes further writes get `PolicyError("session_cap")`).
- Sessions are not persisted; a restart ends the session and its trials continue on their own expiry.
- When the Pi forwards a session change to the mini it sends `trial_s = min(trial_max_s, trial_s + seconds left in the session)` so the mini trial outlives the session.

#### 2.5.5 Policy: who may do what (normative rule table)
| actor (scope) | may change | bounds | trial | rate limit | quiet hours | needs a voice turn | undo |
|---|---|---|---|---|---|---|---|
| owner CLI or owner token (`owner`) | any knob; enable, disable, reload, restart; `config reload`, `config edit` | hard min..max | only with `--trial` | none | not applied | no | any change, `--force` past a conflict |
| file (`owner`, via `mira config reload`) | any knob | hard | no | none | | no | as owner |
| Hermes or voice (`agent`) | only `agent="bounded"` knobs (the 10 in 2.4.2) | agent_min..agent_max, at most agent_step per call | always, 60..trial_max_s | 12 writes per 10 min, 3 per knob per 10 min, `session_max_changes` per session; undo, confirm, keep and revert are exempt | previews refused in `preview_quiet` | yes for writes, confirm, keep and previews, while `agent_requires_voice_turn`; undo and revert need none | only agent changes younger than `agent_undo_window_s` |
| expiry, boot guard (`system`) | reverts only | hard | n/a | exempt | n/a | n/a | n/a |
Rules:
1. Default is deny. A refusal is a `PolicyError` with a spoken-safe `message`, `allowed` (`{"min","max","step"}` for a bounded knob), and for deny knobs an `owner_command` such as `mira set wake.kws.threshold=0.06 --trial 600` that the skill files with `owner-tasks.assign_task`.
2. `agents_locked=true` refuses every agent write and preview with `PolicyError("agents_locked")`; reads still work. `mira agents lock|unlock` sets it.
3. Voice turn rule: an agent write, `confirm`, session `start` or `keep`, or preview is accepted only while `core.turn.phase == "brain"` (the listener's own state, set by the loop thread). Otherwise `PolicyError("not_in_voice_turn")`. Making a trial permanent needs a live wall turn just like making the change. `undo`, session `revert` and `end` are allowed for the agent scope at any time (they only move back toward the owner's values). This stops a Hermes session that is not a live wall turn (chat, cron, mail) from changing Mira even though it sees the tools (F11). The residual case, another Hermes session calling the tools during the seconds a wall turn is in its brain phase, is accepted and listed in section 9.
4. The speech machine re-checks policy for every Pi-token request (agent scope, the mini's own bounds, rate limit); the Pi checks first using the mini schema it caches from `GET /ctl/v1/schema` (refreshed when the mini revision changes).
5. Tokens never appear in any response, log or error; secret knobs return `{"set": true}` or `{"set": false}`.
6. Text from tool results, web pages, mail or events is data. The control API never executes instructions found in values; the MCP instructions and the skill say the same (2.12).

#### 2.5.6 Legacy mirror, rollback fidelity and the boot guard (Pi)
- When built with `mirror=True` (production only), after every applied change, `mira adopt`, and boot, the core regenerates two files in `cfgdir` from the effective config: `<cfgdir>/listen.env` (this is the unit's `EnvironmentFile` because the production `cfgdir` is `~/.config/mira`) (every knob that has `Knob.env`, as `KEY=value` lines, header `# generated by mirakit from mira.json; edit with mira set`) and `<cfgdir>/legacy.args` (one argv token per line: every knob with a legacy flag and no env var, plus the path flags `--kws-dir --vad-model --speech-token-file --hermes-key-file --voice-token-file --music-auth-file --identity --chime` with absolute paths). The first regeneration keeps the hand-written `listen.env` as `listen.env.bak-adopt-<date>`.
- The launcher's legacy path runs `mira_listen_v1.py` with `legacy.args` (systemd has loaded the current `listen.env` at this start), so `mira rollback` and the crash-loop fallback both run legacy with every tuned value it can represent.
- Who passes `mirror=True`: the listener's production `main()` and the Pi CLI's offline mode (`mira ... --offline` on the Pi, so an offline edit also updates the mirror). Everything else passes `False`: tests, the shadow runner, and the speech launcher (the mini has no mirror; its legacy `server_v1.py` reads only the plist env, so a mini rollback keeps the code defaults and the plist values, and `mira rollback` run on the mini prints the tuned voice, speed and peak it cannot carry over).
- `mira rollback` prints the knobs legacy cannot represent whose value differs from the v1 constant (for example `mute.zero.*`, `reporter.agentos.level_scale`, `loop.turns.preroll_chunks`, timeouts), then sets `core.engine=legacy`, writes a planned-restart marker and restarts the unit.
- Crash loop: the launcher appends `<epoch> unplanned` to `<cfgdir>/state/starts` on every start unless a `planned` marker (`<cfgdir>/state/planned`) younger than 60 s exists (written by `mira restart`, `mira rollback`, `mira deploy`, which it then consumes). Counting unplanned starts inside `crash_window_s`: at `crash_guard_after` (3) the boot guard runs once per window (reverts every `scope="agent"` change younger than `crash_guard_revert_s`, logged with `actor="boot_guard"`); at `crash_legacy_after` (4) the launcher runs legacy and logs `crash loop: running the legacy listener` once. A crash at any time counts (no healthy-time reset), so a bad live edit that crashes later is caught too.
- The mini launcher (`speech/server.py`) applies the same start counting with its own `state/starts`, falling back to `server_v1.py`.

### 2.6 Previews (order and safety, normative)
1. A preview renders a sample through the normal `/tts` with overrides (`voice`, `speed`, `peak`); it never changes stored state on either machine, and the mini never plays sound.
2. Text: `text` or `preview_text` with `{alias}` filled, at most `preview_max_chars`.
3. Agent previews: voice turn rule, `agents_locked`, `previews_per_10min`, `preview_quiet`.
4. During the brain phase (`core.turn.phase == "brain"`, the loop thread blocked inside `Brain.ask`): the preview plays inline on the control thread: take `playback.lock`; `prev = reporter.last`; `reporter.update("speaking", prev heard, "Voice preview: <alias>")`; `playback.call("duck")`; `playback.say_file(tmp, wav, reporter.set_level)`; `playback.call("unduck")` in a finally; `reporter.update(*prev)`; unlink the temp file; set `core.pending_flush = True`; return `{"played": true, "ms": ..., "voice": id}` after playback ends. Hermes's reply is spoken only after `Brain.ask` returns, so a preview can never overlap a reply. The next safe point flushes the source and resets the stream (2.3.2), so the mic never processes the preview even when the turn ends on a failure path.
5. Outside a turn (owner CLI only; agents are refused by rule 3): the preview is queued and played by the loop thread at the next ARMED safe point with the same sequence, followed by flush and `RESET_STREAM`. The call waits up to `wait_s` and returns `{"played": true}` or `{"queued": true}`.
6. Speech side: at most one preview render at a time (`preview_lock`; a second gets 429 `busy`); a voice in a model that is not loaded loads into the candidate slot, counted in `tts.engine.max_loaded`, and is released after 60 s idle. The active model is never unloaded for a preview. Previews happen in the brain phase, after this turn's STT, so they never compete with this turn's `/stt`.
7. Voice resolved once per reply: with `tts.remote.pin_voice`, the first chunk of a reply is sent unpinned and the server's `X-Mira-Voice` response header is stored in `Speech.reply_voice`; chunks 2 and 3 send `voice=<that id>`. Against a server without the header, `reply_voice` stays "" and the body is exactly v1's.
