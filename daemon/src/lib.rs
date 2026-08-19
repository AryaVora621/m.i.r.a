pub mod broker;
pub mod guardrail_client;
pub mod registry;

pub use broker::AppState;
pub use guardrail_client::GuardrailClient;
pub use registry::Registry;

pub fn build_router(guardrail_sock_path: std::path::PathBuf) -> axum::Router {
    let state = AppState {
        registry: Registry::new(),
        guardrail: GuardrailClient::new(guardrail_sock_path),
    };
    broker::router(state)
}
