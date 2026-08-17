# Specification: Multi-Node SSH Automation Bridge & Execution Protocol

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-macos-command-sandbox-permission-guard.md`

---

## 1. Executive Summary

This specification defines the multi-node remote execution architecture connecting the Ceiling Raspberry Pi + Projector head node, the macOS PC node, and the Homelab Tesla P40 GPU desktop. To allow seamless, unrestricted device control across the local network, the system uses an **Automated SSH Execution Bridge** backed by key-based authentication and `sshpass` fallback credentials. All node-to-node remote commands execute with full administrative privileges and are logged to an append-only audit trail.

---

## 2. Network Topology & Remote SSH Control Map

```
┌────────────────────────────────────────────────────────┐
│ Ceiling Raspberry Pi + Projector (Head Server Node)    │
│ - Runs core daemon, voice pipeline, gesture tracker    │
└──────────────────────────┬─────────────────────────────┘
                           │
             ┌─────────────┴─────────────┐
             │ SSH Automated Key Bridge  │ (ed25519 / sshpass)
             ▼                           ▼
┌───────────────────────────┐ ┌───────────────────────────┐
│ macOS PC Node             │ │ Tesla P40 GPU Desktop     │
│ - AppleScript GUI control │ │ - Ollama LLM Inference    │
│ - System volume / display │ │ - CUDA / PyTorch tasks    │
│ - Dev environment tools   │ │ - Docker container fleet  │
└───────────────────────────┘ └───────────────────────────┘
```

---

## 3. SSH Execution Protocol & Security Setup

### 3.1 Authentication Setup
1. **Primary Auth**: Ed25519 SSH Key Pair (`~/.ssh/id_jarvis_ed25519`) generated on the ceiling Raspberry Pi and deployed to `~/.ssh/authorized_keys` on macOS PC and P40 Desktop.
2. **Fallback Auth**: Automated `sshpass` wrapper using encrypted environment credentials (`JARVIS_SSH_SECRET`).
3. **Keep-Alive**: Persistent SSH connection pooling (`ControlMaster` / `ControlPath`) to eliminate SSH handshake overhead, reducing remote execution latency to **< 15ms**.

### 3.2 Command Dispatcher Matrix

| Target Node | Command Type | Execution Method | Example Remote Command |
| :--- | :--- | :--- | :--- |
| **macOS PC** | System Control | `ssh user@mac-node` | `osascript -e "set volume output volume 80"` |
| **macOS PC** | App Launch | `ssh user@mac-node` | `open -a "Visual Studio Code"` |
| **macOS PC** | Dev Workflow | `ssh user@mac-node` | `cd ~/personal\ projects/jarvis && git status` |
| **P40 Desktop** | Ollama Control | `ssh user@p40-node` | `ollama run nemotron-3.5-lightning "..."` |
| **P40 Desktop** | GPU Monitoring | `ssh user@p40-node` | `nvidia-smi --query-gpu=utilization.gpu --format=csv` |

---

## 4. Command Audit Trail (`nodes/logs/command_audit.log`)

To maintain complete transparency over all remote commands executed across nodes, the Core Daemon writes every dispatch to an append-only JSON Lines log file:

```json
{
  "timestamp": "2026-08-17T11:42:00Z",
  "source_node": "rpi-ceiling-head",
  "target_node": "macos-pc",
  "command": "osascript -e 'set volume output volume 80'",
  "execution_status": "SUCCESS",
  "latency_ms": 12.4
}
```

---

## 5. Implementation Strategy
1. Add `daemon/src/bridge/ssh_executor.rs` supporting `ssh` connection pooling.
2. Implement credential loader for `~/.ssh/id_jarvis_ed25519` and `sshpass` fallback.
3. Scaffold remote macOS AppleScript bridge and P40 GPU monitoring hooks.
