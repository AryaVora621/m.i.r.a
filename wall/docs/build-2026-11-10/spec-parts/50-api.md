
### 2.7 Control API (`mirakit/control.py`; JSON over HTTP/1.1)

#### 2.7.1 Transport and auth
- Entry point (P6, `mirakit/control.py`): `start_server(core: CoreAPI, cfg: PluginConfig, *, machine: Literal["listener", "speech"], log: logging.Logger, bind: Sequence[str] = (), port: int = 0) -> ServerHandle` (`ServerHandle.stop()` joins the thread; `.address`), and `class HttpRemote(RemoteClient): __init__(self, base_url: str, token_file: str, timeout_s: float = 15.0)`. The two plugin spec modules `listener_plugins/control_http.py` (P4) and `speech_plugins/control_http.py` (P5) hold the knobs of 2.4.2, import `mirakit.control` lazily inside `start()`, call `start_server(ctx.get("core"), cfg, ...)`, register `ctx.effect(handle.stop)`, and on the listener `core.register_remote("speech", HttpRemote(cfg.speech_url, cfg.speech_token_file))`. The mini plugin mounts its routes on the speech server's existing `ThreadingHTTPServer` instead of binding (P5's `server.http` calls `control.handle(core, handler, path, scope)` for `/ctl/v1/` paths; P6 implements `handle`).
- Pi: `http://127.0.0.1:8650`, served by the `control.http` plugin (one `ThreadingHTTPServer` thread, zero idle CPU), loopback only. Mini: the speech server's existing binds and port 8765, routes under `/ctl/v1/`, served by the speech `control.http` plugin inside the same process (no new always-on agent).
- `Authorization: Bearer <token>`; compared with `hmac.compare_digest` against the token file contents, re-read on every request. Scope comes from which file matched, never from a header. Pi: `owner_token_file` gives `owner`, `agent_token_file` gives `agent`. Mini: `owner_token_file` gives `owner`, `pi_token_file` gives `agent`; the data-plane `speech.token` is NOT accepted on `/ctl/v1`.
- Request bodies at most 64 KiB; unauthenticated bodies are not read; failed auth returns 401 and closes the connection. Request lines are logged (method, path, code, ms); bodies never.
- Every response carries `"revision"`. Errors: `{"error": code, "message": spoken-safe text, "field"?: key, "allowed"?: {...}, "owner_command"?: str, "current"?: value, "revision": n}`.

| HTTP | error codes |
|---|---|
| 400 | invalid, unknown_key, unknown_plugin, unknown_voice (with `near`: up to 3 aliases) |
| 401 | unauthorized |
| 403 | policy, not_in_voice_turn, quiet_hours, session_cap |
| 409 | stale_revision, undo_conflict, file_changed, provider_conflict |
| 423 | agents_locked |
| 429 | rate_limited, busy (speech preview slot) |
| 502 | speech_unreachable (Pi proxy) |
| 500 | internal (message never contains request text) |

