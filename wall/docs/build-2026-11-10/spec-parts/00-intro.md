# Mira plugin harness: final spec (v1, 2026-11-10)

Companion files: `spec-parts/*.md` hold the same text split by section (00 intro and decisions, 10 plugin and services, 20 core, 30 knobs, 31 example files, 40 writes and policy, 50 API, CLI, speech, events, lint, 60 Hermes, 65 agent contract, 70 layout and facade, 80 migration, tests, deployment, 90 risks, owner steps, packages, questions, traceability); read the parts when this file is too long for one read. SPEC.md is the concatenation and wins on any difference.

Single source of truth for turning Mira (the wall voice: listener on the Pi, speech server on the Mac mini, Hermes as the brain) into a set of small plugins with one clean tuning path, and for teaching Mira to tune herself by voice.

Base design: Design A (services are the original objects, JSON config, safe-point apply, golden parity), with grafts from B (service Protocols, launcher with engine switch and crash-loop fallback, 24 h shadow, measure-before-deciding, `announce` text, tuning guide, quiet hours, exit codes) and C (ARMED-only rebuilds, kill switch, session undo, glossary, effect hints, per-call step limit, contract test suite, `doctor`). Section 13 maps every judge "must contain" item to the section that answers it.

Repos and machines:
- mira repo: `/Users/aryavora/Desktop/Personal Projects/mira`. All mira paths below are relative to its `wall/` folder unless they start with `mira:` (repo root) or `agentic-os:`.
- agentic-os repo: `/Users/aryavora/Desktop/Personal Projects/agentic-os`. Paths prefixed `agentic-os:`.
- Pi (listener, user `mira`, uid 1000): `ssh -o ConnectTimeout=8 -o HostName=192.168.1.179 mira '<cmd>'`, root via `sudo -n`, Hermes runs as user `agent`.
- Mac mini (speech server, user `aryavora`): `ssh -o BatchMode=yes aryavora@192.168.1.174 '<cmd>'`. Never touch its gateway, vault sync or Remote Control.
- Test interpreter on this Mac (Python 3.13.13, sherpa-onnx 1.12.40, numpy 2.5.4, pytest 9.2.0): `PY=/private/tmp/claude-501/-Users-aryavora-Desktop-Personal-Projects-agentic-os/a699da48-f6d2-4aec-8907-3db40e3fb372/scratchpad/mira-plugins/venv/bin/python`. If it is missing: `uv venv --python 3.13 "$VENV" && uv pip install --python "$VENV/bin/python" sherpa-onnx==1.12.40 numpy pytest`. Python 3.15 cannot install sherpa-onnx 1.12.40; never use the default `python3`.

## 0. Decisions

### 0.1 Verified facts (2026-11-10; these override anything the designs assumed)

