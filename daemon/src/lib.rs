pub mod broker;
pub mod registry;

pub use registry::Registry;

pub fn build_router_with_registry(registry: Registry) -> axum::Router {
    broker::router(registry)
}
