use tokio::net::TcpListener;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let router = mira_daemon::build_router_with_registry(mira_daemon::Registry::new());
    let listener = TcpListener::bind("127.0.0.1:8080").await.expect("failed to bind 127.0.0.1:8080");
    tracing::info!("mira-daemon listening on {}", listener.local_addr().unwrap());
    axum::serve(listener, router).await.expect("server error");
}
