//! Headless reference implementation of the Client's forwarding component.
//! Queues are transport state only: no business request/return map exists.
use crate::{
    config::{constant_time_eq, private_read},
    server::Channel,
};
use cam_protocol::{error, now, read_frame, text, validate, write_frame, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    path::{Path, PathBuf},
    pin::Pin,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite, BufReader},
    net::UnixStream,
    process::Command,
    sync::{Notify, Semaphore},
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Endpoint {
    pub node_id: String,
    pub token_file: PathBuf,
    #[serde(default)]
    pub socket: Option<PathBuf>,
    #[serde(default)]
    pub command: Vec<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Agent {
    pub node_id: String,
    pub id: String,
    pub name: String,
    pub status: String,
    #[serde(default)]
    pub updated_at: Option<i64>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub client_id: String,
    pub endpoints: Vec<Endpoint>,
    #[serde(default)]
    pub agents: Vec<Agent>,
    #[serde(default)]
    pub agents_file: Option<PathBuf>,
    #[serde(default = "cache_ttl")]
    pub agent_ttl_seconds: i64,
}
fn cache_ttl() -> i64 {
    60
}
impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let mut config: Self = serde_json::from_slice(&private_read(path)?)?;
        let parent = path.parent().unwrap_or(Path::new("."));
        let mut nodes = HashSet::new();
        if !cam_protocol::valid_id(&config.client_id)
            || config.endpoints.len() > 128
            || !(1..=3600).contains(&config.agent_ttl_seconds)
        {
            return Err(error("invalid Client configuration"));
        }
        for endpoint in &mut config.endpoints {
            if !cam_protocol::valid_id(&endpoint.node_id)
                || !nodes.insert(endpoint.node_id.clone())
                || endpoint.socket.is_some() == !endpoint.command.is_empty()
            {
                return Err(error(
                    "each endpoint needs a unique Node and exactly one socket/command",
                ));
            }
            if endpoint.token_file.is_relative() {
                endpoint.token_file = parent.join(&endpoint.token_file);
            }
            if private_read(&endpoint.token_file)?.len() < 32 {
                return Err(error("short endpoint credential"));
            }
        }
        if let Some(file) = &mut config.agents_file {
            if file.is_relative() {
                *file = parent.join(&file);
            }
        }
        Ok(config)
    }
}
#[derive(Clone)]
struct Ready {
    generation: String,
    channel: Channel,
}
struct Relay {
    config: Config,
    ready: Mutex<HashMap<String, Ready>>,
    changed: Notify,
    budget: Arc<Semaphore>,
    started: i64,
}
impl Relay {
    fn agent_matches(&self, node: Option<&str>, reference: &str) -> Vec<Value> {
        let (agents, default_time) = match &self.config.agents_file {
            Some(path) => match private_read(path)
                .and_then(|data| Ok(serde_json::from_slice::<Vec<Agent>>(&data)?))
            {
                Ok(values) => {
                    let mtime = std::fs::metadata(path)
                        .and_then(|m| m.modified())
                        .ok()
                        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                        .map(|d| d.as_secs() as i64)
                        .unwrap_or(0);
                    (values, mtime)
                }
                Err(_) => return vec![],
            },
            None => (self.config.agents.clone(), self.started),
        };
        let ready = self.ready.lock().unwrap();
        let valid: Vec<_> = agents
            .into_iter()
            .filter(|a| {
                ready.contains_key(&a.node_id)
                    && node.map(|n| a.node_id == n).unwrap_or(true)
                    && now() - a.updated_at.unwrap_or(default_time) <= self.config.agent_ttl_seconds
            })
            .collect();
        let exact = valid
            .iter()
            .any(|a| a.id == reference || a.name.eq_ignore_ascii_case(reference));
        valid
            .into_iter()
            .filter(|a| {
                if exact {
                    a.id == reference || a.name.eq_ignore_ascii_case(reference)
                } else {
                    a.id.starts_with(reference)
                        || a.name.to_lowercase().starts_with(&reference.to_lowercase())
                }
            })
            .map(|a| json!({"node_id":a.node_id,"agent_id":a.id}))
            .collect()
    }
    fn ready_nodes(&self) -> HashSet<String> {
        self.ready
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, r)| !r.channel.closed())
            .map(|(n, _)| n.clone())
            .collect()
    }
    async fn endpoint(self: Arc<Self>, endpoint: Endpoint) -> Result<()> {
        let mut child = None;
        let (read, mut write): (
            Pin<Box<dyn AsyncRead + Send>>,
            Pin<Box<dyn AsyncWrite + Send>>,
        ) = if let Some(socket) = &endpoint.socket {
            let stream = UnixStream::connect(socket).await?;
            let (read, write) = stream.into_split();
            (Box::pin(read), Box::pin(write))
        } else {
            let mut command = Command::new(&endpoint.command[0]);
            command
                .args(&endpoint.command[1..])
                .kill_on_drop(true)
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::null());
            let mut process = command.spawn()?;
            let read = process
                .stdout
                .take()
                .ok_or_else(|| error("transport stdout unavailable"))?;
            let write = process
                .stdin
                .take()
                .ok_or_else(|| error("transport stdin unavailable"))?;
            child = Some(process);
            (Box::pin(read), Box::pin(write))
        };
        let token = String::from_utf8(private_read(&endpoint.token_file)?)?;
        write_frame(&mut write,&json!({"type":"hello","version":1,"role":"client","client_id":self.config.client_id,"token":token.trim()})).await?;
        let mut reader = BufReader::new(read);
        let (hello, _) =
            tokio::time::timeout(Duration::from_secs(5), read_frame(&mut reader)).await??;
        if hello["type"] != "hello" || hello["version"] != 1 || hello["node_id"] != endpoint.node_id
        {
            return Err(error("Server identity or version mismatch"));
        }
        let generation = text(&hello, "session_id")?.to_string();
        let ttl = hello["route_ttl_seconds"]
            .as_u64()
            .filter(|n| (3..=120).contains(n))
            .ok_or_else(|| error("invalid route TTL"))?;
        let allowed: HashSet<_> = hello["allowed_targets"]
            .as_array()
            .ok_or_else(|| error("missing Server route grant"))?
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect();
        let channel = Channel::start(write, self.budget.clone());
        self.ready.lock().unwrap().insert(
            endpoint.node_id.clone(),
            Ready {
                generation: generation.clone(),
                channel: channel.clone(),
            },
        );
        self.changed.notify_waiters();
        eprintln!(
            "cam-forwarder: connected client={} node={}",
            self.config.client_id, endpoint.node_id
        );
        let outcome:Result<()>=async {
            let mut seq=0u64;let mut refresh=tokio::time::interval(Duration::from_secs((ttl/3).max(1)));let mut snapshot=true;
            loop {
                if snapshot {
                    let reachable:Vec<_>=self.ready_nodes().into_iter().filter(|n|n!=&endpoint.node_id && allowed.contains(n)).collect();
                    seq+=1;channel.frame(&json!({"type":"routes.snapshot","session_id":generation,"seq":seq,"ttl_seconds":ttl,"reachable":reachable}))?;snapshot=false;
                }
                tokio::select! {
                    frame=read_frame(&mut reader)=>{
                        let (frame,raw)=frame?;
                        match frame["type"].as_str() {
                            Some("message")=>{
                                validate(&frame)?;
                                if frame["source"]["node_id"]!=endpoint.node_id {return Err(error("message source does not match authenticated ingress Server"));}
                                let target=text(&frame["target"],"node_id")?;
                                if target==endpoint.node_id || !allowed.contains(target) {return Err(error("recursive or unauthorized destination"));}
                                let destination=self.ready.lock().unwrap().get(target).cloned();
                                let failed=if let Some(destination)=destination {
                                    // Exactly the original bytes, including payload and formatting.
                                    destination.channel.forward(raw,channel.clone(),&frame["message_id"],target).is_err()
                                } else {true};
                                if failed {
                                    // Transport status only, never a fabricated business result.
                                    // No pending map or return channel is needed in the Client.
                                    channel.frame(&json!({"type":"forward.error","message_id":frame["message_id"],"target_node":target,"status":"not_forwarded"}))?;
                                    self.changed.notify_waiters();
                                }
                            },
                            Some("resolve.query")=>{
                                let reference=text(&frame,"agent")?;
                                let matches=self.agent_matches(frame["node"].as_str(),reference);
                                channel.frame(&json!({"type":"resolve.answer","id":frame["id"],"matches":matches}))?;
                            },
                            Some("pong")=>{},
                            _=>return Err(error("unsupported Server frame")),
                        }
                    },
                    _=self.changed.notified()=>{snapshot=true;},
                    _=refresh.tick()=>{snapshot=true;},
                }
            }
        }.await;
        {
            let mut ready = self.ready.lock().unwrap();
            if ready
                .get(&endpoint.node_id)
                .map(|r| constant_time_eq(r.generation.as_bytes(), generation.as_bytes()))
                .unwrap_or(false)
            {
                ready.remove(&endpoint.node_id);
            }
        }
        self.changed.notify_waiters();
        drop(child);
        outcome
    }
}
pub async fn run(config: Config) -> Result<()> {
    let relay = Arc::new(Relay {
        config: config.clone(),
        ready: Mutex::new(HashMap::new()),
        changed: Notify::new(),
        budget: Arc::new(Semaphore::new(16 * 1024 * 1024)),
        started: now(),
    });
    let mut tasks = tokio::task::JoinSet::new();
    for endpoint in config.endpoints {
        let relay = relay.clone();
        tasks.spawn(async move {
            let mut failures = 0u32;
            loop {
                let started = std::time::Instant::now();
                let _ = relay.clone().endpoint(endpoint.clone()).await;
                if started.elapsed() >= Duration::from_secs(10) {
                    failures = 0;
                }
                failures = failures.saturating_add(1);
                if failures.is_power_of_two() {
                    eprintln!(
                        "cam-forwarder: disconnected; retrying client={} node={}",
                        relay.config.client_id, endpoint.node_id
                    );
                }
                tokio::time::sleep(Duration::from_millis(
                    500 * (1u64 << failures.saturating_sub(1).min(4)),
                ))
                .await;
            }
        });
    }
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {_=tokio::signal::ctrl_c()=>{},_=terminate.recv()=>{}}
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    Ok(())
}