#### 2.7.2 Routes (Pi `/v1/...`; the mini serves the same family under `/ctl/v1/...` where marked M)
| method and path | scope | body or query | returns |
|---|---|---|---|
| GET /v1/health | none | | `{ok, machine, version, engine, uptime_s, revision}` |
| GET /v1/status | any | | Status (2.7.3) |
| GET /v1/inventory (M) | any | `?machine=listener|speech` (Pi proxies speech) | `[InventoryRow]` |
| GET /v1/plugins/{name} (M) | any | | `{spec, knobs: [KnobRow]}`; agent scope sees agent bounds as min and max |
| GET /v1/schema (M) | any | | `{machine, contract_version, plugins: [{name, kind, version, summary, provides, needs, knobs: [Knob as JSON]}]}` |
| GET /v1/config (M) | any | `?effective=1` | `{revision, core: {...}, plugins: {...}}` with `source` per key when effective; secrets as `{"set": bool}` |
| GET /v1/tuning_guide | any | `?topic=voice|hearing|timing|wake|brain` | 2.7.3 |
| PUT /v1/config/{plugin}/{key} (M) | any | `{value, reason, trial_s?, expect_revision?, dry_run?, wait_s?}` | Change |
| POST /v1/config/batch (M) | any | `{edits: [{key, value}], reason, trial_s?, expect_revision?, dry_run?, wait_s?}` | Change |
| POST /v1/config/reload | owner | `{}` | Change (actor file) |
| GET /v1/changes (M) | any | `?limit=20&since=<id>&session=<id>&actor=<a>` | `[Change]` newest first; the Pi merges mini changes when `machine=all` |
| POST /v1/undo (M) | any | `{change_id?, session?, force?}` | `[Change]` |
| POST /v1/confirm (M) | any | `{change_id?, session?}` | `[Change]` |
| GET /v1/session | any | | Session or `null` |
| POST /v1/session | any | `{action: "start"|"keep"|"revert"|"end", topic?}` | Session or `null` |
| POST /v1/plugins/{name}/enable, /disable, /reload (M) | owner | `{}` | Change |
| POST /v1/restart | owner | `{}` | `{ok, at}`: writes the planned marker, exits at the next safe point after the final idle post; systemd starts it again |
| POST /v1/preview | any | `{text?, voice?, speed?, peak?, wait_s?}` | `{played, queued, ms, voice}` |
| GET /v1/voices (M) | any | `?lang=en&limit=12&include_unusable=0` | Voices (2.9.3); the Pi proxies |
Pi proxy rule: a request with `machine=speech` (query) or a key whose plugin is a speech plugin is checked against agent policy on the Pi first (rule 4 of 2.5.5), then forwarded to `<speech_url>/ctl/v1/<same path>` with the Pi token and `{"actor": "<actor>", "session": "<id>"}` added to the body. Only these speech paths are forwarded: GET inventory, plugins/{name}, schema, config, changes, voices; PUT config/{plugin}/{key}; POST config/batch, undo, confirm. Forwarded results inside an open session are attached with `attach_remote_change`.

#### 2.7.3 Response shapes
InventoryRow: `{name, kind, version, machine, state, enabled, required, core, provides, needs, missing, toggle, rev, pending_restart: [keys], last_error, status: {...}}`.
KnobRow: `{key, type, value, default, source: "default"|"env"|"file"|"argv"|"session", min, max, unit, applies, agent: "deny"|"bounded", agent_min, agent_max, agent_step, settable_by_voice, description, spoken, effect, topic, pending: null|"queued"|"restart", secret}`.
Status: `{ok, machine, engine, version, revision, uptime_s, loop: "armed"|"followup"|"turn", turn: null|{id, phase, since_s}, mic: null|"muted"|"live", failed: [names], pending_restart: [keys], session: null|Session, agents_locked, config_error: null|str, speech: {reachable, engine, stt, voice}}`.
Tuning guide: `{topic, knobs: [{key, spoken, value, unit, settable_by_voice, agent_min, agent_max, agent_step, ladder: [values], effect, phrases, applies}], owner_only: [{key, spoken, why, owner_command}]}`. `ladder` lists the agent-reachable values from agent_min to agent_max in agent_step steps. Generated from the schemas of both machines, so nothing is hand-copied.

### 2.8 CLI (`mira`, `mirakit/cli.py`)
Installed as `~/.local/bin/mira` for user `mira` on the Pi (runs `/home/mira/mira-listen/venv/bin/python -B -m mirakit.cli` from `current/`) and for user `aryavora` on the mini; from the Mac the orchestrator runs it over ssh. Online through the control API with the owner token when the process is up and `control.http` is enabled; otherwise offline on the file under the lock (refused with exit 8 unless `--offline` is passed, so nobody edits behind a running process by accident).

