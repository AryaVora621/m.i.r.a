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
