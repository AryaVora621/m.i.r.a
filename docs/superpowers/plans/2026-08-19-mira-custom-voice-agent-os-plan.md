# M.I.R.A. Custom Voice and Autonomous Agent OS Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task by task. Preserve the completed shared-schema commit `aa8bc51`.

**Goal:** Build a fully custom M.I.R.A. voice-first assistant and autonomous Agent OS that runs on the MacBook first, then moves to the Ubuntu Tesla P40, Raspberry Pi projector, and ESP32 desk controller.

**Architecture:** M.I.R.A. owns the gateway, identity, memory, UI, capabilities, and release policy. A Rust gateway and separately deployed immutable guardrail sit below a Python LangGraph runtime, which coordinates local models, Codex, Claude Code, memory, and isolated workers. Hermes and OpenClaw are reference-only.

**Tech Stack:** Rust gateway, Python LangGraph plus PostgreSQL/pgvector, React/TypeScript PWA, Tauri 2 Mac wrapper, Sherpa-ONNX wake detection, Whisper-family STT, Kokoro/Piper TTS, Docker Compose plus systemd, Tailscale plus mTLS, llama.cpp-compatible model serving.

**Spec:** This plan supersedes unsafe or outdated assumptions in the older SSH/admin, Jarvis, Hermes-runtime, and static voice specifications. Preserve those documents for history, but do not implement them as written.

## Global Constraints

- Continue on `feat/macbook-offline-core-sim` and preserve all existing uncommitted work.
- No raw SSH command bridge, `sshpass`, unrestricted administrator access, or arbitrary model-generated JavaScript.
- Agents may self-edit personality, orchestration, and operational code only through the isolated verifier and release pipeline.
- The guardrail binary, signing keys, audit integrity, rollback enforcement, and irreversible-action policy are untouchable by agents.
- Classify actions as `reversible`, `irreversible`, or `unknown`; only reversible actions with tested rollback may run autonomously.
- Keep the P40 installation by default. Wipe only after a read-only audit finds an unrecoverable issue and the user approves.
- Exact model targets are `Qwen3.8-27B` and `NVIDIA Nemotron 3.5 Lightning 30B-A3B`. Do not silently substitute other model families.

## Architecture and Interfaces

### Gateway and guardrail

- Extend the shared contracts to `MiraEnvelopeV2` with protocol version, event ID, trace ID, topic, event kind, sender node, timestamp, authentication context, payload, and acknowledgement reference.
- Add typed `CapabilityRequest`, `ActionManifest`, and `CapabilityReceipt` contracts containing arguments, reversibility, rollback evidence, credential scope, and result.
- Implement Rust node registry, WebSocket event broker, mTLS identity, capability dispatch, audit receipts, and simulator support.
- Run the guardrail as a separately deployed, root-owned signed binary. It denies destructive deletion without recovery, secret disclosure, external publication or messaging, financial transactions, destructive credential changes, guardrail modification, and deployment without rollback.

### Voice, personality, memory, and UI

- Implement voice states `IDLE`, `WAKE_DETECTED`, `LISTENING`, `TRANSCRIBING`, `THINKING`, `SPEAKING`, `INTERRUPTED`, `MUTED`, and `OFFLINE`.
- Use Sherpa-ONNX open-vocabulary keyword spotting for “Mira”, with Porcupine as a benchmark fallback. Use a pre-wake in-memory ring buffer that is never persisted.
- Use local VAD, Whisper-family STT, Kokoro TTS, and Piper fallback. Support barge-in and output cancellation.
- Store the complete identity in versioned `MIRA_IDENTITY.md`. Personality edits are release-managed and reversible.
- Store permanent post-wake audio, images, transcripts, actions, traces, summaries, and corrections in PostgreSQL/pgvector plus a content-addressed media store.
- Encrypt the live archive and send full encrypted deduplicated snapshots to Google Drive. Keep the Pi as a bounded encrypted cache and recovery bundle, not the archive.
- Build one React/TypeScript PWA. Run it in a Tauri 2 Mac menu-bar app, Mac projector simulator, Pi Chromium kiosk, and private mobile PWA. Projector scenes use true black `#000000`.
- Routine UI is declarative `SceneSpec` JSON. New components go through the Agent OS pipeline and are never executed directly from model output.

### Intelligence and Agent OS