```
mira status [--json]
mira inventory [--machine listener|speech] [--json]            plugins, state, missing, pending restarts, last error
mira describe <plugin> [--json]                                 every knob: value, default, source, bounds, unit, applies, agent policy
mira get [<plugin>[.<key>]] [--effective] [--json]
mira set <plugin.key>=<value> [...] [--reason TEXT] [--trial SEC] [--expect-rev N] [--dry-run] [--wait SEC]
mira reset <plugin.key> [...]
mira undo [<change_id>] [--session V-n] [--force]
mira confirm [<change_id>] [--session V-n]
mira changes [-n 20] [--session V-n] [--actor A] [--json]
mira session [start|keep|revert|end]
mira enable|disable|reload <plugin>
mira restart                                                    planned restart of the unit (Pi) or kickstart (mini)
mira agents lock|unlock
mira preview [--voice V] [--text T] [--speed S] [--peak P]     plays sound on the wall: owner window only
mira voices [--lang en] [--all]
mira voices index                                               speech only, offline: regenerate speech_plugins/voices/*.json from model metadata
mira voices bench [--voice V]                                   speech only, offline, owner: measure rtf per usable voice into rtf_file
mira schema [<plugin>] [--json]
mira tuning-guide <topic> [--json]
mira config validate [FILE] | render --full [--merge] | edit | reload | diff [REV]
mira adopt [--write] [--from-plist FILE]                        freeze effective legacy values into the file (dry run by default)
mira unit render [--check] | apply                              systemd drop-in (Pi) or plist (mini) from core.unit_*; apply = owner window
mira rollback                                                   engine legacy, planned restart, prints what legacy cannot represent
mira deploy pi|speech [--stage | --activate SHA | --rollback [SHA] | --dry-run]
mira tokens install [--machine listener|speech]                 creates missing token files and ACLs; never prints a value
mira doctor [--json]                                            drift: unit vs config, argv overrides, env vars set but ignored, token modes,
                                                                binds, port free, pins (sherpa-onnx==1.12.40), last-good age; no audio
mira check [--fast]                                             contract lint (2.11) plus the plugin test files exist
mira docs [--skill DIR] [--vault DIR]                           regenerate docs/PLUGINS.md, docs/KNOBS.md, schema JSON, skill and vault references
mira plugin new <kind> <variant> [--machine listener|speech]   scaffold (wall/AGENTS.md)
```
Exit codes: 0 applied or ok, 1 invalid value or usage, 2 unknown key or plugin, 3 queued (accepted, not applied yet), 4 policy refused, 5 conflict, 6 failed and rolled back, 7 pending restart, 8 process unreachable and `--offline` not given, 9 speech unreachable.
Output: tables for people; `--json` gives the API shapes unchanged. `mira set` prints one line per edit: `agc.mic.target 0.05 -> 0.06 applied (L-000125)`.

### 2.9 Speech server (mini)

