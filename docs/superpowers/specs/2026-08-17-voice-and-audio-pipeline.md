# Specification: Voice & Audio Pipeline Architecture

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-voice-and-audio-pipeline.md`

---

## 1. Executive Summary

The Voice & Audio Pipeline provides low-latency, hands-free voice interaction across all Jarvis nodes (macOS PC, Raspberry Pi, Mobile, and ambient room microphones). It uses open-source, local-first audio models to ensure instant responsiveness without sending raw ambient microphone audio to the cloud.

---

## 2. Architecture & Pipeline Diagram

```
[ Room Mic / Node Audio Input ]
              │
              ▼ (Audio Stream / Opus)
┌─────────────────────────────────────────┐
│ Voice Activity Detection (Silero VAD)   │
└─────────────────────┬───────────────────┘
                      │ Speech Detected
                      ▼
┌─────────────────────────────────────────┐
│ Local Wake Word Engine (OpenWakeWord)   │
└─────────────────────┬───────────────────┘
                      │ Trigger: "Jarvis"
                      ▼
┌─────────────────────────────────────────┐
│ Local STT Engine (faster-whisper)       │
└─────────────────────┬───────────────────┘
                      │ Text Transcript
                      ▼
┌─────────────────────────────────────────┐
│ Core Daemon / Intelligence Router       │
└─────────────────────┬───────────────────┘
                      │ Response Text
                      ▼
┌─────────────────────────────────────────┐
│ TTS Engine (Kokoro-TTS / Piper Local)   │
└─────────────────────┬───────────────────┘
                      │ Audio Stream
                      ▼
[ Target Node Speaker (Pi / PC / Mobile)  ]
```

---

## 3. Key Components & Technology Choices

### 3.1 Voice Activity Detection (VAD)
- **Engine**: Silero VAD (ONNX runtime).
- **Latency**: < 10ms frame processing.
- **Function**: Constantly filters ambient background silence/noise so wake word and STT run only when human speech is present.

### 3.2 Wake Word Engine
- **Engine**: OpenWakeWord / Rust bindings (`openwakeword-rs`).
- **Trigger Phrase**: `"Jarvis"` (with configurable secondary triggers like `"Computer"`).
- **Execution Location**: Runs locally on individual node clients (macOS menu bar app, Pi client) to prevent constant audio streaming to daemon.

### 3.3 Speech-to-Text (STT)
- **Local Engine**: `faster-whisper` (Python) or `whisper.cpp` (Rust/C++ binding).
- **Model Size**: `whisper-base.en` or `whisper-small.en` quantized to INT8 for GPU/NPU or Apple Silicon Metal acceleration.
- **Streaming Mode**: Real-time chunked transcription using VAD boundaries.
- **Latency Budget**: < 300ms from speech finish to text output.

### 3.4 Text-to-Speech (TTS)
- **Primary (Local & Fast)**: **Kokoro-TTS** (82M parameter lightweight ONNX model producing studio-quality natural voice) or **Piper TTS**.
- **Secondary (Cloud High-Quality)**: **ElevenLabs API** fallback for long story generation, podcasts, or detailed news summaries.
- **Streaming Playback**: Audio chunks streamed via WebSockets as PCM/Opus audio buffers to minimize time-to-first-audio (TTFA < 250ms).

---

## 4. Multi-Node Audio Routing

1. **Spatial Audio Target**: When a wake word is triggered on Node $A$ (e.g. Raspberry Pi in projector room), the response TTS audio defaults to playing back on Node $A$'s speaker.
2. **Follow-Me Audio**: If user moves to macOS PC, Jarvis detects session activity on PC and routes audio output seamlessly to PC headphones/speakers.
3. **Mute & Privacy Controls**: Hardware or web UI software toggle for global/per-node microphone mute.

---

## 5. Implementation Strategy (Phase 2 Roadmap)
1. Scaffold Python audio daemon module in `vision/` or dedicated `audio/` microservice using PyAudio & Silero VAD.
2. Integrate `faster-whisper` streaming interface.
3. Integrate `Kokoro-TTS` model for fast audio synthesis.
4. Define `AudioStreamPacket` and `TTSPlaybackRequest` JSON schemas in `shared/schemas/`.
