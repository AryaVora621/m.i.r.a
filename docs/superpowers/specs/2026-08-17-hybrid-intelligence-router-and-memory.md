# Specification: Hybrid Intelligence Router & Context Memory Engine

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-hybrid-intelligence-router-and-memory.md`

---

## 1. Executive Summary

Jarvis requires a dual-speed intelligence architecture: local-first execution for instant low-latency system actions and offline privacy, paired with cloud-based intelligence for high-reasoning tasks. To minimize API costs, Cloud LLM integration leverages **Claude Pro and ChatGPT/Codex Pro subscription session bridges** rather than pay-as-you-go API keys, with full fallback to the local **Homelab Tesla P40 24GB server**.

---

## 2. Intent Routing Architecture

```
                       [ Incoming User Prompt ]
                                  │
                                  ▼
                 ┌─────────────────────────────────┐
                 │ Intent Classifier / Regex Match │
                 └────────────────┬────────────────┘
                                  │
      ┌───────────────────────────┼───────────────────────────┐
      │ Deterministic / Fast      │ Local Conversational      │ Complex Reasoning
      ▼                           ▼                           ▼
┌───────────┐               ┌──────────────────┐        ┌──────────────────┐
│ Core Host │               │ Homelab GPU      │        │ Cloud LLM Bridge │
│ Handlers  │               │ Tesla P40 (24GB) │        │ Claude Pro /     │
│ Latency   │               │ Ollama / vLLM    │        │ Codex Subscriptions│
│ < 20ms    │               │ Latency < 300ms  │        └─────────┬────────┘
└───────────┘               └──────────────────┘                  │ Rate Limit
                                                                  ▼
                                                        ┌──────────────────┐
                                                        │ Fallback to      │
                                                        │ Homelab GPU      │
                                                        └──────────────────┘
```

### 2.1 Hardware-Aware Local 24GB VRAM Capacity Envelopes

To maintain long-term flexibility, local inference on the Tesla P40 (24GB VRAM) is governed strictly by **VRAM Memory Envelopes** rather than fixed model names:

| VRAM Profile | Parameter Range | Quantization | VRAM Footprint | Context Buffer | Ideal Use Case |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`FAST_STREAMING`** | 7B - 8B Dense | GGUF Q8_0 / Q4_K_M | 5GB - 8GB | 32k - 64k tokens | Low-latency voice triggers, VAD parsing ($<200\text{ms}$) |
| **`BALANCED_AGENT`** | 14B Dense | GGUF Q4_K_M | 9GB - 12GB | 16k - 32k tokens | JSON tool execution, system scripts, device control |
| **`MAX_LOCAL_REASONER`**| 30B MoE / 32B Dense | GGUF Q4_K_M | 18GB - 21GB | 4k - 8k tokens | Complex local reasoning, offline autonomy |

#### Pascal P40 Hardware Guardrails:
- Must run `GGUF` formats via `llama.cpp` using `Q4_K_M` or `Q8_0` (INT8/INT4 matrix math). FP16/BF16 is strictly prohibited due to hardware architecture limits.

### 2.2 Initial Runtime Default Model Mapping

While model targets remain dynamically reconfigurable via OpenRouter and Ollama, the system starts with the following default selections:

```yaml
router:
  defaults:
    local_primary: "nemotron-3.5-lightning" # All local tasks (Fast routing, tool calls & system coding)
    cloud_reasoning: "claude-5-sonnet"       # Escalate via Claude Pro session bridge / OpenRouter
    cloud_coding: "codex-5.6-sol"            # Escalate via Codex Pro session bridge / OpenRouter
```

---

### 2.3 OpenRouter Unified API Gateway Integration

For queries exceeding local VRAM capacity or requiring ultra-deep reasoning, the system routes requests to **OpenRouter** (`https://openrouter.ai/api/v1`):

1. **Unified Tool Calling Standard**:
   - Transmits standard JSON Schema tool definitions via `tools=[...]`.
   - OpenRouter normalizes function calls across proprietary and open-weights model backends without requiring client code changes.
2. **Dynamic Model Capability Resolution**:
   - Queries OpenRouter API (`GET /api/v1/models?supported_parameters=tools`).
   - Automatically selects targets based on capability tags (`agentic`, `context_length >= 100k`, `tool_error_rate < 0.05`).
3. **Escalation Triggers**:
   - **Trigger A (Complexity)**: Local classifier score $C \ge 0.80$.
   - **Trigger B (Context Overflow)**: Request payload exceeds local VRAM context buffer limit.
   - **Trigger C (Fallback)**: Local GPU offloading timeout or memory allocation failure.

---

### 2.3 Router Execution Matrix

| Intent Category | Classifier Criteria | Execution Target | Router Protocol | Target Latency |
| :--- | :--- | :--- | :--- | :--- |
| **`SYSTEM_CONTROL`** | Regex / Host Hook | Native Rust/Go Host | Local IPC Hook | $< 20\text{ms}$ |
| **`SMART_DEVICE`** | Device Keyword | Homelab Worker | Local MQTT Bridge | $< 100\text{ms}$ |
| **`QUICK_ASSISTANT`** | $C < 0.80$, Context $\le 16\text{k}$ | P40 `FAST_STREAMING` | Local Ollama API | $< 300\text{ms}$ |
| **`LOCAL_CODING`** | Code task, $C < 0.80$ | P40 `BALANCED_AGENT` | Local Ollama API | $< 800\text{ms}$ |
| **`COMPLEX_REASONING`** | $C \ge 0.80$ OR Context $> 32\text{k}$ | OpenRouter Gateway | OpenRouter OpenAI API | $1.0\text{s} - 3.0\text{s}$ |

---

## 3. Cloud Subscription Bridge Integration (Claude Pro & Codex)

1. **Authentication Gateway**:
   - Uses session credentials / CLI browser integration for Claude Pro and Codex subscriptions.
   - Eliminates pay-per-token API fees while giving Jarvis access to frontier models (Claude 3.5/3.7 Sonnet, OpenAI o1/o3-mini).
2. **Quota & Rate-Limit Management**:
   - The router monitors subscription message windows (e.g. 45 messages per 5 hours on Claude Pro).
   - When approaching 90% of subscription quota, the router transparently switches high-reasoning tasks to the local **Tesla P40 (24GB VRAM) Homelab server running `qwen2.5-coder:14b` or `qwen2.5:32b`**.

---

## 4. Context & Persistent Memory Architecture

Memory is split into 3 distinct tiers to balance speed, context window limits, and long-term personalization:

```
┌─────────────────────────────────────────────────────────┐
│ Tier 1: Short-Term Memory (In-Memory RAM Ring Buffer)   │
│ - Last 20 messages & current active node state          │
└────────────────────────────┬────────────────────────────┘
                             │ Async Flush
                             ▼
┌─────────────────────────────────────────────────────────┐
│ Tier 2: Relational Episodic Memory (SQLite Database)    │
│ - Full chat history, node logs, user settings & rules   │
└────────────────────────────┬────────────────────────────┘
                             │ Embeddings Index
                             ▼
┌─────────────────────────────────────────────────────────┐
│ Tier 3: Semantic Memory (LanceDB / SQLite-Vec)          │
│ - Personal preferences, long-term facts, document RAG   │
└─────────────────────────────────────────────────────────┘
```

---

## 5. Implementation Strategy (Phase 2 Roadmap)
1. Build `subscription_bridge` module supporting Claude Pro and Codex session handlers.
2. Configure local Ollama client pointing to Homelab GPU Server (`http://homelab-gpu.local:11434`).
3. Implement quota tracking and automatic failover logic to Homelab Tesla P40.
