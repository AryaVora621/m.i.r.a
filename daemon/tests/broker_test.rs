use std::net::SocketAddr;
use std::time::{SystemTime, UNIX_EPOCH};

use futures_util::SinkExt;
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

    // Prove the connection is still alive and functional (not just that it didn't crash
    // synchronously): a second peer publishes a valid event on a topic this connection is
    // subscribed to, and we assert it still arrives. If the server had closed the socket
    // after sending the ERROR, this would time out.
    let (mut ws_other, _) = connect_async(format!("ws://{addr}/ws")).await.unwrap();
    ws_other
        .send(WsMessage::Text(serde_json::to_string(&register_envelope("node-other", &[])).unwrap()))
        .await
        .unwrap();
    let _ack_other = ws_other.next().await.unwrap().unwrap();

    let published = event_envelope("node-other", "topic-1");
    ws_other.send(WsMessage::Text(serde_json::to_string(&published).unwrap())).await.unwrap();

    let msg = tokio::time::timeout(std::time::Duration::from_secs(2), ws.next())
        .await
        .expect("connection should still be open and receiving broadcasts after the malformed-JSON error")
        .unwrap()
        .unwrap();
    let received: MiraEnvelopeV2 = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    assert_eq!(received.event_id, published.event_id);

    // And the connection can still send too: a second malformed frame still gets a second
    // ERROR envelope rather than silence (which would indicate a dead/half-closed socket).
    ws.send(WsMessage::Text("still not valid json".into())).await.unwrap();
    let msg = ws.next().await.unwrap().unwrap();
    let err2: MiraEnvelopeV2 = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    assert_eq!(err2.event_kind, EventKind::Error);
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
