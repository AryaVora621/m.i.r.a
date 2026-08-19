use std::path::Path;
use std::sync::Arc;

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};

use mira_protocol::{CapabilityReceipt, CapabilityRequest, CapabilityResultStatus};

use crate::policy::{PolicyDecision, PolicyTable};
use crate::signing::DevSigner;

pub async fn serve(sock_path: &Path, policy: PolicyTable, signer: Arc<DevSigner>) -> std::io::Result<()> {
    let _ = std::fs::remove_file(sock_path);
    let listener = UnixListener::bind(sock_path)?;
    loop {
        let (stream, _) = listener.accept().await?;
        let policy = policy.clone();
        let signer = signer.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_connection(stream, policy, signer).await {
                tracing::warn!("guardrail IPC connection error: {err}");
            }
        });
    }
}

async fn handle_connection(
    stream: UnixStream,
    policy: PolicyTable,
    signer: Arc<DevSigner>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let (read_half, mut write_half) = stream.into_split();
    let mut reader = BufReader::new(read_half);
    loop {
        let mut line = String::new();
        let bytes_read = reader.read_line(&mut line).await?;
        if bytes_read == 0 {
            return Ok(());
        }
        let request: CapabilityRequest = match serde_json::from_str(line.trim_end()) {
            Ok(request) => request,
            Err(err) => {
                tracing::warn!("guardrail received malformed CapabilityRequest: {err}");
                continue;
            }
        };
        let receipt = dispatch(&policy, &signer, request);
        let mut payload = serde_json::to_vec(&receipt)?;
        payload.push(b'\n');
        write_half.write_all(&payload).await?;
    }
}

fn dispatch(policy: &PolicyTable, signer: &DevSigner, request: CapabilityRequest) -> CapabilityReceipt {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();

    match policy.evaluate(&request) {
        PolicyDecision::Deny { reason } => {
            tracing::info!(capability = %request.capability, %reason, "guardrail denied capability request");
            signer.sign(CapabilityReceipt {
                request_id: request.request_id,
                capability: request.capability,
                result_status: CapabilityResultStatus::Denied,
                rollback_evidence: None,
                executed_at: now,
                executed_by: "guardrail-dev".to_string(),
                signature: None,
            })
        }
        PolicyDecision::Allow { manifest } if request.capability == "mac.volume.set" => {
            crate::volume::execute_volume_set(&crate::volume::OsascriptVolumeController, &request, &manifest, signer)
        }
        // Any other allowed capability has no real device handler yet — unchanged from Phase 1.
        PolicyDecision::Allow { manifest } => signer.sign(CapabilityReceipt {
            request_id: request.request_id,
            capability: request.capability,
            result_status: CapabilityResultStatus::Success,
            rollback_evidence: manifest.rollback_plan.clone(),
            executed_at: now,
            executed_by: "guardrail-dev".to_string(),
            signature: None,
        }),
    }
}
