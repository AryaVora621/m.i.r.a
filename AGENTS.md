# AI Agent Guidelines & Context (`AGENTS.md`)

> 🛑 **START HERE — RESUMING ACTIVE WORK (MacBook-Only Offline Development)**  
> - **Active Branch**: `main` (the `feat/macbook-offline-core-sim` branch that built the protocol-v2/gateway/guardrail plan was merged in and deleted — see below)
> - **Canonical Implementation Plan**: [`docs/superpowers/plans/2026-08-19-mira-custom-voice-agent-os-plan.md`](./docs/superpowers/plans/2026-08-19-mira-custom-voice-agent-os-plan.md)
> - **Just-Completed Plan**: [`docs/superpowers/plans/2026-08-19-mira-protocol-v2-gateway-guardrail-plan.md`](./docs/superpowers/plans/2026-08-19-mira-protocol-v2-gateway-guardrail-plan.md) (Tasks 2-6, done and merged to `main` — its `.superpowers/sdd/` workspace ledger was deleted per the finishing-a-development-branch cleanup step; the plan doc itself and `PROJECT_STATUS.md`/`CHECKPOINT_LAST.md` are the durable record now)
> - **Current Status**: Tasks 1-6 **COMPLETE** and merged to `main` (`commit 89bcefd`). Protocol v2 contracts, the Rust daemon (WebSocket gateway + node registry), and the guardrail (deny-by-default policy engine + Ed25519 dev signing) are built and wired together end-to-end. Whole-branch review passed (1 fix round + 1 closure fix, both re-reviewed clean). See `PROJECT_STATUS.md`'s "Deferred From the Gateway/Guardrail Plan" section for Important findings intentionally deferred to a future LAN/production-hardening plan rather than fixed here.
> - **Next Task to Execute**: Define and execute the voice loop subplan (Implementation Sequence step 3 of the canonical plan) — local "Mira" wake word, local STT/TTS, Codex OAuth answers. It depends on nothing the just-completed plan doesn't already produce (the gateway and guardrail now exist for it to plug into).
> - **Execution Method**: Follow Subagent-Driven Development (SDD) or Executing Plans. Work task-by-task with TDD, verify with unit tests, and update that plan's own `.superpowers/sdd/<plan-name>/progress.md` ledger (each plan gets its own workspace directory — see `superpowers:subagent-driven-development`).
> - **Hardware Simulation Notice**: The user is currently developing exclusively on a MacBook without physical hardware access. All missing hardware (Homelab Tesla P40 GPU, ESP32 Wand, Moonraker 3D Printers, Raspberry Pi Wall Projector) must use their respective mock simulators in `nodes/simulator/` and native MacBook hardware (FaceTime HD camera, mic).

---

## 🤖 Project Overview: Dedicated Personal M.I.R.A. System

This folder (`/mira`) is the workspace for building a **bespoke, modular, multi-node personal AI assistant ("M.I.R.A. — My Interactive Room Assistant")**.

Unlike stock chat UIs (e.g. OpenWebUI, which sits archived in `./open-webui`), M.I.R.A. is designed as an ambient, high-performance, real-world integrated system capable of controlling devices across multiple nodes (macOS PC, Mobile, Raspberry Pi + Projector, Homelab cluster) with real-time gesture, voice, and contextual intelligence.

---

## 🏗️ System Architecture & Polyglot Stack

M.I.R.A. is structured as a **polyglot microservices monorepo**:

| Subsystem | Technology Stack | Responsibilities |
| :--- | :--- | :--- |
| **Core Daemon** | Rust / Go | High-performance background service, event bus, IPC/WebSocket router, node registry, system hook execution, low-latency state. |
| **Web Interface** | TypeScript / React | Ambient dashboard, interactive controls, wall projection overlay, gesture cursors, settings management. |
| **Vision & Perception** | Python (MediaPipe, OpenCV) | Webcam feed ingestion, wall gesture recognition, overhead desk vision. |
| **Homelab GPU Server** | Tesla P40 (24GB VRAM) + verified llama.cpp/Ollama-compatible endpoint | Exact target models `Qwen3.8-27B` and `NVIDIA Nemotron 3.5 Lightning 30B-A3B`, selected by benchmark and Pascal-compatible quantization. |
| **Homelab Worker Fleet** | Laptops (i3/i5, 8GB RAM each) | Offloaded microservices for STT (`faster-whisper`), TTS (`Kokoro-TTS`), web scraping, and background tasks. |
| **Cloud Intelligence** | Subscription / OpenRouter Gateway | High-reasoning and complex code generation via Claude 5 Sonnet / Codex 5.6 session bridges. |
| **macOS PC Node** | Rust/Go + AppleScript + Accessibility | Full host machine control (volume, display, apps, window management, dev workflows, GUI automation). |
| **Room Node** | Raspberry Pi + Wall Projector + Cam | Hands-free wake word ("Mira"), wall projection mapping, spatial hand gesture interaction. |

