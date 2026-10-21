//! Isolated Client fixture; never distributed in release packages.
#[tokio::main]
async fn main() -> cam_protocol::Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("version") {
        println!("cam-clientd {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.len() != 3 || args[0] != "serve" || args[1] != "--config" {
        return Err(cam_protocol::error("fixture requires serve --config FILE"));
    }
    let path = std::path::PathBuf::from(&args[2]);
    cam_tui::daemon::serve_test_client(cam_tui::config::Config::load(&path)?, path).await
}
