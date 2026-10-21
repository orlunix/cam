//! SSH onboarding: public connection fields, native OpenSSH authentication,
//! and an owner-authenticated Server pairing exchange.
use crate::config::Config;
use cam_protocol::{error, Result};
use cam_server::{config::{private_dir, private_write}, relay::Endpoint};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{path::{Path, PathBuf}, process::{Command, Stdio}, time::Duration};

#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Connection {
    pub name: String,
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    pub user: String,
    pub key: String,
    pub expected_node: String,
}
fn default_port() -> u16 { 22 }
impl Default for Connection {
    fn default() -> Self {
        Self { name:String::new(), host:String::new(), port:22, user:String::new(), key:String::new(), expected_node:String::new() }
    }
}
impl Connection {
    pub fn validate(&mut self) -> Result<()> {
        self.host = self.host.trim().to_string();
        self.user = self.user.trim().to_string();
        self.key = self.key.trim().to_string();
        self.name = self.name.trim().to_string();
        if self.host.is_empty() || self.host.starts_with('-') || self.host.len() > 253
            || !self.host.chars().all(|c| c.is_ascii_alphanumeric() || ".-_:".contains(c)) {
            return Err(error("Host must be a hostname, SSH alias or IP address"));
        }
        if self.port == 0 || self.user.len() > 128 || self.user.chars().any(|c| c.is_control() || c.is_whitespace()) {
            return Err(error("Port must be 1-65535; Username may be blank"));
        }
        if self.name.chars().count() > 96 || self.name.chars().any(char::is_control)
            || self.key.chars().any(char::is_control)
            || (!self.expected_node.is_empty() && !cam_protocol::valid_id(&self.expected_node)) {
            return Err(error("invalid connection name, key path or Node identity"));
        }
        if let Some(rest) = self.key.strip_prefix("~/") {
            self.key = cam_server::config::home()?.join(rest).to_string_lossy().into_owned();
        }
        if !self.key.is_empty() && !Path::new(&self.key).is_file() {
            return Err(error("SSH key file does not exist on this Client"));
        }
        if !self.key.is_empty() {
            self.key = std::fs::canonicalize(&self.key)?.to_string_lossy().into_owned();
        }
        Ok(())
    }
    pub fn control(&self, runtime: &Path) -> Result<PathBuf> {
        let identity = serde_json::to_vec(&(&self.host, self.port, &self.user, &self.key))?;
        let key = format!("{:x}", Sha256::digest(identity));
        let path = runtime.join(format!("cam-ssh-{}.sock", &key[..24]));
        if path.as_os_str().len() > 100 { return Err(error("Client runtime path is too long for an SSH socket")); }
        Ok(path)
    }
    fn args(&self) -> Vec<String> {
        let mut args = vec!["-p".into(), self.port.to_string()];
        if !self.user.is_empty() { args.extend(["-l".into(), self.user.clone()]); }
        if !self.key.is_empty() { args.extend(["-i".into(), self.key.clone(), "-o".into(), "IdentitiesOnly=yes".into()]); }
        args
    }
    pub fn transport(&self, runtime: &Path) -> Result<Vec<String>> {
        let mut args = vec!["ssh".into(), "-T".into(), "-S".into(), self.control(runtime)?.to_string_lossy().into_owned(),
            "-o".into(), "ControlMaster=no".into(), "-o".into(), "BatchMode=yes".into(),
            "-o".into(), "ConnectTimeout=10".into(), "-o".into(), "ServerAliveInterval=15".into(),
            "-o".into(), "ServerAliveCountMax=3".into()];
        args.extend(self.args());
        args.extend(["--".into(), self.host.clone()]);
        Ok(args)
    }
}

pub fn profile(endpoint: &Endpoint) -> Option<Connection> {
    let args = &endpoint.command;
    if args.first().and_then(|s| Path::new(s).file_name())?.to_str()? != "ssh"
        || !args.windows(2).any(|p| p[0] == "-S" && Path::new(&p[1]).file_name()
            .map(|s| s.to_string_lossy().starts_with("cam-ssh-")).unwrap_or(false)) { return None; }
    let mut result = Connection { name: endpoint.name.clone(), port: 22,
        expected_node: endpoint.node_id.clone(), ..Connection::default() };
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "-p" => { result.port = args.get(i+1)?.parse().ok()?; i += 2; }
            "-l" => { result.user = args.get(i+1)?.clone(); i += 2; }
            "-i" => { result.key = args.get(i+1)?.clone(); i += 2; }
            "-o" | "-S" => i += 2,
            "--" => { result.host = args.get(i+1)?.clone(); break; }
            "-T" => i += 1,
            _ => return None,
        }
    }
    if result.host.is_empty() { None } else { Some(result) }
}

