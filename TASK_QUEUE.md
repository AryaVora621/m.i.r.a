# M.I.R.A. Task Queue

## Open

- [ ] Task 15 manual end-to-end verification of the voice loop (needs a human physically present with a real mic/speaker — not automatable): say "Mira", confirm listening starts, say "set the volume to forty", confirm the real system volume changes and Mira speaks a confirmation, confirm barge-in interrupts mid-reply, confirm killing the daemon produces OFFLINE rather than a crash. Checklist is in `docs/superpowers/plans/2026-08-19-mira-voice-loop-plan.md` Task 15 Step 2. Run via `voice/.venv/bin/python -m voice.main` (the real ML adapters only import in `voice/.venv`, Python 3.12 — see `voice/requirements.txt`), with `mira-guardrail` and `mira-daemon` already running.
- [ ] Follow-up-plan tickets deferred from the gateway/guardrail whole-branch review (see `PROJECT_STATUS.md`'s "Deferred From the Gateway/Guardrail Plan" section for the full list): sender_node identity spoofing on the broadcast path, non-canonical/Rust-only-verifiable receipt signing, Rust protocol mirror laxity (no deny_unknown_fields, unvalidated protocol_version), CapabilityReceipt missing an authorized-vs-executed distinction, guardrail accept()-panics-the-process, unbounded capability-dispatch mpsc, signing-key file permissions/path, dropped trace_id on receipt/error envelopes, unverified MSRV 1.75 claim. Fold into whichever plan covers LAN/production hardening (P40/mTLS plan) rather than a standalone cleanup pass, since several depend on that plan's trust-boundary decisions anyway.
- [ ] Deferred from the voice loop plan itself (see that plan's Global Constraints for rationale): Porcupine wake-word fallback, Piper TTS fallback, persistent cross-restart memory (own subplan below), automated rollback triggering (`ROLLED_BACK` receipt status) — Agent OS release-pipeline scope.
- [ ] Define focused subplans for memory, UI, Agent OS, P40 deployment, and hardware nodes. Keep `voice/` and the guardrail capability-handler split (cross-platform Python adapters vs. one OS-specific Rust handler behind a trait) in mind when scoping the Pi 4B/Ubuntu-server port — it's the intended transferability path, not a new abstraction to build.
- [ ] Audit Ubuntu/P40/Ollama before any removal or migration.

## In-Progress

(none)

## Done

- [x] Task 1: Shared IPC schemas and typed contracts (`aa8bc51`).
- [x] Re-verified Task 1 on 2026-08-19: 8 unit tests pass.
- [x] Audited the repository and prepared the Phase 1 resume audit.
- [x] Saved the custom voice and Agent OS canonical plan.
- [x] Saved the context-clear start prompt.
- [x] Rebaselined against the canonical plan and wrote the executable protocol-v2/guardrail TDD plan (`docs/superpowers/plans/2026-08-19-mira-protocol-v2-gateway-guardrail-plan.md`, Tasks 2-6).
- [x] Executed Tasks 2-6 via Subagent-Driven Development: protocol v2 contracts (JSON Schema/Pydantic/TS/Rust), Rust workspace (`mira-protocol`/`daemon`/`guardrail` crates), WebSocket gateway + node registry, deny-by-default guardrail policy engine + Ed25519 dev signing, end-to-end capability dispatch over a Unix Domain Socket. Whole-branch review passed (1 fix round + 1 closure fix, both re-reviewed clean). Final commit `28d21e5`. 25/25 Rust tests + 12/12 Python tests + `tsc --noEmit` clean.
- [x] Executed voice loop plan Tasks 7-14 on `feat/mira-voice-loop`: real `mac.volume.set` guardrail handler (`osascript`, real rollback evidence), `VoiceStateEvent` v2 protocol contract, `MIRA_IDENTITY.md` v1 + loader, in-session `ConversationBuffer`, `CodexClient` dev-route, the 9-state `VoiceLoop` FSM (barge-in/mute/offline), real `DaemonClient` WebSocket client, and real Sherpa-ONNX/Silero-VAD/faster-whisper/Kokoro adapters. Each task verified against something real, not just its own unit tests: real `codex` CLI round trip, a real daemon+guardrail capability dispatch that actually changed system volume (restored afterward), a real `KeywordSpotter` built against the "mira" keyword file. Found and fixed 4 real defects the plan's code didn't anticipate: kokoro's PyPI releases are incompatible with Python 3.13 (dedicated `voice/.venv` on Python 3.12 instead), Silero VAD v5+ needs exactly 512-sample frames not 480, `accept_waveform` needs normalized float32 not raw PCM16 bytes, and Kokoro's `Result.audio` is a torch tensor not a numpy array. 18/18 `voice/` tests pass. Task 15's manual mic/speaker verification is still open (see `## Open`).