| # | Fact | How it was checked |
|---|---|---|
| F1 | Repo `wall/mira_listen.py` and the Pi's `/home/mira/mira-listen/mira_listen.py` have the same md5 `8990842340a5ce68aa11515d0432f4ed`. Repo file is 970 lines. | `md5` on the Mac, `sudo -n md5sum` on the Pi |
| F2 | Pi: Python 3.13.5 system and in `~/mira-listen/venv`; 4 cores; about 1.1 GB available memory. The live unit uses about 27 MB (`MemoryCurrent=26886144`), `NRestarts=0`, and about 18 percent of one core averaged since 06:47 (3385 s CPU in about 5.2 h). | `systemctl --user show mira-listen` |
| F3 | Port 127.0.0.1:8770 is already in use on the Pi. 8650 is free. The control API uses 8650; the install step re-checks it is free. | `ss -ltn` |
| F4 | sherpa-onnx 1.12.40 `KeywordSpotter.create_stream(keywords: Optional[str])` exists, but a per-stream `#threshold` had no effect in any of four syntaxes (`▁MI RA #0.05`, with `:1.0`, with `@MIRA`, combined). Wake threshold and score need a spotter rebuild. A spotter build took 0.50 s on this Mac. | probe `mira-plugins/probe/kws_stream_threshold.py`, `kws_stream2.py` |
| F5 | A built `VoiceActivityDetector` keeps its own copy of the config; mutating the `VadModelConfig` afterwards does not change it, and a detector built from the mutated config uses the new value. A detector build took 5 ms. Today's `_reset_vad` builds a fresh detector per capture, so VAD knobs apply at the next capture with no rebuild path. | same probe |
| F6 | The four existing test files give `65 passed, 8 skipped` on this Mac (`tests/test_unit.py tests/test_agc_reporter.py tests/test_e2e.py tests/stt_eval_test.py`). The 8 skips are e2e tests that need the mini speech server and token. | pytest run with `$PY` |
| F7 | Kokoro ONNX metadata carries `speaker_names` and `n_speakers`: `kokoro-int8-multi-lang-v1_0` has 54 (first 28 equal the old hard-coded list, then `ef_dora` ... `em_santa`), `kokoro-en-v0_19` and `kokoro-int8-en-v0_19` have 11 (`af` ... `bm_lewis`). Full lists are saved in `mira-plugins/fixtures/kokoro_speakers.json`. Piper `libritts_r` has 904 speakers with a `speaker_id_map` keyed by numeric strings. | read-only scan on the mini |
| F8 | `has_wake("Let's tune your voice model now, Mira.")` is False; `has_wake("Mira, let's tune your voice model now")` is True. The gate is start-anchored, so the owner's example phrase only works as a follow-up or with "Mira" first. Changing that is outside the freeze list (open question Q1). | `$PY` against the repo module |
| F9 | `stt_eval_test.py` patches `server.MODELS` on the module and re-imports `server` under changed env vars; `bench_*.py` and `stt_eval.py` call `server.Stt(kind, prompt=, hotwords=, compute_type=, beam_size=)`, `server.Tts(server.TTS_DIR, server.TTS_VOICE)`, `server.DEFAULT_VOCAB`, `server.MODELS`. | read the files |
| F10 | `room_eval.EvalListener` replaces `self.kws` after `__init__` and overrides `_mark`, `capture`, `handle_clip`; it reads `followup_until`, `self.speech`, and edits `src.pos`. `test_e2e.py` reads `lis.events`. | read the files |
| F11 | Hermes `config.yaml` already has MCP entries `mail` (untrusted), `vault`, `owner-tasks`, `printers`, `wall`. MCP servers are configured per profile, not per platform, so assume a `mira` MCP is visible to every Hermes session (api_server, chat gateways, cron). | `grep` of the config on the Pi |
| F12 | The house draft persona `scratchpad/house/MIRA_WALL_IDENTITY.md` is 2827 bytes (live file 1743 bytes) and says she never changes settings by voice. The knowledge package must revise that sentence. | `wc -c` |
| F13 | A Python run with `-I` during this survey wrote `wall/__pycache__/mira_listen.cpython-313.pyc` (git-ignored). Reported to the orchestrator; every command in this spec uses `-B` and `PYTHONDONTWRITEBYTECODE=1`. | `ls -la` |

### 0.2 Decisions table (judge conflicts resolved once)

| Topic | Decision | Why |
|---|---|---|
| Config format | JSON, one file per machine, written only by `mirakit` (atomic replace under `flock`). Editing path is `mira set`; hand edits go through `mira config edit` or `mira config reload`. No file polling. | stdlib reads and writes JSON; `tomllib` is read-only; a polled hand-edit path is extra moving parts |
| What a service is | The original object (`MicAGC`, `KeywordSpotter`, `VoiceReporter`, `Speech`, `Hermes`, `Music`, ...). No wrapper on the per-chunk path. | parity by construction (A) |
| The turn loop | A core slot `loop.turns`: inventoried, its numbers are knobs, its code stays in `mira_listen.py`, never swapped or reloaded. | the loop is the contract (judge 3) |
| Threading | Control thread validates, persists, logs and enqueues only. The loop thread applies. One documented exception: an inline preview during the brain phase (section 2.6). | judge 1 item 1 |
| Rebuilds on the Pi | Only at the top-of-loop safe point while ARMED (not in the follow-up window, not capturing, not handling), at least `rebuild_quiet_s` after the last wake or turn end, and never while a voice session is open. After a rebuild: `source.flush()`, new spotter stream, pre-roll cleared. | judge 1 items 2, 3 |
| Agents and rebuild knobs | Agents (Hermes, voice) may not set any Pi knob of class `rebuild` or `restart` (wake threshold, score, keywords, AGC on/off, models, threads). | deaf window and memory (judge 2 item 6) |
| Propose path | No new queue. A refused owner-only change returns `owner_command`; the skill files it with the existing `owner-tasks` MCP `assign_task`. | judge 2 wants a path, judge 3 wants no new concept |
| Dialogue length | A voice session overlays `loop.turns.followup_s` with `control.http.session_followup_s` (15 s) while open. No hold-open call. | judge 1 item 8, judge 2 item 2 |
| Spoken safety | Server-side: agent-scope writes and previews are accepted only while the listener is inside the brain phase of a voice turn (its own loop state), unless the owner turns the rule off. Nothing routed through the model. | judge 2 item 3; F11 |
| Agent access classes | Two: `deny` (default) and `bounded` (inside agent bounds, at most `agent_step` per call, always a trial). | judge 3 item 8 |
| Trials | Every agent write is a trial (default 600 s, max 1800 s). Owner writes are permanent unless `--trial`. Trial expiry of a change that belongs to an open session waits until the session ends. | A plus judge 2 |
| Mini trials | The mini keeps its own change log and trial timer (same `mirakit` code). The Pi forwards keep and revert for session changes and passes a trial long enough to outlive the session. | judge 1 item 9 |
| Tokens | Pi: owner token and agent token (files). Mini: data-plane `speech.token` (unchanged), control owner token, control Pi token. Hermes holds only the Pi agent token. | judge 2 item 12 |
| Error policy | Uncaught exception ends the process, as today. No isolate mode in v1. | freeze list |
| Code rollback | `releases/<sha>` plus a `current` symlink on the Pi; the original `~/mira-listen/mira_listen.py` is never overwritten and stays the last-resort fallback. The mini install stays flat with a snapshot tarball per deploy. | judges 1 and 3 |
| Engine rollback | `core.engine` = `legacy` or `plugins`, read by the launcher at the top of `mira_listen.py`; `mira rollback` flips it. Crash-loop fallback by unplanned starts in a window. A legacy mirror (`listen.env`, `legacy.args`) is regenerated after every applied change, so every rollback keeps tuned values legacy can represent and prints the rest. | judge 1 item 4 |
| Persona reload | `persona.file.reread` default `start` (parity). The owner may set `turn` so persona edits need no restart (Q3). | parity first |
| Events | Bounded `Listener.events` through `loop.turns.events_max`; schema default 0 (unbounded, parity), the Pi file sets 2000. | judge 1 item 12 |
| Hooks | About ten observe-only events at turn boundaries, payload without text. No wrap mode. | judge 3 item 5 |

