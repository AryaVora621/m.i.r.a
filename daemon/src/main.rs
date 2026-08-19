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
    // into_make_service_with_connect_info makes the peer SocketAddr available to extractors
    // (ConnectInfo<SocketAddr>) inside the /ws handler, which the register-time loopback-bind
    // check needs to verify LOOPBACK_TRUSTED registrations actually arrived over loopback.
    axum::serve(listener, router.into_make_service_with_connect_info::<std::net::SocketAddr>())
        .await
        .expect("server error");
}
