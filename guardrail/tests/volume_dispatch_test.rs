use std::sync::Arc;
use tempfile::TempDir;

use mira_guardrail::volume::VolumeController;
use mira_guardrail::{DevSigner, PolicyTable};
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
