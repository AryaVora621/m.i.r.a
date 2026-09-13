# Architectural Specification: Raspberry Pi Zero 2 W Smart Mirror Thin Client

> **Document ID**: `2026-08-25-smart-mirror-thin-client-node`  
> **Status**: Approved Viable Option (Alternative to Wall Projector Node)  
> **Target Subsystem**: `nodes/mirror/` & `web/`  
> **Target Hardware**: Raspberry Pi Zero 2 W + Two-Way Glass Mirror + Homelab Tesla P40 Offload  

---

## 📌 Executive Summary

This document specifies the design, hardware interfaces, and network architecture for using a **Raspberry Pi Zero 2 W** as a low-cost, silent **Smart Mirror Thin Client Node** in the M.I.R.A. ecosystem. 

Because the Zero 2 W has a strict 512 MB RAM limit, heavy compute (MediaPipe hand tracking, Whisper STT, LLM inference, and Kokoro TTS) is entirely offloaded to the **Homelab Tesla P40 GPU & Worker Fleet**. The Zero 2 W acts purely as a synchronized I/O endpoint (CSI camera stream uplink, I2S digital audio uplink, Bluetooth/I2S speaker downlink, and true-black `#000000` ambient UI display).

---

## 🛠️ Hardware Interface & Wiring Specification

To bypass the Zero 2 W's single micro-USB OTG data limitation, all high-throughput peripherals use direct CSI and GPIO bus lines:

```
+-------------------------------------------------------------------------+
|                       Raspberry Pi Zero 2 W                             |
|                                                                         |
|  [22-pin CSI Port] ----> Raspberry Pi Camera Module 3 (Wide-Angle)      |
|                                                                         |
|  [40-pin GPIO Bus]                                                      |
|    - GPIO 18 (BCLK) -----\                                              |
|    - GPIO 19 (LRCLK) ----+---> INMP441 / SPH0645 I2S Digital MEMS Mic   |
|    - GPIO 20 (SD / DIN) -/                                              |
|    - GPIO 21 (DOUT) ---------> Optional MAX98357A I2S 3W DAC Amplifier  |
|                                                                         |
|  [Mini-HDMI Port]  ----> 1080p Monitor / 2-Way Mirror Glass Display     |
|                                                                         |
|  [Bluetooth 4.2]   ----> External Room Bluetooth Speaker (A2DP)         |
|                                                                         |
|  [Micro-USB PWR]   ----> 5V / 2.5A Dedicated Power Supply               |
|  [Micro-USB OTG]   ----> Free (or optional Micro-USB to Ethernet)       |
+-------------------------------------------------------------------------+
```

### 1. Camera Input (CSI Ribbon)
- **Interface**: 22-pin to 15-pin flexible ribbon cable to CSI port.
- **Capture Stack**: Native `rpicam-vid` / `libcamera` hardware H.264 encoding via VideoCore IV ISP.
- **Resource Footprint**: Direct DMA memory transfer (<8% CPU load, ~25 MB RAM).
- **Network Protocol**: RTSP / WebRTC low-latency stream to Homelab OpenCV/MediaPipe worker via `go2rtc` (<80ms glass-to-glass latency).

### 2. Microphone Input (I2S MEMS)
- **Interface**: 3-wire digital I2S on `GPIO 18` (Bit Clock), `GPIO 19` (Word Select/WS), `GPIO 20` (Serial Data/DIN).
- **Driver**: ALSA `googlevoicehat-soundcard` or generic `i2s-mmap` overlay in `/boot/firmware/config.txt`.
- **Audio Format**: Raw 16kHz / 16-bit mono PCM stream.
- **Network Protocol**: UDP / WebSocket audio stream directly to `voice/` service on Homelab / macOS daemon.

### 3. Audio Output (Bluetooth A2DP or I2S DAC)
- **Primary (Wireless)**: Bluetooth 4.2 A2DP sink to external room soundbar/speaker.
- **Zero-Latency Alternative**: MAX98357A Mono I2S DAC wired to `GPIO 21` (DIN) sharing clock lines (`GPIO 18, 19`) with the microphone.

---

## 🏗️ System Topology & Data Flow

```mermaid
flowchart TD
    subgraph Mirror [Smart Mirror (RPi Zero 2 W)]
        CSI[CSI Wide Camera] -->|Hardware H.264| RTSP[go2rtc RTSP Streamer]
        I2S[I2S Digital Mic] -->|16kHz PCM| AUD_TX[Audio UDP Sender]
        DISP[Chromium Kiosk / #000000 2D UI] --> SCREEN[Two-Way Glass Display]
        BT[Bluetooth / I2S DAC] --> SPK[Room Speaker]
    end

    subgraph Homelab [Homelab GPU Server & Worker Fleet]
        RTSP -->|Low Latency Wi-Fi| VISION[Python MediaPipe Gesture Service]
        AUD_TX -->|Raw Audio| STT[Faster-Whisper STT Service]
        STT --> ROUTER[Hybrid Intelligence Router]
        ROUTER --> LLM[Tesla P40 / Claude / Codex]
        LLM --> TTS[Kokoro-TTS Engine]
        TTS -->|PCM Audio Stream| BT
        
        VISION --> MIRA_DAEMON[Rust Core Daemon (Protocol v2)]
        ROUTER --> MIRA_DAEMON
        MIRA_DAEMON -->|WebSocket UI State| DISP
    end
```

---

## 🎨 UI Rendering Constraints for Zero 2 W

To maintain 60 FPS fluidity and avoid Linux Out-Of-Memory (OOM) crashes on 512 MB RAM:

1. **True Black Theme**: Background strictly `#000000` (zero light emission through two-way glass).
2. **2D Lightweight Canvas / CSS**: Use optimized SVG gauges, CSS3 flex cards, and pre-rendered vector graphics rather than unconstrained 3D WebGL meshes.
3. **Optional Full 3D Streaming**: If 3D visualizers (`ai-visualizer` circuit board) are desired, render the WebGL canvas on the Homelab GPU and stream to the Zero 2 W via hardware-accelerated H.264 video decoding (`mpv` / WebRTC receiver).

---

## 📋 Configuration Overlays (`/boot/firmware/config.txt`)

```ini
# Enable CSI Camera
camera_auto_detect=1

# Enable I2S Microphone (INMP441 / SPH0645)
dtoverlay=googlevoicehat-soundcard
dtoverlay=i2s-mmap

# Audio output optimizations
dtoverlay=disable-bt-audio-overhead
```

---

## ⚖️ Architectural Trade-off Summary

| Metric | Projector + Pi 4/5 Node | Smart Mirror (Zero 2 W Thin Client) |
| :--- | :--- | :--- |
| **Room Lighting** | Needs darkened room | Works in full daylight |
| **Physical Appearance** | Requires open wall space | Concealed behind decorative mirror frame |
| **Compute Overhead** | Standalone capable (4GB RAM) | Thin client only (Homelab dependent) |
| **Power & Noise** | High wattage + cooling fan | ~1.5W silent passive operation |
