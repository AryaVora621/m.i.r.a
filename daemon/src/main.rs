use std::path::PathBuf;
use tokio::net::TcpListener;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let guardrail_sock = std::env::var("MIRA_GUARDRAIL_SOCK")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp/mira-dev/guardrail.sock"));
    let router = mira_daemon::build_router(guardrail_sock);
    let listener = TcpListener::bind("127.0.0.1:8080").await.expect("failed to bind 127.0.0.1:8080");
    tracing::info!("mira-daemon listening on {}", listener.local_addr().unwrap());
    axum::serve(listener, router).await.expect("server error");
}
