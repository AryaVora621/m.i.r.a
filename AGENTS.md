# AI Agent Guidelines & Context (`AGENTS.md`)

> 🛑 **START HERE — RESUMING ACTIVE WORK (MacBook-Only Offline Development)**  
> - **Active Branch**: `feat/macbook-offline-core-sim`
> - **Active Implementation Plan**: [`docs/superpowers/plans/2026-08-17-mira-macbook-offline-development-plan.md`](./docs/superpowers/plans/2026-08-17-mira-macbook-offline-development-plan.md)
> - **Progress Ledger**: [`.superpowers/sdd/progress.md`](./.superpowers/sdd/progress.md)
> - **Current Status**: Task 1 (Shared IPC Schemas & Types) is **COMPLETE** (`commit aa8bc51`).
> - **Next Task to Execute**: **Task 2: Rust Daemon Core Scaffolding & WebSocket Broker** (`daemon/`).
> - **Execution Method**: Follow Subagent-Driven Development (SDD) or Executing Plans. Extract task briefs via `bash /Users/aryavora/.gemini/config/plugins/superpowers/skills/subagent-driven-development/scripts/task-brief docs/superpowers/plans/2026-08-17-mira-macbook-offline-development-plan.md <TASK_NUM>`, execute via TDD, verify with unit tests, and update `.superpowers/sdd/progress.md`.
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
| **Homelab GPU Server** | Tesla P40 (24GB VRAM) + Ollama | Local LLM inference (`nemotron-3.5-lightning` / `qwen2.5:14b`) for 85-90% of requests with zero token cost. |
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
