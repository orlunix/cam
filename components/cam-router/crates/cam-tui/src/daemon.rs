use crate::config::{prepare_runtime, Config};
use base64::{engine::general_purpose::STANDARD, Engine};
use cam_protocol::{error, id, now, read_frame, write_frame, Result};
use cam_server::{
    config::uid,
    relay::{Agent, Endpoint, Relay},
};
use fs2::FileExt;
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    fs::{self, OpenOptions},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::BufReader,
    net::{UnixListener, UnixStream},
    sync::{Notify, Semaphore},
};

struct Output {
    node: String,
    agent: String,
    pane: String,
    snapshot: Value,
    bytes: VecDeque<u8>,
    base: u64,
    next: u64,
    closed: bool,
    error: Option<String>,
    writer: String,
    writer_seen: i64,
}

fn geometry_read_only(terminals: &HashMap<String, Output>, tid: &str, view: &str) -> bool {
    terminals.get(tid).map(|target| terminals.values().any(|t|
        t.node == target.node && t.agent == target.agent && t.pane == "primary"
            && !t.closed && !t.writer.is_empty() && now() - t.writer_seen <= 15 && t.writer != view
    )).unwrap_or(false)
}
pub struct Daemon {
    config: Mutex<Config>,
    path: PathBuf,
    epoch: String,
    relay: Arc<Relay>,
    agents: Mutex<HashMap<String, Vec<Value>>>,
    terminals: Mutex<HashMap<String, Output>>,
    snapshots: Mutex<HashMap<String, i64>>,
    database: Mutex<Connection>,
    last_poll: Mutex<i64>,
    last_tick: Mutex<i64>,
    stop: Notify,
    refresh: Notify,
    refresh_nodes: Mutex<HashSet<String>>,
    startup_agents: Mutex<HashMap<(String, String), Instant>>,
    capacity: Arc<Semaphore>,
    jobs_capacity: Arc<Semaphore>,
    attach_lock: tokio::sync::Mutex<()>,
}
impl Daemon {
    pub fn new(config: Config, path: PathBuf) -> Result<Arc<Self>> {
        prepare_runtime(&config.runtime_dir)?;
        let dbpath = config.runtime_dir.join("client.db");
        let database = Connection::open(&dbpath)?;
        fs::set_permissions(&dbpath, fs::Permissions::from_mode(0o600))?;
        database.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS job_runs(id TEXT PRIMARY KEY,job TEXT NOT NULL,data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS agent_visits(node TEXT NOT NULL,agent TEXT NOT NULL,visited INTEGER NOT NULL,PRIMARY KEY(node,agent));")?;
        let relay = Relay::start(config.relay());
        let daemon = Arc::new(Self {
            config: Mutex::new(config),
            path,
            epoch: id(),
            relay,
            agents: Mutex::new(HashMap::new()),
            terminals: Mutex::new(HashMap::new()),
            database: Mutex::new(database),
            snapshots: Mutex::new(HashMap::new()),
            last_poll: Mutex::new(0),
            last_tick: Mutex::new(0),
            stop: Notify::new(),
            refresh: Notify::new(),
            refresh_nodes: Mutex::new(HashSet::new()),
            startup_agents: Mutex::new(HashMap::new()),
            capacity: Arc::new(Semaphore::new(64)),
            jobs_capacity: Arc::new(Semaphore::new(4)),
            attach_lock: tokio::sync::Mutex::new(()),
        });
        for mut job in daemon.jobs()? {
            if job["last_status"] == "running" {
                job["last_status"] = json!("unknown");
                job["last_error"] = json!("daemon stopped during execution; not replayed");
                daemon.save_job(&job)?;
            }
        }
        Ok(daemon)
    }
    fn visits(&self) -> Result<Value> {
        let db = self.database.lock().unwrap();
        let mut statement = db.prepare("SELECT node,agent,visited FROM agent_visits")?;
        let rows = statement.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?)))?;
        let mut visits = serde_json::Map::new();
        for row in rows {
            let (node, agent, visited) = row?;
            visits.insert(format!("{node}:{agent}"), json!(visited));
        }
        Ok(Value::Object(visits))
    }
    fn visit(&self, args: &Value) -> Result<Value> {
        let node = args["node_id"].as_str().ok_or_else(|| error("Node required"))?;
        let agent = args["agent_id"].as_str().ok_or_else(|| error("agent required"))?;
        if !self.agents.lock().unwrap().get(node).map(|list| list.iter().any(|a| a["id"] == agent)).unwrap_or(false) {
            return Err(error("agent not found in Client cache"));
        }
        let mut db = self.database.lock().unwrap();
        let tx = db.transaction()?;
        // A sequence resolves visits within one millisecond and clock rollback.
        // This is Client UI state; never send it to a Server or the agent store.
        let millis = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_millis() as i64;
        tx.execute("INSERT OR REPLACE INTO agent_visits VALUES (?1,?2,MAX(?3,COALESCE((SELECT MAX(visited) FROM agent_visits),0)+1))", params![node, agent, millis])?;
        tx.execute("DELETE FROM agent_visits WHERE (node,agent) IN (SELECT node,agent FROM agent_visits ORDER BY visited DESC LIMIT -1 OFFSET 1000)", [])?;
        tx.commit()?;
        Ok(json!({"ok":true}))
    }
    fn jobs(&self) -> Result<Vec<Value>> {
        let db = self.database.lock().unwrap();
        let mut statement = db.prepare("SELECT data FROM jobs ORDER BY id")?;
        let rows = statement.query_map([], |r| r.get::<_, String>(0))?;
        let mut jobs = vec![];
        for row in rows {
            jobs.push(serde_json::from_str(&row?)?);
        }
        Ok(jobs)
    }
    fn save_job(&self, job: &Value) -> Result<()> {
        self.database.lock().unwrap().execute(
            "INSERT OR REPLACE INTO jobs VALUES (?1,?2)",
            params![
                job["id"].as_str().ok_or_else(|| error("job ID missing"))?,
                serde_json::to_string(job)?
            ],
        )?;
        Ok(())
    }
    fn cache(&self) -> Vec<Value> {
        let freshness = self.config.lock().unwrap().freshness_seconds() as i64;
        let ready: std::collections::HashMap<_, _> = self
            .relay
            .status()
            .into_iter()
            .map(|n| {
                (
                    n["node_id"].as_str().unwrap_or("").to_string(),
                    n["ready"] == true,
                )
            })
            .collect();
        let mut agents: Vec<_> = self
            .agents
            .lock()
            .unwrap()
            .iter()
            .flat_map(|(node, list)| {
                list.iter().map(|a| {
                    let mut a = a.clone();
                    a["stale"] = json!(
                        !ready.get(node).copied().unwrap_or(false)
                            || now() - a["updated_at"].as_i64().unwrap_or(0) > freshness
                    );
                    a
                })
            })
            .collect();
        agents.sort_by_key(|a| {
            (
                a["node_id"].as_str().unwrap_or("").to_string(),
                a["name"].as_str().unwrap_or("").to_string(),
            )
        });
        agents
    }
    fn health(&self) -> Value {
        let freshness = self.config.lock().unwrap().freshness_seconds() as i64;
        let multi_node = self.relay.configured_nodes().len() > 1;
        let nodes: Vec<_> = self
            .relay
            .status()
            .into_iter()
            .map(|mut n| {
                let node = n["node_id"].as_str().unwrap_or("");
                let updated = self.snapshots.lock().unwrap().get(node).copied();
                n["tunnel_health"] = json!(if n["ready"] == true
                    && now() - n["last_received"].as_i64().unwrap_or(0) < 15
                {
                    "healthy"
                } else {
                    "unhealthy"
                });
                n["status_health"] =
                    json!(if updated.map(|t| now() - t < freshness).unwrap_or(false) {
                        "healthy"
                    } else {
                        "unknown"
                    });
                n["forwarding_health"] = json!("unknown");
                n["forwarding_applicable"] = json!(
                    multi_node
                        && n["allowed_targets"]
                            .as_array()
                            .map(|v| !v.is_empty())
                            .unwrap_or(false)
                );
                n["forwarding_detail"] = json!(if n["forwarding_applicable"] == true {
                    "tunnel/heartbeat checked; end-to-end forwarding not actively probed"
                } else {
                    "forwarding not applicable: no configured authorized multi-Node path"
                });
                if n["management"] != true {
                    n["status_detail"] = json!(
                        "Server metadata management capability is not granted or is unsupported"
                    );
                }
                n
            })
            .collect();
        let tick = *self.last_tick.lock().unwrap();
        let poll = *self.last_poll.lock().unwrap();
        json!({"daemon":"healthy","epoch":self.epoch,"checked_at":now(),"status_poll_at":poll,
            "scheduler_tick_at":tick,"scheduler_health":if tick==0{"unknown"}else if now()-tick<5{"healthy"}else{"unhealthy"},
            "status_loop_health":if poll==0{"unknown"}else if now()-poll<90{"healthy"}else{"unhealthy"},"nodes":nodes})
    }
    async fn poll(self: Arc<Self>) {
        let delay = self.config.lock().unwrap().poll_seconds;
        let mut timer = tokio::time::interval(Duration::from_secs(delay));
        let mut startup = tokio::time::interval(Duration::from_secs(2));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        startup.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            let nodes = tokio::select! {
                // An overdue ordinary poll must not be starved by repeated
                // startup timeouts on one Node, or make healthy Nodes stale.
                biased;
                _ = timer.tick() => self.relay.configured_nodes(),
                _ = self.refresh.notified() => self.refresh_nodes.lock().unwrap().drain().collect(),
                _ = startup.tick() => {
                    let snapshots = self.snapshots.lock().unwrap();
                    let mut watched = self.startup_agents.lock().unwrap();
                    watched.retain(|_, since| since.elapsed() < Duration::from_secs(600));
                    self.relay.status().into_iter().filter_map(|node| {
                        let id = node["node_id"].as_str()?;
                        let initializing = watched.keys().any(|(owner, _)| owner == id);
                        (node["ready"] == true && node["management"] == true && (!snapshots.contains_key(id) || initializing)).then(|| id.to_string())
                    }).collect()
                }
            };
            if nodes.is_empty() {
                continue;
            }
            let cycle_started = Instant::now();
            let mut tasks = tokio::task::JoinSet::new();
            for node in nodes {
                let d = self.clone();
                tasks.spawn(async move {
                    let response = tokio::time::timeout(
                        Duration::from_secs(5),
                        d.relay.request(&node, "agents.snapshot", json!({}), &id()),
                    )
                    .await;
                    if let Ok(Ok(snapshot)) = response {
                        if let Some(list) = snapshot["agents"].as_array() {
                            let mut list = list.clone();
                            for a in &mut list {
                                a["updated_at"] = json!(now());
                            }
                            d.startup_agents
                                .lock()
                                .unwrap()
                                .retain(|(owner, agent), since| {
                                    owner != &node
                                        || *since > cycle_started
                                        || !list.iter().any(|a| {
                                            a["id"] == *agent
                                                && (a["phase"] == "started"
                                                    || a["status"] == "dead")
                                        })
                                });
                            d.snapshots.lock().unwrap().insert(node.clone(), now());
                            d.agents.lock().unwrap().insert(node, list);
                        }
                    }
                });
            }
            while tasks.join_next().await.is_some() {}
            let agents=self.cache().into_iter().filter_map(|a|serde_json::from_value::<Agent>(json!({"node_id":a["node_id"],"id":a["id"],"name":a["name"],"status":a["status"],"updated_at":a["updated_at"]})).ok()).collect();
            self.relay.set_agents(agents);
            *self.last_poll.lock().unwrap() = now();
        }
    }
    async fn outputs(self: Arc<Self>) {
        let mut events = self.relay.events.subscribe();
        loop {
            match events.recv().await {
                Ok((node, event)) => {
                    let kind = event["type"].as_str().unwrap_or("");
                    let tid = event["terminal_id"].as_str().unwrap_or("").to_string();
                    let mut terminals = self.terminals.lock().unwrap();
                    if kind == "node.disconnected" {
                        self.snapshots.lock().unwrap().remove(&node);
                        for terminal in terminals.values_mut().filter(|t| t.node == node) {
                            terminal.closed = true;
                            terminal.error =
                                Some("Node disconnected; reopen tab to resynchronize".into());
                        }
                        continue;
                    }
                    let output = terminals.entry(tid.clone()).or_insert_with(|| Output {
                        node: node.clone(),
                        agent: String::new(),
                        pane: String::new(),
                        snapshot: Value::Null,
                        bytes: VecDeque::new(),
                        base: 0,
                        next: 0,
                        closed: false,
                        error: None,
                        writer: String::new(),
                        writer_seen: 0,
                    });
                    if kind == "terminal.snapshot" {
                        output.snapshot = event;
                        output.bytes.clear();
                        output.base = 0;
                        output.next = 0;
                        output.closed = false;
                    } else if kind == "terminal.output" {
                        if event["seq"].as_u64() != Some(output.next) {
                            output.closed = true;
                            output.error =
                                Some("terminal sequence gap; reopen to resynchronize".into());
                            continue;
                        }
                        match event["data"].as_str().and_then(|s| STANDARD.decode(s).ok()) {
                            Some(bytes) => {
                                output.next += bytes.len() as u64;
                                output.bytes.extend(bytes);
                                while output.bytes.len() > 1024 * 1024 {
                                    output.bytes.pop_front();
                                    output.base += 1;
                                }
                            }
                            None => {
                                output.closed = true;
                            }
                        }
                    } else if kind == "terminal.closed" {
                        output.closed = true;
                        output.error = event["error"].as_str().map(str::to_string);
                    }
                }
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                    for t in self.terminals.lock().unwrap().values_mut() {
                        t.closed = true;
                        t.error =
                            Some("terminal event backlog overflow; reopen to resynchronize".into());
                    }
                }
                Err(_) => break,
            }
        }
    }
    async fn connection(self: Arc<Self>, stream: UnixStream) -> Result<()> {
        let _permit = self
            .capacity
            .clone()
            .try_acquire_owned()
            .map_err(|_| error("daemon busy"))?;
        if stream.peer_cred()?.uid() != uid() {
            return Err(error("foreign daemon caller"));
        }
        let (read, mut write) = stream.into_split();
        let mut reader = BufReader::new(read);
        let (request, _) =
            tokio::time::timeout(Duration::from_secs(5), read_frame(&mut reader)).await??;
        let identity = request["id"]
            .as_str()
            .ok_or_else(|| error("request ID missing"))?;
        if !cam_protocol::valid_id(identity) || request["version"] != 1 {
            return Err(error("incompatible request"));
        }
        let result = self
            .clone()
            .dispatch(
                request["action"].as_str().unwrap_or(""),
                request["args"].clone(),
                identity,
            )
            .await;
        let response = match result {
            Ok(data) => json!({"id":identity,"data":data}),
            Err(err) => json!({"id":identity,"error":err.to_string()}),
        };
        write_frame(&mut write, &response).await
    }
    async fn dispatch(
        self: Arc<Self>,
        action: &str,
        args: Value,
        request_id: &str,
    ) -> Result<Value> {
        match action {
            "snapshot" => Ok(
                json!({"epoch":self.epoch,"agents":self.cache(),"recent_visits":self.visits()?,"nodes":self.relay.status(),"connections":self.config.lock().unwrap().endpoints,"health":self.health(),"jobs":self.jobs()?}),
            ),
            "agent.visit" => self.visit(&args),
            "terminal.list" | "terminal.create" | "terminal.close" => {
                let node = args["node_id"].as_str().ok_or_else(|| error("Node required"))?;
                self.relay.request(node, action, args.clone(), request_id).await
            }
            "health" => Ok(self.health()),
            "shutdown" => {
                self.stop.notify_one();
                Ok(json!({"ok":true}))
            }
            "node.upsert" | "node.ssh_add" => {
                let endpoint: Endpoint = if action == "node.ssh_add" {
                    let config = self.config.lock().unwrap().clone();
                    crate::ssh::onboard(&config, serde_json::from_value(args)?).await?
                } else { serde_json::from_value(args)? };
                Config::validate_endpoint(&endpoint)?;
                {
                    let mut config = self.config.lock().unwrap();
                    if config.endpoints.len() >= 128
                        && !config
                            .endpoints
                            .iter()
                            .any(|e| e.node_id == endpoint.node_id)
                    {
                        return Err(error("Client Node capacity reached (128)"));
                    }
                    let mut next = config.clone();
                    next.endpoints.retain(|e| e.node_id != endpoint.node_id);
                    next.endpoints.push(endpoint.clone());
                    next.save(&self.path)?;
                    *config = next;
                }
                let node_id = endpoint.node_id.clone();
                self.relay.update_endpoint(endpoint);
                Ok(json!({"ok":true,"node_id":node_id}))
            }
            "node.connect" | "node.disconnect" | "node.remove" => {
                let node = args["node_id"]
                    .as_str()
                    .ok_or_else(|| error("Node required"))?;
                if action == "node.connect" {
                    let config = self.config.lock().unwrap().clone();
                    let connection = config.endpoints.iter().find(|e| e.node_id == node)
                        .and_then(crate::ssh::profile);
                    if let Some(connection) = connection {
                        let endpoint = crate::ssh::onboard(&config, connection).await?;
                        let mut config = self.config.lock().unwrap();
                        let mut next = config.clone();
                        next.endpoints.retain(|e| e.node_id != node);
                        next.endpoints.push(endpoint.clone());
                        next.save(&self.path)?;
                        *config = next;
                        self.relay.update_endpoint(endpoint);
                    }
                    self.relay.connect_node(node)?;
                } else if action == "node.disconnect" {
                    self.relay.disconnect_node(node);
                } else {
                    {
                        let mut config = self.config.lock().unwrap();
                        config.endpoints.retain(|e| e.node_id != node);
                        config.save(&self.path)?;
                    }
                    self.relay.remove_node(node);
                    self.agents.lock().unwrap().remove(node);
                }
                Ok(json!({"ok":true}))
            }
            "agent.operation" | "loop.deliver" | "agent.update" => {
                let node = args["node_id"]
                    .as_str()
                    .ok_or_else(|| error("Node required"))?;
                let result = self
                    .relay
                    .request(node, action, args.clone(), request_id)
                    .await;
                if action == "agent.update" {
                    if let Ok(result) = &result {
                        if result["agent"]["id"] != args["agent_id"] || result["agent"]["node_id"] != node {
                            return Err(error("invalid agent update result"));
                        }
                        if let Some(agent) = self.agents.lock().unwrap().get_mut(node)
                            .and_then(|agents| agents.iter_mut().find(|a| a["id"] == args["agent_id"])) {
                            *agent = result["agent"].clone();
                            agent["updated_at"] = json!(now());
                        }
                    }
                }
                let operation = args["operation"].as_str().unwrap_or("");
                if ["agent.run", "agent.reboot"].contains(&operation)
                    && result
                        .as_ref()
                        .map(|r| r["payload"]["exit_code"] == 0)
                        .unwrap_or(false)
                {
                    if let Some(agent) = args["agent_id"].as_str() {
                        self.startup_agents
                            .lock()
                            .unwrap()
                            .insert((node.to_string(), agent.to_string()), Instant::now());
                    }
                }
                if ["agent.stop", "agent.kill", "agent.remove"].contains(&operation)
                    && result
                        .as_ref()
                        .map(|r| r["payload"]["exit_code"] == 0)
                        .unwrap_or(false)
                {
                    if let Some(agent) = args["agent_id"].as_str() {
                        self.startup_agents
                            .lock()
                            .unwrap()
                            .remove(&(node.to_string(), agent.to_string()));
                    }
                }
                if !["agent.capture", "agent.status"].contains(&operation) {
                    self.refresh_nodes.lock().unwrap().insert(node.to_string());
                    self.refresh.notify_one();
                }
                result
            }
            "terminal.attach" => {
                let _attach = self.attach_lock.lock().await;
                let node = args["node_id"]
                    .as_str()
                    .ok_or_else(|| error("Node required"))?;
                let agent = args["agent_id"]
                    .as_str()
                    .ok_or_else(|| error("agent required"))?;
                let pane = args["pane_id"].as_str().unwrap_or("primary");
                if !args["pane_id"].is_null() && !args["pane_id"].as_str().and_then(|p| p.strip_prefix('%'))
                    .map(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit())).unwrap_or(false) {
                    return Err(error("invalid pane ID"));
                }
                let view = args["view_id"].as_str().unwrap_or("rpc");
                {
                    let mut terminals = self.terminals.lock().unwrap();
                    if let Some((tid, output)) = terminals.iter_mut().find(|(_, t)| {
                        t.node == node && t.agent == agent && t.pane == pane && !t.closed && t.base == 0
                    }) {
                        if output.writer.is_empty() || now() - output.writer_seen > 15 {
                            output.writer = view.into();
                            output.writer_seen = now();
                        }
                        return Ok(
                            json!({"terminal_id":tid,"rows":output.snapshot["rows"],"cols":output.snapshot["cols"],"read_only":output.writer!=view}),
                        );
                    }
                }
                let stale: Vec<_> = self
                    .terminals
                    .lock()
                    .unwrap()
                    .iter()
                    .filter(|(_, t)| t.node == node && t.agent == agent && t.pane == pane && (t.closed || t.base > 0))
                    .map(|(tid, _)| tid.clone())
                    .collect();
                for tid in stale {
                    let _ = self
                        .relay
                        .request(node, "terminal.detach", json!({"terminal_id":tid}), &id())
                        .await;
                }
                let answer = self
                    .relay
                    .request(node, action, args.clone(), request_id)
                    .await?;
                let tid = answer["terminal_id"]
                    .as_str()
                    .ok_or_else(|| error("terminal ID missing"))?
                    .to_string();
                {
                    let mut terminals = self.terminals.lock().unwrap();
                    terminals.retain(|key, t| key == &tid || !t.closed);
                    let output = terminals.entry(tid).or_insert_with(|| Output {
                        node: node.into(),
                        agent: agent.into(),
                        pane: pane.into(),
                        snapshot: Value::Null,
                        bytes: VecDeque::new(),
                        base: 0,
                        next: 0,
                        closed: false,
                        error: None,
                        writer: view.into(),
                        writer_seen: now(),
                    });
                    output.agent = agent.into();
                    output.pane = pane.into();
                    output.writer = view.into();
                    output.writer_seen = now();
                }
                Ok(answer)
            }
            "terminal.read" => {
                let mut terminals = self.terminals.lock().unwrap();
                let resize_read_only = geometry_read_only(&terminals, args["terminal_id"].as_str().unwrap_or(""), args["view_id"].as_str().unwrap_or("rpc"));
                let terminal = terminals
                    .get_mut(args["terminal_id"].as_str().unwrap_or(""))
                    .ok_or_else(|| error("terminal not found"))?;
                let view = args["view_id"].as_str().unwrap_or("rpc");
                if terminal.writer.is_empty() || now() - terminal.writer_seen > 15 {
                    terminal.writer = view.into();
                }
                if terminal.writer == view {
                    terminal.writer_seen = now();
                }
                let cursor = args["cursor"].as_u64().unwrap_or(0);
                if cursor < terminal.base || cursor > terminal.next {
                    return Err(error(
                        "terminal history gap; close/reopen tab to resynchronize",
                    ));
                }
                let data: Vec<_> = terminal
                    .bytes
                    .iter()
                    .skip((cursor - terminal.base) as usize)
                    .take(32768)
                    .copied()
                    .collect();
                Ok(
                    json!({"snapshot":if args["initial"]==true{terminal.snapshot.clone()}else{Value::Null},"cursor":cursor+data.len()as u64,"data":STANDARD.encode(data),"closed":terminal.closed,"error":terminal.error,"read_only":terminal.writer!=view,"resize_read_only":resize_read_only,"rows":terminal.snapshot["rows"],"cols":terminal.snapshot["cols"]}),
                )
            }
            "terminal.release" => {
                let mut terminals = self.terminals.lock().unwrap();
                if let Some(t) = terminals.get_mut(args["terminal_id"].as_str().unwrap_or("")) {
                    if t.writer == args["view_id"].as_str().unwrap_or("rpc") {
                        t.writer.clear();
                    }
                }
                Ok(json!({"ok":true}))
            }
            "terminal.input" | "terminal.resize" | "terminal.detach" => {
                let node = {
                    let terminals = self.terminals.lock().unwrap();
                    if action == "terminal.resize" && geometry_read_only(&terminals, args["terminal_id"].as_str().unwrap_or(""), args["view_id"].as_str().unwrap_or("rpc")) {
                        return Err(error("terminal geometry is controlled by the agent writer view"));
                    }
                    let terminal = terminals
                        .get(args["terminal_id"].as_str().unwrap_or(""))
                        .filter(|t| !t.closed || action == "terminal.detach")
                        .ok_or_else(|| error("terminal closed"))?;
                    if terminal.writer != args["view_id"].as_str().unwrap_or("rpc")
                        || now() - terminal.writer_seen > 15
                    {
                        return Err(error("terminal is read-only in this view; close/release the writer view first"));
                    }
                    terminal.node.clone()
                };
                let result = self
                    .relay
                    .request(&node, action, args.clone(), request_id)
                    .await?;
                if action == "terminal.detach" {
                    if let Some(t) = self
                        .terminals
                        .lock()
                        .unwrap()
                        .get_mut(args["terminal_id"].as_str().unwrap_or(""))
                    {
                        t.closed = true;
                    }
                }
                if action == "terminal.resize" {
                    let mut terminals = self.terminals.lock().unwrap();
                    let target = terminals.get(args["terminal_id"].as_str().unwrap_or(""))
                        .map(|t| (t.node.clone(), t.agent.clone()));
                    for t in terminals.values_mut().filter(|t| target.as_ref() == Some(&(t.node.clone(), t.agent.clone()))) {
                        if !t.closed && !t.snapshot.is_null() {
                            t.snapshot["rows"] = args["rows"].clone();
                            t.snapshot["cols"] = args["cols"].clone();
                        }
                    }
                }
                Ok(result)
            }
            "job.save" => {
                let mut job = validate_job(args)?;
                if let Some(existing) = self.jobs()?.iter().find(|j| j["id"] == job["id"]) {
                    if existing["last_status"] == "running" {
                        return Err(error("pause/wait for the active job before editing"));
                    }
                    if existing["revision"].as_u64().unwrap_or(0)
                        != job["revision"].as_u64().unwrap_or(0)
                    {
                        return Err(error("job revision changed; reload before editing"));
                    }
                    job["revision"] = json!(existing["revision"].as_u64().unwrap_or(0) + 1);
                } else {
                    job["revision"] = json!(0);
                }
                self.save_job(&job)?;
                Ok(job)
            }
            "job.history" => {
                let db = self.database.lock().unwrap();
                let mut statement = db.prepare(
                    "SELECT data FROM job_runs WHERE job=?1 ORDER BY rowid DESC LIMIT 100",
                )?;
                let rows = statement
                    .query_map(params![args["id"].as_str().unwrap_or("")], |r| {
                        r.get::<_, String>(0)
                    })?;
                let mut result = vec![];
                for row in rows {
                    result.push(serde_json::from_str::<Value>(&row?)?);
                }
                Ok(json!(result))
            }
            "job.remove" => {
                self.database.lock().unwrap().execute(
                    "DELETE FROM jobs WHERE id=?1",
                    params![args["id"].as_str().unwrap_or("")],
                )?;
                Ok(json!({"ok":true}))
            }
            "job.toggle" => {
                let mut job = self
                    .jobs()?
                    .into_iter()
                    .find(|j| j["id"] == args["id"])
                    .ok_or_else(|| error("job not found"))?;
                if job["last_status"] == "unknown" {
                    return Err(error(
                        "unknown execution requires explicit review; edit job to re-enable",
                    ));
                }
                job["enabled"] = json!(job["enabled"] != true);
                self.save_job(&job)?;
                Ok(job)
            }
            _ => Err(error("unsupported daemon action")),
        }
    }
    async fn scheduler(self: Arc<Self>) {
        let mut timer = tokio::time::interval(Duration::from_secs(1));
        loop {
            timer.tick().await;
            *self.last_tick.lock().unwrap() = now();
            let jobs = match self.jobs() {
                Ok(jobs) => jobs,
                Err(_) => continue,
            };
            for mut job in jobs {
                if job["enabled"] != true
                    || job["next_due"].as_i64().unwrap_or(i64::MAX) > now()
                    || ["running", "unknown"]
                        .iter()
                        .any(|s| job["last_status"] == *s)
                {
                    continue;
                }
                let kind = job["kind"].as_str().unwrap_or("prompt");
                if kind != "command" {
                    let target = self
                        .cache()
                        .into_iter()
                        .find(|a| a["node_id"] == job["node_id"] && a["id"] == job["agent_id"]);
                    if target
                        .as_ref()
                        .map(|a| {
                            a["stale"] == false
                                && a["status"] == "alive"
                                && (kind != "loop"
                                    || (a["state"] == "idle" && a["phase"] == "started"))
                        })
                        .unwrap_or(false)
                        == false
                    {
                        job["last_status"] = json!("deferred");
                        job["last_error"] = json!("target unavailable, stale, or busy");
                        let _ = self.save_job(&job);
                        continue;
                    }
                }
                let permit = match self.jobs_capacity.clone().try_acquire_owned() {
                    Ok(p) => p,
                    Err(_) => break,
                };
                let original = job.to_string();
                job["last_status"] = json!("running");
                job["last_started"] = json!(now());
                job["message_id"] = json!(id());
                {
                    let db = self.database.lock().unwrap();
                    if db
                        .execute(
                            "UPDATE jobs SET data=?2 WHERE id=?1 AND data=?3",
                            params![job["id"].as_str().unwrap_or(""), job.to_string(), original],
                        )
                        .unwrap_or(0)
                        != 1
                    {
                        continue;
                    }
                    let _=db.execute("INSERT INTO job_runs VALUES (?1,?2,?3)",params![job["message_id"].as_str().unwrap_or(""),job["id"].as_str().unwrap_or(""),json!({"message_id":job["message_id"],"started_at":job["last_started"],"status":"running"}).to_string()]);
                }
                let daemon = self.clone();
                tokio::spawn(async move {
                    let _permit = permit;
                    daemon.execute_job(job).await;
                });
            }
        }
    }
    async fn execute_job(&self, mut job: Value) {
        let outcome:Result<Value>=async{
            if job["kind"]=="command"{
                let argv:Vec<String>=serde_json::from_value(job["argv"].clone())?;
                let mut child=tokio::process::Command::new(&argv[0]);child.args(&argv[1..]).kill_on_drop(true).stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
                if let Some(cwd)=job["cwd"].as_str(){child.current_dir(cwd);}
                let status=tokio::time::timeout(Duration::from_secs(job["timeout"].as_u64().unwrap_or(60)),child.status()).await??;
                Ok(json!({"exit_code":status.code().unwrap_or(1)}))
            }else {
                let node=job["node_id"].as_str().unwrap_or("");let action=if job["kind"]=="loop"{"loop.deliver"}else{"agent.operation"};
                let answer=self.relay.request(node,action,json!({"operation":"agent.send","agent_id":job["agent_id"],"loop_id":job["id"],"options":{"text":job["prompt"]}}),job["message_id"].as_str().unwrap_or("")).await?;
                if answer["deferred"]==true{return Ok(json!({"deferred":true}));}
                let payload=if action=="loop.deliver"{&answer["result"]["payload"]}else{&answer["payload"]};Ok(payload.clone())
            }
        }.await;
        match outcome {
            Ok(result) if result["deferred"] == true => {
                job["last_status"] = json!("deferred");
                job["last_error"] = json!("owner Server deferred busy target");
            }
            Ok(result) => {
                job["last_status"] = json!(if result["status"] == "unknown" {
                    "unknown"
                } else if result["exit_code"] == 0 {
                    "ok"
                } else {
                    "error"
                });
                job["last_result"] = result;
                let every = job["every_seconds"].as_i64().unwrap_or(0);
                if every > 0 {
                    job["next_due"] = json!(now() + every);
                } else {
                    job["enabled"] = json!(false);
                }
                if job["last_status"] == "error" {
                    let attempts = job["attempts"].as_u64().unwrap_or(0) + 1;
                    job["attempts"] = json!(attempts);
                    if attempts >= 3 {
                        job["enabled"] = json!(false);
                    }
                }
            }
            Err(err) => {
                job["last_status"] = json!("unknown");
                job["last_error"] = json!(err.to_string());
            }
        }
        job["last_finished"] = json!(now());
        // A deleted job must not be resurrected by an in-flight worker.
        let db = self.database.lock().unwrap();
        let existing: Option<String> = db
            .query_row(
                "SELECT data FROM jobs WHERE id=?1",
                params![job["id"].as_str().unwrap_or("")],
                |r| r.get(0),
            )
            .ok();
        if let Some(existing) = existing.and_then(|s| serde_json::from_str::<Value>(&s).ok()) {
            if existing["message_id"] == job["message_id"] {
                if existing["enabled"] == false {
                    job["enabled"] = json!(false);
                }
                let _ = db.execute(
                    "UPDATE jobs SET data=?2 WHERE id=?1",
                    params![job["id"].as_str().unwrap_or(""), job.to_string()],
                );
            }
        }
        let _=db.execute("UPDATE job_runs SET data=?2 WHERE id=?1",params![job["message_id"].as_str().unwrap_or(""),json!({"message_id":job["message_id"],"started_at":job["last_started"],"finished_at":job["last_finished"],"status":job["last_status"],"result":job["last_result"],"error":job["last_error"]}).to_string()]);
    }
}
pub fn validate_job(mut job: Value) -> Result<Value> {
    if !job.is_object() {
        return Err(error("job must be an object"));
    }
    if job.get("id").is_none() {
        job["id"] = json!(id());
    }
    if !job["id"]
        .as_str()
        .map(cam_protocol::valid_id)
        .unwrap_or(false)
    {
        return Err(error("invalid job ID"));
    }
    let kind = job["kind"].as_str().unwrap_or("loop").to_string();
    if !["loop", "prompt", "command"].contains(&kind.as_str()) {
        return Err(error("invalid job kind"));
    }
    if kind == "command" {
        let argv: Vec<String> = serde_json::from_value(job["argv"].clone())?;
        if argv.is_empty() || argv[0].is_empty() {
            return Err(error("command argv required"));
        }
    } else {
        for field in ["node_id", "agent_id", "prompt"] {
            if job[field].as_str().map(str::is_empty).unwrap_or(true) {
                return Err(error("job target and prompt required"));
            }
        }
    }
    let every = job["every_seconds"].as_i64().unwrap_or(0);
    if every < 0 || (every == 0 && job["next_due"].as_i64().is_none()) {
        return Err(error(
            "use positive every_seconds or absolute next_due Unix time",
        ));
    }
    job["kind"] = json!(kind);
    if job.get("enabled").is_none() {
        job["enabled"] = json!(true);
    }
    if job.get("next_due").is_none() {
        job["next_due"] = json!(now() + every);
    }
    if job.get("last_status").is_none() {
        job["last_status"] = json!("scheduled");
    }
    Ok(job)
}
pub async fn serve(config: Config, path: PathBuf) -> Result<()> {
    serve_inner(config, path, false).await
}
#[cfg(feature = "test-harness")]
pub async fn serve_test_client(config: Config, path: PathBuf) -> Result<()> {
    serve_inner(config, path, true).await
}
async fn serve_inner(config: Config, path: PathBuf, isolated_test: bool) -> Result<()> {
    prepare_runtime(&config.runtime_dir)?;
    let lockpath = if isolated_test { config.runtime_dir.join("client.lock") }
        else { PathBuf::from(format!("/tmp/cam-client-{}.lock", uid())) };
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(lockpath)?;
    if lock.metadata()?.uid() != uid() || lock.metadata()?.mode() & 0o077 != 0 {
        return Err(error(
            "Client daemon lock must be private and owned by this user",
        ));
    }
    lock.try_lock_exclusive()
        .map_err(|_| error("local Client daemon already running"))?;
    let socket = config.runtime_dir.join("client.sock");
    if socket.exists() {
        fs::remove_file(&socket)?;
    }
    let listener = UnixListener::bind(&socket)?;
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
    let daemon = Daemon::new(config, path)?;
    let output = {
        let d = daemon.clone();
        tokio::spawn(d.outputs())
    };
    let poll = {
        let d = daemon.clone();
        tokio::spawn(d.poll())
    };
    let scheduler = {
        let d = daemon.clone();
        tokio::spawn(d.scheduler())
    };
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    loop {
        tokio::select! {
            accepted=listener.accept()=>{let (stream,_)=accepted?;let d=daemon.clone();tokio::spawn(async move{let _=d.connection(stream).await;});},
            _=daemon.stop.notified()=>break,
            _=tokio::signal::ctrl_c()=>break,
            _=terminate.recv()=>break,
        }
    }
    output.abort();
    poll.abort();
    scheduler.abort();
    daemon.relay.shutdown().await;
    let _ = fs::remove_file(socket);
    drop(lock);
    Ok(())
}
