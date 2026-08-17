# Specification: Offline Circuit-Breaker & Fallback Cascade Router

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-offline-circuit-breaker-fallback-router.md`

---

## 1. Executive Summary

This specification defines the high-availability resilience layer for the Jarvis Core Daemon router. It guarantees zero system crashes and zero user-facing hanging delays when network connections fail, Homelab servers reboot, or Cloud APIs experience outages. Combining **proactive background health probes** with a **3-state circuit breaker** and **exponential backoff**, the router seamlessly cascades requests down to local hardware or native Rust command execution tables.

---

## 2. Fallback Cascade Hierarchy

The system operates with a **Local-First, Low-Cost Priority Order**. Because the Homelab Tesla P40 GPU server is always online and free of token fees, it acts as the primary execution engine. The head server running on the ceiling-mounted Raspberry Pi + Projector node executes native Rust/Go hooks as the offline fallback:

```
[ User Request (Voice / Web UI / Hand Gesture) ]
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│ Tier 1: Homelab GPU Server (Tesla P40 24GB)             │ Primary local AI (Nemotron 3.5 MoE / Ollama)
└──────────────────────────┬──────────────────────────────┘
                           │ Complexity C >= 0.80 OR P40 Offline / Circuit OPEN
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Tier 2: Cloud API Gateway (OpenRouter / Claude / Codex) │ High-reasoning & deep code generation
└──────────────────────────┬──────────────────────────────┘
                           │ Network / Internet Offline
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Tier 3: Ceiling Pi Head Server (Native Rust Daemon)     │ Local regex & system execution hooks (< 1ms)
└─────────────────────────────────────────────────────────┘
```

---

## 3. Circuit Breaker State Machine

Each upstream endpoint (OpenRouter API, Homelab Tesla P40 GPU, Laptop Worker Fleet) is monitored by an isolated **Circuit Breaker State Machine**:

```
           ┌──────────────────────────────────────┐
           │               CLOSED                 │ (Normal operation)
           └──────────┬────────────────▲──────────┘
                      │                │
     3 Consecutive    │                │ 1 Successful Probe
     Errors / Timeouts│                │
                      ▼                │
           ┌───────────────────────────┴──────────┐
           │                OPEN                  │ (Instant bypass to lower tier)
           └──────────┬───────────────────────────┘
                      │
                      │ Backoff Timer Expired (5s -> 15s -> 45s -> 120s)
                      ▼
           ┌──────────────────────────────────────┐
           │              HALF-OPEN               │ (Send 1 probe query)
           └──────────────────────────────────────┘
```

### 3.1 State Transitions
- **`CLOSED`**: All requests route normally.
- **`OPEN`**: Endpoint is flagged as UNHEALTHY. Bypasses connection attempts immediately with **0ms overhead**, redirecting queries to Tier 2/3.
- **`HALF-OPEN`**: After the exponential backoff interval, sends 1 lightweight probe query. On success, transitions back to `CLOSED`. On failure, returns to `OPEN` and multiplies backoff duration by $3\times$.

---

## 4. Proactive Health Probing Subsystem

To eliminate first-query latency penalties, the daemon background event loop executes **Proactive Health Probes**:

- **Interval**: Every 5 seconds (when endpoint is `CLOSED`).
- **Probe Endpoints**:
  - Homelab P40 GPU: `GET http://homelab-gpu.local:11434/api/tags`
  - OpenRouter API: `GET https://openrouter.ai/api/v1/models`
- **Probe Timeout**: 1,500ms.
- **State Updating**: Updates internal status table in memory (`Arc<RwLock<EndpointHealthTable>>` in Rust daemon).

---

## 5. Offline Native Command Lookup Table

If the local network is entirely disconnected and all GPU nodes are unreachable, Jarvis switches to **Offline Native Execution Mode**:

- **Engine**: Native Rust/Go regex and fuzzy string matcher (`daemon/src/offline_router.rs`).
- **Supported Capabilities**:
  - System controls (volume up/down/mute, screen brightness, lock screen).
  - App launching ("open Finder", "open Terminal", "open VS Code").
  - System status ("what is my battery level?", "check CPU usage").
  - Alarm / Timer creation ("set timer for 10 minutes").
- **Latency**: **< 1ms** execution time.

---

## 6. Implementation Strategy
1. Scaffold `daemon/src/resilience/circuit_breaker.rs` in Rust daemon.
2. Implement `EndpointHealthTable` with `tokio` async timer loops.
3. Define `SystemHealthEvent` WebSocket notification schema for UI status indicators.
