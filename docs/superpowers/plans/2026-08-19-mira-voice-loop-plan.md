# M.I.R.A. Voice Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Continues task numbering from the completed protocol-v2/gateway/guardrail plan (Tasks 1-6) — this plan is Tasks 7-15.

**Goal:** Build the Mac voice loop from Implementation Sequence step 3 of the canonical plan: local "Mira" wake detection, VAD, local STT, a Codex-powered answer, local TTS, barge-in interruption, a minimal identity + in-session memory, and one real (not simulated) typed Mac capability — system volume — that actually executes and can be rolled back.

**Architecture:** A new Python `voice/` package runs as a WebSocket client node against the already-built Rust `mira-daemon` (registers `LOOPBACK_TRUSTED`, publishes `VoiceStateEvent`s, sends `CapabilityRequest`s). A pure, dependency-injected `VoiceLoop` state machine owns the 9-state FSM and is unit tested with fakes; thin adapter classes wrap real ML libraries (Sherpa-ONNX, Silero VAD, faster-whisper, Kokoro) and real audio I/O (`sounddevice`) and are not meaningfully unit-testable — those are smoke tested and verified manually. The guardrail gains a real `mac.volume.set` execution handler (currently only simulated — see `guardrail/src/ipc.rs:73-76`) so a capability request actually changes system volume and records the real previous level as rollback evidence.

**Tech Stack:** Python 3.13, `sherpa-onnx` (wake word), `silero-vad` (VAD), `faster-whisper` (STT), `kokoro` (TTS), `sounddevice` (mic/speaker I/O), `websockets` (daemon client), local `codex` CLI via subprocess (Codex OAuth dev route). Rust: extends the existing `mira-protocol` and `guardrail` crates — no new Rust dependencies.

## Global Constraints

- Work on a new branch `feat/mira-voice-loop`, created before Task 7 (same convention as the merged `feat/macbook-offline-core-sim` branch). Use `superpowers:using-git-worktrees` if isolation is needed.
- Voice states are exactly: `IDLE`, `WAKE_DETECTED`, `LISTENING`, `TRANSCRIBING`, `THINKING`, `SPEAKING`, `INTERRUPTED`, `MUTED`, `OFFLINE`.
- Wake engine is Sherpa-ONNX open-vocabulary keyword spotting for "Mira". Porcupine benchmark fallback is explicitly **not** built in this plan.
- Use a pre-wake in-memory ring buffer that is never persisted to disk — no raw pre-wake audio may ever be written to a file.
- VAD is Silero VAD. STT is a Whisper-family local engine (`faster-whisper`). TTS is Kokoro; Piper fallback is explicitly **not** built in this plan.
- The voice loop must support barge-in and output cancellation (interrupting `SPEAKING` mid-utterance).
- Identity is stored in a versioned `MIRA_IDENTITY.md`. This plan authors v1 directly (human/plan-authored); a release-managed self-editing pipeline is Agent OS scope (Implementation Sequence step 5), not this plan.
- Memory in this plan is an in-RAM rolling buffer of the last 20 turns, passed to Codex each turn. It does **not** survive process restart. Persistent PostgreSQL/pgvector memory is the separately-queued memory subplan (`TASK_QUEUE.md`) — out of scope here. This is an intentional, documented deviation from the canonical plan's "memory survives restart" acceptance criterion, which spans the whole Mac milestone, not step 3 alone.
- Codex is invoked via the local, already-OAuth-authenticated `codex` CLI (`codex exec`), not a raw API key. This is a development route, not a replacement for the exact P40 models.
- No raw SSH command bridge, `sshpass`, unrestricted administrator access, or arbitrary model-generated JavaScript. The voice loop never executes Codex's response text as code or shell — it is only ever spoken aloud, plus exactly one hardcoded, guardrail-authorized capability path (`mac.volume.set`).
- The guardrail binary, signing keys, audit integrity, and irreversible-action policy remain untouchable by autonomous agents. Task 7 below extends the guardrail's own execution logic as planned, reviewed engineering work (matching how the guardrail itself was built in the prior plan) — not an autonomous self-edit.
- Only reversible actions with a tested rollback may run autonomously. `mac.volume.set` is `REVERSIBLE` with a real (not static) rollback evidence payload after Task 7.
- Automatic rollback *triggering* (a dedicated rollback endpoint, `ROLLED_BACK` receipt status) is **not** built in this plan — `rollback_evidence` carries the real previous volume level, and "succeeds and rolls back" is demonstrated by manually issuing a second `mac.volume.set` request with that value. Automated rollback orchestration belongs to the Agent OS release pipeline (Implementation Sequence step 5).

---

### Task 7: Real macOS volume capability handler (guardrail)