- Use the open-source LangGraph library with PostgreSQL checkpointing for durable workflows. Do not depend on the LangGraph Agent Server.
- Use local planning and supervision, Codex or Claude Code implementation, the other model family for independent review, and a clean deterministic verifier.
- Workflow: `intake → classify → snapshot → plan → sandbox → implement → independent review → deterministic gates → score → integration branch → clean-room gate → default branch → canary → promote or rollback`.
- Use Docker workers with denied-by-default networking, scoped temporary credentials, resource limits, isolated worktrees, and no guardrail mounts.
- Score releases as requirements 30, deterministic correctness 30, security 20, maintainability 10, deployment/rollback 10. Require `>=90/100`; deterministic gates pass 5/5, nondeterministic scenarios may pass 4/5.
- Any required-test failure, high or critical security finding, secret exposure, guardrail edit, unsigned evidence, unresolved merge, or failed rollback blocks release.

### P40 and model serving

- Audit Ubuntu, disks, encryption, GPU drivers, CUDA, Docker, Ollama, networking, and existing model data before changing anything.
- Use Docker Compose plus systemd for gateway, Agent OS, memory, database, backup, and model services.
- P40 deployment must use Pascal-compatible GGUF quantization through a verified `llama.cpp` build. NVIDIA BF16, FP8, and NVFP4 profiles are not accepted.
- Keep one general model hot and swap the second model only for substantial background jobs, then restore the primary. Benchmark both models on conversation, tools, planning, memory, coding supervision, VRAM, latency, and stability.
- Use Codex OAuth for the first real Mac answers because the Mac Ollama registry is currently empty. This is a development route, not a replacement for the exact P40 models.
- Keep Nous Portal optional and metered. Probe capabilities with minimal requests, then leave it disabled by default. Local STT/TTS remains primary.

## Implementation Sequence

1. Rebaseline project status, update the task queue, and create focused executable plans for core security, voice, memory, UI, Agent OS, P40 deployment, and hardware nodes.
2. Extend shared schemas and cross-language fixtures, then implement the guardrail and Rust gateway with simulated nodes.
3. Build the Mac voice loop: “Mira” wake, VAD, local STT, Codex turn, local TTS, interruption, identity, memory, and one safe typed Mac volume action.
4. Build the Tauri menu-bar app, voice-state overlay, projector simulator, and declarative scene pipeline.
5. Complete the Agent OS pilot against the MIRA repository, including snapshotting dirty state, cross-model review, scoring, signed evidence, integration promotion, canary deploy, and injected-failure rollback.
6. Audit and converge the P40 Ubuntu host, install the production Compose stack, verify exact model artifacts and quantization, and move model routing to the P40.
7. Deploy the same PWA to the Pi projector, then add homography calibration, gesture simulation, ESP32 controller bring-up, physical-click camera capture, and mobile Tailscale access.

## Verification and Acceptance

- Voice wakes locally, does not persist pre-wake audio, transcribes, answers, speaks, and supports barge-in.
- Camera capture requires the physical shutter and transmission state is visible on the controller.
- Memory survives restart, supports source drill-down and corrections, backs up encrypted data, and restores on a clean host.
- Security tests reject forged certificates, replayed events, arbitrary shell commands, secret exfiltration, network-policy bypass, guardrail edits, unsigned evidence, and unknown reversibility.
- Agent OS resumes after restart, handles dirty repositories, reviewer disagreement, failed tests, merge conflicts, health failures, canary success, and rollback.
- UI tests cover menu-bar state, overlay, mobile view, projector simulator, true-black rendering, offline recovery, malformed `SceneSpec`, and rejection of executable model output.
- Exact Qwen3.8-27B and Nemotron 3.5 Lightning artifacts load without OOM at the approved context, preserve tool-call syntax, pass the model benchmark, and survive repeated model swaps.
- Mac milestone is complete only when a spoken request produces a Codex-powered spoken answer, a typed Mac capability succeeds and rolls back, memory survives restart, projector state updates, and one autonomous repository change passes the full 90-point release pipeline.

## Defaults

- Hermes and OpenClaw are reference-only. Reused code requires compatible licensing, provenance, attribution, tests, and adaptation behind M.I.R.A.-owned interfaces.
- Sensitive local voice actions use owner speaker verification with a passkey fallback. Low-risk informational commands may operate after wake.
- All post-wake interaction data is permanent unless the user invokes explicit cryptographic erasure.
- Google Drive receives the full encrypted archive. The Pi cache is capped at 96 GB.
- Tailscale plus local mTLS is the only remote path. No public Vercel deployment is used initially.

