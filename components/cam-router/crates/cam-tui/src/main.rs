use cam_protocol::{error, Result};
use cam_tui::{
    config::{prepare_runtime, Config},
    ipc,
};
use serde_json::{json, Value};
use std::{
    fs::OpenOptions,
    os::unix::{fs::OpenOptionsExt, process::CommandExt},
    path::PathBuf,
    process::{Command, Stdio},
    time::Duration,
};

fn help() {
    println!("cam-tui 0.1.0\n\n  cam-tui [--config FILE]                    Open mouse-first UI\n  cam-tui init [--config FILE] [--runtime DIR]\n  cam-tui daemon start|status|stop [--config FILE]\n  cam-tui rpc ACTION [JSON] [--config FILE]\n\nF1 New · F2 Nodes · F3 Settings · F8 tool mouse · Ctrl+Q quit UI\nRight-click agent for operations. Closing UI leaves local daemon running.");
}
async fn ensure(config: &Config, path: &std::path::Path) -> Result<()> {
    let socket = config.runtime_dir.join("client.sock");
    if matches!(
        tokio::time::timeout(
            Duration::from_secs(2),
            ipc::request(&socket, "health", json!({}))
        )
        .await,
        Ok(Ok(_))
    ) {
        return Ok(());
    }
    prepare_runtime(&config.runtime_dir)?;
    let binary = std::env::current_exe()?
        .parent()
        .ok_or_else(|| error("binary parent missing"))?
        .join("cam-clientd");
    if !binary.is_file() {
        return Err(error(
            "cam-clientd must be installed beside cam-tui; build both binaries",
        ));
    }
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(config.runtime_dir.join("client.log"))?;
    let mut command = Command::new(binary);
    command
        .args(["serve", "--config"])
        .arg(path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log);
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    let mut child = command.spawn()?;
    for _ in 0..60 {
        if matches!(
            tokio::time::timeout(
                Duration::from_millis(300),
                ipc::request(&socket, "health", json!({}))
            )
            .await,
            Ok(Ok(_))
        ) {
            return Ok(());
        }
        if child.try_wait()?.is_some() {
            return Err(error("Client daemon could not start; inspect client.log in runtime_dir (another machine/user instance may already be active)"));
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    Err(error("Client daemon startup timed out"))
}
#[tokio::main]
async fn main() {
    if let Err(err) = run().await {
        eprintln!("cam-tui: {err}");
        std::process::exit(1);
    }
}
async fn run() -> Result<()> {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut path = Config::path()?;
    if let Some(i) = args.iter().position(|a| a == "--config") {
        if i + 1 >= args.len() {
            return Err(error("--config needs a path"));
        }
        path = PathBuf::from(args.remove(i + 1));
        args.remove(i);
    }
    if path.is_relative() {
        path = std::env::current_dir()?.join(path);
    }
    if args
        .first()
        .map(|a| ["--help", "-h", "help"].contains(&a.as_str()))
        .unwrap_or(false)
    {
        help();
        return Ok(());
    }
    if args
        .first()
        .map(|a| ["--version", "version"].contains(&a.as_str()))
        .unwrap_or(false)
    {
        println!("cam-tui 0.1.0");
        return Ok(());
    }
    if args.first().map(String::as_str) == Some("init") {
        if path.exists() {
            return Err(error(
                "configuration already exists; edit it or use another --config path",
            ));
        }
        let mut config = Config::default();
        if let Some(i) = args.iter().position(|a| a == "--runtime") {
            config.runtime_dir = PathBuf::from(
                args.get(i + 1)
                    .ok_or_else(|| error("--runtime needs a path"))?,
            );
        }
        if !config.runtime_dir.is_absolute() {
            return Err(error("--runtime must be absolute"));
        }
        config.save(&path)?;
        println!(
            "Created {}\nAdd CAM Server connections in Nodes or with rpc node.upsert.",
            path.display()
        );
        return Ok(());
    }
    if !path.exists() {
        Config::default().save(&path)?;
    }
    let config = Config::load(&path)?;
    let socket = config.runtime_dir.join("client.sock");
    if args.first().map(String::as_str) == Some("daemon") {
        match args.get(1).map(String::as_str).unwrap_or("status") {
            "start" => {
                ensure(&config, &path).await?;
                println!("Client daemon ready at {}", socket.display());
            }
            "status" => println!(
                "{}",
                serde_json::to_string_pretty(&ipc::request(&socket, "health", json!({})).await?)?
            ),
            "stop" => {
                ipc::request(&socket, "shutdown", json!({})).await?;
                println!("Client daemon stopped; agents remain running");
            }
            _ => return Err(error("use daemon start|status|stop")),
        }
        return Ok(());
    }
    ensure(&config, &path).await?;
    if args.first().map(String::as_str) == Some("rpc") {
        let action = args.get(1).ok_or_else(|| error("rpc needs an action"))?;
        let payload: Value = serde_json::from_str(args.get(2).map(String::as_str).unwrap_or("{}"))?;
        let answer = ipc::request(&socket, action, payload).await?;
        println!("{}", serde_json::to_string_pretty(&answer)?);
        return Ok(());
    }
    if !args.is_empty() {
        return Err(error("unknown argument; see --help"));
    }
    let handle = tokio::runtime::Handle::current();
    tokio::task::spawn_blocking(move || cam_tui::ui::run(config, handle)).await??;
    Ok(())
}
