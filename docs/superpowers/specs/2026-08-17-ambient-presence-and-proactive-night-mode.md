# Specification: Ambient Presence Engine & ESP32 Smart Room Integration

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-ambient-presence-and-proactive-night-mode.md`

---

## 1. Executive Summary

This specification defines the 24/7 ambient presence detection and ESP32 hardware automation system for Jarvis. The Raspberry Pi ceiling node runs a continuous 24/7 wide-angle webcam vision loop to detect user presence and spatial interaction with the **Jarvis Wall UI**. Fused with an MQTT broker, Jarvis automatically orchestrates custom ESP32 nodes (relays for room lighting, smart motor curtains, ambient desk LEDs) based on room presence and ambient time-of-day states.

---

## 2. System Architecture & Topology

```
┌────────────────────────────────────────────────────────┐
│ 24/7 Ceiling Webcam (MediaPipe Pose + Gesture Stream)  │
└──────────────────────────┬─────────────────────────────┘
                           │ 5 FPS Pose / Gesture Activity
                           ▼
┌────────────────────────────────────────────────────────┐
│ Core Daemon Presence State Machine                     │
│ - Evaluates: ACTIVE_WALL_UI | DESK_PRESENT | ROOM_AWAY │
└──────────────────────────┬─────────────────────────────┘
                           │ MQTT Pub/Sub (`jarvis/esp32/...`)
                           ▼
┌────────────────────────────────────────────────────────┐
│ Local ESP32 Hardware Fleet (WiFi)                      │
│ ├─ ESP32 Relay #1: Room Overhead Lighting              │
│ ├─ ESP32 Relay #2: Smart Curtains Motor Drive          │
│ └─ ESP32 Relay #3: Custom Ambient Desk Accent Lights   │
└────────────────────────────────────────────────────────┘
```

---

## 3. Presence States & ESP32 Automation Triggers

| Presence State | Vision & VAD Trigger | Wall UI Projector State | ESP32 Hardware Actions |
| :--- | :--- | :--- | :--- |
| **`ACTIVE_WALL_UI`** | User facing wall, hand gestures detected | 100% Brightness, full interactive cursors | Lights dimmed to 30%, Curtains closed (anti-glare mode) |
| **`DESK_WORKING`** | Body detected at desk, no wall gestures | 50% Ambient Clock / Status HUD overlay | Overhead desk lights 100%, Accent LEDs active |
| **`IDLE_ROOM`** | No motion detected for $> 5$ minutes | 20% Dimmed standby overlay | Overhead lights off, Accent LEDs 20% |
| **`ROOM_AWAY`** | No body detected for $> 15$ minutes | Standby / Display OFF via HDMI-CEC | All relays OFF, Curtains closed (privacy mode) |
| **`NIGHT_MODE`** | Time $> 10:30\text{ PM}$ + 10 min audio silence | Dark OLED red-shift night HUD | Lights off, Curtains closed, audio chimes muted |

---

## 4. ESP32 MQTT Protocol Specification

The Core Daemon runs an internal MQTT broker (`Mosquitto` or embedded Rust MQTT server on port `1883`). ESP32 nodes connect over WiFi and subscribe to standard JSON topics:

### 4.1 Topic Structure
- **Command Topic**: `jarvis/esp32/<device_id>/set`
- **State Topic**: `jarvis/esp32/<device_id>/state`
- **Telemetry Topic**: `jarvis/esp32/<device_id>/telemetry`

### 4.2 Payload Schema (`shared/schemas/esp32_control.json`)

```json
{
  "device_id": "esp32-curtains-01",
  "command": "SET_POSITION",
  "state": {
    "relay_1": true,
    "position_pct": 100,
    "brightness_pct": 80
  },
  "timestamp": "2026-08-17T11:45:00Z"
}
```

---

## 5. Implementation Roadmap
1. Build `vision/presence_tracker.py` using MediaPipe Pose at 5 FPS to output presence state.
2. Integrate `rumqttc` MQTT broker client into `daemon/`.
3. Add C++/Arduino ESP32 reference firmware in `nodes/esp32/firmware.ino`.
