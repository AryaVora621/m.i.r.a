# M.I.R.A. Protocol v2 & Gateway Guardrail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Task numbering continues from Task 1 (`aa8bc51`, shared IPC schemas v1) — this plan starts at Task 2.

**Goal:** Extend the shared contracts to protocol v2 (`MiraEnvelopeV2`, `CapabilityRequest`, `ActionManifest`, `CapabilityReceipt`) and stand up the Rust gateway (node registry + WebSocket broker) and a separately-run guardrail process (deny-by-default policy engine + signed capability receipts), proven end-to-end against simulated WebSocket nodes — before any daemon code is written against an underspecified wire protocol.

**Architecture:** A Rust Cargo workspace with three crates: `mira-protocol` (serde structs mirroring the JSON Schema/Pydantic/TypeScript contracts), `daemon` (the Axum WebSocket gateway: node registry, topic broadcast, capability-request routing), and `guardrail` (a separate binary reachable only over a local Unix Domain Socket, holding the deny-by-default policy table and the dev signing key). The daemon never evaluates policy itself — every `CapabilityRequest` crosses the process boundary to the guardrail and comes back as a signed `CapabilityReceipt`.

**Tech Stack:** Rust (Tokio, Axum, Serde, `ed25519-dalek`), Python (Pydantic v2, `jsonschema`), TypeScript (compile-time-only via `tsc --noEmit`, no runtime dependency added yet).

## Global Constraints

- Continue on `feat/macbook-offline-core-sim` and preserve all existing uncommitted work and the Task 1 commit `aa8bc51`.
- No raw SSH command bridge, `sshpass`, unrestricted administrator access, or arbitrary model-generated JavaScript.
- The guardrail's policy table, signing key, and audit trail are untouchable by the daemon and by any capability handler — the daemon can only ask the guardrail a question and relay the signed answer.
- Classify every capability as `REVERSIBLE`, `IRREVERSIBLE`, or `UNKNOWN`; only `REVERSIBLE` capabilities with a non-null rollback plan on file may be allowed. `IRREVERSIBLE` and `UNKNOWN` are denied by default in this plan (no human-approval escalation path exists yet — that is a later plan).
- **Phase 1 trust boundary (decision made by this plan, supersedes the open question in the superseded `PLAN.md` audit):** the gateway binds `127.0.0.1` only. Registration may use `auth_mode: LOOPBACK_TRUSTED` on a loopback-bound listener; the gateway rejects `LOOPBACK_TRUSTED` on any non-loopback bind (checked at register time, enforced in code even though only loopback binding exists today). `auth_mode: MTLS` is defined in the schema for forward compatibility with the P40/LAN plan but is not implemented by this plan — a daemon that receives an `MTLS` registration returns `ERROR` and closes the connection.
- Pin dependency versions in every `Cargo.toml` (no bare `"*"`); MSRV for the workspace is Rust 1.75 (`rust-version.workspace = true`). If a pinned crate version has been yanked by the time this plan executes, the implementer may bump the patch/minor version and note the change in the task report — that is not a plan deviation.
- The guardrail's Ed25519 signing key is generated on first run and persisted to a local file for developer convenience. This is explicitly a **dev-only** convenience, not the "root-owned signed binary" the canonical plan specifies for production — production key custody is scoped to the P40/production-hardening plan (Implementation Sequence step 6 of the canonical plan). Say so in code comments only where the "why" is non-obvious (the key-reload path), not as a wall of caveats.
- Exact model targets (`Qwen3.8-27B`, `NVIDIA Nemotron 3.5 Lightning 30B-A3B`) and the P40 host are out of scope for this plan entirely.

## Explicitly Out of Scope (deferred to later, focused plans per `TASK_QUEUE.md`)

- Voice loop, STT/TTS, memory (PostgreSQL/pgvector), UI/Tauri, LangGraph Agent OS — separate subplans.
- `nodes/simulator/` as a standing hardware-mock framework (ESP32 wand, Pi projector, 3D printers, P40 telemetry) — this plan's "simulated nodes" are in-process WebSocket test clients inside the Rust integration tests, nothing more.
- mTLS implementation, Tailscale, and any LAN/remote binding — loopback only.
- A real macOS volume-control handler — the guardrail's default policy table declares `mac.volume.set` as an allowed capability and the integration test exercises the full request/response path, but no AppleScript/CoreAudio call is made; `CapabilityReceipt.rollback_evidence` is populated from the manifest's static rollback plan text, not a live system read. Wiring a real handler is Implementation Sequence step 3 of the canonical plan (the Mac voice loop's "one safe typed Mac capability").
- Surfacing a human-readable denial *reason* to callers — `CapabilityReceipt` has no reason field (see Task 6). The guardrail logs the reason via `tracing`; a future UI/voice plan can add a reason field if product needs it.
- Broadcast-channel lag/overflow behavior under load — the code path exists (`RecvError::Lagged` is handled), but no test forces an overflow. Flag this as a known gap, not a silent skip.
- The wand's separate `/ws/controller` route mentioned in the earlier (superseded) Task 2 brief — that belongs to the ESP32/hardware-nodes plan.

---

### Task 2: Protocol v2 Shared Contracts (JSON Schema, Pydantic, TypeScript)

**Files:**
- Create: `shared/schemas/mira_envelope_v2.json`
- Create: `shared/schemas/capability_request.json`
- Create: `shared/schemas/action_manifest.json`
- Create: `shared/schemas/capability_receipt.json`
- Create: `shared/fixtures/mira_envelope_v2.json`
- Create: `shared/fixtures/capability_request.json`
- Create: `shared/fixtures/action_manifest.json`
- Create: `shared/fixtures/capability_receipt.json`
- Modify: `shared/types/events.py` (append new Pydantic models)
- Modify: `shared/types/index.ts` (append new TypeScript types)
- Create: `shared/tsconfig.json`
- Create: `shared/package.json`
- Create: `shared/types/fixtures.ts`
- Modify: `shared/tests/test_schemas.py` (append new test methods + fixture loader)

**Interfaces:**
- Consumes: nothing new (extends Task 1's `shared/schemas/`, `shared/types/`, `shared/tests/` conventions — draft-07 JSON Schema, Pydantic v2 with `ConfigDict(extra="forbid")`, TS interfaces).
- Produces: Python symbols `EventKind`, `AuthMode`, `AuthContext`, `MiraEnvelopeV2`, `Reversibility`, `CapabilityRequest`, `ActionManifest`, `CapabilityResultStatus`, `CapabilityReceipt` from `shared.types.events`. TypeScript types of the same names from `shared/types/index.ts`. Fixture files under `shared/fixtures/*.json` that Task 3's Rust crate will also deserialize — do not change their field names or types after this task without updating all three languages plus the Rust crate.

- [ ] **Step 1: Write the four new JSON Schemas**

`shared/schemas/mira_envelope_v2.json`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MiraEnvelopeV2",
  "description": "Universal protocol v2 envelope for every message routed through the M.I.R.A. gateway.",
  "type": "object",
  "properties": {
    "protocol_version": { "type": "integer", "const": 2 },
    "event_id": { "type": "string", "minLength": 1 },
    "trace_id": { "type": "string", "minLength": 1 },
    "topic": { "type": "string", "minLength": 1 },
    "event_kind": {
      "type": "string",
      "enum": ["EVENT", "COMMAND", "CAPABILITY_REQUEST", "CAPABILITY_RECEIPT", "ACK", "ERROR", "REGISTER", "REGISTER_ACK", "DISCONNECT"]
    },
    "sender_node": { "type": "string", "minLength": 1 },
    "timestamp": { "type": "number" },
    "auth_context": {
      "type": "object",
      "properties": {
        "node_id": { "type": "string", "minLength": 1 },
        "auth_mode": { "type": "string", "enum": ["LOOPBACK_TRUSTED", "MTLS"] },
        "token": { "type": ["string", "null"] },
        "cert_fingerprint": { "type": ["string", "null"] }
      },
      "required": ["node_id", "auth_mode", "token", "cert_fingerprint"],
      "additionalProperties": false
    },
    "payload": {},
    "ack_ref": { "type": ["string", "null"] }
  },
  "required": ["protocol_version", "event_id", "trace_id", "topic", "event_kind", "sender_node", "timestamp", "auth_context", "payload", "ack_ref"],
  "additionalProperties": false
}
```

`shared/schemas/capability_request.json`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CapabilityRequest",
  "description": "A typed request asking the guardrail to authorize and dispatch a node capability.",
  "type": "object",
  "properties": {
    "request_id": { "type": "string", "minLength": 1 },
    "trace_id": { "type": "string", "minLength": 1 },
    "sender_node": { "type": "string", "minLength": 1 },
    "capability": { "type": "string", "minLength": 1 },
    "arguments": { "type": "object" },
    "reversibility": { "type": "string", "enum": ["REVERSIBLE", "IRREVERSIBLE", "UNKNOWN"] },
    "requested_at": { "type": "number" }
  },
  "required": ["request_id", "trace_id", "sender_node", "capability", "arguments", "reversibility", "requested_at"],
  "additionalProperties": false
}
```

`shared/schemas/action_manifest.json`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ActionManifest",
  "description": "Static policy declaration for a registered capability, evaluated by the guardrail before dispatch.",
  "type": "object",
  "properties": {
    "capability": { "type": "string", "minLength": 1 },
    "reversibility": { "type": "string", "enum": ["REVERSIBLE", "IRREVERSIBLE", "UNKNOWN"] },
    "rollback_plan": { "type": ["string", "null"] },
    "credential_scope": { "type": "array", "items": { "type": "string" } },
    "risk_notes": { "type": "string" }
  },
  "required": ["capability", "reversibility", "rollback_plan", "credential_scope", "risk_notes"],
  "additionalProperties": false
}
```

`shared/schemas/capability_receipt.json`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CapabilityReceipt",
  "description": "Signed evidence of a capability's execution outcome, produced by the guardrail.",
  "type": "object",
  "properties": {
    "request_id": { "type": "string", "minLength": 1 },
    "capability": { "type": "string", "minLength": 1 },
    "result_status": { "type": "string", "enum": ["SUCCESS", "FAILURE", "DENIED", "ROLLED_BACK"] },
    "rollback_evidence": { "type": ["string", "null"] },
    "executed_at": { "type": "number" },
    "executed_by": { "type": "string", "minLength": 1 },
    "signature": { "type": ["string", "null"] }
  },
  "required": ["request_id", "capability", "result_status", "rollback_evidence", "executed_at", "executed_by", "signature"],
  "additionalProperties": false
}
```

