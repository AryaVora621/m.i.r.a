# M.I.R.A. Task Queue

## Open

- [ ] Pi Runtime Deployment: package and install the Rust daemon and guardrail as systemd services, add a Linux audio capability backend, then decide which voice components stay on the Pi versus move to the homelab worker.
- [ ] Task 15 manual end-to-end verification of the voice loop: Run either directly on the Pi hardware or on Mac via `verify_voice_loop.command`. Checklist is in `docs/superpowers/plans/2026-08-19-mira-voice-loop-plan.md` Task 15 Step 2.
- [ ] Wake word "Mira" threshold tuning: `diagnose_wake.command` is ready if needed.
- [ ] Speaker verification: `owner_embeddings.npy` enrolled; verify rejection of non-owner voices.
- [ ] Follow-up-plan tickets deferred from the gateway/guardrail whole-branch review (see `PROJECT_STATUS.md`'s "Deferred From the Gateway/Guardrail Plan" section for the full list).
- [ ] Homelab Tesla P40 GPU local LLM setup & Ollama bridge.

## In-Progress

(none)

## Done

- [x] Pi deployment preflight: confirmed ARM64 Raspberry Pi OS, 106 GB free storage, 3 GB available RAM, Python 3.13.5, working USB-camera microphone and HDMI/headphone outputs, and identified missing Git/Rust toolchains plus the Python 3.12 voice dependency gap.
- [x] Projector Wall Alignment & Optical Centering: physically aligned the projector and confirmed the persisted wall calibration under purple ambient lighting.
- [x] Auto-Homography Calibration ($H$): fixed purple-light ArUco detection, locked 4/4 markers live, and persisted `/home/mira/mira/vision/wall_calibration.json` on the Pi.
- [x] Raspberry Pi OS Lite (64-bit Bookworm) flashed, booted, connected to LAN at `mira@mira.local` / `192.168.1.174`, passwordless SSH keys deployed, and hardware verified (Cortex-A72, USB Camera on `/dev/video0`, HDMI audio).

- [x] Task 1: Shared IPC schemas and typed contracts (`aa8bc51`).
- [x] Re-verified Task 1 on 2026-08-19: 8 unit tests pass.
- [x] Audited the repository and prepared the Phase 1 resume audit.
- [x] Saved the custom voice and Agent OS canonical plan.
- [x] Saved the context-clear start prompt.
- [x] Rebaselined against the canonical plan and wrote the executable protocol-v2/guardrail TDD plan (`docs/superpowers/plans/2026-08-19-mira-protocol-v2-gateway-guardrail-plan.md`, Tasks 2-6).
- [x] Executed Tasks 2-6 via Subagent-Driven Development: protocol v2 contracts (JSON Schema/Pydantic/TS/Rust), Rust workspace (`mira-protocol`/`daemon`/`guardrail` crates), WebSocket gateway + node registry, deny-by-default guardrail policy engine + Ed25519 dev signing, end-to-end capability dispatch over a Unix Domain Socket. Whole-branch review passed. Final commit `28d21e5`. 25/25 Rust tests + 12/12 Python tests + `tsc --noEmit` clean.
- [x] Executed voice loop plan Tasks 7-14 on `feat/mira-voice-loop`: real `mac.volume.set` guardrail handler (`osascript`), `VoiceStateEvent` v2 protocol contract, `MIRA_IDENTITY.md` v1 + loader, in-session `ConversationBuffer`, `CodexClient` dev-route, 9-state `VoiceLoop` FSM, real `DaemonClient`, and real Sherpa-ONNX/Silero-VAD/faster-whisper/Kokoro adapters. 20/20 tests passing.
- [x] Created Raspberry Pi Lite 64-bit bringup plan with lightweight `cage` kiosk module ([`2026-08-21-raspberry-pi-projector-node-bringup.md`](file:///Users/aryavora/desktop/personal%20projects/mira/docs/superpowers/plans/2026-08-21-raspberry-pi-projector-node-bringup.md)).
