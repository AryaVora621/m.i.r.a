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
