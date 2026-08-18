# M.I.R.A. MacBook-Only Local Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, simulate, and verify the complete M.I.R.A. ambient AI monorepo on this MacBook without physical hardware (Raspberry Pi, Wall Projector, Tesla P40 Homelab, ESP32 Wand), using native macOS hardware (FaceTime HD camera, microphone) and dedicated virtual node simulators.

**Architecture:** Polyglot monorepo featuring a high-performance **Rust Core Daemon** (Tokio event bus, SQLite memory, hybrid router, circuit breaker, macOS host hooks), a **TypeScript React 19 Ambient Web UI** (`#000000` pitch-black projection canvas, 3D SVG globe, synced lyrics, Klipper 3D printers, admin panel), a **Python Vision & Speech Service** (MediaPipe hand tracking, ArUco auto-homography, Silero VAD + faster-whisper), and a **Hardware Simulation Suite** (Homelab GPU, ESP32 Wand, Klipper Moonraker, Pi Projector).

**Tech Stack:** 
- **Core Daemon**: Rust 1.96 (Tokio, Axum/Tungstenite, Rusqlite/SQLx, Serde)
- **Web UI**: TypeScript, React 19, Vite, Vanilla CSS Design Tokens, `Zen Dots` / `Geo` Google Fonts
- **Vision & Voice**: Python 3.13 (OpenCV, MediaPipe, NumPy, Silero VAD ONNX, faster-whisper, WebSockets)
- **Shared Contracts**: JSON Schema, typed models in Rust, TS, and Python
- **Simulation**: Python + Node.js virtual node test harnesses

---

## Global Constraints

- **Single Host Development**: All microservices must bind cleanly to `localhost` / `127.0.0.1` with isolated ports (`8080` daemon, `5173` web, `8765` wand, `11434` mock ollama, `7125` mock moonraker).
- **Zero-Luminance `#000000` UI**: Web UI canvas background must be strict `#000000` true black with Electric Purple (`#9333ea`) and Neon Orange (`#ff6b00`) accents.
- **Hardware Fallback Compatibility**: Vision and speech modules must support both live MacBook hardware (camera/mic) and synthetic recorded mock loops.
- **Non-Blocking Architecture**: Rust daemon and ESP32 wand simulators must never block async event loops.

---

## Architecture Overview & Port Mapping

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                            MacBook Host Machine                         │
 │                                                                         │
 │  ┌─────────────────────────────────┐   ┌─────────────────────────────┐  │
 │  │   TypeScript Web Interface      │   │    Python Vision Service    │  │
 │  │   Port 5173 (React 19 / Vite)   │   │  (MediaPipe + ArUco + VAD)  │  │
 │  │   - Pitch Black Wall Canvas     │   │  - FaceTime HD Cam Ingestion│  │
 │  │   - /admin Debug Console        │   │  - Homography H Transform   │  │
 │  └───────────────┬─────────────────┘   └──────────────┬──────────────┘  │
 │                  │ WebSocket                          │ WebSocket       │
 │                  ▼                                    ▼                 │
 │  ┌───────────────────────────────────────────────────────────────────┐  │
 │  │                    Rust Core Daemon (Port 8080)                   │  │
 │  │  - Tokio Async Event Broker (/ws, /ws/controller)                 │  │
 │  │  - Hybrid Router & 3-Tier SQLite Memory Engine                    │  │
 │  │  - 3-State Circuit Breaker & Offline Fallback                     │  │
 │  │  - Native macOS Host Hooks (Volume, Display, AppleScript)         │  │
 │  └───────────────────────────────┬───────────────────────────────────┘  │
 │                                  │                                      │
 │                  ┌───────────────┴───────────────┐                      │
 │                  ▼                               ▼                      │
 │  ┌───────────────────────────────┐   ┌───────────────────────────────┐  │
 │  │   Homelab & Printer Mock      │   │    ESP32 Wand Simulator       │  │
 │  │   - Port 11434 (Mock Ollama)  │   │    - CLI / GUI Virtual Wand   │  │
 │  │   - Port 7125 (Mock Moonraker)│   │    - Buttons D0-D6 + PTT Audio│  │
 │  └───────────────────────────────┘   └───────────────────────────────┘  │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Shared Protocols & Data Schemas

### Task 1: JSON Schema Definitions & Type Generators

