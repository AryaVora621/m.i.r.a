use std::path::PathBuf;
use std::sync::Arc;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let sock_path = std::env::var("MIRA_GUARDRAIL_SOCK")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp/mira-dev/guardrail.sock"));
    let key_path = std::env::var("MIRA_GUARDRAIL_KEY")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp/mira-dev/guardrail.key"));
    let signer = Arc::new(
        mira_guardrail::DevSigner::load_or_generate(&key_path)
            .expect("failed to load or generate guardrail signing key"),
    );
    let policy = mira_guardrail::PolicyTable::dev_default();
    tracing::info!("mira-guardrail listening on {}", sock_path.display());
    mira_guardrail::ipc::serve(&sock_path, policy, signer)
        .await
        .expect("guardrail IPC server error");
}
