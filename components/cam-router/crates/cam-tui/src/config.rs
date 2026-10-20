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
