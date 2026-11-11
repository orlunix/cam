use cam_protocol::{error, id, Result};
use cam_server::{
    config::{private_dir, private_read, uid},
    relay::{self, Endpoint},
};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub client_id: String,
    pub runtime_dir: PathBuf,
    pub endpoints: Vec<Endpoint>,
    pub poll_seconds: u64,
    pub scrollback_lines: usize,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            client_id: format!("tui-{}", id()),
            runtime_dir: PathBuf::from(format!("/var/tmp/cam-client-{}", uid())),
            endpoints: vec![],
            poll_seconds: 30,
            scrollback_lines: 10000,
        }
    }
}
impl Config {
    pub fn path() -> Result<PathBuf> {
        let root = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                cam_server::config::home()
                    .unwrap_or_else(|_| PathBuf::from("."))
                    .join(".config")
            });
        // Home may be NFS-shared by several Client machines. Each machine
        // keeps its own stable Client identity and endpoint credentials.
        Ok(root
            .join("cam-client")
            .join(format!("{}.json", cam_server::config::hostname()?)))
    }
    pub fn load(path: &Path) -> Result<Self> {
        let mut config: Self = serde_json::from_slice(&private_read(path)?)?;
        if config.runtime_dir.is_relative() {
            return Err(error("runtime_dir must be absolute"));
        }
        if !cam_protocol::valid_id(&config.client_id)
            || !(1..=60).contains(&config.poll_seconds)
            || !(100..=50000).contains(&config.scrollback_lines)
            || config.endpoints.len() > 128
        {
            return Err(error("invalid Client configuration"));
        }
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        let mut nodes = std::collections::HashSet::new();
        for e in &mut config.endpoints {
            if e.token_file.is_relative() {
                e.token_file = parent.join(&e.token_file);
            }
            if let Some(socket) = &mut e.socket {
                if socket.is_relative() {
                    *socket = parent.join(&socket);
                }
            }
            Self::validate_endpoint(e)?;
            if !nodes.insert(e.node_id.clone()) {
                return Err(error("duplicate Node ID"));
            }
        }
        Ok(config)
    }
    pub fn validate_endpoint(e: &Endpoint) -> Result<()> {
        if e.name.chars().count() > 96 || e.name.chars().any(char::is_control) {
            return Err(error(
                "Node name must be at most 96 characters without control characters",
            ));
        }
        if !cam_protocol::valid_id(&e.node_id) || e.socket.is_some() == !e.command.is_empty() {
            return Err(error(
                "Node needs a stable ID and exactly one socket or argv command",
            ));
        }
        if private_read(&e.token_file)?.len() < 32 {
            return Err(error("Node token is too short"));
        }
        Ok(())
    }
    pub fn relay(&self) -> relay::Config {
        relay::Config {
            client_id: self.client_id.clone(),
            endpoints: self.endpoints.clone(),
            agents: vec![],
            agents_file: None,
            agent_ttl_seconds: self.freshness_seconds() as i64,
        }
    }
    pub fn save(&self, path: &Path) -> Result<()> {
        save_json(path, self)
    }
    pub fn freshness_seconds(&self) -> u64 {
        (self.poll_seconds * 3).max(15)
    }
}

/// Names belong to the Client UI; routing continues to use the pinned Node ID.
pub fn endpoint_name(endpoint: &Endpoint, local_host: &str) -> String {
    if !endpoint.name.trim().is_empty() {
        return endpoint.name.trim().into();
    }
    if endpoint.socket.is_some() {
        return local_host.into();
    }
    if endpoint
        .command
        .first()
        .and_then(|s| Path::new(s).file_name())
        .and_then(|s| s.to_str())
        == Some("ssh")
    {
        let mut args = endpoint.command.iter().skip(1);
        while let Some(arg) = args.next() {
            if [
                "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O",
                "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
            ]
            .contains(&arg.as_str())
            {
                args.next();
                continue;
            }
            let host = if arg == "--" {
                args.next()
            } else if arg.starts_with('-') {
                continue;
            } else {
                Some(arg)
            };
            if let Some(host) = host {
                let host = host.strip_prefix("ssh://").unwrap_or(host);
                let host = host.rsplit('@').next().unwrap_or(host);
                if host.starts_with('[') {
                    return host
                        .split(']')
                        .next()
                        .unwrap_or(host)
                        .trim_start_matches('[')
                        .into();
                }
                return host.split(':').next().unwrap_or(host).into();
            }
            break;
        }
    }
    format!(
        "Node {}",
        endpoint.node_id.chars().take(8).collect::<String>()
    )
}
pub fn save_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| error("configuration parent missing"))?;
    if !parent.exists() {
        fs::create_dir_all(parent)?;
    }
    let tmp = parent.join(format!(".cam-{}.tmp", id()));
    let result: Result<()> = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)?;
        file.write_all(&serde_json::to_vec_pretty(value)?)?;
        file.sync_all()?;
        fs::rename(&tmp, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(tmp);
    }
    result
}
pub fn prepare_runtime(path: &Path) -> Result<()> {
    private_dir(path, true)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn names_use_hostname_or_alias_without_changing_identity() {
        let mut e: Endpoint = serde_json::from_value(serde_json::json!({
            "node_id":"0123456789abcdef0123456789abcdef", "socket":"/tmp/server.sock", "token_file":"/private/token"
        })).unwrap();
        assert_eq!(endpoint_name(&e, "local.example.com"), "local.example.com");
        assert!(!serde_json::to_value(&e)
            .unwrap()
            .as_object()
            .unwrap()
            .contains_key("name"));
        e.socket = None;
        e.command = vec![
            "/bin/ssh",
            "-F",
            "/dev/null",
            "-p",
            "5865",
            "-T",
            "-o",
            "BatchMode=yes",
            "hren@site-a.example.com",
            "/path/cam-server",
        ]
        .into_iter()
        .map(String::from)
        .collect();
        assert_eq!(endpoint_name(&e, "local"), "site-a.example.com");
        e.command = vec![
            "ssh".into(),
            "-p5865".into(),
            "--".into(),
            "ssh://hren@[::1]:5865".into(),
        ];
        assert_eq!(endpoint_name(&e, "local"), "::1");
        e.command = vec!["custom-transport".into(), "unrelated-argument".into()];
        assert_eq!(endpoint_name(&e, "local"), "Node 01234567");
        e.name = "SITE-C".into();
        assert_eq!(endpoint_name(&e, "local"), "SITE-C");
        assert_eq!(e.node_id, "0123456789abcdef0123456789abcdef");
        e.name = "bad\nname".into();
        assert!(Config::validate_endpoint(&e)
            .unwrap_err()
            .to_string()
            .contains("Node name"));
    }
}