#### 2.9.1 Process and compatibility
- The LaunchAgent `org.aryavora.mira-speech` and its `ProgramArguments` (`.../mira-speech/server.py`) do not change. `speech/server.py` becomes: (a) the compat module, (b) the launcher.
- Compat (F9): `server.py` keeps every module-level name `bench_*.py`, `stt_eval.py` and `stt_eval_test.py` use, computed from env at import exactly as v1 (`BASE`, `MODELS`, `TOKEN_FILE`, `BIND`, `PORT`, `STT_KIND`, `TTS_DIR`, `TTS_VOICE`, `THREADS`, `TTS_THREADS`, `TARGET_PEAK`, `DEFAULT_VOCAB`, `STT_PROMPT`, `STT_HOTWORDS`, `STT_COMPUTE`, `STT_BEAM`, `MAX_STT_BYTES`, `MAX_TTS_CHARS`, `STT_RATE`), plus `Stt(kind, prompt=None, hotwords=None, compute_type=None, beam_size=None)`, `Tts(model_dir, voice)`, `read_wav16k`, `write_wav`, `normalise`, `main`. `Stt` and `Tts` read `MODELS`, the env defaults and `THREADS` from the `server` module globals AT CALL TIME and pass them to the backend plugin modules, so `monkeypatch.setattr(server, "MODELS", tmp)` keeps working. Backends import `faster_whisper` and `sherpa_onnx` inside their load functions.
- Launcher: `main()` reads `MIRA_SPEECH_CONFIG` (default `~/.config/mira/speech.json`); `core.engine == "legacy"` (or file missing, or `ImportError` of `mirakit`, or the crash-loop rule) runs `server_v1.main()` (byte copy of today's file, beside it); `plugins` boots a speech `Core` and serves.
- Kept exactly: binds (wildcard refused, retry bind every 5 s until tailscaled is up), `server_bind` without getfqdn, `/health` unauthenticated with `ok, stt, tts, voice, uptime_s` (additive: `engine`, `revision`, `voice_id`), `/stt` and `/tts` auth, limits and 404 behaviour, 401 closes without draining, request text never logged, token read once at start.

#### 2.9.2 Engines and swaps
- `stt.engine` provides `stt` (`SttEngine`): one active `SttModel` built by the backend whose `backend_ids` contain the backend part of `kind`; the `-fp32` suffix and the transducer variant are passed as options. A `kind` or `threads` change builds the new model on the apply worker, outside every lock, warms it with 1 s of silence (`warm`), then swaps the reference; requests in flight finish on the old model (each model keeps its own lock); the old one is dropped by refcount. A failed build keeps the old model, marks the change `failed` with the model name (never text) and writes the old value back. `prompt`, `hotwords`, `beam` are read per transcribe (live).
- `tts.engine` provides `tts` (`TtsEngine`): `synth(text, voice, speed, peak)` takes one reference to (active model, settings snapshot) per request. Voice resolution: empty means the active voice; the same multi-speaker model as the active one only changes `sid` (no load); a voice in the candidate slot uses it; any other usable voice loads into the candidate slot (at most `max_loaded` models resident; the candidate is evicted first; the active model is never unloaded for a request). A persisted `voice` change: same model, settings swap only; candidate holds it, promote; else build first on the apply worker (warm with "Ready."), swap, then drop the old model. A failed load keeps the old voice and reverts the value. `speed` goes to `generate(text, sid, speed)` per call; `peak` replaces the fixed 0.89 as the normalisation target per call, clipped to the knob bounds.
- `/tts` gains optional `speed` and `peak` (and `voice` accepts an id or an alias for every engine). An unknown voice returns 400 `{"error": "unknown_voice", "effective": "<id>", "near": [...]}` instead of being silently ignored. Every `/tts` response carries `X-Mira-Voice: <resolved id>`. The WAV contract (mono s16 at the model rate, `audio/wav`) is unchanged.
- Restart only for `server.http` binds, port, token file, base dir, `max_stt_s`.

#### 2.9.3 Voice catalog contract (`voices.catalog`)
- Pure scan of `<base_dir>/models` for `vits-piper-*` and `kokoro-*` directories; rescans when the folder mtime changes or every `rescan_s`.
- Piper: read `*.onnx.json` (`audio.sample_rate`, `num_speakers`, `speaker_id_map`, `language.code`). Kokoro: names and count from `speech_plugins/voices/<model_dir>.json`, generated by `mira voices index` from the ONNX metadata (`speaker_names`, `n_speakers`) and committed; a test asserts the name count equals `n_speakers` for every table, using `mira-plugins/fixtures/kokoro_speakers.json` (F7) as the fixture. Names are never hand-typed.
- Voice id: `<model_dir>#<speaker>`, speaker = `0` for single-speaker Piper, the `speaker_id_map` key for multi-speaker Piper (`libritts_r`), the Kokoro name.
- Variants: a dir `X-int8` is a variant of `X` when `X` exists; listings fold it into `X` (`variants: [...]`); the fp32 model is preferred (AVX1 CPU, F-speech map). When only the int8 dir exists (lessac), it is the entry.
- Aliases, case-insensitive, with space, underscore and hyphen treated as equal: Piper single speaker: the name part (`en_US-amy-medium` gives `amy`, `en_GB-jenny_dioco-medium` gives `jenny_dioco`, plus the short alias `jenny` when unique). Kokoro: the name without its two-letter prefix (`af_heart` gives `heart`) and the full name (`af_heart`). Multi-speaker Piper: `libritts:<key>`, listed as a family, not one entry per speaker. Collisions: the first claimant in the order (Piper before Kokoro, newer Kokoro model before older, fp32 before int8) keeps the bare alias; a loser keeps only its qualified alias `<name>@<model_short>` (for example `af_bella@v0_19`). Resolution order: exact id, alias, short alias, qualified alias; else `unknown_voice` with the 3 nearest aliases (difflib).
- Entry: `{id, alias, aliases, label, lang, gender, engine, model_dir, speaker, speakers, variants, usable, reason, loaded, current, featured, size_mb, rtf}`. `lang`: Piper `language.code`; Kokoro from the first letter (a en_US, b en_GB, e es, f fr, h hi, i it, j ja, p pt_BR, z zh). `gender`: Kokoro second letter (f, m); Piper "" (unknown, not guessed). `usable`: required files present and metadata parsed; a dir that fails is listed with `usable: false` and `reason`. `rtf`: measured real-time factor from `rtf_file` (moving average updated after each synthesis), `null` when unknown. `featured` from the knob.
- Listing response: `{current: {id, alias, label}, voices: [entries, current first, then featured, then by lang and alias], families: [{model_dir, speakers, alias_prefix}]}`; `lang=en` keeps en_US and en_GB.

### 2.10 Events (observe-only hooks)
`ctx.on(event, fn)` subscribes; `fn(payload)` runs on the loop thread, must return within about 1 ms (hand longer work to the plugin's own thread), and an exception in it is logged and swallowed. Events fire only at turn boundaries, never per chunk. Payload: `{"event", "at", "turn"}` plus the listed fields; never transcript or reply text.
| event | fired when | extra fields |
|---|---|---|
| wake.hit | spotter hit | agc_gain |
| wake.accepted | transcript gate accepted | |
| wake.rejected | gate rejected | reason: "empty" or "no_wake" |
| turn.heard | a request was accepted | words: count |
| turn.replied | reply text chosen | source: "intent" or "brain" or "fail" |
| turn.spoken | reply finished playing | chunks |
| turn.offline | speech offline path | stage: "stt" or "tts" |
| mic.changed | mute verdict changed | mic |
| setting.changed | a change applied or reverted | change_id, keys, status |
| plugin.state | a plugin changed state | plugin, state |
The facade emits the first eight from its `_mark` calls; the core emits the last two. No wrap mode: behaviour that must change a turn goes into a plugin service the loop already calls.

### 2.11 `mira check` lint (contract gate)
Fails on: a knob without description, unit (numeric), bounds (numeric) or applies; `agent="bounded"` without agent bounds and step inside the hard bounds; a description over 160 chars; plugin code reading `os.environ` or calling `open()` on a literal path (classes named in the module's `LEGACY_CLASSES` are exempt); a bare numeric literal that matches a knob default inside a plugin's service code (allow-list `FIXED:` names); a spec without its test files; two specs with one name; an exclusive service with two default-enabled providers; `deploy/*.example.json` not validating or differing from schema defaults outside the documented deployment values (2.4.5); stale generated files (`docs/PLUGINS.md`, `docs/KNOBS.md`, `docs/contracts/schema.generated.json`); any U+2013 or U+2014 character in `wall/` text files.
