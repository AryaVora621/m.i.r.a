# M.I.R.A. Web Ambient Projection Interface Specification

> **Date**: August 17, 2026  
> **Topic**: M.I.R.A. Room Node Web Projection Interface & Visual System  
> **Status**: Approved Specification  
> **Target Path**: `/Users/aryavora/desktop/personal projects/mira/web`

---

## 1. Executive Summary & Core Vision

The **M.I.R.A. Web Subsystem (`/web`)** is a dedicated ambient projection user interface built specifically for the **Raspberry Pi Wall Projector Node**. 

Unlike standard desktop admin panels, M.I.R.A. Web is designed for physical room projection. It enforces **`#000000` Pitch Black Zero-Luminance Canvas Standards** to ensure that projected cards and widgets float seamlessly against the physical room wall without projecting a bright rectangular backlight glow.

---

## 2. Visual Design System & Aesthetics

### 2.1 Brand Color Tokens
- **Canvas Base**: `#000000` (True Pitch Black, zero backlight bleed).
- **Primary Accent**: **Electric Purple** (`#9333ea` / glow `#c084fc` / background `#3b0764`).
- **Secondary Accent**: **Vivid Neon Orange** (`#ff6b00` / glow `#ff9e00` / border `#7c2d12`).
- **Surface Elevation**: Glassmorphism cards rendered with `rgba(8, 6, 16, 0.85)` backdrop blur (`16px`), custom purple/orange borders (`rgba(192, 132, 252, 0.25)`), and soft outer glow shadows.

### 2.2 Typography System
- **Display & Headings**: **`Zen Dots`** (Google Fonts) — Used for major clock numerals, track titles, widget headers, and wake-word badges.
- **Telemetry & Transcripts**: **`Geo`** (Google Fonts) — Used for date/weather readouts, node health metrics, and live intent speech stream text.

### 2.3 Dynamic Aspect Ratio & Compact Wall Scaling (30" x 40" Vertical Wall)
The UI enforces **Fluid Container Layouts & Media Queries** (`@media (aspect-ratio < 1)`) to seamlessly adapt when projected onto compact or vertical wall spaces (e.g. $30'' \text{ wide} \times 40'' \text{ high}$ portrait surface):
- **Normalized Coordinate Mapping**: Spatial hand coordinates from Python MediaPipe vision service are normalized to $(x, y) \in [0.0, 1.0]^2$ via the 4-point Homography matrix ($H$). The CSS cursor scale maps $(x \times 100\%, y \times 100\%)$ regardless of aspect ratio.
- **Compact Portrait Mode ($30'' \times 40''$ Wall)**: 
  - Text and widget dimensions scale down proportionally (Clock font size $\approx 18\text{px} - 22\text{px}$, subtext $\approx 10\text{px} - 12\text{px}$) to keep the wall projection crisp and non-intrusive.
  - `WidgetClock` anchors Top-Left or Top-Center.
  - `WireframeGlobe` scales proportionally in the central vertical zone.
  - `WidgetMusic` anchors cleanly at the Bottom-Right.
  - **No Voice Transcript Popups**: Voice activity text popups are omitted to keep the physical wall completely uncluttered.

---

## 3. Key Interface Components

```
┌─────────────────────────────────────────────────────────────────────────┐
│ [Top-Left: WidgetClock]                     [Top-Right: WidgetNodeStatus]│
│  21:20 (Zen Dots, Compact)                   ● M.I.R.A. ONLINE 60 FPS  │
│  Mon, Aug 17 • 72°F (Geo)                                               │
│                                                                         │
│                                                                         │
│                    ┌───────────────────────────────┐                    │
│                    │   [Center: WireframeGlobe]    │                    │
│                    │ 3D Purple SVG Wireframe Sphere│                    │
│                    │  (No center text inside orb)  │                    │
│                    └───────────────┬───────────────┘                    │
│                                    │                                    │
│                         [Center: SpatialCursor]                         │
│                         Glowing Orange Reticle                          │
│                         (60 FPS CSS translate3d)                        │
│                                                                         │
│                                             [Bottom-Right: WidgetMusic] │
│                                              ⚡ STARBOY - The Weeknd     │
│                                             [||||| EQ Frequency Bars]   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Component Directory Structure (`web/src/`)
- `components/WallCanvas.tsx`: Fullscreen root container matching projector viewport bounds.
- `components/WireframeGlobe.tsx`: Concentric 3D SVG wireframe globe with electric purple latitude/longitude curves and neon orange axis joints (`#ff6b00`).
- `components/WidgetClock.tsx`: Compact glassmorphic top-left card displaying `Zen Dots` time and `Geo` weather data.
- `components/WidgetMusic.tsx`: Compact glassmorphic bottom-right music player with active CSS EQ frequency bar animation.
- `components/WidgetNodeStatus.tsx`: Top-right status pill displaying daemon connection state and FPS.
- `components/SpatialCursor.tsx`: Hardware-accelerated CSS ring reticle positioned via `transform: translate3d(x, y, 0)`.

---

## 4. Technical Architecture & Stack

- **Framework**: **React 19 + Vite** (Single Page App, lightweight bundle $< 120\text{ KB}$).
- **Styling**: Vanilla CSS Design System with CSS Custom Properties (`index.css`).
- **Communication**: Native WebSocket Client connecting to `ws://localhost:8080/ws` (Rust/Go Core Daemon).

### 4.1 WebSocket IPC Schemas Handled by Web UI
1. `GESTURE_EVENT`: `{ "x": float, "y": float, "gesture": "PINCH" | "HOVER" | "SWIPE_LEFT" }`
2. `SPEECH_EVENT`: `{ "state": "LISTENING" | "THINKING" | "SPEAKING", "transcript": string }`
3. `MEDIA_EVENT`: `{ "track": string, "artist": string, "playing": boolean }`
4. `TELEMETRY_EVENT`: `{ "node_id": "pi-projector", "fps": 60, "status": "OK" }`

---

## 5. Hardware-Free Vacation Test Harness

To enable complete frontend development and testing while away from physical hardware:
- **`web/mock-server.cjs`**: A local Node.js WebSocket mock server script.
- Simulates hand cursor smooth movement along sine wave paths.
- Emulates synthetic voice trigger events and music track updates so the UI can be fully verified in local development mode (`npm run dev`).

---

## 6. Implementation Milestones (Phase 1)

1. Scaffold React 19 + Vite app under `web/`.
2. Configure `index.css` design system with `Zen Dots` and `Geo` Google Fonts imports.
3. Build `WebSocketProvider` context and `SpatialCursor` hardware-accelerated overlay.
4. Scaffold `WireframeGlobe`, `WidgetClock`, `WidgetMusic`, `WidgetTranscript`, and `WidgetNodeStatus`.
5. Build `mock-server.cjs` harness for vacation testing.
