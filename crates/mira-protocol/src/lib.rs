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
