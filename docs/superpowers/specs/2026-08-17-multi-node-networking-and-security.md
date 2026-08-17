# Specification: Multi-Node Networking, Discovery & Security Protocol

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-multi-node-networking-and-security.md`

---

## 1. Executive Summary

Jarvis operates across a distributed local network of devices (macOS PC, Raspberry Pi + Projector, Mobile Web app, and Homelab infrastructure). This specification outlines zero-configuration node discovery, authenticated pairing, resilient WebSocket RPC channels, and security permission scopes.

---

## 2. Node Discovery Protocol (mDNS / Bonjour)

### 2.1 Daemon Broadcast
The Core Daemon advertises its presence on the local network using mDNS (Multicast DNS):
- **Service Type**: `_jarvis-core._tcp.local.`
- **Port**: `8080` (WebSocket RPC) / `50051` (gRPC optional)
- **TXT Record Payload**:
  ```json
  {
    "version": "1.0.0",
    "hostname": "jarvis-macbook-pro.local",
    "status": "ONLINE",
    "auth_required": "true"
  }
  ```

### 2.2 Client Node Auto-Join Flow
```
[ Node Client (Pi / Mobile / PC) ]
               │
               ▼ 1. mDNS Query (_jarvis-core._tcp.local)
[ Core Daemon Discovered: 192.168.1.150:8080 ]
               │
               ▼ 2. Handshake Request with Auth Token
[ Core Daemon Validates Node Registration ]
               │
               ▼ 3. Persistent WebSocket Connection Established
[ Real-Time Bi-Directional Event Stream ]
```

---

## 3. Node Security & Permission Scopes

Every node must register with an authenticated **Node Token** issued by the main daemon. Nodes are assigned granular permission scopes to prevent compromised endpoints (e.g. outdoor Pi or webcam) from executing sensitive host commands.

| Permission Tier | Allowed Actions | Example Nodes |
| :--- | :--- | :--- |
| **`ADMIN`** | System execution, shell commands, file access, security config | Main macOS PC |
| **`PERIPHERAL_CONTROLLER`** | Audio playback, display control, projector power, smart home | Raspberry Pi Projector |
| **`SENSOR_PUBLISHER`** | Send gesture packets, send VAD/audio chunks | Webcam Pi Node |
| **`VIEWER_UI`** | View ambient dashboard state, send user chat prompts | Mobile PWA / Browser |

---

## 4. Connection Lifecycle & Health Monitoring

### 4.1 Heartbeat Mechanism
- **Interval**: 5 seconds.
- **Payload**: Ping/Pong containing CPU load, memory usage, and battery/thermal status.
- **Timeout**: If no heartbeat is received for 15 seconds, daemon marks node status as `DISCONNECTED` and updates ambient dashboard.

### 4.2 Offline Event Queueing & Reconnection
- Nodes maintain a localized circular buffer (up to 100 events) while disconnected.
- Upon reconnection, non-time-sensitive commands (such as telemetry or queued logs) are synchronized to the core daemon.

---

## 5. Implementation Strategy (Phase 1 Roadmap)
1. Add `mdns` / `zeroconf` discovery module to `daemon/`.
2. Implement token authentication middleware in WebSocket gateway.
3. Define `NodeRegistrationRequest` and `HeartbeatPacket` JSON schemas under `shared/schemas/`.
