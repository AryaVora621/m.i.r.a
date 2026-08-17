# Specification: Handheld Multimodal Wand Controller & Lab Ecosystem

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-handheld-multimodal-controller-wand.md`

---

## 1. Executive Summary

This specification defines the hardware, firmware, and server integration for the **Handheld Multimodal Hardware Controller ("Wand / Desk Node")**. Built around the **Seeed Studio XIAO ESP32-S3 Sense**, the device functions both as a stationary desk input macropad (digital MEMS PDM microphone + 0.96" OLED display + RGB status ring) and an active mobile macro inspection scanner (OV2640 macro camera + dedicated tactile triggers). It streams audio, macro JPEGs, and hardware events over a persistent WebSocket connection to the M.I.R.A. Homelab core daemon.

---

## 2. Hardware Architecture & Pin Mapping

### 2.1 Component Specifications
- **MCU**: Seeed Studio XIAO ESP32-S3 Sense (Dual-core Xtensa LX7 @ 240MHz, 8MB PSRAM, 8MB Flash, 2.4GHz Wi-Fi/BLE).
- **Macro Vision**: Onboard OV2640 2MP DVP camera module with manual lens modification (rotated ~180° counter-clockwise for macro focus between 5cm and 15cm).
- **Digital Audio**: Onboard MSM261D3526H1CPM digital PDM MEMS microphone.
- **Display**: 0.96" I2C OLED screen (128x64, SSD1306 driver) on hardware I2C (D4/D5).
- **Illumination / Status**: 8/12-LED WS2812B / SK6812 RGB Ring (32mm OD) surrounding camera lens.
- **Controls**: 5 discrete momentary tactile switches (`INPUT_PULLUP` logic) + side shoulder shutter switch.
- **Power**: 3.7V 1S LiPo pouch battery (800–1000mAh, size 503048) connected directly to `BAT+`/`BAT-` pads; autonomous USB-C charging.

### 2.2 GPIO Pin Mapping Table

| Function / Component | Physical Label | ESP32-S3 GPIO | Electrical Logic | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Push-To-Talk (PTT)** | **D0** | `GPIO 1` | `INPUT_PULLUP` (Active LOW) | Thumb key 1: Streams raw 16kHz PCM audio |
| **Mute / Privacy** | **D1** | `GPIO 2` | `INPUT_PULLUP` (Active LOW) | Thumb key 2: Toggles hardware privacy mute |
| **Inspect / Auto-VLM**| **D2** | `GPIO 3` | `INPUT_PULLUP` (Active LOW) | Thumb key 3: Triggers macro OCR & datasheet search |
| **HUD Clear / Cycle** | **D3** | `GPIO 4` | `INPUT_PULLUP` (Active LOW) | Thumb key 4: Resets or advances projected UI slide |
| **OLED Display SDA** | **D4** | `GPIO 5` | Hardware I2C | Data line for 0.96" telemetry screen |
| **OLED Display SCL** | **D5** | `GPIO 6` | Hardware I2C | Clock line for 0.96" telemetry screen |
| **Side Shutter Switch**| **D6** | `GPIO 43` | `INPUT_PULLUP` (Active LOW) | Shoulder trigger: Macro image snap + VLM prompt |
| **NeoPixel LED Ring** | **D7** | `GPIO 44` | RMT Channel | WS2812B data line for camera ring light |
| **PDM Mic Data** | *Internal* | `GPIO 41` | Sense Expansion Header | 16kHz 16-bit mono audio stream |
| **PDM Mic Clock** | *Internal* | `GPIO 42` | Sense Expansion Header | PDM bit clock |
| **Camera DVP Bus** | *Internal* | `GPIO 10-18, 38-40, 47, 48` | DVP 8-bit Parallel | Framebuffer allocation in PSRAM |

---

## 3. Firmware Architecture & FreeRTOS State Machine

The ESP32-S3 firmware operates as a FreeRTOS dual-core thin client:
- **Core 0 (Network & Audio Worker)**: Handles persistent WebSocket client (`ws://<homelab-ip>:8765/ws/controller`), 16kHz mono PDM streaming, and Wi-Fi auto-reconnect.
- **Core 1 (Vision & UI State)**: Handles 20ms switch debouncing, SSD1306 OLED telemetry updates, WS2812B RMT LED animations, and PSRAM JPEG framebuffer captures.

### LED Ring State Machine
- **Standby / Idle**: Breathing subtle Cyan (10% brightness).
- **PTT Active (Listening)**: Solid Blue ring (50% brightness).
- **Reasoning / Inference**: Rotating Amber chase animation.
- **Inspection Triggered**: Pure White 5000K at 100% Brightness for macro illumination.
- **System Muted**: Flashing Red pulses.

---

## 4. Communication Protocol & Schemas

Endpoint: `ws://<homelab-ip>:8765/ws/controller`

### Upstream Events (ESP32 -> Gateway)
```json
{
  "event": "button_down",
  "button": "shutter" | "ptt" | "inspect" | "cycle_hud" | "mute",
  "gpio": 43,
  "timestamp_ms": 1723891234
}
```

---

## 5. Model Context Protocol (MCP) Tool Schemas

```json
[
  {
    "name": "render_slide",
    "description": "Renders a visual slide or replaces the hero canvas on the wall projector.",
    "parameters": {
      "type": "object",
      "properties": {
        "slide_type": {
          "type": "string",
          "enum": ["model_3d", "hardware_inspect", "telemetry_grid", "dynamic_html", "markdown_doc"]
        },
        "title": { "type": "string" },
        "payload": { "type": "object" },
        "duration_seconds": { "type": "number" }
      },
      "required": ["slide_type", "title", "payload"]
    }
  },
  {
    "name": "mutate_active_slide",
    "description": "Applies a partial update or highlight to the active projected slide without reloading.",
    "parameters": {
      "type": "object",
      "properties": {
        "target_element_id": { "type": "string" },
        "action": { "type": "string", "enum": ["highlight", "explode_cad", "append_log", "close"] },
        "data": { "type": "object" }
      },
      "required": ["action"]
    }
  },
  {
    "name": "wand_set_led",
    "description": "Sets the animation mode and color on the wand's WS2812B ring.",
    "parameters": {
      "type": "object",
      "properties": {
        "mode": { "type": "string", "enum": ["idle", "listening", "thinking", "success", "muted", "flash"] },
        "color_hex": { "type": "string" },
        "brightness": { "type": "integer" }
      },
      "required": ["mode"]
    }
  }
]
```
