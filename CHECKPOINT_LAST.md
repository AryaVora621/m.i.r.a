# Last Checkpoint

**Date:** 2026-08-19
**Agent:** `/root`
**Mode:** Builder — executed the voice loop plan Tasks 7-14, task by task, each verified against a real component before committing

## Completed

On branch `feat/mira-voice-loop` (10 commits, all pushed to no remote yet — local only):

- **Task 7**: Real `mac.volume.set` guardrail handler (`guardrail/src/volume.rs`) — shells out to `osascript`, records the real previous volume level as rollback evidence, wired into `dispatch()`. 5 unit tests + 1 integration test pass, zero warnings.
- **Task 8**: `VoiceStateEvent` v2 protocol contract across JSON Schema/Pydantic/TS/Rust, fixture-tested both languages.
- **Task 9**: `MIRA_IDENTITY.md` v1 (authored in full) + `voice/identity.py` loader.
- **Task 10**: `voice/memory.py` `ConversationBuffer` — in-RAM rolling 20-turn buffer.
- **Task 11**: `voice/codex_client.py` — shells out to the real `codex` CLI. Manually verified against the real, OAuth-authenticated CLI (got back exactly the requested reply).
- **Task 12**: `voice/loop.py` `VoiceLoop` — the 9-state FSM (barge-in, mute, offline). All 7 tests pass; the barge-in interruption test was run 20x with zero flakes.
- **Task 13**: `voice/daemon_client.py` — real WebSocket client. Verified against the real `mira-daemon`/`mira-guardrail` binaries: register, publish_state, and a real `mac.volume.set` round trip that actually changed system volume (restored to 25 afterward, both times it ran).
- **Task 14**: Real hardware adapters (`voice/adapters/`) — Sherpa-ONNX wake word ("mira"), Silero VAD, faster-whisper STT, Kokoro TTS. Found and fixed 4 real defects the plan's code didn't anticipate:
  - kokoro's PyPI releases pin `numpy==1.26.4` exactly, which has no Python 3.13 wheels, and only `kokoro==0.7.4` has a satisfiable `misaki[en]` dependency (misaki hasn't published past 0.7.4). Fixed by running `voice/` in a dedicated `voice/.venv` (Python 3.12, gitignored) instead of the repo's system Python 3.13.
  - Silero VAD v5+ requires exactly 512-sample (32ms) chunks at 16kHz, not the planned 30ms/480-sample frames — raises `ValueError: Input audio chunk is too short` otherwise. Fixed in `audio_io.py`/`vad.py`.
  - `sherpa_onnx.OnlineStream.accept_waveform` needs float32 samples normalized to [-1, 1], not raw PCM16 bytes — fixed in `wake.py`.
  - Kokoro's `Result.audio` is a `torch.FloatTensor`, not a numpy array — `play_pcm` now converts via `.detach().cpu().numpy()`.
  - Built the real "mira" keyword file (`sherpa-onnx-cli text2token`, uppercase required to match the model's BPE vocab) and replaced the model's shipped demo keywords entirely so they can't false-trigger a wake. `voice/models/` (~6MB) is gitignored; the exact reproduction recipe is in `wake.py`'s module docstring.
  - Manually confirmed: the real `KeywordSpotter` constructs with the mira-only `keywords.txt`, produces no result on silence, and produces no result on the model's own non-mira demo test wavs (a true-negative check).
- Updated `PROJECT_STATUS.md` (new Voice Loop readiness-grid row, updated status line) and `TASK_QUEUE.md` (Tasks 7-14 moved to Done, Task 15 manual verification moved to Open with an explicit checklist pointer).
- Full `voice/` suite (18 tests) passes under `voice/.venv`; the non-ML subset (29 tests across `voice/` + `shared/tests/`, 2 correctly skipped) also passes under the repo's system Python.
- **Task 15 entrypoint + a new browser tester** (`voice/main.py`, `voice/tester/index.html`): a self-contained local HTML page (no build step) that connects directly to `ws://127.0.0.1:8080/ws` as a `LOOPBACK_TRUSTED` node, live-highlights the 9-state FSM diagram from real `VoiceStateEvent` broadcasts, logs every envelope, and has a manual panel to fire a real `mac.volume.set` request without needing the mic/ML pipeline running. Verified live in a real browser against the real daemon+guardrail: a manual volume request round-tripped a correct signed receipt and actually changed system volume (restored after), and a simulated voice-loop client's published state sequence correctly drove the live highlight.

## In Progress

`voice/main.py` entrypoint is written and imports cleanly, but **Task 15's manual end-to-end verification has not been run** — it needs a human physically at this Mac with a real mic and speaker. Checklist: `docs/superpowers/plans/2026-08-19-mira-voice-loop-plan.md` Task 15 Step 2 (say "Mira", confirm listening, say "set the volume to forty", confirm the real volume changes and Mira speaks a confirmation, confirm barge-in works, confirm killing the daemon produces OFFLINE not a crash).

## Next Action

1. User runs the Task 15 manual verification: `cargo run --bin mira-guardrail` (terminal 1), `cargo run --bin mira-daemon` (terminal 2), `voice/.venv/bin/python -m voice.main` (terminal 3). Optionally also open `voice/tester/index.html` (via `python3 -m http.server` in that folder) alongside to watch state transitions live and sanity-check the guardrail independently of the mic pipeline.
2. After manual verification passes (or with any fixes it surfaces), commit Task 15's final state (`voice/main.py` + tracking doc updates, already staged conceptually — not yet committed pending the manual check) and this plan is closed out.
3. Then: Implementation Sequence step 4 (Tauri menu-bar app / projector simulator) per the canonical plan.
4. User confirmed architecture: the P40 homelab desktop (32GB DDR4, i7-6900K, GTX 1050) is where the real LLM inference eventually runs, replacing the Codex dev-route — already the documented plan, no code change needed now. The Pi is display/projector only, not compute. User also wants `voice/`'s design kept portable to an eventual Pi 4B/Ubuntu-server setup — current design already leans that way (cross-platform Python libs; the one OS-specific piece, `osascript` volume control, is isolated behind the guardrail's `VolumeController` Rust trait).

## Human Decisions Needed

None blocking further automated work — the only remaining step in this plan needs the user's physical presence (mic/speaker), not a decision.