**Files:**
- Create: `shared/schemas/gesture_event.json`
- Create: `shared/schemas/speech_event.json`
- Create: `shared/schemas/media_event.json`
- Create: `shared/schemas/node_command.json`
- Create: `shared/schemas/telemetry_event.json`
- Create: `shared/schemas/wand_event.json`
- Create: `shared/schemas/calibration_event.json`
- Create: `shared/types/index.ts`
- Create: `shared/types/events.py`
- Test: `shared/tests/test_schemas.py`

**Interfaces:**
- Consumes: JSON Schema Draft-07
- Produces: TypeScript types in `shared/types/index.ts`, Python Pydantic models in `shared/types/events.py`

- [ ] **Step 1: Write JSON schemas in `shared/schemas/`**
- [ ] **Step 2: Create Python Pydantic models and TS type declarations**
- [ ] **Step 3: Write schema validation test in `shared/tests/test_schemas.py`**
- [ ] **Step 4: Run test to verify schema compliance**
  - Run: `python3 -m unittest shared/tests/test_schemas.py`
  - Expected: PASS
- [ ] **Step 5: Commit shared protocol schemas**
  - Run: `git add shared/ && git commit -m "feat(shared): add IPC schemas and typed contracts"`

---

## Phase 2: Rust Core Daemon & Event Router

### Task 2: Rust Daemon Core Scaffolding & WebSocket Broker

**Files:**
- Create: `daemon/Cargo.toml`
- Create: `daemon/src/main.rs`
- Create: `daemon/src/server/mod.rs`
- Create: `daemon/src/server/ws.rs`
- Create: `daemon/src/bus/mod.rs`
- Create: `daemon/src/bus/events.rs`
- Create: `daemon/src/registry/mod.rs`
- Test: `daemon/tests/ws_broker_test.rs`

**Interfaces:**
- Consumes: `shared/schemas/*.json` payloads via JSON WebSocket
- Produces: `ws://127.0.0.1:8080/ws` for clients and `ws://127.0.0.1:8080/ws/controller` for wand

- [ ] **Step 1: Write Cargo.toml with Tokio, Axum, Serde, Rusqlite, Tracing**
- [ ] **Step 2: Implement Tokio broadcast event channel and Node Registry**
- [ ] **Step 3: Implement WebSocket connection handler for client registration and message forwarding**
- [ ] **Step 4: Write test verifying concurrent client broadcast in `daemon/tests/ws_broker_test.rs`**
- [ ] **Step 5: Run `cargo test` and verify PASS**
  - Run: `cargo test --manifest-path daemon/Cargo.toml`
  - Expected: PASS
- [ ] **Step 6: Commit Rust core broker**
  - Run: `git add daemon/ && git commit -m "feat(daemon): scaffold rust tokio websocket broker"`

---

### Task 3: 3-Tier Context Memory Engine (SQLite + RAM Ring)

**Files:**
- Create: `daemon/src/memory/mod.rs`
- Create: `daemon/src/memory/ram_ring.rs`
- Create: `daemon/src/memory/sqlite_store.rs`
- Test: `daemon/tests/memory_test.rs`

**Interfaces:**
- Consumes: User utterances, node execution logs, room settings
- Produces: `MemoryEngine::get_recent_context()`, `MemoryEngine::log_event()`

- [ ] **Step 1: Write `ram_ring.rs` for last 20 messages in-memory buffer**
- [ ] **Step 2: Write `sqlite_store.rs` for persistent chat logs, nodes, and device states**
- [ ] **Step 3: Implement async background flush from RAM ring to SQLite**
- [ ] **Step 4: Write memory unit test verifying persistence and retrieval**
- [ ] **Step 5: Run `cargo test --test memory_test`**
  - Expected: PASS
- [ ] **Step 6: Commit Memory Engine**
  - Run: `git add daemon/src/memory/ daemon/tests/ && git commit -m "feat(daemon): implement 3-tier memory engine"`

---

### Task 4: Hybrid Intelligence Router & Circuit Breaker

**Files:**
- Create: `daemon/src/router/mod.rs`
- Create: `daemon/src/router/classifier.rs`
- Create: `daemon/src/router/circuit_breaker.rs`
- Create: `daemon/src/router/ollama_client.rs`
- Create: `daemon/src/router/openrouter_client.rs`
- Test: `daemon/tests/router_test.rs`

**Interfaces:**
- Consumes: Cleaned speech transcripts, intent categories (`SYSTEM_CONTROL`, `SMART_DEVICE`, `QUICK_ASSISTANT`, `COMPLEX_REASONING`)
- Produces: Streaming tokens, structured tool calls, automatic failover response