- [ ] **Step 2: Write the four canonical fixtures shared across languages**

`shared/fixtures/mira_envelope_v2.json`:

```json
{
  "protocol_version": 2,
  "event_id": "evt-9f2b3c1a-0000-4000-8000-000000000001",
  "trace_id": "trace-9f2b3c1a-0000-4000-8000-000000000001",
  "topic": "capability",
  "event_kind": "CAPABILITY_REQUEST",
  "sender_node": "mac-host",
  "timestamp": 1755600000.0,
  "auth_context": {
    "node_id": "mac-host",
    "auth_mode": "LOOPBACK_TRUSTED",
    "token": null,
    "cert_fingerprint": null
  },
  "payload": { "request_id": "req-0000", "capability": "mac.volume.set" },
  "ack_ref": null
}
```

`shared/fixtures/capability_request.json`:

```json
{
  "request_id": "req-1111-2222-3333-4444",
  "trace_id": "trace-9f2b3c1a-0000-4000-8000-000000000001",
  "sender_node": "mac-host",
  "capability": "mac.volume.set",
  "arguments": { "level": 40 },
  "reversibility": "REVERSIBLE",
  "requested_at": 1755600000.0
}
```

`shared/fixtures/action_manifest.json`:

```json
{
  "capability": "mac.volume.set",
  "reversibility": "REVERSIBLE",
  "rollback_plan": "Restore the previous system output volume level captured before this action executed.",
  "credential_scope": ["mac.audio"],
  "risk_notes": "Adjusts system output volume only; no data loss or irreversible side effects."
}
```

`shared/fixtures/capability_receipt.json`:

```json
{
  "request_id": "req-1111-2222-3333-4444",
  "capability": "mac.volume.set",
  "result_status": "SUCCESS",
  "rollback_evidence": "previous_level=55",
  "executed_at": 1755600001.5,
  "executed_by": "guardrail-dev",
  "signature": "ZGV2LXNpZ25hdHVyZS1wbGFjZWhvbGRlcg=="
}
```

- [ ] **Step 3: Append the new Pydantic models to `shared/types/events.py`**

Append after the existing `MiraEventEnvelope` class:

```python
class EventKind(str, Enum):
    EVENT = "EVENT"
    COMMAND = "COMMAND"
    CAPABILITY_REQUEST = "CAPABILITY_REQUEST"
    CAPABILITY_RECEIPT = "CAPABILITY_RECEIPT"
    ACK = "ACK"
    ERROR = "ERROR"
    REGISTER = "REGISTER"
    REGISTER_ACK = "REGISTER_ACK"
    DISCONNECT = "DISCONNECT"


class AuthMode(str, Enum):
    LOOPBACK_TRUSTED = "LOOPBACK_TRUSTED"
    MTLS = "MTLS"


class AuthContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    auth_mode: AuthMode
    token: Optional[str] = None
    cert_fingerprint: Optional[str] = None


class MiraEnvelopeV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal[2]
    event_id: str
    trace_id: str
    topic: str
    event_kind: EventKind
    sender_node: str
    timestamp: float
    auth_context: AuthContext
    payload: Any
    ack_ref: Optional[str] = None


class Reversibility(str, Enum):
    REVERSIBLE = "REVERSIBLE"
    IRREVERSIBLE = "IRREVERSIBLE"
    UNKNOWN = "UNKNOWN"


class CapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    trace_id: str
    sender_node: str
    capability: str
    arguments: Dict[str, Any]
    reversibility: Reversibility
    requested_at: float


class ActionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str
    reversibility: Reversibility
    rollback_plan: Optional[str] = None
    credential_scope: List[str]
    risk_notes: str


class CapabilityResultStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"
    ROLLED_BACK = "ROLLED_BACK"


class CapabilityReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    capability: str
    result_status: CapabilityResultStatus
    rollback_evidence: Optional[str] = None
    executed_at: float
    executed_by: str
    signature: Optional[str] = None
```

- [ ] **Step 4: Append the new TypeScript types to `shared/types/index.ts`**

Append after the existing `MiraEventEnvelope` interface:

```typescript
export type EventKind =
  | 'EVENT'
  | 'COMMAND'
  | 'CAPABILITY_REQUEST'
  | 'CAPABILITY_RECEIPT'
  | 'ACK'
  | 'ERROR'
  | 'REGISTER'
  | 'REGISTER_ACK'
  | 'DISCONNECT';

export type AuthMode = 'LOOPBACK_TRUSTED' | 'MTLS';

export interface AuthContext {
  node_id: string;
  auth_mode: AuthMode;
  token: string | null;
  cert_fingerprint: string | null;
}

export interface MiraEnvelopeV2<T = unknown> {
  protocol_version: number;
  event_id: string;
  trace_id: string;
  topic: string;
  event_kind: EventKind;
  sender_node: string;
  timestamp: number;
  auth_context: AuthContext;
  payload: T;
  ack_ref: string | null;
}

export type Reversibility = 'REVERSIBLE' | 'IRREVERSIBLE' | 'UNKNOWN';

export interface CapabilityRequest {
  request_id: string;
  trace_id: string;
  sender_node: string;
  capability: string;
  arguments: Record<string, unknown>;
  reversibility: Reversibility;
  requested_at: number;
}

export interface ActionManifest {
  capability: string;
  reversibility: Reversibility;
  rollback_plan: string | null;
  credential_scope: string[];
  risk_notes: string;
}

export type CapabilityResultStatus = 'SUCCESS' | 'FAILURE' | 'DENIED' | 'ROLLED_BACK';

export interface CapabilityReceipt {
  request_id: string;
  capability: string;
  result_status: CapabilityResultStatus;
  rollback_evidence: string | null;
  executed_at: number;
  executed_by: string;
  signature: string | null;
}
```

- [ ] **Step 5: Add the TypeScript compile-time fixture check**

`shared/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ES2022",
    "moduleResolution": "bundler",
    "strict": true,
    "resolveJsonModule": true,
    "esModuleInterop": true,
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["types/**/*.ts"]
}
```

`shared/package.json`:

```json
{
  "name": "@mira/shared-types",
  "private": true,
  "version": "0.1.0",
  "scripts": {
    "test": "tsc --noEmit -p tsconfig.json"
  },
  "devDependencies": {
    "typescript": "5.6.3"
  }
}
```

`shared/types/fixtures.ts`:

```typescript
import mira_envelope_v2 from '../fixtures/mira_envelope_v2.json';
import capability_request from '../fixtures/capability_request.json';
import action_manifest from '../fixtures/action_manifest.json';
import capability_receipt from '../fixtures/capability_receipt.json';
import type { MiraEnvelopeV2, CapabilityRequest, ActionManifest, CapabilityReceipt } from './index';

const envelope: MiraEnvelopeV2 = mira_envelope_v2;
const request: CapabilityRequest = capability_request;
const manifest: ActionManifest = action_manifest;
const receipt: CapabilityReceipt = capability_receipt;

void envelope;
void request;
void manifest;
void receipt;
```

- [ ] **Step 6: Run the TypeScript compile check**

Run: `cd shared && npm install && npm test`
Expected: exits 0 with no `tsc` errors. If any fixture literal doesn't structurally match its interface, `tsc` will fail here — fix the mismatch before moving on.

- [ ] **Step 7: Extend `shared/tests/test_schemas.py` with fixture-backed tests**

Add these imports to the existing import block:

```python
from shared.types.events import (
    ActionManifest,
    AuthContext,
    AuthMode,
    CalibrationEvent,
    CalibrationStage,
    CapabilityReceipt,
    CapabilityRequest,
    CapabilityResultStatus,
    Coordinates3D,
    EventKind,
    GestureEvent,
    GestureType,
    LyricLine,
    MediaEvent,
    MiraEnvelopeV2,
    NodeCommand,
    Point2D,
    Reversibility,
    SpeechEvent,
    SpeechState,
    TelemetryEvent,
    TelemetryMetrics,
    TelemetryStatus,
    WandButton,
    WandEvent,
    WandEventType,
)
```

Update `test_schema_files_exist`'s `expected_schemas` set to also include `"mira_envelope_v2.json"`, `"capability_request.json"`, `"action_manifest.json"`, `"capability_receipt.json"`.

Add a fixture loader to `setUpClass` (alongside the existing `cls.schema_dir` block):

```python
        cls.fixture_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"
        )

    @classmethod
    def _load_fixture(cls, filename):
        with open(os.path.join(cls.fixture_dir, filename), "r", encoding="utf-8") as f:
            return json.load(f)
```

