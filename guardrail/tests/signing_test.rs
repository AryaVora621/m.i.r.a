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