/// Temporarily run in the user's real terminal. OpenSSH handles passwords,
/// encrypted keys, SSH agent and known_hosts confirmation; no secret enters
/// a config, IPC frame, process argument or CAM log.
pub fn authenticate(connection: &Connection, runtime: &Path) -> Result<()> {
    private_dir(runtime, true)?;
    let control = connection.control(runtime)?;
    let mut check = Command::new("ssh");
    check.args(["-S"]).arg(&control).args(["-O", "check"])
        .args(connection.args()).arg("--").arg(&connection.host)
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    if check.status()?.success() { return Ok(()); }
    println!("\r\nConnecting to {}. OpenSSH may ask for a host-key confirmation, password or key passphrase.\r", connection.host);
    let mut command = Command::new("ssh");
    command.args(["-M", "-N", "-f", "-S"]).arg(&control)
        .arg("-E").arg(control.with_extension("log"))
        .args(["-o", "BatchMode=no", "-o", "ControlPersist=600", "-o", "ConnectTimeout=10"])
        .args(connection.args()).arg("--").arg(&connection.host);
    use std::os::unix::process::CommandExt;
    // Ctrl+C cancels SSH authentication without terminating the surrounding UI.
    let previous = unsafe { libc::signal(libc::SIGINT, libc::SIG_IGN) };
    unsafe { command.pre_exec(|| { libc::signal(libc::SIGINT, libc::SIG_DFL); Ok(()) }); }
    let outcome = command.status();
    unsafe { libc::signal(libc::SIGINT, previous); }
    if !outcome?.success() { return Err(error("SSH login failed or was cancelled; check Host, Port, Username and authentication")); }
    Ok(())
}

fn quote(value: &str) -> String { format!("'{}'", value.replace('\'', "'\\''")) }

/// The bootstrap travels as a single shell word even under csh/tcsh. Only
/// release bytes use stdin; OpenSSH credentials never enter CAM's protocol.
async fn setup_exchange(config: &Config, connection: &Connection, request: &Value, bundle: Option<Vec<u8>>) -> Result<Value> {
    use base64::{engine::general_purpose::STANDARD, Engine};
    use tokio::io::AsyncWriteExt;
    let code = format!("import base64;exec(base64.b64decode({:?}))", STANDARD.encode(cam_server::SETUP_SCRIPT));
    let mut argv = connection.transport(&config.runtime_dir)?;
    argv.push(format!("exec python3 -c {} {}", quote(&code), quote(&request.to_string())));
    let mut child = tokio::process::Command::new(&argv[0]).args(&argv[1..])
        .kill_on_drop(true).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()?;
    let mut input = child.stdin.take().ok_or_else(|| error("SSH input unavailable"))?;
    let timeout = if bundle.is_some() { 150 } else { 30 };
    let output = tokio::time::timeout(Duration::from_secs(timeout), async {
        let upload = async {
            if let Some(data) = bundle { input.write_all(&data).await?; }
            input.shutdown().await?;
            drop(input);
            Ok::<_, std::io::Error>(())
        };
        let (_, output) = tokio::try_join!(upload, child.wait_with_output())?;
        Ok::<_, std::io::Error>(output)
    }).await.map_err(|_| error("CAM Server setup timed out; inspect the remote Server log before retrying"))??;
    if output.stdout.len() > 65536 { return Err(error("invalid Server setup response")); }
    let info: Value = String::from_utf8(output.stdout)?.lines().rev()
        .find_map(|line| serde_json::from_str::<Value>(line).ok())
        .ok_or_else(|| error("SSH Server setup failed; requires Linux, Python 3.6+ and a writable private Home"))?;
    if let Some(code) = info["setup_error"].as_str() {
        let safe = code.len() < 80 && code.chars().all(|c| c.is_ascii_lowercase() || c == '_');
        return Err(error(&format!("CAM Server setup failed ({}); inspect ~/.cam/server/logs on the Node", if safe { code } else { "setup_failed" })));
    }
    if !output.status.success() { return Err(error("SSH Server setup failed")); }
    Ok(info)
}

