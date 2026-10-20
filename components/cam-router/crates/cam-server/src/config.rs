use cam_protocol::{error, id, Result};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClientGrant {
    pub client_id: String,
    pub token_file: PathBuf,
    #[serde(default)]
    pub source_nodes: Vec<String>,
    /// Explicitly grant the Client local management/terminal API access.
    #[serde(default)]
    pub management: bool,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Peer {
    pub node_id: String,
    #[serde(default)]
    pub aliases: Vec<String>,
    pub operations: Vec<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Backend {
    pub python: PathBuf,
    pub module_path: PathBuf,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub node_id: String,
    pub alias: String,
    pub runtime_dir: PathBuf,
    pub clients: Vec<ClientGrant>,
    pub peers: Vec<Peer>,
    pub backend: Option<Backend>,
    pub echo: bool,
    pub discover_camc: bool,
    pub max_messages: usize,
    pub max_bytes: usize,
    pub max_connections: usize,
    pub queue_bytes: usize,
    pub retry_ms: u64,
    pub route_ttl_seconds: u64,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            node_id: String::new(),
            alias: String::new(),
            runtime_dir: PathBuf::new(),
            clients: vec![],
            peers: vec![],
            backend: None,
            echo: false,
            discover_camc: true,
            max_messages: 10000,
            max_bytes: 64 * 1024 * 1024,
            max_connections: 32,
            queue_bytes: 16 * 1024 * 1024,
            retry_ms: 500,
            route_ttl_seconds: 30,
        }
    }
}
pub fn uid() -> u32 {
    unsafe { libc::geteuid() }
}
pub fn hostname() -> Result<String> {
    let mut raw = vec![0u8; 256];
    if unsafe { libc::gethostname(raw.as_mut_ptr() as *mut _, raw.len()) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let end = raw
        .iter()
        .position(|b| *b == 0)
        .ok_or_else(|| error("hostname too long"))?;
    Ok(std::str::from_utf8(&raw[..end])?.to_owned())
}
pub fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| error("HOME is unset; supply explicit config and runtime paths"))
}
pub fn default_config() -> Result<PathBuf> {
    let base = match std::env::var_os("XDG_CONFIG_HOME") {
        Some(path) => PathBuf::from(path),
        None => home()?.join(".config"),
    };
    Ok(base
        .join("cam-server")
        .join(format!("{}.json", hostname()?)))
}
pub fn node_key(node: &str) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(node.as_bytes()))[..24].to_string()
}
pub fn lock_dir(node: &str) -> PathBuf {
    PathBuf::from(format!("/tmp/cam-server-{}-{}", uid(), node_key(node)))
}
pub fn local_filesystem(path: &Path) -> Result<()> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};
    let name = CString::new(path.as_os_str().as_bytes())?;
    let mut info = std::mem::MaybeUninit::<libc::statfs>::uninit();
    if unsafe { libc::statfs(name.as_ptr(), info.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let kind = unsafe { info.assume_init() }.f_type as u64;
    if [0x6969u64, 0xff534d42, 0xfe534d42, 0x517b].contains(&(kind & 0xffffffff)) {
        return Err(error(
            "runtime directory must be on a local filesystem, not NFS/SMB",
        ));
    }
    Ok(())
}
pub fn private_dir(path: &Path, local: bool) -> Result<()> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)?;
    let info = fs::symlink_metadata(path)?;
    if !info.is_dir() || info.uid() != uid() || info.mode() & 0o077 != 0 {
        return Err(error(
            "directory must be private, owned by this user, and not a symlink",
        ));
    }
    if local {
        local_filesystem(path)?;
    }
    Ok(())
}
pub fn private_read(path: &Path) -> Result<Vec<u8>> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let info = file.metadata()?;
    if !info.is_file()
        || info.uid() != uid()
        || info.mode() & 0o077 != 0
        || info.len() > 4 * 1024 * 1024
    {
        return Err(error(
            "config/token must be a private regular file owned by this user",
        ));
    }
    let mut data = Vec::new();
    file.read_to_end(&mut data)?;
    Ok(data)
}
pub fn private_write(path: &Path, data: &[u8]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(parent)?;
    }
    let temporary = path.with_extension(format!("{}.tmp", id()));
    let result = (|| {
        let mut f = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        f.write_all(data)?;
        f.sync_all()?;
        fs::rename(&temporary, path)?;
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    result
}
impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let mut cfg: Self = serde_json::from_slice(&private_read(path)?)?;
        if !cam_protocol::valid_id(&cfg.node_id) {
            return Err(error("node_id must be a stable identifier"));
        }
        if cfg.runtime_dir.as_os_str().is_empty() {
            cfg.runtime_dir = lock_dir(&cfg.node_id).join("runtime");
        }
        if !cfg.runtime_dir.is_absolute() || cfg.runtime_dir.as_os_str().len() > 75 {
            return Err(error(
                "runtime_dir must be an absolute short path (<=75 bytes) for Unix sockets",
            ));
        }
        if cfg.queue_bytes < cam_protocol::FRAME_LIMIT
            || cfg.queue_bytes > 256 * 1024 * 1024
            || cfg.max_messages == 0
            || cfg.max_bytes < cam_protocol::FRAME_LIMIT
            || !(1..=128).contains(&cfg.max_connections)
            || cfg.retry_ms < 50
            || !(3..=120).contains(&cfg.route_ttl_seconds)
        {
            return Err(error("invalid capacity or time limits"));
        }
        if cfg.clients.len() > 128 || cfg.peers.len() > 128 {
            return Err(error("peer/Client capacity exceeded"));
        }
        let parent = path.parent().unwrap_or(Path::new("."));
        let mut ids = std::collections::HashSet::new();
        for client in &mut cfg.clients {
            if !cam_protocol::valid_id(&client.client_id) || !ids.insert(client.client_id.clone()) {
                return Err(error("invalid/duplicate Client identity"));
            }
            if client.token_file.is_relative() {
                client.token_file = parent.join(&client.token_file);
            }
            if private_read(&client.token_file)?.len() < 32 {
                return Err(error("Client token must have at least 32 bytes"));
            }
        }
        let mut nodes = std::collections::HashSet::new();
        for peer in &cfg.peers {
            if !cam_protocol::valid_id(&peer.node_id)
                || peer.node_id == cfg.node_id
                || !nodes.insert(peer.node_id.clone())
            {
                return Err(error("invalid/duplicate peer Node"));
            }
        }
        if let Some(backend) = &mut cfg.backend {
            if !backend.python.is_absolute() || !backend.module_path.is_absolute() {
                return Err(error(
                    "backend python and module_path must be absolute paths",
                ));
            }
        }
        Ok(cfg)
    }
    pub fn permitted(&self, peer: &str, op: &str) -> bool {
        peer == self.node_id
            || self.peers.iter().any(|p| {
                p.node_id == peer
                    && (op == "operation.result"
                        || op == "operation.delivery"
                        || p.operations.iter().any(|v| v == op))
            })
    }
    pub fn canonical_node(&self, reference: &str) -> Result<String> {
        if reference == self.node_id || (!self.alias.is_empty() && reference == self.alias) {
            return Ok(self.node_id.clone());
        }
        let matches: Vec<_> = self
            .peers
            .iter()
            .filter(|p| p.node_id == reference || p.aliases.iter().any(|a| a == reference))
            .collect();
        if matches.len() != 1 {
            return Err(error("TARGET_UNRESOLVED or ambiguous Node"));
        }
        Ok(matches[0].node_id.clone())
    }
}
pub struct Instance {
    _locks: Vec<File>,
    pub socket: PathBuf,
    pub epoch: String,
}
impl Instance {
    pub fn acquire(cfg: &Config) -> Result<Self> {
        Self::acquire_inner(cfg, false)
    }
    #[cfg(feature = "test-harness")]
    pub fn acquire_test_node(cfg: &Config) -> Result<Self> {
        Self::acquire_inner(cfg, true)
    }
    fn acquire_inner(cfg: &Config, isolated_test: bool) -> Result<Self> {
        let directory = lock_dir(&cfg.node_id);
        private_dir(&directory, true)?;
        let lock = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(directory.join("instance.lock"))?;
        lock.try_lock_exclusive()
            .map_err(|_| error("CAM Server for this Node is already running"))?;
        let mut locks = vec![lock];
        if !isolated_test {
            let machine = PathBuf::from(format!("/tmp/cam-server-{}", uid()));
            private_dir(&machine, true)?;
            let lock = OpenOptions::new()
                .create(true)
                .read(true)
                .write(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(machine.join("instance.lock"))?;
            lock.try_lock_exclusive()
                .map_err(|_| error("CAM Server for this machine/user is already running"))?;
            locks.push(lock);
        }
        private_dir(&cfg.runtime_dir, true)?;
        let socket = cfg.runtime_dir.join("server.sock");
        if socket.exists() {
            let info = fs::symlink_metadata(&socket)?;
            if info.uid() != uid() {
                return Err(error("foreign socket owner"));
            }
            fs::remove_file(&socket)?;
        }
        Ok(Self {
            _locks: locks,
            socket,
            epoch: id(),
        })
    }
}
impl Drop for Instance {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.socket);
    }
}

pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    let mut different = a.len() ^ b.len();
    for (x, y) in a.iter().zip(b.iter()) {
        different |= (x ^ y) as usize;
    }
    different == 0
}
