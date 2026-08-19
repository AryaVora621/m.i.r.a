use std::path::PathBuf;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

use mira_protocol::{CapabilityReceipt, CapabilityRequest, CapabilityResultStatus};

#[derive(Clone)]
pub struct GuardrailClient {
    sock_path: PathBuf,
}

impl GuardrailClient {
    pub fn new(sock_path: PathBuf) -> Self {
        Self { sock_path }
    }

    pub async fn dispatch(&self, request: &CapabilityRequest) -> CapabilityReceipt {
        match tokio::time::timeout(Duration::from_secs(5), self.dispatch_inner(request)).await {
            Ok(Ok(receipt)) => receipt,
            Ok(Err(err)) => self.failure_receipt(request, &err.to_string()),
            Err(_) => self.failure_receipt(request, "guardrail did not respond within 5s"),
        }
    }

    async fn dispatch_inner(
        &self,
        request: &CapabilityRequest,
    ) -> Result<CapabilityReceipt, Box<dyn std::error::Error + Send + Sync>> {
        let stream = UnixStream::connect(&self.sock_path).await?;
        let (read_half, mut write_half) = stream.into_split();
        let mut reader = BufReader::new(read_half);

        let mut payload = serde_json::to_vec(request)?;
        payload.push(b'\n');
        write_half.write_all(&payload).await?;

        let mut line = String::new();
        reader.read_line(&mut line).await?;
        let receipt: CapabilityReceipt = serde_json::from_str(line.trim_end())?;
        Ok(receipt)
    }

    fn failure_receipt(&self, request: &CapabilityRequest, reason: &str) -> CapabilityReceipt {
        tracing::error!("guardrail dispatch failed for {}: {reason}", request.capability);
        CapabilityReceipt {
            request_id: request.request_id.clone(),
            capability: request.capability.clone(),
            result_status: CapabilityResultStatus::Failure,
            rollback_evidence: None,
            executed_at: request.requested_at,
            executed_by: "daemon-guardrail-client".to_string(),
            signature: None,
        }
    }
}
