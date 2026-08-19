use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::{broadcast, RwLock};

use mira_protocol::MiraEnvelopeV2;

#[derive(Debug)]
pub struct NodeHandle {
    pub node_id: String,
    pub topics: HashSet<String>,
}

#[derive(Clone)]
pub struct Registry {
    nodes: Arc<RwLock<HashMap<String, NodeHandle>>>,
    bus: broadcast::Sender<MiraEnvelopeV2>,
}

impl Registry {
    pub fn new() -> Self {
        let (bus, _rx) = broadcast::channel(256);
        Self {
            nodes: Arc::new(RwLock::new(HashMap::new())),
            bus,
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<MiraEnvelopeV2> {
        self.bus.subscribe()
    }

    pub fn publish(&self, envelope: MiraEnvelopeV2) {
        let _ = self.bus.send(envelope);
    }

    pub async fn register(&self, node_id: String, topics: HashSet<String>) -> Result<(), RegisterError> {
        let mut nodes = self.nodes.write().await;
        if nodes.contains_key(&node_id) {
            return Err(RegisterError::DuplicateNodeId);
        }
        nodes.insert(node_id.clone(), NodeHandle { node_id, topics });
        Ok(())
    }

    pub async fn deregister(&self, node_id: &str) {
        self.nodes.write().await.remove(node_id);
    }

    pub async fn contains(&self, node_id: &str) -> bool {
        self.nodes.read().await.contains_key(node_id)
    }

    pub async fn len(&self) -> usize {
        self.nodes.read().await.len()
    }
}

impl Default for Registry {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, thiserror::Error)]
pub enum RegisterError {
    #[error("node_id already registered")]
    DuplicateNodeId,
}
