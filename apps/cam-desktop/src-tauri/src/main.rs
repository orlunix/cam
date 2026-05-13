use serde::{Deserialize, Serialize};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct BackendProfile {
    kind: String,
    camc_path: Option<String>,
    distro: Option<String>,
    host: Option<String>,
    user: Option<String>,
    port: Option<u16>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CmdOutput {
    code: i32,
    stdout: String,
    stderr: String,
    timed_out: bool,
}

fn sh_quote(value: &str) -> String {
    if value.is_empty() {
        return "''".to_string();
    }
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn build_command(profile: &BackendProfile, args: &[String]) -> Result<Command, String> {
    match profile.kind.as_str() {
        "local" => {
            let mut cmd = Command::new(profile.camc_path.as_deref().unwrap_or("camc"));
            cmd.args(args);
            Ok(cmd)
        }
        "wsl" => {
            let mut cmd = Command::new("wsl.exe");
            if let Some(distro) = profile.distro.as_deref().filter(|v| !v.is_empty()) {
                cmd.args(["-d", distro]);
            }
            cmd.arg("--exec");
            match profile.camc_path.as_deref().filter(|v| !v.is_empty()) {
                Some("camc") | None => {
                    cmd.arg("/usr/bin/env");
                    cmd.arg("camc");
                }
                Some(path) => {
                    cmd.arg(path);
                }
            }
            cmd.args(args);
            Ok(cmd)
        }
        "ssh" => {
            let host = profile
                .host
                .as_deref()
                .filter(|v| !v.is_empty())
                .ok_or_else(|| "ssh profile requires host".to_string())?;
            let target = match profile.user.as_deref().filter(|v| !v.is_empty()) {
                Some(user) => format!("{}@{}", user, host),
                None => host.to_string(),
            };
            let camc_path = profile.camc_path.as_deref().unwrap_or("~/.cam/camc");
            let mut remote = camc_path.to_string();
            for arg in args {
                remote.push(' ');
                remote.push_str(&sh_quote(arg));
            }

            let mut cmd = Command::new("ssh");
            if let Some(port) = profile.port {
                cmd.args(["-p", &port.to_string()]);
            }
            cmd.arg(target);
            cmd.arg(remote);
            Ok(cmd)
        }
        other => Err(format!("unsupported backend profile: {}", other)),
    }
}

fn run_with_timeout(mut cmd: Command, timeout_ms: u64) -> Result<CmdOutput, String> {
    let mut child = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;

    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(_) => {
                let output = child.wait_with_output().map_err(|e| e.to_string())?;
                return Ok(CmdOutput {
                    code: output.status.code().unwrap_or(-1),
                    stdout: String::from_utf8_lossy(&output.stdout).to_string(),
                    stderr: String::from_utf8_lossy(&output.stderr).to_string(),
                    timed_out: false,
                });
            }
            None if Instant::now() >= deadline => {
                let _ = child.kill();
                let output = child.wait_with_output().map_err(|e| e.to_string())?;
                return Ok(CmdOutput {
                    code: output.status.code().unwrap_or(-1),
                    stdout: String::from_utf8_lossy(&output.stdout).to_string(),
                    stderr: String::from_utf8_lossy(&output.stderr).to_string(),
                    timed_out: true,
                });
            }
            None => thread::sleep(Duration::from_millis(50)),
        }
    }
}

#[tauri::command]
fn camc_exec(
    profile: BackendProfile,
    args: Vec<String>,
    timeout_ms: Option<u64>,
) -> Result<CmdOutput, String> {
    if args.is_empty() {
        return Err("camc args must not be empty".to_string());
    }
    let cmd = build_command(&profile, &args)?;
    run_with_timeout(cmd, timeout_ms.unwrap_or(30_000))
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![camc_exec])
        .run(tauri::generate_context!())
        .expect("error while running Cam Desktop");
}

