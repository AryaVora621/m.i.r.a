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

    // Subscribe before registering: registry.register() makes this node visible to other
    // connections' registry.contains()/broadcasts immediately, and the ack send below is a
    // real async I/O flush that yields the task. If we subscribed after that yield, any event
    // published by another node during the window between registration and subscription would
    // be silently lost. A broadcast::Receiver created early just buffers until we start reading.
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

    // Outbound channel for capability-dispatch replies. GuardrailClient::dispatch can take up
    // to its full 5s timeout on a slow/stuck guardrail; if we awaited it inline inside the
    // select! loop below, this connection would stop polling the broadcast arm (and incoming
    // reads) for that whole window, which can silently drop unrelated broadcast events for this
    // node (Lagged(_) => continue) or miss a Close frame. So each CapabilityRequest is dispatched
    // on its own spawned task that reports back over this channel, and a dedicated select! arm
    // drains it — the guardrail round trip no longer blocks anything else on this connection.
    let (capability_tx, mut capability_rx) = tokio::sync::mpsc::unbounded_channel::<MiraEnvelopeV2>();

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
                                        let guardrail = guardrail.clone();
                                        let capability_tx = capability_tx.clone();
                                        tokio::spawn(async move {
                                            let receipt = guardrail.dispatch(&request).await;
                                            let reply = capability_receipt_envelope(&request_event_id, receipt);
                                            let _ = capability_tx.send(reply);
                                        });
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
            capability_reply = capability_rx.recv() => {
                if let Some(reply) = capability_reply {
                    if !send_envelope(&mut socket, &reply).await { break; }
                }
            }
        }
    }

    registry.deregister(&node_id).await;
}