pub async fn onboard(config: &Config, mut connection: Connection) -> Result<Endpoint> {
    connection.validate()?;
    let mut request = serde_json::json!({"mode":"probe", "version":cam_server::VERSION,
        "client_id":config.client_id,"expected_node":connection.expected_node});
    let mut info = setup_exchange(config, &connection, &request, None).await?;
    if info["need_bundle"] == true {
        let path = std::env::current_exe()?.with_file_name("cam-server.bundle");
        let metadata = std::fs::metadata(&path).map_err(|_| error("CAM Server needs an update; install cam-server.bundle and cam-server.bundle.json beside cam-clientd from the matching release"))?;
        if metadata.len() > 32 * 1024 * 1024 { return Err(error("Server release bundle is too large")); }
        let manifest: Value = serde_json::from_slice(&std::fs::read(path.with_extension("bundle.json"))?)?;
        if manifest["version"] != cam_server::VERSION || manifest["target"] != info["target"] {
            return Err(error("Server release bundle does not match the required version or remote CPU; install the matching Linux release"));
        }
        let bundle = std::fs::read(&path)?;
        if manifest["size"].as_u64() != Some(bundle.len() as u64)
            || manifest["sha256"].as_str() != Some(format!("{:x}", Sha256::digest(&bundle)).as_str()) {
            return Err(error("Server release bundle checksum mismatch"));
        }
        for key in ["target", "size", "sha256"] { request[key] = manifest[key].clone(); }
        request["mode"] = "install".into();
        info = setup_exchange(config, &connection, &request, Some(bundle)).await?;
    }
    let node = info["node_id"].as_str().filter(|n| cam_protocol::valid_id(n))
        .ok_or_else(|| error("invalid Server identity"))?;
    if !connection.expected_node.is_empty() && connection.expected_node != node {
        return Err(error("Host belongs to a different Node; use Add instead of editing this connection"));
    }
    let token = info["token"].as_str().filter(|t| (32..=1024).contains(&t.len()))
        .ok_or_else(|| error("invalid pairing credential"))?;
    let absolute = |key: &str| -> Result<&str> {
        info[key].as_str().filter(|s| s.starts_with('/') && !s.chars().any(char::is_control))
            .ok_or_else(|| error("invalid Server executable/socket path"))
    };
    let mut command = connection.transport(&config.runtime_dir)?;
    command.push(format!("exec {} connect --socket {}", quote(absolute("binary")?), quote(absolute("socket")?)));
    let directory = config.runtime_dir.join("credentials");
    private_dir(&directory, false)?;
    let token_file = directory.join(format!("{node}.token"));
    // The token is returned only on the authenticated SSH stream and stored
    // privately. User passwords and key passphrases stay within OpenSSH.
    private_write(&token_file, token.as_bytes())?;
    Ok(Endpoint { node_id: node.into(), name: connection.name, token_file, socket: None, command })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn optional_user_and_key_use_openssh_defaults_and_public_fields_round_trip() {
        let mut connection: Connection = serde_json::from_value(serde_json::json!({"host":"server.example"})).unwrap();
        connection.validate().unwrap();
        assert_eq!(connection.port, 22);
        let command = connection.transport(Path::new("/tmp/private")).unwrap();
        assert!(!command.iter().any(|s| s == "-l" || s == "-i"));
        let endpoint = Endpoint { node_id:"node".into(), name:"My server".into(), token_file:"/private/token".into(), socket:None, command };
        let parsed = profile(&endpoint).unwrap();
        assert_eq!(parsed.host, connection.host);
        assert_eq!(parsed.user, "");
        assert_eq!(parsed.expected_node, "node");
        for host in ["-oProxyCommand=bad", "a;touch /tmp/no", "u@host", "a\nb"] {
            connection.host = host.into();
            assert!(connection.validate().is_err());
        }
    }
}