Add these test methods:

```python
    def test_mira_envelope_v2(self):
        schema = self.schemas["mira_envelope_v2.json"]
        payload = self._load_fixture("mira_envelope_v2.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = MiraEnvelopeV2(**payload)
        self.assertEqual(model.event_kind, EventKind.CAPABILITY_REQUEST)
        self.assertEqual(model.auth_context.auth_mode, AuthMode.LOOPBACK_TRUSTED)
        self.assertEqual(model.model_dump(mode="json"), payload)

        # protocol_version must be exactly 2
        invalid_version = dict(payload, protocol_version=1)
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_version, schema=schema)
        with self.assertRaises(Exception):
            MiraEnvelopeV2(**invalid_version)

        # unknown event_kind is rejected
        invalid_kind = dict(payload, event_kind="UNKNOWN_KIND")
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_kind, schema=schema)
        with self.assertRaises(Exception):
            MiraEnvelopeV2(**invalid_kind)

    def test_capability_request(self):
        schema = self.schemas["capability_request.json"]
        payload = self._load_fixture("capability_request.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = CapabilityRequest(**payload)
        self.assertEqual(model.reversibility, Reversibility.REVERSIBLE)
        self.assertEqual(model.model_dump(mode="json"), payload)

        invalid_reversibility = dict(payload, reversibility="MAYBE")
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_reversibility, schema=schema)
        with self.assertRaises(Exception):
            CapabilityRequest(**invalid_reversibility)

    def test_action_manifest(self):
        schema = self.schemas["action_manifest.json"]
        payload = self._load_fixture("action_manifest.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = ActionManifest(**payload)
        self.assertEqual(model.credential_scope, ["mac.audio"])
        self.assertEqual(model.model_dump(mode="json"), payload)

    def test_capability_receipt(self):
        schema = self.schemas["capability_receipt.json"]
        payload = self._load_fixture("capability_receipt.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = CapabilityReceipt(**payload)
        self.assertEqual(model.result_status, CapabilityResultStatus.SUCCESS)
        self.assertEqual(model.model_dump(mode="json"), payload)

        # a DENIED receipt must still validate with a null rollback_evidence
        denied_payload = dict(
            payload,
            result_status="DENIED",
            rollback_evidence=None,
            signature=None,
        )
        jsonschema.validate(instance=denied_payload, schema=schema)
        denied_model = CapabilityReceipt(**denied_payload)
        self.assertIsNone(denied_model.rollback_evidence)
```

- [ ] **Step 8: Run the Python test suite**

Run: `python3 -m unittest shared/tests/test_schemas.py`
Expected: PASS, `Ran 12 tests in ...s` (8 existing + 4 new), `OK`.

- [ ] **Step 9: Commit**

```bash
git add shared/
git commit -m "feat(shared): add protocol v2 envelope and capability contracts"
```

---

### Task 3: Rust Workspace, `mira-protocol` Crate, Cross-Language Fixture Test

**Files:**
- Create: `Cargo.toml` (workspace root)
- Create: `crates/mira-protocol/Cargo.toml`
- Create: `crates/mira-protocol/src/lib.rs`
- Create: `crates/mira-protocol/tests/fixtures_test.rs`
- Create: `daemon/Cargo.toml` (minimal stub — fleshed out in Task 4)
- Create: `daemon/src/main.rs` (minimal stub — fleshed out in Task 4)
- Create: `guardrail/Cargo.toml` (minimal stub — fleshed out in Task 5)
- Create: `guardrail/src/main.rs` (minimal stub — fleshed out in Task 5)

**Interfaces:**
- Consumes: `shared/fixtures/*.json` from Task 2 — read the exact same files, byte for byte, do not create separate Rust-only fixtures.
- Produces: the `mira-protocol` crate's public types — `EventKind`, `AuthMode`, `AuthContext`, `MiraEnvelopeV2`, `Reversibility`, `CapabilityRequest`, `ActionManifest`, `CapabilityResultStatus`, `CapabilityReceipt` — all `pub`, all `Debug + Clone + Serialize + Deserialize + PartialEq`. Task 4 and Task 5 depend on this crate by path (`mira-protocol = { path = "../crates/mira-protocol" }`) and use these exact names.

- [ ] **Step 1: Write the workspace root `Cargo.toml`**

```toml
[workspace]
resolver = "2"
members = ["crates/mira-protocol", "daemon", "guardrail"]

[workspace.package]
edition = "2021"
rust-version = "1.75"
```

- [ ] **Step 2: Write minimal stub crates for `daemon` and `guardrail` so the workspace resolves**

`daemon/Cargo.toml`:

```toml
[package]
name = "mira-daemon"
version = "0.1.0"
edition.workspace = true
rust-version.workspace = true
```

`daemon/src/main.rs`:

```rust
fn main() {
    println!("mira-daemon: scaffolded in Task 3, implemented in Task 4");
}
```

`guardrail/Cargo.toml`:

```toml
[package]
name = "mira-guardrail"
version = "0.1.0"
edition.workspace = true
rust-version.workspace = true
```

`guardrail/src/main.rs`:

```rust
fn main() {
    println!("mira-guardrail: scaffolded in Task 3, implemented in Task 5");
}
```

- [ ] **Step 3: Write the `mira-protocol` crate**

`crates/mira-protocol/Cargo.toml`:

```toml
[package]
name = "mira-protocol"
version = "0.1.0"
edition.workspace = true
rust-version.workspace = true

[dependencies]
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
```

`crates/mira-protocol/src/lib.rs`:

```rust
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EventKind {
    Event,
    Command,
    CapabilityRequest,
    CapabilityReceipt,
    Ack,
    Error,
    Register,
    RegisterAck,
    Disconnect,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AuthMode {
    LoopbackTrusted,
    Mtls,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AuthContext {
    pub node_id: String,
    pub auth_mode: AuthMode,
    pub token: Option<String>,
    pub cert_fingerprint: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MiraEnvelopeV2 {
    pub protocol_version: u8,
    pub event_id: String,
    pub trace_id: String,
    pub topic: String,
    pub event_kind: EventKind,
    pub sender_node: String,
    pub timestamp: f64,
    pub auth_context: AuthContext,
    pub payload: Value,
    pub ack_ref: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Reversibility {
    Reversible,
    Irreversible,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CapabilityRequest {
    pub request_id: String,
    pub trace_id: String,
    pub sender_node: String,
    pub capability: String,
    pub arguments: serde_json::Map<String, Value>,
    pub reversibility: Reversibility,
    pub requested_at: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ActionManifest {
    pub capability: String,
    pub reversibility: Reversibility,
    pub rollback_plan: Option<String>,
    pub credential_scope: Vec<String>,
    pub risk_notes: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CapabilityResultStatus {
    Success,
    Failure,
    Denied,
    RolledBack,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CapabilityReceipt {
    pub request_id: String,
    pub capability: String,
    pub result_status: CapabilityResultStatus,
    pub rollback_evidence: Option<String>,
    pub executed_at: f64,
    pub executed_by: String,
    pub signature: Option<String>,
}
```

- [ ] **Step 4: Write the failing cross-language fixture test**

`crates/mira-protocol/tests/fixtures_test.rs`:

```rust
use mira_protocol::{ActionManifest, CapabilityReceipt, CapabilityRequest, MiraEnvelopeV2};
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::Value;
use std::fs;
use std::path::PathBuf;

fn load_fixture(name: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../shared/fixtures")
        .join(name);
    let raw = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("failed to read fixture {}: {}", path.display(), e));
    serde_json::from_str(&raw).expect("fixture is not valid JSON")
}

fn assert_round_trip<T: DeserializeOwned + Serialize>(fixture: &Value) {
    let parsed: T = serde_json::from_value(fixture.clone()).expect("fixture failed to deserialize");
    let re_serialized = serde_json::to_value(&parsed).expect("failed to re-serialize");
    assert_eq!(&re_serialized, fixture);
}

#[test]
fn mira_envelope_v2_round_trips() {
    let fixture = load_fixture("mira_envelope_v2.json");
    assert_round_trip::<MiraEnvelopeV2>(&fixture);
}

#[test]
fn capability_request_round_trips() {
    let fixture = load_fixture("capability_request.json");
    assert_round_trip::<CapabilityRequest>(&fixture);
}

#[test]
fn action_manifest_round_trips() {
    let fixture = load_fixture("action_manifest.json");
    assert_round_trip::<ActionManifest>(&fixture);
}

#[test]
fn capability_receipt_round_trips() {
    let fixture = load_fixture("capability_receipt.json");
    assert_round_trip::<CapabilityReceipt>(&fixture);
}
```

- [ ] **Step 5: Run the test and verify PASS**

Run: `cargo test -p mira-protocol`
Expected: `test result: ok. 4 passed`.

- [ ] **Step 6: Commit**

```bash
git add Cargo.toml Cargo.lock crates/ daemon/ guardrail/
git commit -m "feat(protocol): scaffold Rust workspace and mira-protocol crate"
```

---

### Task 4: Daemon — Node Registry & WebSocket Broker

**Files:**
- Modify: `daemon/Cargo.toml`
- Create: `daemon/src/lib.rs`
- Create: `daemon/src/registry.rs`
- Create: `daemon/src/broker.rs`
- Modify: `daemon/src/main.rs`
- Create: `daemon/tests/broker_test.rs`

