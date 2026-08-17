# Jarvis Architecture & System Design Specification

> **Date**: August 17, 2026  
> **Topic**: Bespoke Multi-Node Ambient AI System ("Jarvis") Architecture  
> **Status**: Approved Blueprint  
> **Target Monorepo Location**: `/Users/aryavora/desktop/personal projects/jarvis`

---

## 1. Executive Summary & Vision

Jarvis is a personal ambient AI system engineered for low-latency, real-world physical and digital integration. Rather than operating purely as a web chat application (such as OpenWebUI), Jarvis is designed as an agentic network bridging background daemons, vision sensors, projection surfaces, mobile nodes, and local/cloud AI models.

### Key Capabilities
- **Multi-Node Device Orchestration**: Synchronizes state and triggers across macOS PC, Mobile, Raspberry Pi + Projector, and Homelab services.
- **Ambient Perception & Gestures**: Ingests real-time webcam video feeds via Python MediaPipe services to parse hand gestures and spatial interactions for projected UIs.
- **Hybrid Intelligence Routing**: Distributes workloads between instant local LLMs (Ollama / llama.cpp) for offline/low-latency intents and Cloud LLMs (Anthropic Claude, OpenAI, Gemini) for deep multi-step reasoning.
- **Polyglot Monorepo Architecture**: Leverages Rust/Go for core daemon performance, TypeScript for rich ambient web interfaces, and Python for machine learning / vision processing.

---

## 2. System Architecture & Microservices

```
                          ┌────────────────────────────────┐
                          │   TypeScript Ambient Web UI    │
                          │ (Dashboard & Projection Layer) │
                          └───────────────┬────────────────┘
                                          │ WebSocket JSON/Protobuf
                                          ▼
┌──────────────────────┐  WebSocket/RPC  ┌────────────────────────────────┐
│  Python Vision Svc   ├────────────────►│      Rust / Go Core Daemon     │
│(MediaPipe / Cam Feed)│                 │   (Event Router & State Hub)   │
└──────────────────────┘                 └───────────────┬────────────────┘
                                                         │
                                        ┌────────────────┼────────────────┐
                                        │                │                │
                                        ▼                ▼                ▼
                              ┌────────────────┐ ┌───────────────┐ ┌───────────────┐
                              │ Hybrid LLM     │ │ Raspberry Pi  │ │ macOS / PC    │
                              │ Router         │ │ Projector Node│ │ System Agent  │
                              └───────┬────────┘ └───────────────┘ └───────────────┘
                                      │
                         ┌────────────┴────────────┐
                         ▼                         ▼
               ┌──────────────────┐       ┌──────────────────┐
               │ Ollama (Local)   │       │ Cloud APIs       │
               │ Fast / Offline   │       │ Claude / OpenAI  │
               └──────────────────┘       └──────────────────┘
```

### 2.1 Core Subsystems

#### A. Core Daemon (`/daemon`) — Language: Rust or Go
- **Role**: Central heartbeat, event broker, and IPC gateway.
- **Responsibilities**:
  - Maintains persistent WebSocket and gRPC servers for internal microservices and external node clients.
  - Houses the **Node Registry**, tracking active device sessions (PC, Pi, Mobile, Homelab).
  - Executes low-latency local system hooks (AppleScript/macOS system control, shell scripts).
  - Routes requests through the Hybrid Intelligence Router.

#### B. Web Interface (`/web`) — Language: TypeScript + React (Vite / Next.js)
- **Role**: Visual dashboard, spatial overlay, and command console.
- **Responsibilities**:
  - Provides a sleek, dark-themed ambient dashboard for state visualization.
  - Serves as the projected UI component for the Pi + camera setup (fullscreen gesture-reactive UI).
  - Renders live camera feed bounding boxes and gesture cursor feedback.

#### C. Vision & Perception Service (`/vision`) — Language: Python (MediaPipe / OpenCV)
- **Role**: Computer vision sensor node.
- **Responsibilities**:
  - Captures video frames from USB/IP webcams.
  - Runs hand landmark tracking via MediaPipe.
  - Translates hand gestures (e.g. pinch, point, swipe, palm stop) into standardized IPC `GestureEvent` packets sent to the daemon.

