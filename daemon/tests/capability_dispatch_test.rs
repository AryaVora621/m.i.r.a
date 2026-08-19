use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use futures_util::SinkExt;
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