**Interfaces:**
- Consumes: `mira_protocol::{AuthContext, AuthMode, EventKind, MiraEnvelopeV2}` from Task 3.
- Produces: `mira_daemon::registry::Registry` (methods: `new()`, `subscribe() -> broadcast::Receiver<MiraEnvelopeV2>`, `publish(MiraEnvelopeV2)`, `register(node_id, topics) -> Result<(), RegisterError>`, `deregister(node_id)`, `contains(node_id) -> bool`, `len() -> usize` — all `async` except `new`/`subscribe`/`publish`). `mira_daemon::broker::router(registry: Registry) -> axum::Router`. Task 6 will change the router's parameter type from `Registry` to an `AppState` struct — Task 6's brief carries that change; do not build `AppState` in this task.

- [ ] **Step 1: Write `daemon/Cargo.toml` with pinned dependencies**

```toml
[package]
name = "mira-daemon"
version = "0.1.0"
edition.workspace = true
rust-version.workspace = true

[dependencies]
mira-protocol = { path = "../crates/mira-protocol" }
tokio = { version = "1.40", features = ["full"] }
axum = { version = "0.7", features = ["ws"] }
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
uuid = { version = "1.10", features = ["v4"] }
futures-util = "0.3"
thiserror = "1.0"

[dev-dependencies]
tokio-tungstenite = "0.24"
```

- [ ] **Step 2: Write the node registry**

`daemon/src/registry.rs`:

```rust
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::{broadcast, RwLock};

use mira_protocol::MiraEnvelopeV2;

#[derive(Debug)]
pub struct NodeHandle {
    pub node_id: String,
    pub topics: HashSet<String>,
}

#[derive(Clone)]
pub struct Registry {
    nodes: Arc<RwLock<HashMap<String, NodeHandle>>>,
    bus: broadcast::Sender<MiraEnvelopeV2>,
}

impl Registry {
    pub fn new() -> Self {
        let (bus, _rx) = broadcast::channel(256);
        Self {
            nodes: Arc::new(RwLock::new(HashMap::new())),
            bus,
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<MiraEnvelopeV2> {
        self.bus.subscribe()
    }

    pub fn publish(&self, envelope: MiraEnvelopeV2) {
        let _ = self.bus.send(envelope);
    }

    pub async fn register(&self, node_id: String, topics: HashSet<String>) -> Result<(), RegisterError> {
        let mut nodes = self.nodes.write().await;
        if nodes.contains_key(&node_id) {
            return Err(RegisterError::DuplicateNodeId);
        }
        nodes.insert(node_id.clone(), NodeHandle { node_id, topics });
        Ok(())
    }

    pub async fn deregister(&self, node_id: &str) {
        self.nodes.write().await.remove(node_id);
    }

    pub async fn contains(&self, node_id: &str) -> bool {
        self.nodes.read().await.contains_key(node_id)
    }

    pub async fn len(&self) -> usize {
        self.nodes.read().await.len()
    }
}

impl Default for Registry {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, thiserror::Error)]
pub enum RegisterError {
    #[error("node_id already registered")]
    DuplicateNodeId,
}
```

- [ ] **Step 3: Write the WebSocket broker**

`daemon/src/broker.rs`:

```rust
use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    response::IntoResponse,
    routing::get,
    Router,
};
use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

use mira_protocol::{AuthContext, AuthMode, EventKind, MiraEnvelopeV2};

use crate::registry::{RegisterError, Registry};

pub fn router(registry: Registry) -> Router {
    Router::new().route("/ws", get(ws_handler)).with_state(registry)
}

async fn ws_handler(ws: WebSocketUpgrade, State(registry): State<Registry>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, registry))
}

fn now_unix() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

fn error_envelope(sender_node: &str, message: &str) -> MiraEnvelopeV2 {
    MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        topic: "system".into(),
        event_kind: EventKind::Error,
        sender_node: sender_node.into(),
        timestamp: now_unix(),
        auth_context: AuthContext {
            node_id: sender_node.into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::json!({ "message": message }),
        ack_ref: None,
    }
}

async fn send_envelope(socket: &mut WebSocket, envelope: &MiraEnvelopeV2) -> bool {
    socket
        .send(Message::Text(serde_json::to_string(envelope).unwrap()))
        .await
        .is_ok()
}

async fn handle_socket(mut socket: WebSocket, registry: Registry) {
    let first = match socket.recv().await {
        Some(Ok(Message::Text(text))) => text,
        _ => {
            let _ = send_envelope(&mut socket, &error_envelope("unknown", "expected REGISTER as first message")).await;
            return;
        }
    };

    let register_envelope: MiraEnvelopeV2 = match serde_json::from_str(&first) {
        Ok(env) => env,
        Err(_) => {
            let _ = send_envelope(&mut socket, &error_envelope("unknown", "malformed JSON")).await;
            return;
        }
    };

    if register_envelope.event_kind != EventKind::Register {
        let _ = send_envelope(
            &mut socket,
            &error_envelope(&register_envelope.sender_node, "first message must be event_kind REGISTER"),
        )
        .await;
        return;
    }

    if register_envelope.auth_context.auth_mode != AuthMode::LoopbackTrusted {
        let _ = send_envelope(
            &mut socket,
            &error_envelope(
                &register_envelope.sender_node,
                "only LOOPBACK_TRUSTED auth_mode is supported on this Phase 1 gateway",
            ),
        )
        .await;
        return;
    }

    let node_id = register_envelope.sender_node.clone();
    let topics: HashSet<String> = register_envelope
        .payload
        .get("topics")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|v| v.as_str().map(String::from)).collect())
        .unwrap_or_default();

    // Subscribe before registering: register() makes this node visible to
    // publishers immediately, and the ack send below is a real async flush
    // that yields the task — subscribing after either point leaves a window
    // where an event published to this node's topics is silently lost.
    let mut rx = registry.subscribe();

    if let Err(RegisterError::DuplicateNodeId) = registry.register(node_id.clone(), topics.clone()).await {
        let _ = send_envelope(&mut socket, &error_envelope(&node_id, "duplicate node_id")).await;
        return;
    }

    let ack = MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: register_envelope.trace_id.clone(),
        topic: "system".into(),
        event_kind: EventKind::RegisterAck,
        sender_node: "gateway".into(),
        timestamp: now_unix(),
        auth_context: AuthContext {
            node_id: "gateway".into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::json!({ "node_id": node_id }),
        ack_ref: Some(register_envelope.event_id.clone()),
    };
    if !send_envelope(&mut socket, &ack).await {
        registry.deregister(&node_id).await;
        return;
    }

    loop {
        tokio::select! {
            incoming = socket.recv() => {
                match incoming {
                    Some(Ok(Message::Text(text))) => {
                        match serde_json::from_str::<MiraEnvelopeV2>(&text) {
                            Ok(envelope) => registry.publish(envelope),
                            Err(_) => {
                                let _ = send_envelope(&mut socket, &error_envelope(&node_id, "malformed JSON")).await;
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Err(_)) => break,
                    _ => {}
                }
            }
            broadcast = rx.recv() => {
                match broadcast {
                    Ok(envelope) => {
                        if envelope.sender_node == node_id { continue; }
                        if !topics.contains(&envelope.topic) { continue; }
                        if !send_envelope(&mut socket, &envelope).await { break; }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
        }
    }

    registry.deregister(&node_id).await;
}
```

- [ ] **Step 4: Wire the library and binary entry points**

`daemon/src/lib.rs`:

```rust
pub mod broker;
pub mod registry;

pub use registry::Registry;

pub fn build_router_with_registry(registry: Registry) -> axum::Router {
    broker::router(registry)
}
```

`daemon/src/main.rs`:

```rust
use tokio::net::TcpListener;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let router = mira_daemon::build_router_with_registry(mira_daemon::Registry::new());
    let listener = TcpListener::bind("127.0.0.1:8080").await.expect("failed to bind 127.0.0.1:8080");
    tracing::info!("mira-daemon listening on {}", listener.local_addr().unwrap());
    axum::serve(listener, router).await.expect("server error");
}
```

- [ ] **Step 5: Write the failing broker integration tests**

`daemon/tests/broker_test.rs`:

