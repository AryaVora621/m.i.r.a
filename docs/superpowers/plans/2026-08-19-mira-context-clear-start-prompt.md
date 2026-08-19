# M.I.R.A. Context-Clear Start Prompt

Paste this after a context clear:

```text
We are continuing work in /Users/aryavora/Desktop/Personal Projects/mira, on branch
feat/mira-voice-loop (not merged to main yet).

Read AGENTS.md, then run ls. Read TASK_QUEUE.md and CHECKPOINT_LAST.md (and BLOCKED.md if
present). The canonical approved plan is:
docs/superpowers/plans/2026-08-19-mira-custom-voice-agent-os-plan.md

Current state (as of the last session, 2026-08-19/20):
- Implementation Sequence steps 1-2 are done and merged to main: protocol v2 contracts, the Rust
  daemon, and the guardrail, wired end to end over a Unix Domain Socket. main is ahead of
  origin/main and not pushed (push only if explicitly asked).
- Implementation Sequence step 3 (the Mac voice loop, Tasks 7-15 of
  docs/superpowers/plans/2026-08-19-mira-voice-loop-plan.md) is implemented and committed on
  feat/mira-voice-loop (10 commits), task by task, each verified against something real rather
  than just its own mocks:
  - Task 7: real mac.volume.set guardrail handler (osascript, real rollback evidence).
  - Task 8: VoiceStateEvent v2 protocol contract (JSON Schema/Pydantic/TS/Rust).
  - Tasks 9-11: MIRA_IDENTITY.md v1 + loader, in-session ConversationBuffer, CodexClient
    (verified against the real, OAuth-authenticated codex CLI).
  - Task 12: the 9-state VoiceLoop FSM (barge-in, mute, offline) — barge-in stress-tested 20x
    with zero flakes.
  - Task 13: real DaemonClient — verified against the real mira-daemon/mira-guardrail binaries,
    including a capability round trip that actually changed system volume.
  - Task 14: real Sherpa-ONNX/Silero/faster-whisper/Kokoro hardware adapters, in a dedicated
    voice/.venv (Python 3.12 — kokoro's PyPI releases don't support the repo's system Python
    3.13). Four real bugs were found and fixed while getting this working for real (Silero's
    fixed 512-sample chunk requirement, accept_waveform needing normalized float32 not raw
    bytes, Kokoro returning a torch tensor not a numpy array, the Python-version/dependency
    conflict) — see CHECKPOINT_LAST.md for details.
  - Task 15: voice/main.py entrypoint (imports cleanly), PROJECT_STATUS.md/TASK_QUEUE.md
    updated. Also built voice/tester/index.html — a self-contained local webpage (no build
    step) that connects directly to the real daemon over WebSocket, live-visualizes the voice
    FSM, and has a manual mac.volume.set test panel; verified live in a real browser session.
  - 18/18 voice/ tests pass under voice/.venv; the non-ML subset (29 tests, 2 correctly
    skipped) also passes under system Python. cargo test --workspace and tsc --noEmit both
    clean.
- **Only thing not done**: Task 15's manual end-to-end mic/speaker verification (say "Mira", set
  the volume by voice, confirm barge-in, confirm OFFLINE on daemon death) — needs a human
  physically at this Mac with a real mic/speaker, not something an agent or CI can do. Checklist:
  docs/superpowers/plans/2026-08-19-mira-voice-loop-plan.md Task 15 Step 2.

Important decisions already locked:
- Build M.I.R.A. fully custom. Hermes and OpenClaw are reference-only.
- Hardware role split (confirmed by user 2026-08-19): this Mac runs the voice loop locally for
  latency (wake word/VAD/STT/TTS can't round-trip to a remote box); the homelab P40 desktop
  (Tesla P40, 32GB DDR4, i7-6900K, GTX 1050) is the intended target for real LLM inference,
  replacing the current Codex dev-route — already the documented plan, not a pending decision.
  The Raspberry Pi is projector/display only, not compute.
- User wants voice/'s design kept portable to an eventual Pi 4B/Ubuntu-server deployment for
  "the final project." Current design already leans that way (voice/ is pure Python on
  cross-platform libs; the one OS-specific piece, osascript volume control, is isolated behind
  the guardrail's VolumeController Rust trait, not inside voice/ itself). Keep this in mind for
  any new architecture choices in voice/, but it's not urgent enough to block on right now.
- The guardrail binary, signing keys, and irreversible-action policy are untouchable by agents.
  Only reversible actions with a tested rollback may run autonomously.
- Exact P40 model targets: Qwen3.8-27B and NVIDIA Nemotron 3.5 Lightning 30B-A3B. Do not
  substitute other models. Audit and reuse the existing Ubuntu/Ollama box first — do not wipe
  without evidence and explicit approval.

Next concrete action: the user runs Task 15's manual verification themselves (three terminals —
mira-guardrail, mira-daemon, `voice/.venv/bin/python -m voice.main`; optionally also open
voice/tester/index.html to watch state live). Once that passes (or surfaces fixes), commit
whatever it needs and this plan is closed out. After that: Implementation Sequence step 4 (Tauri
menu-bar app / projector simulator) per the canonical plan.

Start with a read-only current-state inventory (git status, git log --oneline -5, git branch
--show-current) to confirm nothing has drifted since this prompt was written before doing
anything else.
```
