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
fn denies_irreversible_by_default() {
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

#[test]
fn denies_unknown_by_default() {
    let mut table = PolicyTable::dev_default();
    table.register(mira_protocol::ActionManifest {
        capability: "mac.mystery_action".to_string(),
        reversibility: Reversibility::Unknown,
        rollback_plan: None,
        credential_scope: vec!["mac.disk".to_string()],
        risk_notes: "Reversibility not yet classified.".to_string(),
    });
    let decision = table.evaluate(&request("mac.mystery_action", Reversibility::Unknown));
    assert!(matches!(decision, PolicyDecision::Deny { .. }));
}