- [ ] **Step 1: Implement 3-State Circuit Breaker (`CLOSED`, `OPEN`, `HALF_OPEN`) with exponential backoff**
- [ ] **Step 2: Implement intent classifier (regex fast-path + prompt complexity scorer $C$)**
- [ ] **Step 3: Implement Ollama HTTP streaming client with fallback to OpenRouter**
- [ ] **Step 4: Write test verifying circuit tripping and fallback cascade**
- [ ] **Step 5: Run `cargo test --test router_test`**
  - Expected: PASS
- [ ] **Step 6: Commit Hybrid Router**
  - Run: `git add daemon/src/router/ daemon/tests/ && git commit -m "feat(daemon): implement hybrid router and circuit breaker"`

---

### Task 5: Native macOS Host Control Hooks

**Files:**
- Create: `daemon/src/hooks/mod.rs`
- Create: `daemon/src/hooks/macos.rs`
- Test: `daemon/tests/macos_hooks_test.rs`

**Interfaces:**
- Consumes: System commands (`set_volume`, `get_battery`, `toggle_mute`, `open_app`, `run_applescript`)
- Produces: System state changes executed via `osascript` and CoreAudio APIs (<20ms execution)

- [ ] **Step 1: Implement safe macOS execution wrappers (volume, mute, app focus)**
- [ ] **Step 2: Write test verifying mock command execution and safe output parsing**
- [ ] **Step 3: Run `cargo test --test macos_hooks_test`**
  - Expected: PASS
- [ ] **Step 4: Commit macOS Host Hooks**
  - Run: `git add daemon/src/hooks/ daemon/tests/ && git commit -m "feat(daemon): add native macos system hooks"`

---

## Phase 3: TypeScript React 19 Ambient Web Projection UI

### Task 6: Web App Scaffolding & Zero-Luminance CSS Design Tokens

**Files:**
- Create: `web/package.json`
- Create: `web/vite.config.ts`
- Create: `web/index.html`
- Create: `web/src/main.tsx`
- Create: `web/src/App.tsx`
- Create: `web/src/index.css`
- Create: `web/src/context/WebSocketContext.tsx`
- Test: `web/tests/theme_tokens.test.ts`

**Interfaces:**
- Consumes: Google Fonts (`Zen Dots`, `Geo`), CSS variables (`--color-bg: #000000`, `--color-primary: #9333ea`, `--color-orange: #ff6b00`, `--color-emerald: #34d399`)
- Produces: Responsive React 19 app with automatic WebSocket reconnect logic

- [ ] **Step 1: Scaffold Vite + React 19 TypeScript project in `web/`**
- [ ] **Step 2: Configure `index.css` with pitch-black canvas, micro-pill styles, and font imports**
- [ ] **Step 3: Implement `WebSocketContext` managing connection to `ws://127.0.0.1:8080/ws`**
- [ ] **Step 4: Run build check `npm run build`**
  - Run: `cd web && npm install && npm run build`
  - Expected: Clean build with 0 errors
- [ ] **Step 5: Commit Web Core**
  - Run: `git add web/ && git commit -m "feat(web): scaffold react 19 ambient ui with pitch-black design system"`

---

### Task 7: Ambient Screen 1 (3D Wireframe Globe & Micro-Pill Widgets) & Reticle

**Files:**
- Create: `web/src/components/WallCanvas.tsx`
- Create: `web/src/components/WireframeGlobe.tsx`
- Create: `web/src/components/SpatialCursor.tsx`
- Create: `web/src/components/WidgetClock.tsx`
- Create: `web/src/components/WidgetWeather.tsx`
- Create: `web/src/components/WidgetMusic.tsx`
- Create: `web/src/components/WidgetNodeStatus.tsx`
- Create: `web/src/components/DevControlBar.tsx`
- Test: `web/tests/globe_render.test.tsx`

**Interfaces:**
- Consumes: Live time, mock weather, mock track data, normalized $(x, y) \in [0.0, 1.0]$ cursor coordinates
- Produces: 60 FPS hardware-accelerated ambient projection wall with interactive testing bar