---

## 📁 Workspace Directory Layout

```
mira/
├── AGENTS.md                  # This file: AI agent instructions & codebase context
├── PROJECT_STATUS.md          # Live roadmap, phase tracking, and component status
├── README.md                  # Human overview & setup guide
├── docs/                      # Architectural specs and system documentation
│   └── superpowers/specs/     # Design specifications (e.g. 2026-08-17-*.md)
├── open-webui/                # Legacy reference setup (isolated/archived)
├── daemon/                    # [Phase 1] Rust/Go Core Daemon & Event Router
├── web/                       # [Phase 1] TypeScript Web Dashboard & Ambient UI
├── vision/                    # [Phase 1] Python Computer Vision & Hand-Tracking Microservices
├── nodes/                     # [Phase 2] Node client handlers (Pi Projector, macOS PC CLI, Homelab)
└── shared/                    # [Phase 1] Shared Protocol Buffers / IPC schemas & type definitions
```

---

## 🎯 Primary Goals & Principles for AI Agents

When implementing features or refactoring code in this repository, adhere strictly to these principles:

1. **Modular Isolation & Microservices Boundaries**:
   - Keep the Rust/Go daemon, TypeScript UI, Python vision services, Homelab workers, and Node clients cleanly decoupled.
   - All inter-process communication MUST go through well-defined WebSocket / gRPC / Protobuf schemas defined under `shared/`.

2. **Cost-Optimized Hybrid Intelligence**:
   - Route simple commands to native Rust/Go handlers ($<20\text{ms}$).
   - Route routine queries to the **Homelab Tesla P40 GPU server** ($<300\text{ms}$, $0 token cost).
   - Route complex reasoning to **Claude Pro / Codex subscription bridges**, falling back to local Homelab GPU if subscription quota limits are reached.

3. **Multi-Node Hardware & Room Projection**:
   - Support Raspberry Pi wall projector setup with 4-point Homography matrix calibration ($H$).
   - Offload heavy compute (STT/TTS/crawlers) to the laptop worker fleet.

4. **Self-Documenting Agent Handoffs**:
   - Always update `PROJECT_STATUS.md` after adding components, changing API contracts, or reaching new roadmap milestones.

5. **Check for Prior Art Before Building Large**:
   - Before implementing anything "somewhat large" (a new crate/service, a nontrivial protocol or integration layer, a security-sensitive component like signing/auth/policy engines), search GitHub/existing OSS for a working reference implementation or library first. Prefer copying/adapting a real implementation, or pulling in an existing dependency directly, over writing it fully from scratch.
   - This does not apply to small, fully-specified mechanical tasks (e.g. an SDD task brief that already contains the exact code to write) — it applies to the design/research step before such a brief is written, or to open-ended implementation work.

---

## 🚀 Immediate Next Steps (When Returning Home)

1. Review `PROJECT_STATUS.md` and all design specs in `docs/superpowers/specs/`.
2. Scaffold `shared/` Protobuf/JSON schema definitions for WebSocket RPC.
3. Scaffold `daemon/` skeleton in Rust/Go with event routing.
4. Scaffold `web/` ambient dashboard in TypeScript.
5. **Handheld Multimodal Wand & Room UI Guidelines**:
   - **Preserve Pin Budget**: Do NOT reassign `D0–D7` (`GPIO 1, 2, 3, 4, 5, 6, 43, 44`) or conflicts will occur with the Sense board's internal DVP camera (`GPIO 10–18, 38–40, 47, 48`) or PDM microphone (`GPIO 41, 42`).
   - **Non-Blocking Async Execution**: All ESP32 button reads, OLED telemetry updates, LED animations, and network I/O must remain non-blocking via FreeRTOS tasks and hardware timers.
   - **Zero-Luminance #000000 UI Standards**: All ceiling projector UI slides and dynamic cards must render against `#000000` true black with high-contrast accent colors (cyan, emerald, amber) to blend into the wall without projecting a bright rectangular backlight halo.
   - **Pascal P40 Compute Limits**: Specify GGUF INT8/DP4A quantized formats via `llama.cpp`/Ollama for the Tesla P40 GPU. Avoid native FP16/BF16 tensor execution.