```rust
use std::net::SocketAddr;
use std::time::{SystemTime, UNIX_EPOCH};

use futures_util::StreamExt;
use mira_daemon::{build_router_with_registry, Registry};
use mira_protocol::{AuthContext, AuthMode, EventKind, MiraEnvelopeV2};
use tokio::net::TcpListener;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use uuid::Uuid;

async fn spawn_daemon() -> (SocketAddr, Registry) {
    let registry = Registry::new();
    let router = build_router_with_registry(registry.clone());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    (addr, registry)
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

fn register_envelope(node_id: &str, topics: &[&str]) -> MiraEnvelopeV2 {
    MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        topic: "system".into(),
        event_kind: EventKind::Register,
        sender_node: node_id.into(),
        timestamp: now(),
        auth_context: AuthContext {
            node_id: node_id.into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::json!({ "topics": topics }),
        ack_ref: None,
    }
}

fn event_envelope(node_id: &str, topic: &str) -> MiraEnvelopeV2 {
    MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        topic: topic.into(),
        event_kind: EventKind::Event,
        sender_node: node_id.into(),
        timestamp: now(),
        auth_context: AuthContext {
            node_id: node_id.into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::json!({ "hello": "world" }),
        ack_ref: None,
    }
}

#[tokio::test]
async fn register_receives_ack() {
    let (addr, _registry) = spawn_daemon().await;
    let (mut ws, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    let reg = register_envelope("node-a", &["topic-1"]);
    ws.send(WsMessage::Text(serde_json::to_string(&reg).unwrap())).await.unwrap();

    let msg = ws.next().await.unwrap().unwrap();
    let ack: MiraEnvelopeV2 = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    assert_eq!(ack.event_kind, EventKind::RegisterAck);
    assert_eq!(ack.ack_ref.as_deref(), Some(reg.event_id.as_str()));
}

#[tokio::test]
async fn broadcast_excludes_sender_and_respects_topic_subscription() {
    let (addr, _registry) = spawn_daemon().await;

    let (mut ws_a, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws_a.send(WsMessage::Text(serde_json::to_string(&register_envelope("node-a", &["topic-1"])).unwrap())).await.unwrap();
    let _ack_a = ws_a.next().await.unwrap().unwrap();

    let (mut ws_b, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws_b.send(WsMessage::Text(serde_json::to_string(&register_envelope("node-b", &["topic-1"])).unwrap())).await.unwrap();
    let _ack_b = ws_b.next().await.unwrap().unwrap();

    let (mut ws_c, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws_c.send(WsMessage::Text(serde_json::to_string(&register_envelope("node-c", &["topic-2"])).unwrap())).await.unwrap();
    let _ack_c = ws_c.next().await.unwrap().unwrap();

    let published = event_envelope("node-a", "topic-1");
    ws_a.send(WsMessage::Text(serde_json::to_string(&published).unwrap())).await.unwrap();

    let msg = tokio::time::timeout(std::time::Duration::from_secs(2), ws_b.next())
        .await
        .expect("node-b should receive the broadcast")
        .unwrap()
        .unwrap();
    let received: MiraEnvelopeV2 = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    assert_eq!(received.event_id, published.event_id);

    let sender_echo = tokio::time::timeout(std::time::Duration::from_millis(300), ws_a.next()).await;
    assert!(sender_echo.is_err(), "sender must not receive its own event echoed back");

    let unrelated = tokio::time::timeout(std::time::Duration::from_millis(300), ws_c.next()).await;
    assert!(unrelated.is_err(), "node-c is not subscribed to topic-1");
}

#[tokio::test]
async fn duplicate_node_id_is_rejected() {
    let (addr, _registry) = spawn_daemon().await;

    let (mut ws_first, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws_first.send(WsMessage::Text(serde_json::to_string(&register_envelope("dup-node", &[])).unwrap())).await.unwrap();
    let _ack = ws_first.next().await.unwrap().unwrap();

    let (mut ws_second, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws_second.send(WsMessage::Text(serde_json::to_string(&register_envelope("dup-node", &[])).unwrap())).await.unwrap();
    let msg = ws_second.next().await.unwrap().unwrap();
    let err: MiraEnvelopeV2 = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    assert_eq!(err.event_kind, EventKind::Error);
}

#[tokio::test]
async fn malformed_json_returns_error_envelope_without_closing() {
    let (addr, _registry) = spawn_daemon().await;
    let (mut ws, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws.send(WsMessage::Text(serde_json::to_string(&register_envelope("node-malformed", &["topic-1"])).unwrap())).await.unwrap();
    let _ack = ws.next().await.unwrap().unwrap();

    ws.send(WsMessage::Text("not valid json".into())).await.unwrap();
    let msg = ws.next().await.unwrap().unwrap();
    let err: MiraEnvelopeV2 = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    assert_eq!(err.event_kind, EventKind::Error);
}

#[tokio::test]
async fn disconnect_removes_node_from_registry() {
    let (addr, registry) = spawn_daemon().await;
    let (mut ws, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws.send(WsMessage::Text(serde_json::to_string(&register_envelope("node-leaving", &[])).unwrap())).await.unwrap();
    let _ack = ws.next().await.unwrap().unwrap();
    assert!(registry.contains("node-leaving").await);

    drop(ws);

    let mut cleaned_up = false;
    for _ in 0..20 {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        if !registry.contains("node-leaving").await {
            cleaned_up = true;
            break;
        }
    }
    assert!(cleaned_up, "registry should remove the node after disconnect");
}
```

Note: `ws_a.send(...)`, `ws_b.send(...)` etc. use `futures_util::SinkExt` — add `use futures_util::SinkExt;` alongside the `StreamExt` import at the top of the file.

- [ ] **Step 6: Run the tests and verify PASS**