- [ ] **Step 1: Implement 3D SVG rotating wireframe sphere (`WireframeGlobe.tsx`) with purple longitude/latitude and orange joints**
- [ ] **Step 2: Implement hardware-accelerated CSS `translate3d` glowing reticle (`SpatialCursor.tsx`)**
- [ ] **Step 3: Implement corner micro-pills: `WidgetClock`, `WidgetWeather`, `WidgetMusic`, `WidgetNodeStatus`**
- [ ] **Step 4: Implement `DevControlBar.tsx` (vacation test bar allowing manual cursor dragging, gesture trigger buttons, and mode switching)**
- [ ] **Step 5: Verify visually and run test `npm test`**
- [ ] **Step 6: Commit Ambient Screen 1**
  - Run: `git add web/src/components/ && git commit -m "feat(web): build ambient 3d globe, corner widgets, and spatial cursor"`

---

### Task 8: Screen 2 (Expanded Media & Synced Lyrics) & Screen 3 (Visual / LLM Code Stage)

**Files:**
- Create: `web/src/components/ExpandedMediaView.tsx`
- Create: `web/src/components/SyncedLyricsView.tsx`
- Create: `web/src/components/VisualResponseView.tsx`
- Create: `web/src/components/ActiveLLMStageView.tsx`
- Create: `web/src/components/HomeExitButton.tsx`

**Interfaces:**
- Consumes: Media stream events, speech transcripts, LLM markdown code blocks
- Produces: Fullscreen interactive media stage with 9-bar audio visualizer, Google Nest-style visual query card, LLM code container, and universal `🏠 HOME` exit button

- [ ] **Step 1: Implement `ExpandedMediaView.tsx` and `SyncedLyricsView.tsx` with karaoke-style line highlighting**
- [ ] **Step 2: Implement `VisualResponseView.tsx` (Screen 3A default visual card) and `ActiveLLMStageView.tsx` (Screen 3B explicit code view)**
- [ ] **Step 3: Implement reusable `HomeExitButton.tsx` on all overlay screens**
- [ ] **Step 4: Verify screen transitions in Vite dev server**
- [ ] **Step 5: Commit Media & LLM Stages**
  - Run: `git add web/src/components/ && git commit -m "feat(web): implement expanded media lyrics and llm visual response views"`

---

### Task 9: Screen 4 (Optical Calibration) & Screen 5 (Smart Home Control Center)

**Files:**
- Create: `web/src/components/CalibrationView.tsx`
- Create: `web/src/components/SmartHomeControlView.tsx`
- Create: `web/src/components/PrintersTab.tsx`
- Create: `web/src/components/HomelabTelemetryTab.tsx`
- Create: `web/src/components/LightsTab.tsx`

**Interfaces:**
- Consumes: Klipper Moonraker printer status, Homelab GPU/CPU metrics, room LED states, ArUco corner coordinates
- Produces: 4-corner optical calibration screen, 3-tab smart home control center with live SVG sparkline waveform graphs

- [ ] **Step 1: Implement `CalibrationView.tsx` rendering 4 ArUco crosshairs ($P_1 - P_4$) and homography error metrics**
- [ ] **Step 2: Implement `PrintersTab.tsx` with progress bars, nozzle/bed temperatures, and ETA for 3-4 printers**
- [ ] **Step 3: Implement `HomelabTelemetryTab.tsx` with Tesla P40 VRAM sparkline, Host CPU sparkline, and Pi thermal sparkline**
- [ ] **Step 4: Implement `LightsTab.tsx` with preset pills (`PURPLE`, `WARM`, `CYAN`, `OFF`) and brightness slider**
- [ ] **Step 5: Commit Calibration & Smart Home Stages**
  - Run: `git add web/src/components/ && git commit -m "feat(web): build calibration stage and smart home control center"`

---

### Task 10: Remote Admin Dashboard (`/admin`)

**Files:**
- Create: `web/src/admin/AdminDashboard.tsx`
- Create: `web/src/admin/HomographyEditor.tsx`
- Create: `web/src/admin/NodeManager.tsx`
- Create: `web/src/admin/LLMRouterPanel.tsx`

**Interfaces:**
- Consumes: Daemon WebSocket state, node registry list, active homography matrix
- Produces: Interactive drag-and-drop 4-point corner calibration canvas, auto-swap rules config, node restart buttons

- [ ] **Step 1: Implement `/admin` route in React app**
- [ ] **Step 2: Build interactive canvas for dragging corner points $P_1 - P_4$ over camera feed**
- [ ] **Step 3: Build node health inspector and LLM router priority toggles**
- [ ] **Step 4: Commit Admin Dashboard**
  - Run: `git add web/src/admin/ && git commit -m "feat(web): add remote admin dashboard and homography editor"`

---

## Phase 4: Python Vision & Speech Pipeline

### Task 11: Python Service Scaffolding & MediaPipe Hand Tracking