## 1. Scope, freeze list and what is not a plugin

### 1.1 Behaviour frozen in this refactor
Any change to these is a separate, later, owner-approved step:
1. Chunk size `CHUNK=1600` at `RATE=16000` (100 ms) and every count of 100 ms chunks (AGC release and decay are per chunk).
2. Stage order on the audio path: raw chunk to the mute detector, then the AGC, then the wall level, then spotter or VAD and the clip.
3. Wake clip construction: VAD seed `before[-3:]`, STT clip `before[:-3] + seed + captured`.
4. The transcript gate: `WAKE_*` regexes as defaults, verify on every wake clip, an empty transcript is a reject, a reject opens no follow-up window, hallucination set.
5. Follow-up semantics: window opens after a spoken reply and the flush, or after an empty transcript; spotter not fed in the window; seed `pre[-5:]`; wait 1.0 s; `verify=False`; leading wake word still stripped; new spotter stream after a follow-up capture.
6. Reporter contract: payload keys, dedupe on `(state, heard, reply)`, coalescing, 0.5 s state gap, level posts only in listening or speaking, `error` hold, `set_mic` as its own item.
7. Error-then-speak ordering on a Hermes failure; `HTTPError` counted as speech offline (chime); flush only on the success path.
8. An uncaught exception ends the process (systemd restarts it in 3 s).
9. No SIGTERM handler by default (`loop.turns.sigterm_idle=false`).
10. Secrets read from files on every call; tokens never cached.
11. Code defaults (`kws_threshold` 0.15, music URL loopback); deployment values (0.05, mini music URL) live in the machine file, never in the schema.

### 1.2 Not a plugin (core, stated once)
- Protocol constants, never knobs: `RATE=16000`, `CHUNK=1600`, 16-bit mono WAV to `/stt`, Silero `window_size=512`, KWS `provider="cpu"`, the wall hook payload keys, the `mira-audio` commands (`duck`, `unduck`, `say FILE`), the speech server routes and their 16 kHz contract, the clean-for-speech regexes.
- The turn loop in `mira_listen.py` (`Listener.run`, `capture`, `handle_clip`, `say`, `fail_offline`, `_read`): its numbers are `loop.turns` knobs, its logic is code. It is the declared core slot `loop.turns`, shown in the inventory with `core: true`, cannot be disabled, swapped or reloaded.
- The `mirakit` core: store, change log, policy, registry, safe point, control server, CLI.
- The launcher at the top of `mira_listen.py` and `speech/server.py`.

### 1.3 Plugins (every other part)
Pi (`machine="listener"`): `source.pipewire`, `source.wav`, `mute.zero`, `agc.mic`, `wake.kws`, `vad.silero`, `loop.turns` (core slot), `speech.link`, `stt.remote`, `tts.remote`, `gate.wake`, `intent.music`, `brain.hermes`, `persona.file`, `playback.miraaudio`, `reporter.agentos`, `control.http`.
Mini (`machine="speech"`): `server.http`, `stt.engine`, `stt.faster_whisper`, `stt.sherpa_whisper`, `stt.sherpa_moonshine`, `stt.sherpa_moonshine_v2`, `stt.sherpa_transducer`, `tts.engine`, `tts.piper`, `tts.kokoro`, `voices.catalog`, `control.http`.
Plugin names are `kind.variant`, unique across both machines, and are also the config key. A rename needs a migration entry (section 2.3.6).