Run: `cargo test -p mira-daemon`
Expected: `test result: ok. 5 passed` (plus Task 3's 4 `mira-protocol` tests still passing if run with `cargo test --workspace`).

- [ ] **Step 7: Commit**

```bash
git add daemon/
git commit -m "feat(daemon): add node registry and websocket broker"
```

---

### Task 5: Guardrail — Deny-by-Default Policy Engine & Dev Receipt Signing

**Files:**
- Modify: `guardrail/Cargo.toml`
- Create: `guardrail/src/lib.rs`
- Create: `guardrail/src/policy.rs`
- Create: `guardrail/src/signing.rs`
- Modify: `guardrail/src/main.rs`
- Create: `guardrail/tests/policy_test.rs`
- Create: `guardrail/tests/signing_test.rs`

**Interfaces:**
- Consumes: `mira_protocol::{ActionManifest, CapabilityRequest, CapabilityReceipt, CapabilityResultStatus, Reversibility}` from Task 3.
- Produces: `mira_guardrail::{PolicyDecision, PolicyTable, DevSigner}`. `PolicyTable::dev_default()` registers exactly one capability, `"mac.volume.set"`, as `Reversibility::Reversible` with a non-null rollback plan. `PolicyTable::evaluate(&CapabilityRequest) -> PolicyDecision`. `DevSigner::load_or_generate(&Path) -> io::Result<Self>`, `DevSigner::sign(CapabilityReceipt) -> CapabilityReceipt`, `DevSigner::verify(&CapabilityReceipt) -> bool`. Task 6 calls all of these directly — do not rename them.

- [ ] **Step 1: Write `guardrail/Cargo.toml` with pinned dependencies**

```toml
[package]
name = "mira-guardrail"
version = "0.1.0"
edition.workspace = true
rust-version.workspace = true

[dependencies]
mira-protocol = { path = "../crates/mira-protocol" }
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
ed25519-dalek = { version = "2.1", features = ["rand_core"] }
rand = "0.8"
base64 = "0.22"
tracing = "0.1"
tracing-subscriber = "0.3"

[dev-dependencies]
tempfile = "3.13"
```

- [ ] **Step 2: Write the policy engine**

`guardrail/src/policy.rs`:

```rust
use std::collections::HashMap;

use mira_protocol::{ActionManifest, CapabilityRequest, Reversibility};

#[derive(Debug, Clone, PartialEq)]
pub enum PolicyDecision {
    Allow { manifest: ActionManifest },
    Deny { reason: String },
}

#[derive(Clone)]
pub struct PolicyTable {
    manifests: HashMap<String, ActionManifest>,
}

impl PolicyTable {
    pub fn new() -> Self {
        Self { manifests: HashMap::new() }
    }

    pub fn register(&mut self, manifest: ActionManifest) {
        self.manifests.insert(manifest.capability.clone(), manifest);
    }

    pub fn dev_default() -> Self {
        let mut table = Self::new();
        table.register(ActionManifest {
            capability: "mac.volume.set".to_string(),
            reversibility: Reversibility::Reversible,
            rollback_plan: Some(
                "Restore the previous system output volume level captured before this action executed."
                    .to_string(),
            ),
            credential_scope: vec!["mac.audio".to_string()],
            risk_notes: "Adjusts system output volume only; no data loss or irreversible side effects."
                .to_string(),
        });
        table
    }

    pub fn evaluate(&self, request: &CapabilityRequest) -> PolicyDecision {
        let Some(manifest) = self.manifests.get(&request.capability) else {
            return PolicyDecision::Deny {
                reason: format!("capability '{}' is not registered", request.capability),
            };
        };

        if manifest.reversibility != request.reversibility {
            return PolicyDecision::Deny {
                reason: "request reversibility does not match the registered action manifest".to_string(),
            };
        }

        match manifest.reversibility {
            Reversibility::Reversible => {
                if manifest.rollback_plan.is_none() {
                    return PolicyDecision::Deny {
                        reason: "reversible capability has no rollback plan on file".to_string(),
                    };
                }
            }
            Reversibility::Irreversible | Reversibility::Unknown => {
                return PolicyDecision::Deny {
                    reason: "only reversible actions with a tested rollback may run autonomously".to_string(),
                };
            }
        }

        PolicyDecision::Allow { manifest: manifest.clone() }
    }
}

impl Default for PolicyTable {
    fn default() -> Self {
        Self::new()
    }
}
```

- [ ] **Step 3: Write the dev receipt signer**

`guardrail/src/signing.rs`:

```rust
use std::fs;
use std::path::Path;

use base64::{engine::general_purpose::STANDARD, Engine};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use rand::rngs::OsRng;

use mira_protocol::CapabilityReceipt;

pub struct DevSigner {
    signing_key: SigningKey,
}

impl DevSigner {
    // Dev-only key custody: generated on first run and persisted at `path`
    // so repeated local runs sign with the same key. Production key
    // custody for the root-owned signed guardrail binary is a later plan.
    pub fn load_or_generate(path: &Path) -> std::io::Result<Self> {
        if let Ok(bytes) = fs::read(path) {
            let key_bytes: [u8; 32] = bytes
                .try_into()
                .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "bad key length"))?;
            return Ok(Self { signing_key: SigningKey::from_bytes(&key_bytes) });
        }
        let signing_key = SigningKey::generate(&mut OsRng);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(path, signing_key.to_bytes())?;
        Ok(Self { signing_key })
    }

    pub fn verifying_key(&self) -> VerifyingKey {
        self.signing_key.verifying_key()
    }

    pub fn sign(&self, mut receipt: CapabilityReceipt) -> CapabilityReceipt {
        receipt.signature = None;
        let canonical = serde_json::to_vec(&receipt).expect("receipt always serializes");
        let signature: Signature = self.signing_key.sign(&canonical);
        receipt.signature = Some(STANDARD.encode(signature.to_bytes()));
        receipt
    }

    pub fn verify(&self, receipt: &CapabilityReceipt) -> bool {
        let Some(sig_b64) = &receipt.signature else { return false };
        let Ok(sig_bytes) = STANDARD.decode(sig_b64) else { return false };
        let Ok(signature) = Signature::from_slice(&sig_bytes) else { return false };
        let mut unsigned = receipt.clone();
        unsigned.signature = None;
        let canonical = match serde_json::to_vec(&unsigned) {
            Ok(bytes) => bytes,
            Err(_) => return false,
        };
        self.verifying_key().verify(&canonical, &signature).is_ok()
    }
}
```

- [ ] **Step 4: Wire the library and binary entry points**

`guardrail/src/lib.rs`:

```rust
pub mod policy;
pub mod signing;

pub use policy::{PolicyDecision, PolicyTable};
pub use signing::DevSigner;
```

`guardrail/src/main.rs`:

```rust
fn main() {
    tracing_subscriber::fmt::init();
    tracing::info!("mira-guardrail: IPC server added in Task 6");
}
```

- [ ] **Step 5: Write the failing policy tests**

`guardrail/tests/policy_test.rs`:

```rust
use mira_guardrail::{PolicyDecision, PolicyTable};
use mira_protocol::{CapabilityRequest, Reversibility};

fn request(capability: &str, reversibility: Reversibility) -> CapabilityRequest {
    CapabilityRequest {
        request_id: "req-test".to_string(),
        trace_id: "trace-test".to_string(),
        sender_node: "mac-host".to_string(),
        capability: capability.to_string(),
        arguments: serde_json::Map::new(),
        reversibility,
        requested_at: 0.0,
    }
}

#[test]
fn allows_registered_reversible_capability_with_matching_reversibility() {
    let table = PolicyTable::dev_default();
    let decision = table.evaluate(&request("mac.volume.set", Reversibility::Reversible));
    assert!(matches!(decision, PolicyDecision::Allow { .. }));
}

#[test]
fn denies_unregistered_capability() {
    let table = PolicyTable::dev_default();
    let decision = table.evaluate(&request("mac.shutdown", Reversibility::Reversible));
    assert!(matches!(decision, PolicyDecision::Deny { .. }));
}

#[test]
fn denies_reversibility_mismatch() {
    let table = PolicyTable::dev_default();
    let decision = table.evaluate(&request("mac.volume.set", Reversibility::Irreversible));
    assert!(matches!(decision, PolicyDecision::Deny { .. }));
}

#[test]
fn denies_irreversible_and_unknown_by_default() {
    let mut table = PolicyTable::dev_default();
    table.register(mira_protocol::ActionManifest {
        capability: "mac.reformat_disk".to_string(),
        reversibility: Reversibility::Irreversible,
        rollback_plan: None,
        credential_scope: vec!["mac.disk".to_string()],
        risk_notes: "Destroys data.".to_string(),
    });
    let decision = table.evaluate(&request("mac.reformat_disk", Reversibility::Irreversible));
    assert!(matches!(decision, PolicyDecision::Deny { .. }));
}
```

`guardrail/tests/signing_test.rs`:

```rust
use mira_guardrail::DevSigner;
use mira_protocol::{CapabilityReceipt, CapabilityResultStatus};
use tempfile::tempdir;

fn unsigned_receipt() -> CapabilityReceipt {
    CapabilityReceipt {
        request_id: "req-test".to_string(),
        capability: "mac.volume.set".to_string(),
        result_status: CapabilityResultStatus::Success,
        rollback_evidence: Some("previous_level=55".to_string()),
        executed_at: 0.0,
        executed_by: "guardrail-dev".to_string(),
        signature: None,
    }
}

#[test]
fn signs_and_verifies_a_receipt() {
    let dir = tempdir().unwrap();
    let key_path = dir.path().join("guardrail.key");
    let signer = DevSigner::load_or_generate(&key_path).unwrap();

    let signed = signer.sign(unsigned_receipt());
    assert!(signed.signature.is_some());
    assert!(signer.verify(&signed));
}

#[test]
fn rejects_a_tampered_receipt() {
    let dir = tempdir().unwrap();
    let key_path = dir.path().join("guardrail.key");
    let signer = DevSigner::load_or_generate(&key_path).unwrap();

    let mut signed = signer.sign(unsigned_receipt());
    signed.rollback_evidence = Some("previous_level=99".to_string());
    assert!(!signer.verify(&signed));
}

#[test]
fn reloads_the_same_key_across_signer_instances() {
    let dir = tempdir().unwrap();
    let key_path = dir.path().join("guardrail.key");
    let signer_a = DevSigner::load_or_generate(&key_path).unwrap();
    let signer_b = DevSigner::load_or_generate(&key_path).unwrap();

    let signed = signer_a.sign(unsigned_receipt());
    assert!(signer_b.verify(&signed));
}
```

- [ ] **Step 6: Run the tests and verify PASS**

Run: `cargo test -p mira-guardrail`
Expected: `test result: ok. 7 passed` (4 policy + 3 signing).

- [ ] **Step 7: Commit**

```bash
git add guardrail/
git commit -m "feat(guardrail): add deny-by-default policy engine and dev receipt signing"
```

---

### Task 6: Capability Dispatch Integration (Daemon ↔ Guardrail over UDS) & End-to-End Test

**Files:**
- Modify: `guardrail/Cargo.toml` (add `tokio` dependency)
- Create: `guardrail/src/ipc.rs`
- Modify: `guardrail/src/lib.rs`
- Modify: `guardrail/src/main.rs`
- Modify: `daemon/Cargo.toml` (add `mira-guardrail` and `tempfile` dev-dependencies)
- Create: `daemon/src/guardrail_client.rs`
- Modify: `daemon/src/broker.rs` (replace with the version below — `AppState` replaces the bare `Registry` state)
- Modify: `daemon/src/lib.rs`
- Modify: `daemon/src/main.rs`
- Modify: `daemon/tests/broker_test.rs` (update `spawn_daemon()` for the new `AppState`-based router constructor)
- Create: `daemon/tests/capability_dispatch_test.rs`

**Interfaces:**
- Consumes: `mira_guardrail::{PolicyTable, DevSigner}` from Task 5, `mira_daemon::registry::Registry` from Task 4.
- Produces: `mira_guardrail::ipc::serve(sock_path: &Path, policy: PolicyTable, signer: Arc<DevSigner>) -> io::Result<()>` (runs forever, accepting one newline-delimited JSON `CapabilityRequest` per connection and writing back one newline-delimited JSON `CapabilityReceipt`). `mira_daemon::GuardrailClient::new(sock_path: PathBuf)`, `.dispatch(&CapabilityRequest) -> CapabilityReceipt` (never panics — a dead/unreachable guardrail becomes a synthesized `CapabilityResultStatus::Failure` receipt after a 5s timeout). `mira_daemon::AppState { registry: Registry, guardrail: GuardrailClient }` (both fields `pub`), `mira_daemon::broker::router(state: AppState) -> axum::Router`.

- [ ] **Step 1: Add `tokio` to `guardrail/Cargo.toml`**

Add to `[dependencies]`:

```toml
tokio = { version = "1.40", features = ["full"] }
```

- [ ] **Step 2: Write the guardrail's Unix Domain Socket server**

`guardrail/src/ipc.rs`:

```rust
use std::path::Path;
use std::sync::Arc;

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};

use mira_protocol::{CapabilityReceipt, CapabilityRequest, CapabilityResultStatus};

use crate::policy::{PolicyDecision, PolicyTable};
use crate::signing::DevSigner;

pub async fn serve(sock_path: &Path, policy: PolicyTable, signer: Arc<DevSigner>) -> std::io::Result<()> {
    let _ = std::fs::remove_file(sock_path);
    let listener = UnixListener::bind(sock_path)?;
    loop {
        let (stream, _) = listener.accept().await?;
        let policy = policy.clone();
        let signer = signer.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_connection(stream, policy, signer).await {
                tracing::warn!("guardrail IPC connection error: {err}");
            }
        });
    }
}

async fn handle_connection(
    stream: UnixStream,
    policy: PolicyTable,
    signer: Arc<DevSigner>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let (read_half, mut write_half) = stream.into_split();
    let mut reader = BufReader::new(read_half);
    loop {
        let mut line = String::new();
        let bytes_read = reader.read_line(&mut line).await?;
        if bytes_read == 0 {
            return Ok(());
        }
        let request: CapabilityRequest = match serde_json::from_str(line.trim_end()) {
            Ok(request) => request,
            Err(err) => {
                tracing::warn!("guardrail received malformed CapabilityRequest: {err}");
                continue;
            }
        };
        let receipt = dispatch(&policy, &signer, request);
        let mut payload = serde_json::to_vec(&receipt)?;
        payload.push(b'\n');
        write_half.write_all(&payload).await?;
    }
}

fn dispatch(policy: &PolicyTable, signer: &DevSigner, request: CapabilityRequest) -> CapabilityReceipt {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();

    match policy.evaluate(&request) {
        PolicyDecision::Deny { reason } => {
            tracing::info!(capability = %request.capability, %reason, "guardrail denied capability request");
            signer.sign(CapabilityReceipt {
                request_id: request.request_id,
                capability: request.capability,
                result_status: CapabilityResultStatus::Denied,
                rollback_evidence: None,
                executed_at: now,
                executed_by: "guardrail-dev".to_string(),
                signature: None,
            })
        }
        // Phase 1: no real device handler is wired up yet — an allowed
        // capability is recorded as executed, with the manifest's static
        // rollback plan as the rollback evidence. A real macOS handler is
        // Implementation Sequence step 3 of the canonical plan.
        PolicyDecision::Allow { manifest } => signer.sign(CapabilityReceipt {
            request_id: request.request_id,
            capability: request.capability,
            result_status: CapabilityResultStatus::Success,
            rollback_evidence: manifest.rollback_plan.clone(),
            executed_at: now,
            executed_by: "guardrail-dev".to_string(),
            signature: None,
        }),
    }
}
```

- [ ] **Step 3: Update the guardrail's library and binary entry points**

`guardrail/src/lib.rs`:

```rust
pub mod ipc;
pub mod policy;
pub mod signing;

pub use policy::{PolicyDecision, PolicyTable};
pub use signing::DevSigner;
```

`guardrail/src/main.rs`:

```rust
use std::path::PathBuf;
use std::sync::Arc;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let sock_path = std::env::var("MIRA_GUARDRAIL_SOCK")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp/mira-dev/guardrail.sock"));
    let key_path = std::env::var("MIRA_GUARDRAIL_KEY")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp/mira-dev/guardrail.key"));
    let signer = Arc::new(
        mira_guardrail::DevSigner::load_or_generate(&key_path)
            .expect("failed to load or generate guardrail signing key"),
    );
    let policy = mira_guardrail::PolicyTable::dev_default();
    tracing::info!("mira-guardrail listening on {}", sock_path.display());
    mira_guardrail::ipc::serve(&sock_path, policy, signer)
        .await
        .expect("guardrail IPC server error");
}
```

- [ ] **Step 4: Add daemon dev-dependencies for the integration test**

Add to `daemon/Cargo.toml`'s `[dev-dependencies]`:

```toml
mira-guardrail = { path = "../guardrail" }
tempfile = "3.13"
```

- [ ] **Step 5: Write the daemon's guardrail UDS client**

`daemon/src/guardrail_client.rs`:

```rust
use std::path::PathBuf;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

use mira_protocol::{CapabilityReceipt, CapabilityRequest, CapabilityResultStatus};

#[derive(Clone)]
pub struct GuardrailClient {
    sock_path: PathBuf,
}

impl GuardrailClient {
    pub fn new(sock_path: PathBuf) -> Self {
        Self { sock_path }
    }

    pub async fn dispatch(&self, request: &CapabilityRequest) -> CapabilityReceipt {
        match tokio::time::timeout(Duration::from_secs(5), self.dispatch_inner(request)).await {
            Ok(Ok(receipt)) => receipt,
            Ok(Err(err)) => self.failure_receipt(request, &err.to_string()),
            Err(_) => self.failure_receipt(request, "guardrail did not respond within 5s"),
        }
    }

    async fn dispatch_inner(
        &self,
        request: &CapabilityRequest,
    ) -> Result<CapabilityReceipt, Box<dyn std::error::Error + Send + Sync>> {
        let stream = UnixStream::connect(&self.sock_path).await?;
        let (read_half, mut write_half) = stream.into_split();
        let mut reader = BufReader::new(read_half);

        let mut payload = serde_json::to_vec(request)?;
        payload.push(b'\n');
        write_half.write_all(&payload).await?;

        let mut line = String::new();
        reader.read_line(&mut line).await?;
        let receipt: CapabilityReceipt = serde_json::from_str(line.trim_end())?;
        Ok(receipt)
    }

    fn failure_receipt(&self, request: &CapabilityRequest, reason: &str) -> CapabilityReceipt {
        tracing::error!("guardrail dispatch failed for {}: {reason}", request.capability);
        CapabilityReceipt {
            request_id: request.request_id.clone(),
            capability: request.capability.clone(),
            result_status: CapabilityResultStatus::Failure,
            rollback_evidence: None,
            executed_at: request.requested_at,
            executed_by: "daemon-guardrail-client".to_string(),
            signature: None,
        }
    }
}
```

- [ ] **Step 6: Replace `daemon/src/broker.rs` to route `CAPABILITY_REQUEST` envelopes through the guardrail**

`daemon/src/broker.rs` (full replacement):

```rust
use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    response::IntoResponse,
    routing::get,
    Router,
};
use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

use mira_protocol::{AuthContext, AuthMode, CapabilityRequest, EventKind, MiraEnvelopeV2};

use crate::guardrail_client::GuardrailClient;
use crate::registry::{RegisterError, Registry};

#[derive(Clone)]
pub struct AppState {
    pub registry: Registry,
    pub guardrail: GuardrailClient,
}

pub fn router(state: AppState) -> Router {
    Router::new().route("/ws", get(ws_handler)).with_state(state)
}

async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

fn now_unix() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

fn error_envelope(sender_node: &str, message: &str) -> MiraEnvelopeV2 {
    MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        topic: "system".into(),
        event_kind: EventKind::Error,
        sender_node: sender_node.into(),
        timestamp: now_unix(),
        auth_context: AuthContext {
            node_id: sender_node.into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::json!({ "message": message }),
        ack_ref: None,
    }
}

fn capability_receipt_envelope(request_event_id: &str, receipt: mira_protocol::CapabilityReceipt) -> MiraEnvelopeV2 {
    MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        topic: "capability".into(),
        event_kind: EventKind::CapabilityReceipt,
        sender_node: "gateway".into(),
        timestamp: now_unix(),
        auth_context: AuthContext {
            node_id: "gateway".into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::to_value(&receipt).unwrap(),
        ack_ref: Some(request_event_id.to_string()),
    }
}

async fn send_envelope(socket: &mut WebSocket, envelope: &MiraEnvelopeV2) -> bool {
    socket.send(Message::Text(serde_json::to_string(envelope).unwrap())).await.is_ok()
}

async fn handle_socket(mut socket: WebSocket, state: AppState) {
    let AppState { registry, guardrail } = state;

    let first = match socket.recv().await {
        Some(Ok(Message::Text(text))) => text,
        _ => {
            let _ = send_envelope(&mut socket, &error_envelope("unknown", "expected REGISTER as first message")).await;
            return;
        }
    };

    let register_envelope: MiraEnvelopeV2 = match serde_json::from_str(&first) {
        Ok(env) => env,
        Err(_) => {
            let _ = send_envelope(&mut socket, &error_envelope("unknown", "malformed JSON")).await;
            return;
        }
    };

    if register_envelope.event_kind != EventKind::Register {
        let _ = send_envelope(
            &mut socket,
            &error_envelope(&register_envelope.sender_node, "first message must be event_kind REGISTER"),
        )
        .await;
        return;
    }

    if register_envelope.auth_context.auth_mode != AuthMode::LoopbackTrusted {
        let _ = send_envelope(
            &mut socket,
            &error_envelope(
                &register_envelope.sender_node,
                "only LOOPBACK_TRUSTED auth_mode is supported on this Phase 1 gateway",
            ),
        )
        .await;
        return;
    }

    let node_id = register_envelope.sender_node.clone();
    let topics: HashSet<String> = register_envelope
        .payload
        .get("topics")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|v| v.as_str().map(String::from)).collect())
        .unwrap_or_default();

    // Subscribe before registering — see the note in Task 4's broker.rs.
    let mut rx = registry.subscribe();

    if let Err(RegisterError::DuplicateNodeId) = registry.register(node_id.clone(), topics.clone()).await {
        let _ = send_envelope(&mut socket, &error_envelope(&node_id, "duplicate node_id")).await;
        return;
    }

    let ack = MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: register_envelope.trace_id.clone(),
        topic: "system".into(),
        event_kind: EventKind::RegisterAck,
        sender_node: "gateway".into(),
        timestamp: now_unix(),
        auth_context: AuthContext {
            node_id: "gateway".into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::json!({ "node_id": node_id }),
        ack_ref: Some(register_envelope.event_id.clone()),
    };
    if !send_envelope(&mut socket, &ack).await {
        registry.deregister(&node_id).await;
        return;
    }

    loop {
        tokio::select! {
            incoming = socket.recv() => {
                match incoming {
                    Some(Ok(Message::Text(text))) => {
                        match serde_json::from_str::<MiraEnvelopeV2>(&text) {
                            Ok(envelope) if envelope.event_kind == EventKind::CapabilityRequest => {
                                let request_event_id = envelope.event_id.clone();
                                match serde_json::from_value::<CapabilityRequest>(envelope.payload.clone()) {
                                    Ok(request) => {
                                        let receipt = guardrail.dispatch(&request).await;
                                        let reply = capability_receipt_envelope(&request_event_id, receipt);
                                        if !send_envelope(&mut socket, &reply).await { break; }
                                    }
                                    Err(_) => {
                                        let _ = send_envelope(&mut socket, &error_envelope(&node_id, "malformed CapabilityRequest payload")).await;
                                    }
                                }
                            }
                            Ok(envelope) => registry.publish(envelope),
                            Err(_) => {
                                let _ = send_envelope(&mut socket, &error_envelope(&node_id, "malformed JSON")).await;
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Err(_)) => break,
                    _ => {}
                }
            }
            broadcast = rx.recv() => {
                match broadcast {
                    Ok(envelope) => {
                        if envelope.sender_node == node_id { continue; }
                        if !topics.contains(&envelope.topic) { continue; }
                        if !send_envelope(&mut socket, &envelope).await { break; }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
        }
    }

    registry.deregister(&node_id).await;
}
```

- [ ] **Step 7: Update the daemon's library and binary entry points**

`daemon/src/lib.rs`:

```rust
pub mod broker;
pub mod guardrail_client;
pub mod registry;

pub use broker::AppState;
pub use guardrail_client::GuardrailClient;
pub use registry::Registry;

pub fn build_router(guardrail_sock_path: std::path::PathBuf) -> axum::Router {
    let state = AppState {
        registry: Registry::new(),
        guardrail: GuardrailClient::new(guardrail_sock_path),
    };
    broker::router(state)
}
```

`daemon/src/main.rs`:

```rust
use std::path::PathBuf;
use tokio::net::TcpListener;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let guardrail_sock = std::env::var("MIRA_GUARDRAIL_SOCK")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp/mira-dev/guardrail.sock"));
    let router = mira_daemon::build_router(guardrail_sock);
    let listener = TcpListener::bind("127.0.0.1:8080").await.expect("failed to bind 127.0.0.1:8080");
    tracing::info!("mira-daemon listening on {}", listener.local_addr().unwrap());
    axum::serve(listener, router).await.expect("server error");
}
```

- [ ] **Step 8: Update `daemon/tests/broker_test.rs`'s `spawn_daemon()` helper for the new `AppState`-based router**

Replace the existing `spawn_daemon` function with:

```rust
async fn spawn_daemon() -> (SocketAddr, Registry) {
    let registry = Registry::new();
    let state = mira_daemon::AppState {
        registry: registry.clone(),
        guardrail: mira_daemon::GuardrailClient::new(std::path::PathBuf::from("/tmp/mira-dev-unused.sock")),
    };
    let router = mira_daemon::broker::router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    (addr, registry)
}
```

Remove the now-unused `build_router_with_registry` import from the top of the file (replace `use mira_daemon::{build_router_with_registry, Registry};` with `use mira_daemon::Registry;`). None of Task 4's tests send a `CapabilityRequest`, so the unreachable guardrail socket path is never dialed and those tests are unaffected.

- [ ] **Step 9: Write the failing end-to-end capability dispatch test**

`daemon/tests/capability_dispatch_test.rs`:

```rust
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use futures_util::StreamExt;
use mira_daemon::{AppState, GuardrailClient, Registry};
use mira_guardrail::{DevSigner, PolicyTable};
use mira_protocol::{AuthContext, AuthMode, CapabilityRequest, EventKind, MiraEnvelopeV2, Reversibility};
use tempfile::TempDir;
use tokio::net::TcpListener;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use uuid::Uuid;

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

async fn spawn_stack() -> (SocketAddr, TempDir) {
    let dir = TempDir::new().unwrap();
    let sock_path = dir.path().join("guardrail.sock");
    let key_path = dir.path().join("guardrail.key");

    let signer = Arc::new(DevSigner::load_or_generate(&key_path).unwrap());
    let policy = PolicyTable::dev_default();
    let sock_path_clone = sock_path.clone();
    tokio::spawn(async move {
        mira_guardrail::ipc::serve(&sock_path_clone, policy, signer).await.unwrap();
    });
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;

    let state = AppState {
        registry: Registry::new(),
        guardrail: GuardrailClient::new(sock_path),
    };
    let router = mira_daemon::broker::router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    (addr, dir)
}

fn register_envelope(node_id: &str) -> MiraEnvelopeV2 {
    MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        topic: "system".into(),
        event_kind: EventKind::Register,
        sender_node: node_id.into(),
        timestamp: now(),
        auth_context: AuthContext {
            node_id: node_id.into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::json!({ "topics": [] }),
        ack_ref: None,
    }
}

fn capability_request_envelope(node_id: &str, capability: &str, reversibility: Reversibility) -> MiraEnvelopeV2 {
    let request = CapabilityRequest {
        request_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        sender_node: node_id.to_string(),
        capability: capability.to_string(),
        arguments: serde_json::json!({ "level": 40 }).as_object().unwrap().clone(),
        reversibility,
        requested_at: now(),
    };
    MiraEnvelopeV2 {
        protocol_version: 2,
        event_id: Uuid::new_v4().to_string(),
        trace_id: request.trace_id.clone(),
        topic: "capability".into(),
        event_kind: EventKind::CapabilityRequest,
        sender_node: node_id.into(),
        timestamp: now(),
        auth_context: AuthContext {
            node_id: node_id.into(),
            auth_mode: AuthMode::LoopbackTrusted,
            token: None,
            cert_fingerprint: None,
        },
        payload: serde_json::to_value(&request).unwrap(),
        ack_ref: None,
    }
}

#[tokio::test]
async fn allowed_capability_request_returns_signed_success_receipt() {
    let (addr, _dir) = spawn_stack().await;
    let (mut ws, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws.send(WsMessage::Text(serde_json::to_string(&register_envelope("mac-host-sim")).unwrap())).await.unwrap();
    let _ack = ws.next().await.unwrap().unwrap();

    let request_envelope = capability_request_envelope("mac-host-sim", "mac.volume.set", Reversibility::Reversible);
    ws.send(WsMessage::Text(serde_json::to_string(&request_envelope).unwrap())).await.unwrap();

    let msg = tokio::time::timeout(std::time::Duration::from_secs(3), ws.next()).await.unwrap().unwrap().unwrap();
    let reply: MiraEnvelopeV2 = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    assert_eq!(reply.event_kind, EventKind::CapabilityReceipt);
    assert_eq!(reply.ack_ref.as_deref(), Some(request_envelope.event_id.as_str()));

    let receipt: mira_protocol::CapabilityReceipt = serde_json::from_value(reply.payload).unwrap();
    assert_eq!(receipt.result_status, mira_protocol::CapabilityResultStatus::Success);
    assert!(receipt.signature.is_some());
    assert!(receipt.rollback_evidence.is_some());
}

#[tokio::test]
async fn unregistered_capability_request_is_denied() {
    let (addr, _dir) = spawn_stack().await;
    let (mut ws, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws.send(WsMessage::Text(serde_json::to_string(&register_envelope("mac-host-sim-2")).unwrap())).await.unwrap();
    let _ack = ws.next().await.unwrap().unwrap();

    let request_envelope = capability_request_envelope("mac-host-sim-2", "mac.shutdown", Reversibility::Irreversible);
    ws.send(WsMessage::Text(serde_json::to_string(&request_envelope).unwrap())).await.unwrap();

    let msg = tokio::time::timeout(std::time::Duration::from_secs(3), ws.next()).await.unwrap().unwrap().unwrap();
    let reply: MiraEnvelopeV2 = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    let receipt: mira_protocol::CapabilityReceipt = serde_json::from_value(reply.payload).unwrap();
    assert_eq!(receipt.result_status, mira_protocol::CapabilityResultStatus::Denied);
    assert!(receipt.rollback_evidence.is_none());
}
```

Note: `ws.send(...)` uses `futures_util::SinkExt` — add `use futures_util::SinkExt;` alongside the `StreamExt` import at the top of the file.

- [ ] **Step 10: Run the full workspace test suite and verify PASS**

Run: `cargo test --workspace`
Expected: `mira-protocol` 4 passed, `mira-daemon` (broker_test 5 + capability_dispatch_test 2) 7 passed, `mira-guardrail` 7 passed. All green, zero failures.

- [ ] **Step 11: Commit**

```bash
git add guardrail/ daemon/
git commit -m "feat(gateway): wire capability dispatch through the guardrail over a unix socket"
```

---

## Verification and Acceptance

- `python3 -m unittest shared/tests/test_schemas.py` passes with 12 tests (8 from Task 1 plus 4 from Task 2).
- `cd shared && npm install && npm test` (`tsc --noEmit`) exits 0.
- `cargo test --workspace` passes with 18 tests across three crates (4 protocol + 7 daemon + 7 guardrail).
- Two simulated WebSocket clients can register on a loopback-bound daemon, one publishes a topic event the other receives (and does not receive its own echo, and a third client subscribed to a different topic does not receive it).
- A `CapabilityRequest` for the registered `mac.volume.set` capability round-trips through the daemon to the guardrail over a Unix Domain Socket and back as a signed `CapabilityReceipt` with `result_status: SUCCESS` and non-null `rollback_evidence`.
- A `CapabilityRequest` for an unregistered capability comes back `DENIED` with null `rollback_evidence` and a **non-null, verifiable** `signature` — the guardrail never executes evidence for a denied action, but it does sign the denial itself so the outcome (including a denial) is tamper-evident. (Decision made mid-Task-6, correcting this document: an earlier draft of this line said "null signature," but the plan's own Step 2 IPC code always signs every receipt — signing denials was chosen as the correct behavior, since a denial is exactly the kind of outcome that benefits from a tamper-evident audit trail.)
- Duplicate `node_id` registration, malformed JSON, and client disconnect are all handled without crashing the daemon.
- `git log` shows five new commits on `feat/macbook-offline-core-sim` following `aa8bc51`, one per task.

## Defaults

- This plan's daemon and guardrail are dev binaries run manually (`cargo run -p mira-daemon`, `cargo run -p mira-guardrail`) — no systemd/launchd unit, Docker Compose entry, or `mira.local` wiring exists yet. That is P40/deployment-plan territory.
- Everything here binds and listens on `127.0.0.1` or a local Unix Domain Socket. Nothing in this plan is reachable from another machine.