**Files:**
- Create: `vision/requirements.txt`
- Create: `vision/src/main.py`
- Create: `vision/src/tracker/hand_tracker.py`
- Create: `vision/src/tracker/gestures.py`
- Create: `vision/src/camera/camera_stream.py`
- Test: `vision/tests/test_gestures.py`

**Interfaces:**
- Consumes: Video frames from MacBook FaceTime HD Camera (`cv2.VideoCapture(0)`) or synthetic MP4 loop
- Produces: 21 3D hand landmarks, gesture classifications (`PINCH`, `POINT`, `SWIPE_LEFT`, `SWIPE_RIGHT`, `PALM_STOP`, `HOVER`) streamed over WebSocket to daemon

- [ ] **Step 1: Setup `vision/requirements.txt` (`opencv-python`, `mediapipe`, `numpy`, `websockets`, `pydantic`)**
- [ ] **Step 2: Implement `hand_tracker.py` using MediaPipe Hands solution**
- [ ] **Step 3: Implement `gestures.py` calculating finger angle and pinch distance thresholds**
- [ ] **Step 4: Write unit tests with synthetic landmark vectors in `vision/tests/test_gestures.py`**
- [ ] **Step 5: Run tests `python3 -m unittest vision/tests/test_gestures.py`**
  - Expected: PASS
- [ ] **Step 6: Commit Python Hand Tracker**
  - Run: `git add vision/ && git commit -m "feat(vision): implement mediapipe hand tracking and gesture recognition"`

---

### Task 12: Optical Homography Matrix Calibration & Auto-ArUco

**Files:**
- Create: `vision/src/calibration/homography.py`
- Create: `vision/src/calibration/aruco_detector.py`
- Test: `vision/tests/test_homography.py`

**Interfaces:**
- Consumes: Camera pixel coordinates $(x, y)_{cam}$, 4 reference corner points $(P_1 - P_4)$
- Produces: Perspective transformation matrix $H$ via `cv2.findHomography`, mapped normalized screen coordinates $(x', y') \in [0.0, 1.0]$

- [ ] **Step 1: Implement `homography.py` perspective transformation and coordinate remapping**
- [ ] **Step 2: Implement `aruco_detector.py` for OpenCV ArUco 4-marker automated detection**
- [ ] **Step 3: Write homography transform mathematical verification test in `vision/tests/test_homography.py`**
- [ ] **Step 4: Run `python3 -m unittest vision/tests/test_homography.py`**
  - Expected: PASS
- [ ] **Step 5: Commit Homography Engine**
  - Run: `git add vision/src/calibration/ vision/tests/ && git commit -m "feat(vision): add 4-point homography and aruco detector"`

---

### Task 13: Low-Latency Speech Sanitizer & VAD ("WhisperFlow")

**Files:**
- Create: `vision/src/speech/vad_engine.py`
- Create: `vision/src/speech/whisper_transcriber.py`
- Create: `vision/src/speech/sanitizer.py`
- Test: `vision/tests/test_sanitizer.py`

**Interfaces:**
- Consumes: Raw audio chunks from MacBook microphone or mock WAV files
- Produces: Cleaned intent prompts with filler words stripped and self-corrections resolved

- [ ] **Step 1: Implement `vad_engine.py` with Silero VAD / energy threshold boundary detection**
- [ ] **Step 2: Implement `whisper_transcriber.py` using `faster-whisper` (or mock speech stub if offline)**
- [ ] **Step 3: Implement `sanitizer.py` disfluency regex & rule-based correction filter**
- [ ] **Step 4: Write test verifying cleaning of utterances (e.g. "turn off um lights wait no ceiling")**
- [ ] **Step 5: Run `python3 -m unittest vision/tests/test_sanitizer.py`**
  - Expected: PASS
- [ ] **Step 6: Commit Voice Sanitizer**
  - Run: `git add vision/src/speech/ vision/tests/ && git commit -m "feat(vision): implement whisperflow speech sanitizer and vad"`

---

## Phase 5: Hardware Simulation Suite & Virtual Nodes

### Task 14: Homelab Server & 3D Printer Mock Server

**Files:**
- Create: `nodes/simulator/homelab_mock.py`
- Create: `nodes/simulator/moonraker_mock.py`
- Test: `nodes/simulator/tests/test_homelab_mock.py`

**Interfaces:**
- Consumes: HTTP/REST requests on `11434` (Ollama) and `7125` (Moonraker)
- Produces: Simulated Tesla P40 model streaming responses (`nemotron-3.5-lightning`), realistic 3D printer telemetry (Ender 3, Voron 2.4 nozzle temps, layers, progress %)