#### D. Hybrid Intelligence Router (`/daemon/router`)
- **Role**: Intelligent model dispatching.
- **Responsibilities**:
  - Evaluates incoming prompts/intents based on complexity, required latency, and network status.
  - **Local Path**: Directs simple system commands ("mute volume", "turn on projector", "navigate UI") to Ollama running locally.
  - **Cloud Path**: Directs multi-step code generation, deep research, or complex queries to Anthropic Claude or OpenAI APIs.

---

## 3. Protocol & Data Schemas (IPC Specification)

All inter-service communication between Daemon, Web UI, Vision service, and Node clients uses strict JSON/Protobuf contracts defined in `/shared`.

### 3.1 Gesture Event Schema (`shared/schemas/gesture_event.json`)
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "GestureEvent",
  "type": "object",
  "properties": {
    "node_id": { "type": "string" },
    "timestamp": { "type": "integer" },
    "gesture_type": { 
      "type": "string", 
      "enum": ["PINCH", "POINT", "SWIPE_LEFT", "SWIPE_RIGHT", "PALM_STOP", "HOVER"] 
    },
    "coordinates": {
      "type": "object",
      "properties": {
        "x": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
        "y": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
        "z": { "type": "number" }
      },
      "required": ["x", "y"]
    },
    "confidence": { "type": "number", "minimum": 0.0, "maximum": 1.0 }
  },
  "required": ["node_id", "timestamp", "gesture_type", "coordinates", "confidence"]
}
```

### 3.2 Node Command Schema (`shared/schemas/node_command.json`)
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "NodeCommand",
  "type": "object",
  "properties": {
    "command_id": { "type": "string" },
    "target_node": { "type": "string" },
    "action": { "type": "string" },
    "payload": { "type": "object" },
    "timeout_ms": { "type": "integer", "default": 5000 }
  },
  "required": ["command_id", "target_node", "action"]
}
```

---

## 4. Real-World Integration: Raspberry Pi + Projector Setup

### 4.1 Physical Hardware Layout
1. **Raspberry Pi 4 / 5**: Mounted near or connected to the projector.
2. **Projector**: Displays Jarvis ambient UI onto a desk surface or wall.
3. **Webcam (1080p / 60fps)**: Positioned facing the projected interaction zone.

### 4.2 Spatial Calibration Protocol
1. **Homography Calibration**: The `vision` microservice displays a 4-point calibration target on the projection surface via the `web` UI.
2. The webcam captures the projected points to calculate a perspective transform matrix ($H$).
3. Hand landmark $(x, y)_{camera}$ coordinates are transformed into normalized $(x', y')_{projector}$ coordinates for cursor placement.

---

## 5. Phased Roadmap & Action Items

### Phase 1: Monorepo & Vertical Slice (Immediate Focus upon Return)
1. **Repository Structure**:
   - Create directories: `daemon/`, `web/`, `vision/`, `shared/`, `nodes/`, `docs/`.
2. **Shared IPC Definitions**:
   - Save JSON/Protobuf schemas in `shared/schemas/`.
3. **Core Daemon Skeleton**:
   - Implement Rust/Go WebSocket server listening on `127.0.0.1:8080`.
4. **Web UI Skeleton**:
   - Scaffold Vite React TypeScript app in `web/` with WebSocket connection status and gesture cursor overlay component.
5. **Python Vision Service**:
   - Create Python venv in `vision/`, install `mediapipe opencv-python websockets`.
   - Run live webcam feed and stream hand point coordinates to `ws://127.0.0.1:8080`.

### Phase 2: Intelligence & Voice Pipeline
- Connect Ollama local LLM client in daemon.
- Connect Anthropic Claude / OpenAI SDKs with fallback logic.
- Integrate Whisper STT and local TTS engine.

### Phase 3: Hardware & Node Expansion
- Deploy vision node on Raspberry Pi.
- Complete homography projection calibration.
- Build macOS menu bar daemon / CLI client node.

---

## 6. Self-Review & Verification Check
- **No Vagueness**: All tech stack choices (Rust/Go, TS, Python), microservices boundaries, IPC schemas, and roadmap phases are explicitly defined.
- **Consistency**: Matches decisions made across `AGENTS.md`, `PROJECT_STATUS.md`, and `README.md`.
- **Target Location**: `docs/superpowers/specs/2026-08-17-jarvis-architecture-design.md`.
