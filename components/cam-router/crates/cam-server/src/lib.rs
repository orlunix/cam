pub mod backend;
pub mod config;
pub mod relay;
pub mod server;
pub mod store;
pub mod terminal;
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const SETUP_SCRIPT: &str = include_str!("../../../scripts/server_setup.py");