The guardrail currently signs `SUCCESS` for `mac.volume.set` without touching the actual system volume (`guardrail/src/ipc.rs:73-76` says so explicitly — this task is that comment's own pointer). This task makes it real: it shells out to `osascript` to read and change the actual output volume, and returns the real previous level as rollback evidence.

**Files:**
- Create: `guardrail/src/volume.rs`
- Modify: `guardrail/src/lib.rs` (add `pub mod volume;`)
- Modify: `guardrail/src/ipc.rs:54-87` (`dispatch` calls the handler on Allow for `mac.volume.set`)
- Test: `guardrail/src/volume.rs` (inline `#[cfg(test)]` module)
- Test: `guardrail/tests/volume_dispatch_test.rs`

**Interfaces:**
- Produces: `pub trait VolumeController { fn get_volume(&self) -> Result<u8, String>; fn set_volume(&self, level: u8) -> Result<(), String>; }`, `pub struct OsascriptVolumeController;` (real impl), `pub fn execute_volume_set(controller: &dyn VolumeController, request: &CapabilityRequest, manifest: &ActionManifest, signer: &DevSigner) -> CapabilityReceipt`
- Consumes: `mira_protocol::{CapabilityRequest, CapabilityReceipt, CapabilityResultStatus, ActionManifest}` (existing), `crate::signing::DevSigner` (existing), `crate::policy::{PolicyTable, PolicyDecision}` (existing)

- [ ] **Step 1: Write the failing unit tests for the controller trait and dispatch logic**

```rust
// guardrail/src/volume.rs
use std::process::Command;

use mira_protocol::{ActionManifest, CapabilityRequest, CapabilityReceipt, CapabilityResultStatus};

use crate::signing::DevSigner;

/// Abstraction over "read/write the real system output volume" so `execute_volume_set` is
/// unit-testable without touching real macOS audio hardware (which isn't available in CI).
pub trait VolumeController {
    fn get_volume(&self) -> Result<u8, String>;
    fn set_volume(&self, level: u8) -> Result<(), String>;
}

/// Real implementation: shells out to `osascript` (AppleScript), the standard way to read/set
/// macOS system output volume from a CLI process.
pub struct OsascriptVolumeController;

impl VolumeController for OsascriptVolumeController {
    fn get_volume(&self) -> Result<u8, String> {
        let output = Command::new("osascript")
            .args(["-e", "output volume of (get volume settings)"])
            .output()
            .map_err(|e| format!("failed to run osascript: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "osascript exited with {}: {}",
                output.status,
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        String::from_utf8_lossy(&output.stdout)
            .trim()
            .parse::<u8>()
            .map_err(|e| format!("could not parse osascript volume output: {e}"))
    }

    fn set_volume(&self, level: u8) -> Result<(), String> {
        let output = Command::new("osascript")
            .args(["-e", &format!("set volume output volume {level}")])
            .output()
            .map_err(|e| format!("failed to run osascript: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "osascript exited with {}: {}",
                output.status,
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        Ok(())
    }
}

fn now_unix() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

/// Executes an already-Allow'd `mac.volume.set` request against a real (or fake) volume
/// controller, capturing the pre-change level as rollback evidence, and returns a signed
/// receipt. Called only after `PolicyTable::evaluate` has already returned `Allow` — this
/// function does not itself decide policy.
pub fn execute_volume_set(
    controller: &dyn VolumeController,
    request: &CapabilityRequest,
    manifest: &ActionManifest,
    signer: &DevSigner,
) -> CapabilityReceipt {
    let now = now_unix();
    let fail = |reason: String| {
        tracing::warn!(capability = %request.capability, %reason, "mac.volume.set execution failed");
        signer.sign(CapabilityReceipt {
            request_id: request.request_id.clone(),
            capability: request.capability.clone(),
            result_status: CapabilityResultStatus::Failure,
            rollback_evidence: None,
            executed_at: now,
            executed_by: "guardrail-dev".to_string(),
            signature: None,
        })
    };

    let level = match request.arguments.get("level").and_then(|v| v.as_u64()) {
        Some(level) if level <= 100 => level as u8,
        Some(_) => return fail("arguments.level must be between 0 and 100".to_string()),
        None => return fail("arguments.level is required and must be an integer".to_string()),
    };

    let previous_level = match controller.get_volume() {
        Ok(level) => level,
        Err(reason) => return fail(reason),
    };

    if let Err(reason) = controller.set_volume(level) {
        return fail(reason);
    }

    let _ = &manifest.rollback_plan; // static plan text is superseded by the real evidence below
    signer.sign(CapabilityReceipt {
        request_id: request.request_id.clone(),
        capability: request.capability.clone(),
        result_status: CapabilityResultStatus::Success,
        rollback_evidence: Some(
            serde_json::json!({ "previous_level": previous_level }).to_string(),
        ),
        executed_at: now,
        executed_by: "guardrail-dev".to_string(),
        signature: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::signing::DevSigner;
    use mira_protocol::Reversibility;
    use std::cell::Cell;
    use tempfile::TempDir;

    struct FakeVolumeController {
        current: Cell<u8>,
        fail_get: bool,
        fail_set: bool,
    }

    impl VolumeController for FakeVolumeController {
        fn get_volume(&self) -> Result<u8, String> {
            if self.fail_get {
                return Err("simulated get failure".to_string());
            }
            Ok(self.current.get())
        }
        fn set_volume(&self, level: u8) -> Result<(), String> {
            if self.fail_set {
                return Err("simulated set failure".to_string());
            }
            self.current.set(level);
            Ok(())
        }
    }

    fn signer() -> DevSigner {
        let dir = TempDir::new().unwrap();
        DevSigner::load_or_generate(&dir.path().join("key")).unwrap()
    }

    fn manifest() -> ActionManifest {
        ActionManifest {
            capability: "mac.volume.set".to_string(),
            reversibility: Reversibility::Reversible,
            rollback_plan: Some("Restore the previous system output volume level.".to_string()),
            credential_scope: vec!["mac.audio".to_string()],
            risk_notes: "Adjusts system output volume only.".to_string(),
        }
    }

    fn request(level_arg: Option<serde_json::Value>) -> CapabilityRequest {
        let mut arguments = serde_json::Map::new();
        if let Some(v) = level_arg {
            arguments.insert("level".to_string(), v);
        }
        CapabilityRequest {
            request_id: "req-1".to_string(),
            trace_id: "trace-1".to_string(),
            sender_node: "mac-host".to_string(),
            capability: "mac.volume.set".to_string(),
            arguments,
            reversibility: Reversibility::Reversible,
            requested_at: 0.0,
        }
    }

    #[test]
    fn sets_volume_and_records_real_previous_level_as_rollback_evidence() {
        let controller = FakeVolumeController { current: Cell::new(22), fail_get: false, fail_set: false };
        let signer = signer();
        let receipt = execute_volume_set(&controller, &request(Some(serde_json::json!(40))), &manifest(), &signer);

        assert_eq!(receipt.result_status, CapabilityResultStatus::Success);
        assert_eq!(controller.current.get(), 40);
        let evidence: serde_json::Value = serde_json::from_str(receipt.rollback_evidence.as_ref().unwrap()).unwrap();
        assert_eq!(evidence["previous_level"], 22);
        assert!(signer.verify(&receipt));
    }

    #[test]
    fn missing_level_argument_fails_without_touching_hardware() {
        let controller = FakeVolumeController { current: Cell::new(22), fail_get: false, fail_set: false };
        let signer = signer();
        let receipt = execute_volume_set(&controller, &request(None), &manifest(), &signer);

        assert_eq!(receipt.result_status, CapabilityResultStatus::Failure);
        assert_eq!(controller.current.get(), 22, "volume must be untouched when the request is malformed");
    }

    #[test]
    fn out_of_range_level_fails() {
        let controller = FakeVolumeController { current: Cell::new(22), fail_get: false, fail_set: false };
        let signer = signer();
        let receipt = execute_volume_set(&controller, &request(Some(serde_json::json!(140))), &manifest(), &signer);

        assert_eq!(receipt.result_status, CapabilityResultStatus::Failure);
        assert_eq!(controller.current.get(), 22);
    }

    #[test]
    fn hardware_get_failure_is_reported_as_failure_not_a_panic() {
        let controller = FakeVolumeController { current: Cell::new(22), fail_get: true, fail_set: false };
        let signer = signer();
        let receipt = execute_volume_set(&controller, &request(Some(serde_json::json!(40))), &manifest(), &signer);
        assert_eq!(receipt.result_status, CapabilityResultStatus::Failure);
    }

    #[test]
    fn hardware_set_failure_is_reported_as_failure() {
        let controller = FakeVolumeController { current: Cell::new(22), fail_get: false, fail_set: true };
        let signer = signer();
        let receipt = execute_volume_set(&controller, &request(Some(serde_json::json!(40))), &manifest(), &signer);
        assert_eq!(receipt.result_status, CapabilityResultStatus::Failure);
    }
}
```

- [ ] **Step 2: Run the new tests to verify they fail to compile (the module doesn't exist on `main` yet)**

Run: `cargo test -p mira-guardrail volume:: 2>&1 | head -20`
Expected: FAIL — `error[E0432]: unresolved import` or similar, since `guardrail/src/volume.rs` doesn't exist yet.

- [ ] **Step 3: Wire the module into the crate and into dispatch**

In `guardrail/src/lib.rs`, add alongside the existing `pub mod policy;` / `pub mod signing;` / `pub mod ipc;` lines:

```rust
pub mod volume;
```

In `guardrail/src/ipc.rs`, replace the `dispatch` function (lines 54-87) with:

```rust
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
        PolicyDecision::Allow { manifest } if request.capability == "mac.volume.set" => {
            crate::volume::execute_volume_set(&crate::volume::OsascriptVolumeController, &request, &manifest, signer)
        }
        // Any other allowed capability has no real device handler yet — unchanged from Phase 1.
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

- [ ] **Step 4: Run the unit tests**

Run: `cargo test -p mira-guardrail volume::`
Expected: PASS — all 5 tests in `guardrail/src/volume.rs`'s test module.

- [ ] **Step 5: Write the integration test proving the existing daemon capability-dispatch test suite still passes with a real (non-simulated) handler behind it**

```rust
// guardrail/tests/volume_dispatch_test.rs
use std::sync::Arc;
use tempfile::TempDir;

use mira_guardrail::{DevSigner, PolicyTable};
use mira_guardrail::volume::VolumeController;
use mira_protocol::{CapabilityRequest, Reversibility};

struct FixedVolumeController;
impl VolumeController for FixedVolumeController {
    fn get_volume(&self) -> Result<u8, String> { Ok(55) }
    fn set_volume(&self, _level: u8) -> Result<(), String> { Ok(()) }
}

#[test]
fn allowed_volume_request_produces_signed_success_with_real_previous_level() {
    let dir = TempDir::new().unwrap();
    let signer = Arc::new(DevSigner::load_or_generate(&dir.path().join("key")).unwrap());
    let policy = PolicyTable::dev_default();

    let request = CapabilityRequest {
        request_id: "req-int-1".to_string(),
        trace_id: "trace-int-1".to_string(),
        sender_node: "mac-host".to_string(),
        capability: "mac.volume.set".to_string(),
        arguments: serde_json::json!({ "level": 40 }).as_object().unwrap().clone(),
        reversibility: Reversibility::Reversible,
        requested_at: 0.0,
    };

    let manifest = match policy.evaluate(&request) {
        mira_guardrail::PolicyDecision::Allow { manifest } => manifest,
        other => panic!("expected Allow, got {other:?}"),
    };

    let receipt = mira_guardrail::volume::execute_volume_set(&FixedVolumeController, &request, &manifest, &signer);
    assert_eq!(receipt.result_status, mira_protocol::CapabilityResultStatus::Success);
    let evidence: serde_json::Value = serde_json::from_str(receipt.rollback_evidence.as_ref().unwrap()).unwrap();
    assert_eq!(evidence["previous_level"], 55);
    assert!(signer.verify(&receipt));
}
```

This test uses `mira_guardrail::PolicyDecision` and `mira_guardrail::volume::execute_volume_set` — both already public via `guardrail/src/lib.rs`'s existing `pub use policy::{PolicyDecision, PolicyTable};` re-export plus Step 3's new `pub mod volume;`.

- [ ] **Step 6: Run the full guardrail test suite**

Run: `cargo test -p mira-guardrail`
Expected: PASS — all prior guardrail tests plus the 5 new unit tests and 1 new integration test.

- [ ] **Step 7: Manually verify against real hardware (not automated — no CI runner has this Mac's audio device)**

Run `cargo run --bin mira-guardrail` in one terminal, note your current system volume, then in another terminal use the `websocat` flow from `PROJECT_STATUS.md` (or Task 15's manual checklist) to send a real `mac.volume.set` request with a level clearly different from your current volume. Confirm you actually hear/see the system volume change, and that the receipt's `rollback_evidence` contains the level you started at.

- [ ] **Step 8: Commit**

```bash
git add guardrail/src/volume.rs guardrail/src/lib.rs guardrail/src/ipc.rs guardrail/tests/volume_dispatch_test.rs
git commit -m "feat(guardrail): execute real macOS volume changes for mac.volume.set"
```

---

### Task 8: Voice protocol v2 contracts — `VoiceState` / `VoiceStateEvent`

Adds a new cross-language contract (not a rewrite of the existing unused-elsewhere `SpeechEvent` v1 type) following the exact pattern used for `MiraEnvelopeV2`/`CapabilityRequest` in the prior plan: JSON Schema, Pydantic, TypeScript, Rust, a fixture, and round-trip tests in both Python and Rust.

**Files:**
- Create: `shared/schemas/voice_state_event.json`
- Create: `shared/fixtures/voice_state_event.json`
- Modify: `shared/types/events.py` (append `VoiceState` + `VoiceStateEvent`)
- Modify: `shared/types/index.ts` (append `VoiceState` + `VoiceStateEvent`)
- Modify: `crates/mira-protocol/src/lib.rs` (append `VoiceState` + `VoiceStateEvent`)
- Modify: `shared/tests/test_schemas.py` (add `test_voice_state_event`, add schema filename to `test_schema_files_exist`)
- Modify: `crates/mira-protocol/tests/fixtures_test.rs` (add `voice_state_event_round_trips`)

**Interfaces:**
- Produces: Python `VoiceState(str, Enum)`, `VoiceStateEvent(BaseModel)`; TS `type VoiceState`, `interface VoiceStateEvent`; Rust `pub enum VoiceState`, `pub struct VoiceStateEvent` — all with fields `node_id: str, timestamp: float, state: VoiceState, transcript: Optional[str], is_final: Optional[bool]`.

- [ ] **Step 1: Write the JSON Schema**

```json
// shared/schemas/voice_state_event.json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "VoiceStateEvent",
  "description": "Voice loop lifecycle state transition, broadcast on the 'voice' topic.",
  "type": "object",
  "properties": {
    "node_id": { "type": "string", "minLength": 1 },
    "timestamp": { "type": "number" },
    "state": {
      "type": "string",
      "enum": ["IDLE", "WAKE_DETECTED", "LISTENING", "TRANSCRIBING", "THINKING", "SPEAKING", "INTERRUPTED", "MUTED", "OFFLINE"]
    },
    "transcript": { "type": ["string", "null"] },
    "is_final": { "type": ["boolean", "null"] }
  },
  "required": ["node_id", "timestamp", "state", "transcript", "is_final"],
  "additionalProperties": false
}
```

- [ ] **Step 2: Write the fixture**

```json
// shared/fixtures/voice_state_event.json
{
  "node_id": "mac-host",
  "timestamp": 1755600000.0,
  "state": "TRANSCRIBING",
  "transcript": "set the volume to forty",
  "is_final": true
}
```

- [ ] **Step 3: Write the failing Python test**

Append to `shared/tests/test_schemas.py`'s import list (alphabetical, matching the existing style):

```python
    VoiceState,
    VoiceStateEvent,
```

Add `"voice_state_event.json"` to the `expected_schemas` set in `test_schema_files_exist`. Add this test method after `test_capability_receipt`:

```python
    def test_voice_state_event(self):
        schema = self.schemas["voice_state_event.json"]
        payload = self._load_fixture("voice_state_event.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = VoiceStateEvent(**payload)
        self.assertEqual(model.state, VoiceState.TRANSCRIBING)
        self.assertEqual(model.model_dump(mode="json"), payload)

        # a non-transcript state (e.g. IDLE) validates with null transcript/is_final
        idle_payload = dict(payload, state="IDLE", transcript=None, is_final=None)
        jsonschema.validate(instance=idle_payload, schema=schema)
        idle_model = VoiceStateEvent(**idle_payload)
        self.assertIsNone(idle_model.transcript)

        # unknown state is rejected
        invalid_payload = dict(payload, state="SLEEPING")
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_payload, schema=schema)
        with self.assertRaises(Exception):
            VoiceStateEvent(**invalid_payload)
```

- [ ] **Step 4: Run it to verify it fails**

Run: `python3 -m pytest shared/tests/test_schemas.py::TestSchemasAndModels::test_voice_state_event -v`
Expected: FAIL with `ImportError: cannot import name 'VoiceState'`.

- [ ] **Step 5: Implement the Pydantic model**

Append to `shared/types/events.py` (after `CapabilityReceipt`, at the end of the file):

```python
class VoiceState(str, Enum):
    IDLE = "IDLE"
    WAKE_DETECTED = "WAKE_DETECTED"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    MUTED = "MUTED"
    OFFLINE = "OFFLINE"


class VoiceStateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    timestamp: float
    state: VoiceState
    transcript: Optional[str] = None
    is_final: Optional[bool] = None
```

- [ ] **Step 6: Run the Python tests to verify they pass**

Run: `python3 -m pytest shared/tests/test_schemas.py -v`
Expected: PASS — all existing tests plus `test_voice_state_event`.

- [ ] **Step 7: Add the TypeScript mirror**

Append to `shared/types/index.ts` (after the `CapabilityReceipt` interface):

```typescript
export type VoiceState =
  | 'IDLE'
  | 'WAKE_DETECTED'
  | 'LISTENING'
  | 'TRANSCRIBING'
  | 'THINKING'
  | 'SPEAKING'
  | 'INTERRUPTED'
  | 'MUTED'
  | 'OFFLINE';

export interface VoiceStateEvent {
  node_id: string;
  timestamp: number;
  state: VoiceState;
  transcript: string | null;
  is_final: boolean | null;
}
```

Run: `cd shared && npx tsc --noEmit`
Expected: PASS — no type errors.

- [ ] **Step 8: Write the failing Rust round-trip test**

Append to `crates/mira-protocol/tests/fixtures_test.rs`:

```rust
use mira_protocol::VoiceStateEvent;
```

(add to the existing `use mira_protocol::{...}` import line instead of a new line)

```rust
#[test]
fn voice_state_event_round_trips() {
    let fixture = load_fixture("voice_state_event.json");
    assert_round_trip::<VoiceStateEvent>(&fixture);
}
```

- [ ] **Step 9: Run it to verify it fails**

Run: `cargo test -p mira-protocol voice_state_event_round_trips`
Expected: FAIL — `error[E0432]: unresolved import` (`VoiceStateEvent` doesn't exist in the crate yet).

- [ ] **Step 10: Implement the Rust struct**

Append to `crates/mira-protocol/src/lib.rs`:

```rust
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum VoiceState {
    Idle,
    WakeDetected,
    Listening,
    Transcribing,
    Thinking,
    Speaking,
    Interrupted,
    Muted,
    Offline,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct VoiceStateEvent {
    pub node_id: String,
    pub timestamp: f64,
    pub state: VoiceState,
    pub transcript: Option<String>,
    pub is_final: Option<bool>,
}
```

- [ ] **Step 11: Run the Rust tests to verify they pass**

Run: `cargo test -p mira-protocol`
Expected: PASS — all 4 existing round-trip tests plus `voice_state_event_round_trips`.

- [ ] **Step 12: Commit**

```bash
git add shared/schemas/voice_state_event.json shared/fixtures/voice_state_event.json \
  shared/types/events.py shared/types/index.ts shared/tests/test_schemas.py \
  crates/mira-protocol/src/lib.rs crates/mira-protocol/tests/fixtures_test.rs
git commit -m "feat(protocol): add VoiceStateEvent v2 contract across JSON Schema/Pydantic/TS/Rust"
```

---

### Task 9: `MIRA_IDENTITY.md` v1 and the identity loader

**Files:**
- Create: `MIRA_IDENTITY.md` (repo root, alongside `README.md`/`AGENTS.md`)
- Create: `voice/__init__.py`
- Create: `voice/identity.py`
- Test: `voice/tests/__init__.py`
- Test: `voice/tests/test_identity.py`

**Interfaces:**
- Produces: `def load_identity(path: str | Path = "MIRA_IDENTITY.md") -> str` — raises `FileNotFoundError` with a clear message if the file is missing.

- [ ] **Step 1: Write `MIRA_IDENTITY.md` v1**

```markdown
# MIRA_IDENTITY.md

> Version: 1 (2026-08-19). Personality edits to this file are release-managed once the Agent OS
> pipeline exists (Implementation Sequence step 5); for now it is edited directly, deliberately,
> by hand.

## Who you are

You are Mira, a personal ambient assistant running on Arya's own hardware. You are not a generic
chatbot persona — you are the voice of one specific system: a Rust daemon, a guardrail that
authorizes and executes real actions on this machine, and (eventually) a body of memory and
sensors across a MacBook, a Raspberry Pi projector, and a homelab GPU box. Speak as that system,
not as an assistant pretending to be one.

## Voice and tone

- You are heard, not read. Answers are spoken aloud through TTS — keep them short, use plain
  sentences, and never produce markdown, bullet lists, or code blocks in a spoken answer.
- Be direct and warm, not chipper. Skip filler openers ("Great question!", "Sure thing!") and
  filler closers ("Let me know if you need anything else!").
- If you don't know something or a capability isn't available yet, say so plainly instead of
  guessing or inventing a capability you don't have.

## What you can actually do right now

Be honest about scope. As of this version, you can hold a spoken conversation and change this
Mac's system volume — nothing else is wired up to a real action yet. If asked to do something
else physical or systemic, say clearly that it isn't built yet rather than pretending to comply.

## Boundaries

- Never claim an action succeeded unless a signed `CapabilityReceipt` with `result_status:
  SUCCESS` actually came back from the guardrail. If a capability request is denied or fails,
  say so plainly — do not paper over it.
- Never read back or repeat secrets, tokens, or signing material, even if asked directly.
- You do not have persistent memory across restarts yet (see the voice-loop plan's Global
  Constraints) — if asked about something from a previous session, say you don't remember it
  rather than fabricating a memory.

## Current limitations to be upfront about

- Answers currently come from the local `codex` CLI as a development route, not the intended
  Qwen/Nemotron models on the homelab GPU box — response latency can be a few seconds.
- Memory is a short in-session rolling buffer only; nothing persists once the process restarts.
```

- [ ] **Step 2: Write the failing test**

```python
# voice/tests/test_identity.py
import pytest

from voice.identity import load_identity


def test_load_identity_returns_file_contents(tmp_path):
    identity_file = tmp_path / "MIRA_IDENTITY.md"
    identity_file.write_text("# Test Identity\n\nYou are a test persona.\n")

    result = load_identity(identity_file)

    assert result == "# Test Identity\n\nYou are a test persona.\n"


def test_load_identity_missing_file_raises_clear_error(tmp_path):
    missing = tmp_path / "does_not_exist.md"

    with pytest.raises(FileNotFoundError, match="MIRA identity file not found"):
        load_identity(missing)
```

Create empty `voice/__init__.py` and `voice/tests/__init__.py` (both empty files, matching `shared/`'s package layout) so pytest can import `voice.identity`.

- [ ] **Step 3: Run it to verify it fails**

Run: `python3 -m pytest voice/tests/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voice.identity'`.

- [ ] **Step 4: Implement the loader**

```python
# voice/identity.py
"""Loads M.I.R.A.'s versioned identity document as the voice loop's system prompt."""

from pathlib import Path


def load_identity(path: str | Path = "MIRA_IDENTITY.md") -> str:
    identity_path = Path(path)
    if not identity_path.is_file():
        raise FileNotFoundError(f"MIRA identity file not found at {identity_path}")
    return identity_path.read_text(encoding="utf-8")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest voice/tests/test_identity.py -v`
Expected: PASS — both tests.

- [ ] **Step 6: Commit**

```bash
git add MIRA_IDENTITY.md voice/__init__.py voice/identity.py voice/tests/__init__.py voice/tests/test_identity.py
git commit -m "feat(voice): add MIRA_IDENTITY.md v1 and the identity loader"
```

---

### Task 10: In-session conversation memory

**Files:**
- Create: `voice/memory.py`
- Test: `voice/tests/test_memory.py`

**Interfaces:**
- Produces: `class ConversationBuffer: def __init__(self, max_turns: int = 20); def add(self, role: Literal["user","assistant"], text: str) -> None; def to_history(self) -> list[dict[str, str]]`
- Consumes: nothing (pure in-memory, no I/O)

- [ ] **Step 1: Write the failing tests**

```python
# voice/tests/test_memory.py
from voice.memory import ConversationBuffer


def test_empty_buffer_returns_empty_history():
    buffer = ConversationBuffer()
    assert buffer.to_history() == []


def test_add_preserves_order():
    buffer = ConversationBuffer()
    buffer.add("user", "set the volume to forty")
    buffer.add("assistant", "done, volume is at forty")

    assert buffer.to_history() == [
        {"role": "user", "content": "set the volume to forty"},
        {"role": "assistant", "content": "done, volume is at forty"},
    ]


def test_buffer_evicts_oldest_turns_past_max_turns():
    buffer = ConversationBuffer(max_turns=3)
    for i in range(5):
        buffer.add("user", f"turn {i}")

    history = buffer.to_history()
    assert len(history) == 3
    assert [turn["content"] for turn in history] == ["turn 2", "turn 3", "turn 4"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest voice/tests/test_memory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voice.memory'`.

- [ ] **Step 3: Implement it**

```python
# voice/memory.py
"""In-session, RAM-only rolling conversation buffer.

Deliberately not persistent — full cross-restart memory (PostgreSQL/pgvector) is the separately
queued memory subplan (see TASK_QUEUE.md), not this voice loop.
"""

from collections import deque
from typing import Literal, TypedDict


class Turn(TypedDict):
    role: str
    content: str


class ConversationBuffer:
    def __init__(self, max_turns: int = 20) -> None:
        self._turns: deque[Turn] = deque(maxlen=max_turns)

    def add(self, role: Literal["user", "assistant"], text: str) -> None:
        self._turns.append({"role": role, "content": text})

    def to_history(self) -> list[Turn]:
        return list(self._turns)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest voice/tests/test_memory.py -v`
Expected: PASS — all 3 tests.

- [ ] **Step 5: Commit**

```bash
git add voice/memory.py voice/tests/test_memory.py
git commit -m "feat(voice): add in-session ConversationBuffer"
```

---

### Task 11: Codex client (dev-route "Codex turn")

Shells out to the local, already-authenticated `codex` CLI. Confirmed working shape (verified manually during planning): `codex exec --skip-git-repo-check --sandbox read-only --ephemeral -o <file> "<prompt>"` writes the agent's final reply, and only that, to `<file>`.

**Files:**
- Create: `voice/codex_client.py`
- Test: `voice/tests/test_codex_client.py`

**Interfaces:**
- Produces: `class CodexError(Exception)`, `class CodexClient: def __init__(self, binary: str = "codex", timeout_s: float = 30.0); def generate(self, system_prompt: str, history: list[dict[str, str]], user_text: str) -> str`
- Consumes: `voice.memory.Turn`-shaped dicts (`{"role": ..., "content": ...}`) from `ConversationBuffer.to_history()`

- [ ] **Step 1: Write the failing tests using a fake `codex` executable (no real network/auth needed)**

```python
# voice/tests/test_codex_client.py
import stat
import textwrap

import pytest

from voice.codex_client import CodexClient, CodexError


def _write_fake_codex(tmp_path, script: str) -> str:
    fake = tmp_path / "codex"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return str(fake)


def test_generate_returns_last_message_file_contents(tmp_path):
    # A real `codex exec ... -o <file> ...` writes the reply to whatever path follows -o;
    # this fake mimics that contract without touching the network or real Codex auth.
    fake_codex = _write_fake_codex(tmp_path, textwrap.dedent("""\
        #!/bin/sh
        for i in "$@"; do
          prev="$cur"
          cur="$i"
          if [ "$prev" = "-o" ]; then
            echo "pipeline works" > "$cur"
          fi
        done
        exit 0
        """))

    client = CodexClient(binary=fake_codex)
    result = client.generate(system_prompt="You are Mira.", history=[], user_text="say the test phrase")

    assert result == "pipeline works"


def test_generate_raises_codex_error_on_nonzero_exit(tmp_path):
    fake_codex = _write_fake_codex(tmp_path, "#!/bin/sh\necho 'boom' >&2\nexit 1\n")

    client = CodexClient(binary=fake_codex)
    with pytest.raises(CodexError, match="exited with"):
        client.generate(system_prompt="You are Mira.", history=[], user_text="hello")


def test_generate_raises_codex_error_on_timeout(tmp_path):
    fake_codex = _write_fake_codex(tmp_path, "#!/bin/sh\nsleep 5\n")

    client = CodexClient(binary=fake_codex, timeout_s=0.2)
    with pytest.raises(CodexError, match="timed out"):
        client.generate(system_prompt="You are Mira.", history=[], user_text="hello")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest voice/tests/test_codex_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voice.codex_client'`.

- [ ] **Step 3: Implement it**

```python
# voice/codex_client.py
"""Dev-route Codex turn: shells out to the local, OAuth-authenticated `codex` CLI.

This is the development route named in the canonical plan (the Mac Ollama registry is empty),
not a replacement for the exact P40 models. Response latency is a few seconds — the caller must
be in the THINKING voice-loop state while this runs.
"""

import subprocess
import tempfile
from pathlib import Path


class CodexError(Exception):
    pass


class CodexClient:
    def __init__(self, binary: str = "codex", timeout_s: float = 30.0) -> None:
        self._binary = binary
        self._timeout_s = timeout_s

    def generate(self, system_prompt: str, history: list[dict[str, str]], user_text: str) -> str:
        prompt = self._build_prompt(system_prompt, history, user_text)
        with tempfile.TemporaryDirectory() as tmp_dir:
            reply_path = Path(tmp_dir) / "reply.txt"
            try:
                result = subprocess.run(
                    [
                        self._binary,
                        "exec",
                        "--skip-git-repo-check",
                        "--sandbox",
                        "read-only",
                        "--ephemeral",
                        "-o",
                        str(reply_path),
                        prompt,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexError(f"codex exec timed out after {self._timeout_s}s") from exc

            if result.returncode != 0:
                raise CodexError(f"codex exec exited with {result.returncode}: {result.stderr.strip()}")

            if not reply_path.is_file():
                raise CodexError("codex exec produced no reply file")

            return reply_path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _build_prompt(system_prompt: str, history: list[dict[str, str]], user_text: str) -> str:
        lines = [system_prompt, ""]
        for turn in history:
            lines.append(f"{turn['role']}: {turn['content']}")
        lines.append(f"user: {user_text}")
        return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest voice/tests/test_codex_client.py -v`
Expected: PASS — all 3 tests.

- [ ] **Step 5: Manually verify against the real `codex` CLI (not automated — real Codex auth/network, several seconds per call)**

```bash
python3 -c "
from voice.codex_client import CodexClient
client = CodexClient()
print(client.generate('You are Mira, a terse voice assistant.', [], 'Reply with exactly: pipeline works'))
"
```

Expected output: `pipeline works` (or close to it — real Codex, not a fake).

- [ ] **Step 6: Commit**

```bash
git add voice/codex_client.py voice/tests/test_codex_client.py
git commit -m "feat(voice): add CodexClient dev-route Codex turn"
```

---

### Task 12: `VoiceLoop` state machine (the core FSM)

The heart of this plan: a pure, dependency-injected state machine implementing the 9 voice states, barge-in interruption, and mute — fully unit tested with fakes, no real audio/network/Codex involved.

**Files:**
- Create: `voice/interfaces.py`
- Create: `voice/loop.py`
- Test: `voice/tests/test_loop.py`

**Interfaces:**
- Consumes: `voice.memory.ConversationBuffer`, `voice.identity.load_identity` (via injected `identity_prompt: str`, not the loader directly — keeps `VoiceLoop` decoupled from the filesystem)
- Produces: `class VoiceLoop: def __init__(self, wake, vad, stt, tts, turns, capabilities, events, identity_prompt, memory, muted=False); def run_once(self) -> None; self.state: str` (one of the 9 `VoiceState` values, as a plain string matching the protocol enum)

- [ ] **Step 1: Write the interfaces (Protocols, not ABCs — lighter weight, structural typing matches how fakes are written in tests)**

```python
# voice/interfaces.py
"""Structural interfaces VoiceLoop depends on. Real implementations (Task 14) wrap sherpa-onnx,
silero-vad, faster-whisper, kokoro, and the daemon WebSocket client; tests use plain fakes that
satisfy these Protocols without any real audio, network, or subprocess I/O."""

from typing import Callable, Protocol


class WakeWordEngine(Protocol):
    def listen_for_wake(self) -> None:
        """Blocks until the wake word is detected, then returns."""


class VoiceActivityDetector(Protocol):
    def capture_utterance(self) -> bytes:
        """Blocks, buffering mic audio, until a trailing silence boundary. Returns 16kHz mono PCM16 bytes."""

    def read_frame(self) -> bytes:
        """Blocks until one short (~30ms) mic frame is available. Used to watch for barge-in during SPEAKING."""

    def has_speech(self, frame: bytes) -> bool:
        """Non-blocking. True if the given frame contains speech."""


class SpeechToText(Protocol):
    def transcribe(self, pcm16_audio: bytes) -> str: ...


class TextToSpeech(Protocol):
    def speak(self, text: str, should_cancel: Callable[[], bool]) -> None:
        """Synthesizes and plays audio for text, checking should_cancel() between chunks and
        stopping playback immediately once it returns True."""


class TurnGenerator(Protocol):
    def generate(self, system_prompt: str, history: list[dict[str, str]], user_text: str) -> str: ...


class CapabilityClient(Protocol):
    def request_capability(self, capability: str, arguments: dict) -> dict:
        """Sends a CapabilityRequest and blocks for the CapabilityReceipt payload (as a dict).
        Raises if the daemon is unreachable."""

    def is_connected(self) -> bool: ...


class EventPublisher(Protocol):
    def publish_state(self, state: str, transcript: str | None = None, is_final: bool | None = None) -> None: ...
```

- [ ] **Step 2: Write the failing tests for the happy path, mute, and Codex-failure recovery**

```python
# voice/tests/test_loop.py
import time

from voice.loop import VoiceLoop
from voice.memory import ConversationBuffer


class FakeWake:
    def __init__(self):
        self.calls = 0
    def listen_for_wake(self):
        self.calls += 1


class FakeVad:
    def __init__(self, utterance=b"fake-pcm", speech_frames=None):
        self.utterance = utterance
        self._speech_frames = speech_frames or []
        self._frame_index = 0
    def capture_utterance(self):
        return self.utterance
    def read_frame(self):
        self._frame_index += 1
        return b"frame-%d" % self._frame_index
    def has_speech(self, frame):
        return self._frame_index <= len(self._speech_frames) and self._speech_frames[self._frame_index - 1]


class FakeStt:
    def __init__(self, text="set the volume to 40"):
        self.text = text
        self.call_count = 0
    def transcribe(self, pcm16_audio):
        self.call_count += 1
        return self.text


class FakeTts:
    def __init__(self):
        self.spoken = []
        self.was_cancelled = []
    def speak(self, text, should_cancel):
        self.spoken.append(text)
        # Poll should_cancel with a real sleep between checks (not a tight loop) so the
        # background barge-in monitor thread (a real OS thread in VoiceLoop) actually gets
        # scheduled to run and flip the flag — a 0-sleep loop can finish before the GIL ever
        # hands control to the monitor thread, making the interruption assertion flaky.
        # Non-interrupted tests pay this as a bounded ~50ms cost, not an open-ended wait.
        for _ in range(10):
            if should_cancel():
                self.was_cancelled.append(text)
                return
            time.sleep(0.005)


class FakeTurns:
    def __init__(self, reply="volume is now forty", raise_error=False):
        self.reply = reply
        self.raise_error = raise_error
        self.calls = []
    def generate(self, system_prompt, history, user_text):
        self.calls.append((system_prompt, list(history), user_text))
        if self.raise_error:
            raise RuntimeError("simulated Codex failure")
        return self.reply


class FakeCapabilities:
    def __init__(self, connected=True):
        self.connected = connected
        self.requests = []
    def request_capability(self, capability, arguments):
        self.requests.append((capability, arguments))
        return {"result_status": "SUCCESS"}
    def is_connected(self):
        return self.connected


class FakeEvents:
    def __init__(self):
        self.published = []
    def publish_state(self, state, transcript=None, is_final=None):
        self.published.append((state, transcript, is_final))


def make_loop(**overrides):
    defaults = dict(
        wake=FakeWake(),
        vad=FakeVad(),
        stt=FakeStt(),
        tts=FakeTts(),
        turns=FakeTurns(),
        capabilities=FakeCapabilities(),
        events=FakeEvents(),
        identity_prompt="You are Mira.",
        memory=ConversationBuffer(),
    )
    defaults.update(overrides)
    return VoiceLoop(**defaults)


def test_happy_path_visits_every_state_in_order_and_returns_to_idle():
    loop = make_loop()
    loop.run_once()

    states = [state for state, _, _ in loop.events.published]
    assert states == [
        "WAKE_DETECTED", "LISTENING", "TRANSCRIBING", "THINKING", "SPEAKING", "IDLE",
    ]
    assert loop.state == "IDLE"


def test_happy_path_speaks_the_codex_reply_and_updates_memory():
    loop = make_loop()
    loop.run_once()

    assert loop.tts.spoken == ["volume is now forty"]
    history = loop.memory.to_history()
    assert history[-2] == {"role": "user", "content": "set the volume to 40"}
    assert history[-1] == {"role": "assistant", "content": "volume is now forty"}


def test_muted_ignores_wake_and_never_transcribes():
    loop = make_loop(muted=True)
    loop.run_once()

    assert loop.state == "MUTED"
    assert loop.stt.call_count == 0
    assert loop.events.published == [("MUTED", None, None)]


def test_codex_failure_speaks_an_apology_and_returns_to_idle_without_crashing():
    loop = make_loop(turns=FakeTurns(raise_error=True))
    loop.run_once()  # must not raise

    assert loop.state == "IDLE"
    assert len(loop.tts.spoken) == 1
    assert "trouble" in loop.tts.spoken[0].lower() or "sorry" in loop.tts.spoken[0].lower()


def test_offline_when_daemon_unreachable_skips_the_turn():
    loop = make_loop(capabilities=FakeCapabilities(connected=False))
    loop.run_once()

    assert loop.state == "OFFLINE"
    assert loop.turns.calls == []


def test_barge_in_interrupts_speaking_and_returns_to_listening():
    # speech_frames=[False, True] means the 2nd frame read during SPEAKING looks like speech
    vad = FakeVad(speech_frames=[False, True])
    loop = make_loop(vad=vad)
    loop.run_once()

    assert "volume is now forty" in loop.tts.was_cancelled
    states = [state for state, _, _ in loop.events.published]
    assert "INTERRUPTED" in states
    # after an interruption the loop re-enters LISTENING rather than settling at IDLE
    assert states.index("INTERRUPTED") > states.index("SPEAKING")


def test_only_mac_volume_set_capability_is_ever_requested():
    loop = make_loop()
    loop.run_once()

    assert loop.capabilities.requests, "the '40' in the transcript should have triggered a capability request"
    for capability, _arguments in loop.capabilities.requests:
        assert capability == "mac.volume.set"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python3 -m pytest voice/tests/test_loop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'voice.loop'`.

- [ ] **Step 4: Implement `VoiceLoop`**

```python
# voice/loop.py
"""The voice loop's 9-state FSM: IDLE -> WAKE_DETECTED -> LISTENING -> TRANSCRIBING -> THINKING
-> SPEAKING -> IDLE, with INTERRUPTED (barge-in), MUTED, and OFFLINE as the non-linear states.

Deliberately holds no real audio/network/subprocess code — everything it touches is injected via
the Protocols in voice/interfaces.py, so this file's tests never need a microphone, speaker, or
network connection."""

import re
import threading

from voice.memory import ConversationBuffer

_VOLUME_PATTERN = re.compile(r"volume.{0,20}?(\d{1,3})", re.IGNORECASE)


class VoiceLoop:
    def __init__(self, wake, vad, stt, tts, turns, capabilities, events, identity_prompt, memory: ConversationBuffer, muted: bool = False):
        self.wake = wake
        self.vad = vad
        self.stt = stt
        self.tts = tts
        self.turns = turns
        self.capabilities = capabilities
        self.events = events
        self.identity_prompt = identity_prompt
        self.memory = memory
        self.muted = muted
        self.state = "IDLE"

    def run_once(self) -> None:
        self.wake.listen_for_wake()

        if not self.capabilities.is_connected():
            self._set_state("OFFLINE")
            return

        if self.muted:
            self._set_state("MUTED")
            return

        self._set_state("WAKE_DETECTED")
        self._listen_and_respond()

    def _listen_and_respond(self) -> None:
        self._set_state("LISTENING")
        audio = self.vad.capture_utterance()

        # Set state directly (not via _set_state) to avoid a duplicate TRANSCRIBING publish —
        # the explicit publish_state call two lines down is the one and only TRANSCRIBING event,
        # carrying the transcript that a bare _set_state("TRANSCRIBING") wouldn't have yet.
        self.state = "TRANSCRIBING"
        transcript = self.stt.transcribe(audio)
        self.events.publish_state("TRANSCRIBING", transcript=transcript, is_final=True)
        self.memory.add("user", transcript)

        self._set_state("THINKING")
        try:
            reply = self.turns.generate(self.identity_prompt, self.memory.to_history(), transcript)
        except Exception:
            reply = "Sorry, I ran into trouble thinking that through."
        self.memory.add("assistant", reply)

        self._maybe_dispatch_capability(transcript)

        interrupted = self._speak(reply)
        if interrupted:
            self._listen_and_respond()
        else:
            self._set_state("IDLE")

    def _speak(self, text: str) -> bool:
        self._set_state("SPEAKING")
        interrupted_flag = threading.Event()
        stop_monitor = threading.Event()

        def monitor():
            while not stop_monitor.is_set():
                frame = self.vad.read_frame()
                if self.vad.has_speech(frame):
                    interrupted_flag.set()
                    return

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        try:
            self.tts.speak(text, should_cancel=interrupted_flag.is_set)
        finally:
            stop_monitor.set()
            monitor_thread.join(timeout=1.0)

        if interrupted_flag.is_set():
            self._set_state("INTERRUPTED")
            return True
        return False

    def _maybe_dispatch_capability(self, transcript: str) -> None:
        match = _VOLUME_PATTERN.search(transcript)
        if match is None:
            return
        level = min(int(match.group(1)), 100)
        self.capabilities.request_capability("mac.volume.set", {"level": level})

    def _set_state(self, state: str) -> None:
        self.state = state
        transcript = None
        is_final = None
        self.events.publish_state(state, transcript=transcript, is_final=is_final)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest voice/tests/test_loop.py -v`
Expected: PASS — all 7 tests.

- [ ] **Step 6: Commit**

```bash
git add voice/interfaces.py voice/loop.py voice/tests/test_loop.py
git commit -m "feat(voice): add VoiceLoop 9-state FSM with barge-in and mute"
```

---

### Task 13: `DaemonClient` — real WebSocket client against the real daemon

**Files:**
- Create: `voice/daemon_client.py`
- Create: `voice/requirements.txt` (starts with just `websockets`; Task 14 appends the ML libraries)
- Test: `voice/tests/test_daemon_client.py`

**Interfaces:**
- Produces: `class DaemonClient: def __init__(self, node_id: str, uri: str = "ws://127.0.0.1:8080/ws"); def connect(self) -> None; def close(self) -> None; def is_connected(self) -> bool; def publish_state(self, state, transcript=None, is_final=None) -> None; def request_capability(self, capability: str, arguments: dict, timeout_s: float = 5.0) -> dict` — implements both `CapabilityClient` and `EventPublisher` from Task 12.
- Consumes: the real `mira-daemon` binary over `ws://127.0.0.1:8080/ws`, wire-compatible with `MiraEnvelopeV2` from `shared/schemas/mira_envelope_v2.json`.

- [ ] **Step 1: Add `websockets` to a new `voice/requirements.txt`**

```
websockets>=13.0
```

- [ ] **Step 2: Write the implementation**

```python
# voice/daemon_client.py
"""Real WebSocket client to the Rust mira-daemon. Owns a background asyncio event loop thread so
VoiceLoop (synchronous by design — see Task 12) can call it with plain blocking method calls."""

import asyncio
import json
import threading
import time
import uuid
from typing import Optional

import websockets


class DaemonClient:
    def __init__(self, node_id: str, uri: str = "ws://127.0.0.1:8080/ws"):
        self.node_id = node_id
        self.uri = uri
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._ws = None
        self._connected = threading.Event()
        self._pending: dict[str, "asyncio.Future"] = {}
        self._thread.start()

    def connect(self, timeout_s: float = 5.0) -> None:
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        future.result(timeout=timeout_s)

    async def _connect(self) -> None:
        self._ws = await websockets.connect(self.uri)
        register = self._envelope("REGISTER", "system", {"topics": ["voice", "capability"]})
        await self._ws.send(json.dumps(register))
        await self._ws.recv()  # REGISTER_ACK
        self._connected.set()
        asyncio.ensure_future(self._read_loop(), loop=self._loop)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                envelope = json.loads(message)
                if envelope.get("event_kind") == "CAPABILITY_RECEIPT":
                    ack_ref = envelope.get("ack_ref")
                    future = self._pending.pop(ack_ref, None)
                    if future is not None and not future.done():
                        future.set_result(envelope["payload"])
        except websockets.ConnectionClosed:
            self._connected.clear()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def close(self) -> None:
        if self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop).result(timeout=2.0)
        self._loop.call_soon_threadsafe(self._loop.stop)

    def publish_state(self, state: str, transcript: Optional[str] = None, is_final: Optional[bool] = None) -> None:
        envelope = self._envelope(
            "EVENT", "voice",
            {"node_id": self.node_id, "timestamp": time.time(), "state": state, "transcript": transcript, "is_final": is_final},
        )
        asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(envelope)), self._loop).result(timeout=2.0)

    def request_capability(self, capability: str, arguments: dict, timeout_s: float = 5.0) -> dict:
        event_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        envelope = self._envelope(
            "CAPABILITY_REQUEST", "capability",
            {
                "request_id": request_id,
                "trace_id": trace_id,
                "sender_node": self.node_id,
                "capability": capability,
                "arguments": arguments,
                "reversibility": "REVERSIBLE",
                "requested_at": time.time(),
            },
            event_id=event_id,
            trace_id=trace_id,
        )

        future: "asyncio.Future" = self._loop.create_future()

        def register_pending():
            self._pending[event_id] = future

        self._loop.call_soon_threadsafe(register_pending)
        asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(envelope)), self._loop).result(timeout=2.0)

        return asyncio.run_coroutine_threadsafe(asyncio.wait_for(future, timeout_s), self._loop).result(timeout=timeout_s + 1)

    def _envelope(self, event_kind: str, topic: str, payload: dict, event_id: Optional[str] = None, trace_id: Optional[str] = None) -> dict:
        return {
            "protocol_version": 2,
            "event_id": event_id or str(uuid.uuid4()),
            "trace_id": trace_id or str(uuid.uuid4()),
            "topic": topic,
            "event_kind": event_kind,
            "sender_node": self.node_id,
            "timestamp": time.time(),
            "auth_context": {"node_id": self.node_id, "auth_mode": "LOOPBACK_TRUSTED", "token": None, "cert_fingerprint": None},
            "payload": payload,
            "ack_ref": None,
        }
```

- [ ] **Step 3: Write the integration test against the real, already-built daemon and guardrail binaries**

```python
# voice/tests/test_daemon_client.py
"""Integration test against the real Rust daemon+guardrail, mirroring
daemon/tests/capability_dispatch_test.rs from the Python side. Requires `cargo build` to have
already produced target/debug/mira-daemon and target/debug/mira-guardrail."""

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from voice.daemon_client import DaemonClient

REPO_ROOT = Path(__file__).resolve().parents[2]
DAEMON_BIN = REPO_ROOT / "target" / "debug" / "mira-daemon"
GUARDRAIL_BIN = REPO_ROOT / "target" / "debug" / "mira-guardrail"


def _port_open(host, port, timeout=0.2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def daemon_stack():
    if not DAEMON_BIN.exists() or not GUARDRAIL_BIN.exists():
        pytest.skip("mira-daemon/mira-guardrail not built — run `cargo build` first")

    with tempfile.TemporaryDirectory() as tmp_dir:
        sock_path = Path(tmp_dir) / "guardrail.sock"
        key_path = Path(tmp_dir) / "guardrail.key"
        env = dict(os.environ, MIRA_GUARDRAIL_SOCK=str(sock_path), MIRA_GUARDRAIL_KEY=str(key_path))

        guardrail_proc = subprocess.Popen([str(GUARDRAIL_BIN)], env=env)
        daemon_proc = subprocess.Popen([str(DAEMON_BIN)], env=env)
        try:
            for _ in range(50):
                if _port_open("127.0.0.1", 8080):
                    break
                time.sleep(0.1)
            else:
                pytest.fail("mira-daemon did not open port 8080 in time")
            yield
        finally:
            daemon_proc.terminate()
            guardrail_proc.terminate()
            daemon_proc.wait(timeout=5)
            guardrail_proc.wait(timeout=5)


def test_register_and_volume_capability_round_trip(daemon_stack):
    client = DaemonClient(node_id="voice-test-node")
    client.connect()
    assert client.is_connected()

    client.publish_state("IDLE")  # must not raise

    receipt = client.request_capability("mac.volume.set", {"level": 33})
    assert receipt["result_status"] in ("SUCCESS", "FAILURE")  # FAILURE is valid on a non-macOS CI runner
    assert receipt["capability"] == "mac.volume.set"

    client.close()
```

- [ ] **Step 4: Build the Rust binaries and run the integration test**

Run: `cargo build && python3 -m pytest voice/tests/test_daemon_client.py -v`
Expected: PASS. On a non-macOS CI runner the receipt legitimately comes back `FAILURE` (Task 7's `osascript` call fails) rather than `SUCCESS` — the test accepts both, since it's proving the wire protocol round-trips correctly, not that this machine has speakers.

- [ ] **Step 5: Commit**

```bash
git add voice/daemon_client.py voice/requirements.txt voice/tests/test_daemon_client.py
git commit -m "feat(voice): add DaemonClient real WebSocket client with integration test"
```

---

### Task 14: Real hardware adapters (wake word, VAD, STT, TTS, audio I/O)

These wrap real ML libraries and real audio hardware. They satisfy the Protocols from Task 12 but are not meaningfully unit-testable without a microphone and speaker — tests here are constructor/smoke tests only (no accuracy assertions). Real verification is manual, in Task 15.

**Files:**
- Create: `voice/adapters/__init__.py`
- Create: `voice/adapters/audio_io.py`
- Create: `voice/adapters/wake.py`
- Create: `voice/adapters/vad.py`
- Create: `voice/adapters/stt.py`
- Create: `voice/adapters/tts.py`
- Modify: `voice/requirements.txt` (append ML libraries)
- Test: `voice/tests/test_adapters_smoke.py`

**Interfaces:**
- Produces: `SherpaWakeEngine`, `SileroVad`, `FasterWhisperStt`, `KokoroTts` — each satisfying its matching Protocol from `voice/interfaces.py`.
- Consumes: `voice.interfaces.{WakeWordEngine, VoiceActivityDetector, SpeechToText, TextToSpeech}` (structural — no explicit inheritance needed, but each adapter's method names/signatures must match exactly)

- [ ] **Step 1: Append ML dependencies to `voice/requirements.txt`**

```
websockets>=13.0
sherpa-onnx>=1.10.0
silero-vad>=6.0
faster-whisper>=1.0
kokoro>=0.9.4
soundfile>=0.12
sounddevice>=0.5
numpy>=1.26
```

Note in a comment above `silero-vad` in the file: `# pulls in torch — first install is a large, slow download; that's expected.`

- [ ] **Step 2: Install system audio dependency and Python packages**

Run: `brew install portaudio && python3 -m pip install -r voice/requirements.txt`

This step has no automated test — it is an environment setup step. Confirm it succeeded by running `python3 -c "import sounddevice, sherpa_onnx, faster_whisper, kokoro; print('ok')"` and expecting `ok`.

- [ ] **Step 3: Download and prepare the Sherpa-ONNX English keyword-spotting model with "mira" as the wake keyword**

```bash
mkdir -p voice/models/kws
cd voice/models/kws
curl -L -o model.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
tar xjf model.tar.bz2 --strip-components=1
rm model.tar.bz2
echo "mira" > wake_word_raw.txt
sherpa-onnx-cli text2token --tokens tokens.txt --tokens-type bpe --bpe-model bpe.model wake_word_raw.txt keywords_raw.txt
# keywords_raw.txt now has the BPE-tokenized line; keywords.txt needs the @-prefixed original
# word appended per the format sherpa-onnx-cli documents — do this by hand once, then commit
# keywords.txt (it is small, deterministic, and only needs regenerating if the wake word changes).
paste -d' ' keywords_raw.txt <(echo "@mira") > keywords.txt
cd -
```

Add `voice/models/` to `.gitignore` except `voice/models/kws/keywords.txt` and `voice/models/kws/tokens.txt`/`bpe.model` if small enough to commit — otherwise document the download step in `voice/adapters/wake.py`'s module docstring so a fresh clone can reproduce it. Check `voice/models/kws/` directory size with `du -sh voice/models/kws` before deciding; if it's over a few MB, `.gitignore` the whole directory and keep only this plan's download recipe as the source of truth.

- [ ] **Step 4: Write the wake word adapter**

```python
# voice/adapters/wake.py
"""Real Sherpa-ONNX open-vocabulary keyword spotter, wired to detect the wake word "mira".
Model and keyword file prepared per this plan's Task 14 Step 3 into voice/models/kws/."""

from pathlib import Path

import sherpa_onnx

from voice.adapters.audio_io import MicStream

_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "kws"
_SAMPLE_RATE = 16000


class SherpaWakeEngine:
    def __init__(self, mic: MicStream, model_dir: Path = _MODEL_DIR):
        self._mic = mic
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / "encoder.onnx"),
            decoder=str(model_dir / "decoder.onnx"),
            joiner=str(model_dir / "joiner.onnx"),
            keywords_file=str(model_dir / "keywords.txt"),
            num_threads=2,
            provider="cpu",
        )

    def listen_for_wake(self) -> None:
        stream = self._kws.create_stream()
        while True:
            frame = self._mic.read_frame()
            stream.accept_waveform(_SAMPLE_RATE, frame)
            while self._kws.is_ready(stream):
                self._kws.decode_stream(stream)
            if self._kws.get_result(stream) != "":
                self._kws.reset_stream(stream)
                return
```

- [ ] **Step 5: Write the audio I/O module (mic capture + speaker playback, never persisting pre-wake audio to disk)**

```python
# voice/adapters/audio_io.py
"""Real microphone capture and speaker playback via sounddevice. The pre-wake ring buffer this
module reads from is in-memory only (a bounded numpy buffer) and is never written to disk, per
this plan's Global Constraints."""

import numpy as np
import sounddevice as sd

_SAMPLE_RATE = 16000
_FRAME_MS = 30
_FRAME_SAMPLES = _SAMPLE_RATE * _FRAME_MS // 1000


class MicStream:
    """Blocking, frame-at-a-time mic reader. One InputStream is opened for the process lifetime;
    read_frame() blocks on an internal queue fed by the audio callback."""

    def __init__(self, sample_rate: int = _SAMPLE_RATE, frame_samples: int = _FRAME_SAMPLES):
        import queue
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=50)
        self._stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            blocksize=frame_samples,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        self._queue.put_nowait(bytes(indata))

    def read_frame(self) -> bytes:
        return self._queue.get()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


def play_pcm(audio: "np.ndarray", sample_rate: int) -> None:
    sd.play(audio, samplerate=sample_rate, blocking=False)


def stop_playback() -> None:
    sd.stop()
```

- [ ] **Step 6: Write the Silero VAD adapter**

```python
# voice/adapters/vad.py
"""Real Silero VAD, used both to capture a full utterance (LISTENING) and to watch for barge-in
speech one short frame at a time (SPEAKING)."""

import numpy as np
import torch
from silero_vad import load_silero_vad

from voice.adapters.audio_io import MicStream, _SAMPLE_RATE

_SILENCE_TAIL_MS = 350
_SPEECH_PROB_THRESHOLD = 0.5


class SileroVad:
    def __init__(self, mic: MicStream):
        self._mic = mic
        self._model = load_silero_vad()

    def capture_utterance(self) -> bytes:
        frames: list[bytes] = []
        silence_ms = 0
        while True:
            frame = self._mic.read_frame()
            frames.append(frame)
            if self.has_speech(frame):
                silence_ms = 0
            else:
                silence_ms += 30
                if silence_ms >= _SILENCE_TAIL_MS and len(frames) > 1:
                    break
        return b"".join(frames)

    def read_frame(self) -> bytes:
        return self._mic.read_frame()

    def has_speech(self, frame: bytes) -> bool:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(samples)
        with torch.no_grad():
            prob = self._model(tensor, _SAMPLE_RATE).item()
        return prob >= _SPEECH_PROB_THRESHOLD
```

- [ ] **Step 7: Write the faster-whisper STT adapter**

```python
# voice/adapters/stt.py
"""Real faster-whisper STT, running the small English model in INT8 on CPU (Apple Silicon
Metal/GPU acceleration is a later optimization, not required for this plan's MVP)."""

import numpy as np
from faster_whisper import WhisperModel

_SAMPLE_RATE = 16000


class FasterWhisperStt:
    def __init__(self, model_size: str = "small.en", device: str = "cpu", compute_type: str = "int8"):
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, pcm16_audio: bytes) -> str:
        samples = np.frombuffer(pcm16_audio, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(samples, beam_size=5)
        return " ".join(segment.text.strip() for segment in segments).strip()
```

- [ ] **Step 8: Write the Kokoro TTS adapter, honoring `should_cancel` between chunks**

```python
# voice/adapters/tts.py
"""Real Kokoro TTS. Synthesizes per-sentence chunks so playback can be cancelled between them for
barge-in support — Kokoro's generator already yields per-segment audio, which this adapter plays
one segment at a time via sounddevice, checking should_cancel() before each."""

from typing import Callable

from kokoro import KPipeline

from voice.adapters.audio_io import play_pcm, stop_playback

_SAMPLE_RATE = 24000


class KokoroTts:
    def __init__(self, lang_code: str = "a", voice: str = "af_heart"):
        self._pipeline = KPipeline(lang_code=lang_code)
        self._voice = voice

    def speak(self, text: str, should_cancel: Callable[[], bool]) -> None:
        generator = self._pipeline(text, voice=self._voice, speed=1, split_pattern=r"\n+|(?<=[.!?])\s+")
        for _graphemes, _phonemes, audio in generator:
            if should_cancel():
                stop_playback()
                return
            play_pcm(audio, _SAMPLE_RATE)
```

- [ ] **Step 9: Write smoke tests (constructor/import-only — no accuracy assertions, no real hardware I/O)**

```python
# voice/tests/test_adapters_smoke.py
"""These adapters wrap real ML libraries and real audio hardware — they cannot be meaningfully
unit tested without a microphone/speaker. These smoke tests only prove the modules import
cleanly and their pure-computation helpers behave; real behavior is verified manually (Task 15)."""

import numpy as np
import pytest


def test_stt_adapter_imports_and_handles_silence_without_crashing():
    pytest.importorskip("faster_whisper")
    from voice.adapters.stt import FasterWhisperStt

    stt = FasterWhisperStt(model_size="tiny.en")  # smallest model, fastest to download for this smoke test
    silence = np.zeros(16000, dtype=np.int16).tobytes()  # 1 second of digital silence
    result = stt.transcribe(silence)
    assert isinstance(result, str)


def test_all_adapter_modules_import_cleanly():
    pytest.importorskip("sherpa_onnx")
    pytest.importorskip("silero_vad")
    pytest.importorskip("kokoro")
    import voice.adapters.wake  # noqa: F401
    import voice.adapters.vad  # noqa: F401
    import voice.adapters.tts  # noqa: F401
    import voice.adapters.audio_io  # noqa: F401
```

- [ ] **Step 10: Run the smoke tests**

Run: `python3 -m pytest voice/tests/test_adapters_smoke.py -v`
Expected: PASS, or cleanly SKIPPED with a clear reason if a library isn't installed in this environment (`pytest.importorskip`) — both are acceptable outcomes for this step; a hard failure is not.

- [ ] **Step 11: Commit**

```bash
git add voice/adapters/ voice/requirements.txt voice/tests/test_adapters_smoke.py voice/models/kws/keywords.txt .gitignore
git commit -m "feat(voice): add real Sherpa-ONNX/Silero/faster-whisper/Kokoro hardware adapters"
```

---

### Task 15: Entrypoint and end-to-end manual verification

**Files:**
- Create: `voice/main.py`
- Modify: `PROJECT_STATUS.md` (mark Phase 2 voice loop as done, update readiness grid)
- Modify: `TASK_QUEUE.md` (move this plan's line from Open to Done)
- Modify: `CHECKPOINT_LAST.md`

**Interfaces:**
- Consumes: every class produced by Tasks 8-14.

- [ ] **Step 1: Write the entrypoint**

```python
# voice/main.py
"""Runnable entrypoint: `python3 -m voice.main`. Wires the real adapters, DaemonClient, and
CodexClient into VoiceLoop and runs it forever. Requires mira-guardrail and mira-daemon already
running (see this plan's Task 15 manual verification checklist)."""

import sys

from voice.adapters.audio_io import MicStream
from voice.adapters.stt import FasterWhisperStt
from voice.adapters.tts import KokoroTts
from voice.adapters.vad import SileroVad
from voice.adapters.wake import SherpaWakeEngine
from voice.codex_client import CodexClient
from voice.daemon_client import DaemonClient
from voice.identity import load_identity
from voice.loop import VoiceLoop
from voice.memory import ConversationBuffer


def main() -> None:
    identity_prompt = load_identity()
    mic = MicStream()
    daemon = DaemonClient(node_id="mac-host-voice")
    daemon.connect()

    loop = VoiceLoop(
        wake=SherpaWakeEngine(mic),
        vad=SileroVad(mic),
        stt=FasterWhisperStt(),
        tts=KokoroTts(),
        turns=CodexClient(),
        capabilities=daemon,
        events=daemon,
        identity_prompt=identity_prompt,
        memory=ConversationBuffer(),
    )

    print("Mira voice loop running. Say \"Mira\" to begin.", file=sys.stderr)
    while True:
        loop.run_once()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Manual end-to-end verification (not automated — real mic, real speaker, real Codex, real system volume)**

1. Terminal 1: `cargo run --bin mira-guardrail`
2. Terminal 2: `cargo run --bin mira-daemon`
3. Terminal 3: `python3 -m voice.main`
4. Say "Mira" clearly, wait for it to start listening, then say "set the volume to forty."
5. Confirm: the system volume actually changes to 40, Mira speaks a confirmation back, and terminal 2's daemon logs show a `CAPABILITY_RECEIPT` with `result_status: SUCCESS`.
6. Say "Mira" again, then start talking again before Mira finishes her first reply from step 4 (or ask an open-ended question that produces a longer reply) — confirm playback stops immediately and the loop starts listening to the new utterance (barge-in).
7. Say "Mira, mute yourself" is **not** wired to anything yet (mute is only exercised via the `muted=True` constructor arg in tests) — do not expect this phrase to do anything; this is expected and not a bug.
8. Kill the daemon (Ctrl-C on terminal 2) while the voice loop is running, then say "Mira" again — confirm the loop reports `OFFLINE` rather than crashing.

- [ ] **Step 3: Update project tracking docs**

In `PROJECT_STATUS.md`'s Component Readiness Grid, add a row:

```markdown
| **Voice Loop** | [`plans/2026-08-19-mira-voice-loop-plan.md`](./docs/superpowers/plans/2026-08-19-mira-voice-loop-plan.md) | ✅ Complete (Phase 1/dev scope) | `voice/` — Sherpa-ONNX wake, Silero VAD, faster-whisper STT, Kokoro TTS, Codex dev-route turn, real macOS volume capability with rollback evidence |
```

Update the top status block's "Next up" line to point at whatever Implementation Sequence step is picked up next (step 4: Tauri menu-bar app / projector simulator, per the canonical plan).

In `TASK_QUEUE.md`, move the "Define and execute the voice loop subplan" line from `## Open` to `## Done`, and add the new deferred items this plan intentionally punted (Porcupine fallback, Piper fallback, persistent memory, automated rollback triggering) as a short new `## Open` entry pointing at their respective future subplans.

- [ ] **Step 4: Update `CHECKPOINT_LAST.md`**

Record what was completed (Tasks 7-15), the manual verification results from Step 2, and the next action (Implementation Sequence step 4).

- [ ] **Step 5: Run the full test suite one more time**

Run: `cargo test --workspace && python3 -m pytest voice/ shared/tests/ -v && cd shared && npx tsc --noEmit`
Expected: PASS across all three.

- [ ] **Step 6: Commit**

```bash
git add voice/main.py PROJECT_STATUS.md TASK_QUEUE.md CHECKPOINT_LAST.md
git commit -m "feat(voice): wire up voice loop entrypoint, verify end-to-end, update project tracking"
```
