//! Headless reference implementation of the Client's forwarding component.
//! Queues are transport state only: no business request/return map exists.
use crate::{
    config::{constant_time_eq, private_read},
    server::Channel,
};
use cam_protocol::{error, id, now, read_frame, text, validate, write_frame, Result};
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
    sync::{broadcast, oneshot, Notify, Semaphore},
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Endpoint {
    pub node_id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub name: String,
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
pub struct Relay {
    config: Config,
    ready: Mutex<HashMap<String, Ready>>,
    changed: Notify,
    budget: Arc<Semaphore>,
    started: i64,
    pending: Mutex<HashMap<(String, String), (String, oneshot::Sender<Value>)>>,
    info: Mutex<HashMap<String, Value>>,
    agents: Mutex<Option<Vec<Agent>>>,
    tasks: Mutex<HashMap<String, tokio::task::JoinHandle<()>>>,
    endpoints: Mutex<HashMap<String, Endpoint>>,
    pub events: broadcast::Sender<(String, Value)>,
}
struct PendingRequest<'a> {
    relay: &'a Relay,
    key: (String, String),
    token: String,
}
impl Drop for PendingRequest<'_> {
    fn drop(&mut self) {
        let mut pending = self.relay.pending.lock().unwrap();
        if pending
            .get(&self.key)
            .map(|(token, _)| token == &self.token)
            .unwrap_or(false)
        {
            pending.remove(&self.key);
        }
    }
}
impl Relay {
    pub fn start(config: Config) -> Arc<Self> {
        let (events, _) = broadcast::channel(512);
        let relay = Arc::new(Self {
            config: config.clone(),
            ready: Mutex::new(HashMap::new()),
            changed: Notify::new(),
            budget: Arc::new(Semaphore::new(16 * 1024 * 1024)),
            started: now(),
            pending: Mutex::new(HashMap::new()),
            info: Mutex::new(HashMap::new()),
            agents: Mutex::new(None),
            tasks: Mutex::new(HashMap::new()),
            endpoints: Mutex::new(
                config
                    .endpoints
                    .iter()
                    .map(|e| (e.node_id.clone(), e.clone()))
                    .collect(),
            ),
            events,
        });
        for endpoint in config.endpoints {
            relay.connect_endpoint(endpoint);
        }
        relay
    }
    fn connect_endpoint(self: &Arc<Self>, endpoint: Endpoint) {
        self.disconnect_node(&endpoint.node_id);
        let key = endpoint.node_id.clone();
        let r = self.clone();
        let task = tokio::spawn(async move {
            let mut failures = 0u32;
            loop {
                let result = r.clone().endpoint(endpoint.clone()).await;
                r.info.lock().unwrap().insert(endpoint.node_id.clone(),json!({"node_id":endpoint.node_id,"ready":false,
                    "last_error":result.err().map(|_|"connection/handshake failed; verify endpoint and credentials"),"checked_at":now()}));
                r.pending
                    .lock()
                    .unwrap()
                    .retain(|(node, _), _| node != &endpoint.node_id);
                let _ = r.events.send((
                    endpoint.node_id.clone(),
                    json!({"type":"node.disconnected"}),
                ));
                failures = failures.saturating_add(1);
                tokio::time::sleep(Duration::from_millis(
                    500 * (1u64 << failures.saturating_sub(1).min(4)),
                ))
                .await;
            }
        });
        self.tasks.lock().unwrap().insert(key, task);
    }
    pub fn disconnect_node(&self, node: &str) {
        if let Some(task) = self.tasks.lock().unwrap().remove(node) {
            task.abort();
        }
        self.ready.lock().unwrap().remove(node);
        self.pending.lock().unwrap().retain(|(n, _), _| n != node);
        self.info.lock().unwrap().insert(
            node.into(),
            json!({"node_id":node,"ready":false,"last_error":"disconnected","checked_at":now()}),
        );
        let _ = self
            .events
            .send((node.into(), json!({"type":"node.disconnected"})));
        self.changed.notify_waiters();
    }
    pub fn connect_node(self: &Arc<Self>, node: &str) -> Result<()> {
        let endpoint = self
            .endpoints
            .lock()
            .unwrap()
            .get(node)
            .cloned()
            .ok_or_else(|| error("unknown Node"))?;
        self.connect_endpoint(endpoint);
        Ok(())
    }
    pub fn update_endpoint(self: &Arc<Self>, endpoint: Endpoint) {
        self.endpoints
            .lock()
            .unwrap()
            .insert(endpoint.node_id.clone(), endpoint.clone());
        self.connect_endpoint(endpoint);
    }
    pub fn remove_node(&self, node: &str) {
        self.disconnect_node(node);
        self.endpoints.lock().unwrap().remove(node);
    }
    pub fn configured_nodes(&self) -> Vec<String> {
        let mut nodes: Vec<_> = self.endpoints.lock().unwrap().keys().cloned().collect();
        nodes.sort();
        nodes
    }
    pub async fn shutdown(&self) {
        let tasks: Vec<_> = self.tasks.lock().unwrap().drain().map(|(_, t)| t).collect();
        for task in &tasks {
            task.abort();
        }
        for task in tasks {
            let _ = task.await;
        }
        self.ready.lock().unwrap().clear();
        self.pending.lock().unwrap().clear();
    }
    pub fn status(&self) -> Vec<Value> {
        let info = self.info.lock().unwrap();
        self.configured_nodes()
            .iter()
            .map(|node| {
                info.get(node).cloned().unwrap_or_else(
                    || json!({"node_id":node,"ready":false,"last_error":"connecting"}),
                )
            })
            .collect()
    }
    pub fn set_agents(&self, agents: Vec<Agent>) {
        *self.agents.lock().unwrap() = Some(agents);
    }
    pub async fn request(
        &self,
        node: &str,
        action: &str,
        args: Value,
        request_id: &str,
    ) -> Result<Value> {
        let capability = if action.starts_with("terminal.") {
            "terminal.control"
        } else {
            action
        };
        let supported = self
            .info
            .lock()
            .unwrap()
            .get(node)
            .and_then(|info| info["capabilities"].as_array())
            .map(|caps| caps.iter().any(|cap| cap == capability))
            .unwrap_or(false);
        if !supported {
            return Err(error("Server does not advertise this capability for the authenticated Client; verify management grant and backend"));
        }
        let channel = self
            .ready
            .lock()
            .unwrap()
            .get(node)
            .map(|r| r.channel.clone())
            .ok_or_else(|| error("Node is offline"))?;
        let key = (node.to_string(), request_id.to_string());
        let token = id();
        let (tx, rx) = oneshot::channel();
        {
            let mut pending = self.pending.lock().unwrap();
            if pending.len() >= 64 || pending.contains_key(&key) {
                return Err(error("Client request capacity or duplicate in-flight ID"));
            }
            pending.insert(key.clone(), (token.clone(), tx));
        }
        // Outer timeouts cancel this future, so cleanup must also run on
        // cancellation. A token prevents an old reply's guard removing a new
        // request that reused the same durable operation ID.
        let _pending = PendingRequest {
            relay: self,
            key,
            token,
        };
        let result: Result<Value> = async {
            channel.frame(
                &json!({"type":"sdk.request","id":request_id,"action":action,"args":args}),
            )?;
            let answer = tokio::time::timeout(Duration::from_secs(75), rx).await??;
            if let Some(message) = answer["error"].as_str() {
                return Err(error(message));
            }
            Ok(answer["data"].clone())
        }
        .await;
        result
    }
    fn agent_matches(&self, node: Option<&str>, reference: &str) -> Vec<Value> {
        let cached = self.agents.lock().unwrap().clone();
        let (agents, default_time) = if let Some(agents) = cached {
            (agents, now())
        } else {
            match &self.config.agents_file {
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
            }
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
        self.info.lock().unwrap().insert(endpoint.node_id.clone(),json!({"node_id":endpoint.node_id,"ready":true,
            "server_epoch":hello["server_epoch"],"capabilities":hello["capabilities"],"management":hello["management"],"allowed_targets":hello["allowed_targets"],"checked_at":now(),"last_received":now()}));
        eprintln!(
            "cam-forwarder: connected client={} node={}",
            self.config.client_id, endpoint.node_id
        );
        let outcome:Result<()>=async {
            let mut seq=0u64;let mut refresh=tokio::time::interval(Duration::from_secs((ttl/3).min(5).max(1)));let mut snapshot=true;
            let mut last_received=std::time::Instant::now();
            loop {
                if snapshot {
                    let reachable:Vec<_>=self.ready_nodes().into_iter().filter(|n|n!=&endpoint.node_id && allowed.contains(n)).collect();
                    seq+=1;channel.frame(&json!({"type":"routes.snapshot","session_id":generation,"seq":seq,"ttl_seconds":ttl,"reachable":reachable}))?;snapshot=false;
                }
                tokio::select! {
                    frame=read_frame(&mut reader)=>{
                        let (frame,raw)=frame?;last_received=std::time::Instant::now();
                        if let Some(info)=self.info.lock().unwrap().get_mut(&endpoint.node_id){info["last_received"]=json!(now());}
                        match frame["type"].as_str() {
                            Some("sdk.result")=>{if let Some(mid)=frame["id"].as_str(){if let Some((_,tx))=self.pending.lock().unwrap().remove(&(endpoint.node_id.clone(),mid.into())){let _=tx.send(frame);}}},
                            Some("terminal.output"|"terminal.snapshot"|"terminal.closed")=>{let _=self.events.send((endpoint.node_id.clone(),frame));},
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
                    _=refresh.tick()=>{if last_received.elapsed()>Duration::from_secs(15){return Err(error("Server heartbeat timed out"));}channel.frame(&json!({"type":"ping"}))?;snapshot=true;},
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
    let relay = Relay::start(config);
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {_=tokio::signal::ctrl_c()=>{},_=terminate.recv()=>{}}
    relay.shutdown().await;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn canceled_requests_release_capacity_without_erasing_a_replacement() {
        let relay = Relay::start(Config {
            client_id: "test-client".into(),
            endpoints: vec![],
            agents: vec![],
            agents_file: None,
            agent_ttl_seconds: 60,
        });
        let (client, mut peer) = tokio::net::UnixStream::pair().unwrap();
        let (_, write) = client.into_split();
        let channel = Channel::start(write, relay.budget.clone());
        relay.ready.lock().unwrap().insert(
            "node".into(),
            Ready {
                generation: id(),
                channel,
            },
        );
        relay
            .info
            .lock()
            .unwrap()
            .insert("node".into(), json!({"capabilities":["agents.snapshot"]}));
        let drain = tokio::spawn(async move {
            use tokio::io::AsyncReadExt;
            let mut bytes = [0; 4096];
            while peer.read(&mut bytes).await.unwrap_or(0) != 0 {}
        });
        // Exceed the request capacity with real canceled futures, without
        // replies. A single stalled Node must not exhaust the whole Client.
        for _ in 0..80 {
            let result = tokio::time::timeout(
                Duration::from_millis(1),
                relay.request("node", "agents.snapshot", json!({}), &id()),
            )
            .await;
            assert!(result.is_err(), "request was rejected before its timeout");
            assert_eq!(relay.pending.lock().unwrap().len(), 0);
        }
        let key = ("node".into(), "same-operation".into());
        let guard = PendingRequest {
            relay: &relay,
            key: key.clone(),
            token: "old".into(),
        };
        let (sender, receiver) = oneshot::channel();
        relay
            .pending
            .lock()
            .unwrap()
            .insert(key.clone(), ("new".into(), sender));
        drop(guard);
        assert!(relay.pending.lock().unwrap().contains_key(&key));
        relay
            .pending
            .lock()
            .unwrap()
            .remove(&key)
            .unwrap()
            .1
            .send(json!("kept"))
            .unwrap();
        assert_eq!(receiver.await.unwrap(), json!("kept"));
        relay.shutdown().await;
        tokio::time::timeout(Duration::from_secs(1), drain)
            .await
            .unwrap()
            .unwrap();
    }
}
