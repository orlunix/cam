use cam_protocol::{error, Result};
use cam_tui::config::Config;
use std::path::PathBuf;
#[tokio::main]
async fn main() {
    if let Err(err) = run().await {
        eprintln!("cam-clientd: {err}");
        std::process::exit(1);
    }
}
async fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args
        .first()
        .map(|s| ["version", "--version"].contains(&s.as_str()))
        .unwrap_or(false)
    {
        println!("cam-clientd {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.first().map(String::as_str) != Some("serve") {
        println!("cam-clientd serve --config FILE\nRust CAM Client background: Nodes, status, forwarding, jobs, engineering loops.");
        return Ok(());
    }
    let index = args
        .iter()
        .position(|s| s == "--config")
        .ok_or_else(|| error("--config required"))?;
    let path = PathBuf::from(
        args.get(index + 1)
            .ok_or_else(|| error("--config path missing"))?,
    );
    cam_tui::daemon::serve(Config::load(&path)?, path).await
}
