# Specification: Homelab Hardware Cluster Architecture & Distributed Workload Offloading

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-homelab-cluster-architecture.md`

---

## 1. Executive Summary

To minimize subscription costs for cloud APIs (Claude Pro / Codex) and deliver ultra-fast response times, Jarvis leverages a dedicated multi-machine Homelab cluster. 

The cluster is anchored by a high-capacity GPU server (Tesla P40 24GB + GTX 1050) for local LLM inference, supported by a fleet of lightweight worker laptops (i3/i5, 8GB RAM each) running specialized microservices (STT, TTS, vision, background tasks, web scraping, and MCP runners).

---

## 2. Homelab Hardware Inventory & Role Allocation

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      HOMELAB HARDWARE CLUSTER                           │
├──────────────────────────────────────┬──────────────────────────────────┤
│ Main GPU Server                      │ Laptop Worker Fleet (3-8 Nodes)  │
│ - 32 GB System RAM                   │ - Intel i3/i5 (6th/7th Gen)      │
│ - NVIDIA Tesla P40 (24 GB VRAM)      │ - 8 GB RAM each                  │
│ - NVIDIA GTX 1050 (4 GB VRAM)        │ - Distributed Microservices      │
└──────────────────┬───────────────────┴────────────────┬─────────────────┘
                   │                                    │
                   ▼                                    ▼
       ┌───────────────────────┐            ┌───────────────────────┐
       │ Primary Local Models  │            │ Specialized Workers   │
       │ (Ollama / vLLM / llama│            │ - STT (Whisper) Node  │
       │  .cpp: Qwen 2.5 14B/  │            │ - TTS (Kokoro) Node   │
       │  Llama 3.1 8B/32B)    │            │ - Web Scraper / MCP   │
       └───────────────────────┘            └───────────────────────┘
```

### 2.1 Main GPU Server (Primary LLM & Embedding Engine)
- **CPU & Memory**: 32 GB RAM.
- **Primary Inference GPU**: **NVIDIA Tesla P40 (24 GB VRAM)**.
  - *VRAM Capacity*: Fits 14B to 32B quantized parameters completely in VRAM for ultra-fast tok/s inference.
  - *Recommended Models*:
    - `qwen2.5-coder:14b-instruct-q4_K_M` (for fast local coding & function calling).
    - `llama3.1:8b-instruct-fp16` or `qwen2.5:32b-instruct-q4_K_M` (for general local conversation & intent routing).
    - `bge-m3` or `all-MiniLM-L6-v2` (for local vector embeddings).
- **Secondary Display/Aux GPU**: **NVIDIA GTX 1050 (4 GB VRAM)**.
  - Offloads lightweight embedding generation or host display rendering.

### 2.2 Laptop Worker Fleet (3–8 Nodes: i3/i5, 8GB RAM each)
Instead of sitting idle, these laptops are organized into a distributed Docker / Kubernetes / HashiCorp Nomad worker cluster running dedicated microservices:

1. **Worker Node Alpha (Audio STT Engine)**: Dedicated `faster-whisper` container offloading Speech-to-Text for the entire network.
2. **Worker Node Beta (Audio TTS Engine)**: Dedicated `Kokoro-TTS` / `Piper` container serving low-latency voice synthesis.
3. **Worker Node Gamma (Web Scraping & RAG Indexer)**: Background crawler parsing user-requested URLs, documentation, and RSS feeds into vector memory.
4. **Worker Nodes Delta+ (MCP & Task Executors)**: Runs isolated Model Context Protocol (MCP) server runners, containerized builds, and scheduled cron jobs.

---

## 3. Cost-Optimization & Intelligence Tiering

```
                          ┌───────────────────────────┐
                          │ Incoming User Request     │
                          └─────────────┬─────────────┘
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │ Intent Classifier Router  │
                          └─────────────┬─────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             │                          │                          │
             ▼                          ▼                          ▼
┌──────────────────────────┐┌──────────────────────────┐┌──────────────────────────┐
│ Tier 1: Local System     ││ Tier 2: Homelab Server   ││ Tier 3: Cloud APIs       │
│ Direct Host Action       ││ Tesla P40 (24GB VRAM)    ││ Claude Pro / Codex       │
│ Latency: < 20ms          ││ Latency: < 300ms         ││ Latency: 1.5s - 4s       │
│ Cost: $0                 ││ Cost: $0                 ││ Cost: Subscription Meter │
└──────────────────────────┘└──────────────────────────┘└──────────────────────────┘
```

1. **Tier 1 (Free Local Execution)**: Simple system commands (volume, brightness, display switching, playback) executed directly by Rust/Go daemon host logic.
2. **Tier 2 (Free Homelab Local LLM)**: 85-90% of all user queries (intent parsing, daily Q&A, ambient updates, basic code generation, memory retrieval) routed to the **Homelab Tesla P40 server** running Ollama/vLLM.
3. **Tier 3 (Cloud Premium Reasoning)**: High-complexity tasks (architectural designs, heavy debugging, creative writing, multi-file code refactoring) routed to **Claude Pro API / Codex API** to stay well within daily subscription limits.

---

## 4. Implementation Strategy for Homelab Setup

1. **Ollama / vLLM Optimization on Tesla P40**:
   - Enable `CUDA_VISIBLE_DEVICES` pointing to Tesla P40.
   - Configure model quantization (Q4_K_M or Q5_K_M) for 14B–32B models to fit within 24GB VRAM.
2. **Microservices Deployment on Laptop Fleet**:
   - Containerize `stt-whisper-service` (Docker container running `faster-whisper`).
   - Containerize `tts-kokoro-service` (Docker container running Kokoro ONNX).
   - Expose endpoints via local DNS / mDNS (`stt.homelab.local`, `tts.homelab.local`).
