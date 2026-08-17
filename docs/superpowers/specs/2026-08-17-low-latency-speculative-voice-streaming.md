# Specification: Low-Latency AI-Polished Voice Pipeline ("WhisperFlow" Engine)

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-low-latency-speculative-voice-streaming.md`

---

## 1. Executive Summary

This specification defines the low-latency, high-accuracy voice ingestion and speech pipeline for Jarvis. Inspired by two-pass transcription workflows (WhisperFlow/Superwhisper), the pipeline captures full spoken utterances, applies real-time local VAD, executes fast `faster-whisper` STT, and routes the raw transcript through an **AI Speech Sanitizer** to strip filler words ("um", "ah", stutters, false starts) before passing the clean prompt to Jarvis for execution and Kokoro-TTS playback.

---

## 2. Architecture & Pipeline Sequence

```
[ Ambient Mic Input ]
          │
          ▼
┌──────────────────────────────────────────┐
│ 1. Silero VAD (ONNX) Local Audio Ring    │ < 10ms frame check
└─────────────────┬────────────────────────┘
                  │ Speech Complete (Silero Silence > 350ms)
                  ▼
┌──────────────────────────────────────────┐
│ 2. Local `faster-whisper` Engine          │ INT8 Metal/CUDA, ~150ms
└─────────────────┬────────────────────────┘
                  │ Raw Transcript (includes "um", "ah", false starts)
                  ▼
┌──────────────────────────────────────────┐
│ 3. AI Speech Sanitizer & Polisher        │ Local Fast Model / Rule Filter, ~50ms
└─────────────────┬────────────────────────┘
                  │ Cleaned Intent Prompt
                  ▼
┌──────────────────────────────────────────┐
│ 4. Jarvis Core Router & Execution        │ Nemotron 3.5 / Native Hooks
└─────────────────┬────────────────────────┘
                  │ Streamed Tokens
                  ▼
┌──────────────────────────────────────────┐
│ 5. Streaming Kokoro-TTS Synthesis        │ Time-to-first-audio < 350ms total
└──────────────────────────────────────────┘
```

---

## 3. Subsystem Specifications

### 3.1 VAD & Complete Utterance Capture
- **VAD Engine**: Silero VAD (ONNX Runtime).
- **Trigger**: Dynamically starts buffering PCM audio into memory when speech is detected.
- **End-of-Utterance Boundary**: Triggered when silence exceeds **350ms** (adaptive window based on pitch cadence).

### 3.2 Fast Local Transcription (`faster-whisper`)
- **Engine**: `faster-whisper` (CTranslate2 backend) running `whisper-small.en` or `whisper-base.en` in INT8 mode.
- **Latency**: Transcribes 5-second audio clip in **< 150ms** on Apple Silicon / Tesla P40.

### 3.3 Two-Pass AI Speech Sanitizer ("WhisperFlow" Filter)
- **Objective**: Converts raw human verbal speech into deterministic, clean AI instruction prompts.
- **Operations**:
  1. Strips verbal disfluencies (`"um"`, `"uh"`, `"like"`, `"you know"`).
  2. Resolves self-corrections (e.g. `"Set timer for 5... no wait 10 minutes"` $\rightarrow$ `"Set timer for 10 minutes"`).
  3. Formats punctuation and capitalizes domain entity names (e.g., node names, smart home light groups).

### 3.4 Overlapped Streaming TTS Synthesis (Kokoro-TTS)
- **Engine**: Kokoro-TTS (82M ONNX model).
- **Pipeline Parallelism**: Begins rendering audio PCM buffers on the first **5 generated tokens** from Jarvis router.
- **Total Latency Budget**: From speech finish to first audio speaker output: **< 350ms**.

---

## 4. IPC Schemas (`shared/schemas/voice.json`)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "VoiceUtteranceEvent",
  "type": "object",
  "properties": {
    "node_id": { "type": "string" },
    "raw_transcript": { "type": "string" },
    "cleaned_prompt": { "type": "string" },
    "sanitizer_latency_ms": { "type": "number" },
    "total_pipeline_latency_ms": { "type": "number" }
  },
  "required": ["node_id", "raw_transcript", "cleaned_prompt"]
}
```

---

## 5. Implementation Steps
1. Create `vision/voice_sanitizer.py` microservice with `faster-whisper` + Silero VAD.
2. Implement regex/light LLM disfluency stripper module.
3. Wire WebSocket output stream to `daemon/` event router.