- [ ] **Step 1: Implement lightweight FastAPI/HTTP mock server for Ollama `/api/chat` and `/api/tags`**
- [ ] **Step 2: Implement Moonraker mock API (`/printer/objects/query`, `/printer/info`) with dynamic print progress**
- [ ] **Step 3: Write test verifying endpoint responses**
- [ ] **Step 4: Commit Homelab & Moonraker Mocks**
  - Run: `git add nodes/simulator/ && git commit -m "feat(sim): build homelab gpu and moonraker 3d printer mocks"`

---

### Task 15: ESP32 Handheld Wand Controller Simulator

**Files:**
- Create: `nodes/simulator/wand_simulator.py`
- Create: `nodes/simulator/wand_ui.html`
- Test: `nodes/simulator/tests/test_wand_sim.py`

**Interfaces:**
- Consumes: User button clicks (PTT D0, Privacy D1, Inspect D2, HUD Cycle D3, Shutter D6)
- Produces: WebSocket connection to `ws://127.0.0.1:8080/ws/controller`, sends button events and audio packets, displays OLED preview and WS2812B LED ring state

- [ ] **Step 1: Implement `wand_simulator.py` WebSocket client with pin mapping logic**
- [ ] **Step 2: Create lightweight web/CLI interactive controller panel displaying virtual buttons, OLED screen, and LED ring animations**
- [ ] **Step 3: Test bidirectional communication with Rust daemon**
- [ ] **Step 4: Commit Wand Simulator**
  - Run: `git add nodes/simulator/ && git commit -m "feat(sim): build esp32 wand controller simulator and oled/led emulator"`

---

## Phase 6: End-to-End Local Orchestration & Verification

### Task 16: Local Dev Launcher & E2E Integration Suite

**Files:**
- Create: `scripts/dev_local.sh`
- Create: `scripts/test_all.sh`
- Create: `tests/e2e/test_full_system_flow.py`
- Modify: `PROJECT_STATUS.md`

**Interfaces:**
- Consumes: One-command launch script
- Produces: Runs Daemon + Web UI + Vision + Simulator stack concurrently with health monitoring

- [ ] **Step 1: Write `scripts/dev_local.sh` using background process management and graceful shutdown trap**
- [ ] **Step 2: Write `scripts/test_all.sh` running Rust tests, TS lint/build, Python unit tests, and E2E simulation**
- [ ] **Step 3: Write `test_full_system_flow.py` verifying full loop (Wand Button -> Daemon Router -> Web View Transition -> macOS Hook)**
- [ ] **Step 4: Run `bash scripts/test_all.sh` and ensure all test suites PASS**
- [ ] **Step 5: Update `PROJECT_STATUS.md` with Phase 1 completion metrics**
- [ ] **Step 6: Commit Orchestration Scripts**
  - Run: `git add scripts/ tests/ PROJECT_STATUS.md && git commit -m "feat: add local dev orchestration launcher and e2e integration suite"`

---

## Verification Plan

### Automated Tests
1. **Shared Schemas**: `python3 -m unittest shared/tests/test_schemas.py`
2. **Rust Core Daemon**: `cargo test --manifest-path daemon/Cargo.toml`
3. **TypeScript Web UI**: `cd web && npm run test && npm run build`
4. **Python Vision & Speech**: `python3 -m unittest discover vision/tests`
5. **Hardware Simulator Suite**: `python3 -m unittest discover nodes/simulator/tests`
6. **Master Monorepo Verification**: `bash scripts/test_all.sh`

### Manual Verification on MacBook
1. **Interactive Ambient UI**: Run `cd web && npm run dev`, open `http://localhost:5173`. Move cursor with mouse or via `DevControlBar.tsx` to verify 60 FPS reticle movement and `#000000` styling.
2. **Live Vision Tracking**: Run `python3 vision/src/main.py`. Raise hand in front of MacBook FaceTime camera; verify normalized coordinates appear on Web UI.
3. **Wand Controller Simulation**: Open `nodes/simulator/wand_ui.html`, click **PTT** or **Cycle HUD**; verify instant screen transition and LED ring animations.
4. **Speech & System Control**: Speak *"Turn volume to 50%"*; verify speech sanitizer strips disfluencies and macOS system volume adjusts automatically.
5. **Remote Admin Console**: Open `http://localhost:5173/admin` to drag calibration corners and toggle router priorities.
