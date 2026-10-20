use crate::{
    backend::Backend,
    config::{self, Config, Instance},
    store::Store,
};
use cam_protocol::{
    encode, error, expiry, id, now, read_frame, result, stamp, text, validate, write_frame, Result,
};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    fs,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncWrite, AsyncWriteExt, BufReader},
    net::{UnixListener, UnixStream},
    sync::{mpsc, oneshot, Notify, OwnedSemaphorePermit, Semaphore},
};

pub struct Queued {
    pub raw: Vec<u8>,
    failure: Option<(Channel, Value)>,
    _permit: OwnedSemaphorePermit,
}
impl Queued {
    fn failed(&self, status: &str) {
        if let Some((ingress, report)) = &self.failure {
            let mut report = report.clone();
            report["status"] = json!(status);
            let _ = ingress.frame(&report);
        }
    }
}
#[derive(Clone)]
pub struct Channel {
    tx: mpsc::Sender<Queued>,
    budget: Arc<Semaphore>,
}
impl Channel {
    pub fn start<W: AsyncWrite + Send + Unpin + 'static>(
        mut writer: W,
        budget: Arc<Semaphore>,
    ) -> Self {
        let (tx, mut rx) = mpsc::channel::<Queued>(32);
        tokio::spawn(async move {
            while let Some(frame) = rx.recv().await {
                let delivered = tokio::time::timeout(Duration::from_secs(10), async {
                    writer.write_all(&frame.raw).await?;
                    writer.flush().await
                })
                .await;
                if !matches!(delivered, Ok(Ok(()))) {
                    frame.failed("unknown");
                    rx.close();
                    while let Some(queued) = rx.recv().await {
                        queued.failed("not_forwarded");
                    }
                    break;
                }
            }
        });
        Self { tx, budget }
    }
    pub fn send(&self, raw: Vec<u8>) -> Result<()> {
        self.enqueue(raw, None)
    }
    pub fn forward(&self, raw: Vec<u8>, ingress: Channel, mid: &Value, target: &str) -> Result<()> {
        self.enqueue(
            raw,
            Some((
                ingress,
                json!({"type":"forward.error","message_id":mid,"target_node":target}),
            )),
        )
    }
    fn enqueue(&self, raw: Vec<u8>, failure: Option<(Channel, Value)>) -> Result<()> {
        let permit = self
            .budget
            .clone()
            .try_acquire_many_owned(raw.len() as u32)
            .map_err(|_| error("CHANNEL_BACKPRESSURE"))?;
        self.tx
            .try_send(Queued {
                raw,
                failure,
                _permit: permit,
            })
            .map_err(|_| error("CHANNEL_BACKPRESSURE or disconnected"))?;
        Ok(())
    }
    pub fn frame(&self, value: &Value) -> Result<()> {
        self.send(encode(value)?)
    }
    pub fn closed(&self) -> bool {
        self.tx.is_closed()
    }
}
struct Client {
    generation: String,
    channel: Channel,
    routes: HashSet<String>,
    expires: Instant,
    seq: u64,
}
struct Query {
    client: String,
    generation: String,
    sender: oneshot::Sender<Value>,
}
pub struct Server {
    pub config: Config,
    pub epoch: String,
    store: Store,
    clients: Mutex<HashMap<String, Client>>,
    queries: Mutex<HashMap<String, Query>>,
    backend: Option<Arc<Backend>>,
    notify: Notify,
    executing: Arc<Semaphore>,
    budget: Arc<Semaphore>,
    connections: Arc<Semaphore>,
    terminals: Arc<crate::terminal::Terminals>,
}
impl Server {
    pub async fn new(config: Config, epoch: String) -> Result<Arc<Self>> {
        let store = Store::open(
            &config.runtime_dir.join("router.db"),
            config.max_messages,
            config.max_bytes,
        )?;
        store.recover()?;
        let backend = match &config.backend {
            Some(b) => Some(Backend::start(b, &config).await?),
            None => None,
        };
        Ok(Arc::new(Self {
            budget: Arc::new(Semaphore::new(config.queue_bytes)),
            connections: Arc::new(Semaphore::new(config.max_connections)),
            config,
            epoch,
            store,
            clients: Mutex::new(HashMap::new()),
            queries: Mutex::new(HashMap::new()),
            backend,
            notify: Notify::new(),
            executing: Arc::new(Semaphore::new(8)),
            terminals: Arc::new(crate::terminal::Terminals::default()),
        }))
    }
    fn validate_local(&self, message: &Value) -> Result<()> {
        validate(message)?;
        if message["source"]["node_id"] != self.config.node_id {
            return Err(error("local source Node mismatch"));
        }
        if !self.config.permitted(
            text(&message["target"], "node_id")?,
            text(message, "operation")?,
        ) {
            return Err(error("target Node/operation is not authorized"));
        }
        Ok(())
    }
    fn publish(&self, message: &Value, raw: &[u8]) -> Result<()> {
        self.validate_local(message)?;
        self.store.save(message, raw, "out")?;
        self.notify.notify_one();
        Ok(())
    }
    fn correlate(&self, message: &Value, raw: &[u8], record: bool) -> Result<()> {
        let mid = match message["reply_to"].as_str() {
            Some(mid) => mid,
            None => return Ok(()),
        };
        let original = match self.store.original(&self.config.node_id, mid)? {
            Some(v) => v,
            None => {
                if message["operation"] == "operation.result"
                    || message["operation"] == "operation.delivery"
                {
                    return Err(error("unknown result correlation"));
                }
                return Ok(());
            }
        };
        if message["source"]["node_id"] != original["target"]["node_id"]
            || message["source"]["agent_id"] != original["target"]["agent_id"]
        {
            return Err(error("result source mismatch"));
        }
        if message["operation"] == "operation.delivery" {
            if message["payload"]["status"] != "accepted" {
                return Err(error("invalid delivery receipt"));
            }
            if record {
                self.store.received(&self.config.node_id, mid)?;
            }
        } else if message["operation"] == "operation.result"
            || (message["operation"] == "message.send" && original["operation"] == "message.send")
        {
            if message["operation"] == "message.send"
                && message["target"]["agent_id"] != original["source"]["agent_id"]
            {
                return Err(error("reply agent mismatch"));
            }
            if record {
                self.store.record_result(mid, message, raw)?;
                self.store.received(&self.config.node_id, mid)?;
            }
        }
        if record {
            self.notify.notify_waiters();
        }
        Ok(())
    }
    fn accept(&self, message: &Value, raw: &[u8]) -> Result<()> {
        validate(message)?;
        if message["target"]["node_id"] != self.config.node_id {
            return Err(error("recursive/intermediate relay is forbidden"));
        }
        // Verify correlation before storing an untrusted result.
        self.correlate(message, raw, false)?;
        let fresh = self.store.save(message, raw, "in")?;
        self.correlate(message, raw, true)?;
        if !fresh {
            self.store.repeat_answers(message, &self.config.node_id)?;
        }
        if fresh
            && message["operation"] != "operation.delivery"
            && message["source"]["node_id"] != self.config.node_id
        {
            let receipt = json!({"version":1,"type":"message","message_id":id(),"reply_to":message["message_id"],
                "source":message["target"],"target":{"node_id":message["source"]["node_id"],"receiver_id":"delivery.status"},
                "operation":"operation.delivery","payload":{"status":"accepted"},"expires_at":stamp(now()+86400)});
            self.store.receipt(message, &receipt)?;
        }
        self.notify.notify_one();
        Ok(())
    }
    fn routes(&self, target: &str, last: &str) -> Vec<(String, Channel)> {
        let mut entries: Vec<_> = self
            .clients
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, c)| {
                c.expires > Instant::now() && c.routes.contains(target) && !c.channel.closed()
            })
            .map(|(name, c)| (name.clone(), c.channel.clone()))
            .collect();
        entries.sort_by_key(|(name, _)| (name == last, name.clone()));
        entries
    }
    async fn pump(self: &Arc<Self>) -> Result<()> {
        for item in self.store.due("out")? {
            if item.message["target"]["node_id"] == self.config.node_id {
                self.accept(&item.message, &item.raw)?;
                self.store
                    .mark(&item.message, "out", "received", 0, "", "")?;
                continue;
            }
            let target = text(&item.message["target"], "node_id")?;
            let mut sent = false;
            for (client, channel) in self.routes(target, &item.last_client) {
                if channel.send(item.raw.clone()).is_ok() {
                    let state = if item.message["operation"] == "operation.delivery" {
                        "done"
                    } else {
                        "sent"
                    };
                    self.store.mark(
                        &item.message,
                        "out",
                        state,
                        ((self.config.retry_ms + 999) / 1000) as i64,
                        &client,
                        "unknown: delivery not yet confirmed",
                    )?;
                    sent = true;
                    break;
                }
            }
            if !sent {
                self.store.mark(
                    &item.message,
                    "out",
                    "queued",
                    1,
                    "",
                    "NO_ROUTE or BACKPRESSURE",
                )?;
            }
        }
        for item in self.store.due("in")? {
            let op = text(&item.message, "operation")?.to_string();
            if op == "operation.result" || op == "operation.delivery" {
                self.store.mark(&item.message, "in", "done", 0, "", "")?;
                continue;
            }
            if op == "test.echo" && self.config.echo {
                self.store.echo(&item.message)?;
                self.notify.notify_one();
                continue;
            }
            let permit = match self.executing.clone().try_acquire_owned() {
                Ok(p) => p,
                Err(_) => break,
            };
            self.store
                .mark(&item.message, "in", "executing", 0, "", "")?;
            let server = self.clone();
            tokio::spawn(async move {
                let _permit = permit;
                let answer = match &server.backend {
                    Some(backend) if cam_protocol::AGENT_OPERATIONS.contains(&op.as_str()) => {
                        match backend.execute(item.message.clone()).await {
                            Ok(v) => v,
                            Err(_) => result(
                                &item.message,
                                "",
                                "native backend unavailable; outcome unknown\n",
                                2,
                                "unknown",
                            ),
                        }
                    }
                    _ => result(
                        &item.message,
                        "",
                        "UNSUPPORTED_OPERATION: local receiver is not registered\n",
                        2,
                        "error",
                    ),
                };
                let valid = validate(&answer).is_ok()
                    && answer["operation"] == "operation.result"
                    && answer["source"] == item.message["target"]
                    && answer["target"]["node_id"] == item.message["source"]["node_id"]
                    && answer["reply_to"] == item.message["message_id"];
                let answer = if valid {
                    answer
                } else {
                    result(
                        &item.message,
                        "",
                        "invalid native result; outcome unknown\n",
                        2,
                        "unknown",
                    )
                };
                if server.store.finish(&item.message, &answer).is_ok() {
                    server.notify.notify_one();
                } else {
                    eprintln!(
                        "cam-server: local result persistence failed message_id={}",
                        item.message["message_id"].as_str().unwrap_or("invalid")
                    );
                }
            });
        }
        Ok(())
    }
    async fn query_client(
        self: &Arc<Self>,
        client: String,
        generation: String,
        channel: Channel,
        node: Option<String>,
        agent: String,
    ) -> Vec<Value> {
        let qid = id();
        let (sender, receiver) = oneshot::channel();
        self.queries.lock().unwrap().insert(
            qid.clone(),
            Query {
                client,
                generation,
                sender,
            },
        );
        let query = json!({"type":"resolve.query","id":qid,"node":node,"agent":agent});
        let answer = if channel.frame(&query).is_ok() {
            tokio::time::timeout(Duration::from_secs(2), receiver)
                .await
                .ok()
                .and_then(|r| r.ok())
        } else {
            None
        };
        self.queries.lock().unwrap().remove(&qid);
        answer
            .and_then(|v| v["matches"].as_array().cloned())
            .unwrap_or_default()
    }
    async fn resolve(self: &Arc<Self>, frame: &Value) -> Result<Value> {
        let node = frame["node"]
            .as_str()
            .map(|n| self.config.canonical_node(n))
            .transpose()?;
        let agent = frame["agent"].as_str();
        if agent.is_none() {
            return Ok(
                json!({"type":"resolved","node_id":node.ok_or_else(||error("run requires a Node"))?,"agent_id":null}),
            );
        }
        let agent = agent.unwrap();
        if let Some(node) = &node {
            if agent.len() == 8 && agent.bytes().all(|c| c.is_ascii_hexdigit()) {
                return Ok(json!({"type":"resolved","node_id":node,"agent_id":agent}));
            }
        }
        let clients: Vec<_> = self
            .clients
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, c)| !c.channel.closed())
            .map(|(name, c)| (name.clone(), c.generation.clone(), c.channel.clone()))
            .collect();
        let mut queries = tokio::task::JoinSet::new();
        for (name, generation, channel) in clients {
            let server = self.clone();
            let node = node.clone();
            let agent = agent.to_string();
            queries.spawn(async move {
                server
                    .query_client(name, generation, channel, node, agent)
                    .await
            });
        }
        let mut matches = HashSet::new();
        while let Some(answer) = queries.join_next().await {
            if let Ok(values) = answer {
                for value in values {
                    if let (Some(n), Some(a)) =
                        (value["node_id"].as_str(), value["agent_id"].as_str())
                    {
                        if self.config.canonical_node(n).is_ok()
                            && cam_protocol::agent_id(a)
                            && node.as_ref().map(|wanted| wanted == n).unwrap_or(true)
                        {
                            matches.insert((n.to_string(), a.to_string()));
                        }
                    }
                }
            }
        }
        if matches.len() != 1 {
            return Err(error("TARGET_UNRESOLVED or ambiguous agent"));
        }
        let (node, agent) = matches.into_iter().next().unwrap();
        Ok(json!({"type":"resolved","node_id":node,"agent_id":agent}))
    }
    pub fn inspect(&self) -> Result<Value> {
        let clients:Vec<_>=self.clients.lock().unwrap().iter().map(|(name,c)|json!({"client_id":name,"session_id":c.generation,"ready":c.expires>Instant::now()&&!c.channel.closed(),"reachable":c.routes})).collect();
        Ok(
            json!({"type":"status","version":1,"node_id":self.config.node_id,"server_epoch":self.epoch,"clients":clients,"storage":self.store.snapshot()?,"backend":self.backend.is_some()}),
        )
    }
    async fn connection(self: Arc<Self>, stream: UnixStream) -> Result<()> {
        let _slot = self
            .connections
            .clone()
            .try_acquire_owned()
            .map_err(|_| error("connection capacity exceeded"))?;
        if stream.peer_cred()?.uid() != config::uid() {
            return Err(error("foreign local UID"));
        }
        let (read, mut write) = stream.into_split();
        let mut reader = BufReader::new(read);
        let (hello, _) =
            tokio::time::timeout(Duration::from_secs(5), read_frame(&mut reader)).await??;
        if hello["type"] != "hello" || hello["version"] != 1 {
            return Err(error("incompatible hello"));
        }
        if hello["role"] == "camc" {
            write_frame(&mut write,&json!({"type":"hello","version":1,"node_id":self.config.node_id,"server_epoch":self.epoch})).await?;
            loop {
                let (frame, raw) = read_frame(&mut reader).await?;
                let outcome: Result<Value> = match frame["type"].as_str() {
                    Some("resolve") => self.resolve(&frame).await,
                    Some("inspect") => self.inspect(),
                    Some("query") => {
                        let mid = text(&frame, "message_id")?;
                        let mut value = self.store.query(&self.config.node_id, mid)?;
                        value["results"] = json!(self
                            .store
                            .results(mid, false)?
                            .into_iter()
                            .map(|r| serde_json::from_slice::<Value>(&r))
                            .collect::<std::result::Result<Vec<_>, _>>()?);
                        Ok(value)
                    }
                    Some("wait") => {
                        let mid = text(&frame, "message_id")?;
                        if self.store.original(&self.config.node_id, mid)?.is_none() {
                            return Err(error("unknown local message"));
                        }
                        let timeout = frame["timeout"]
                            .as_u64()
                            .filter(|v| *v > 0 && *v <= 86400)
                            .ok_or_else(|| error("invalid wait timeout"))?;
                        let deadline = Instant::now() + Duration::from_secs(timeout);
                        loop {
                            if let Some(raw) = self.store.results(mid, true)?.first() {
                                write.write_all(raw).await?;
                                return Ok(());
                            }
                            if Instant::now() >= deadline {
                                return Err(error("reply timed out; original was not resubmitted"));
                            }
                            tokio::select! {_=self.notify.notified()=>{},_=tokio::time::sleep(Duration::from_millis(100))=>{}}
                        }
                    }
                    Some("publish") => {
                        let message = &frame["message"];
                        let bytes = encode(message)?;
                        self.publish(message, &bytes)
                            .map(|_| json!({"type":"accepted","message_id":message["message_id"]}))
                    }
                    Some("message") => {
                        if let Err(err) = self.publish(&frame, &raw) {
                            write_frame(
                                &mut write,
                                &json!({"type":"error","error":public_error(&err.to_string())}),
                            )
                            .await?;
                            return Ok(());
                        }
                        let mid = text(&frame, "message_id")?;
                        let wait = frame["operation"] == "message.send"
                            && frame["payload"]["expect_response"] == true;
                        loop {
                            let notified = self.notify.notified();
                            let answers = self.store.results(mid, wait)?;
                            if let Some(answer) = answers.first() {
                                write.write_all(answer).await?;
                                return Ok(());
                            }
                            if expiry(&frame)? <= now() {
                                write_frame(&mut write,&json!({"type":"error","error":"result timed out; delivery outcome may be unknown","message_id":mid})).await?;
                                return Ok(());
                            }
                            tokio::select! {_=notified=>{},_=tokio::time::sleep(Duration::from_millis(100))=>{}}
                        }
                    }
                    _ => Err(error("unsupported local control frame")),
                };
                let value = match outcome {
                    Ok(v) => v,
                    Err(err) => json!({"type":"error","error":public_error(&err.to_string())}),
                };
                write_frame(&mut write, &value).await?;
            }
        }
        if hello["role"] != "client" {
            return Err(error("unknown role"));
        }
        let name = text(&hello, "client_id")?.to_string();
        let token = text(&hello, "token")?;
        let grant = self
            .config
            .clients
            .iter()
            .find(|c| c.client_id == name)
            .ok_or_else(|| error("unauthorized Client"))?
            .clone();
        let expected = config::private_read(&grant.token_file)?;
        if !config::constant_time_eq(
            token.trim().as_bytes(),
            std::str::from_utf8(&expected)?.trim().as_bytes(),
        ) {
            return Err(error("unauthorized Client"));
        }
        let generation = id();
        write_frame(&mut write,&json!({"type":"hello","version":1,"node_id":self.config.node_id,"server_epoch":self.epoch,"session_id":generation,
            "management":grant.management,"capabilities":if grant.management&&self.backend.is_some(){vec!["agents.snapshot","agent.update","agent.operation","terminal.control","loop.deliver","health"]}else{vec!["health"]},
            "route_ttl_seconds":self.config.route_ttl_seconds,"allowed_targets":self.config.peers.iter().map(|p|p.node_id.clone()).collect::<Vec<_>>()})).await?;
        let channel = Channel::start(write, self.budget.clone());
        self.clients.lock().unwrap().insert(
            name.clone(),
            Client {
                generation: generation.clone(),
                channel: channel.clone(),
                routes: HashSet::new(),
                expires: Instant::now(),
                seq: 0,
            },
        );
        self.notify.notify_one();
        let outcome: Result<()> = async {
            loop {
                let (frame, raw) = tokio::time::timeout(
                    Duration::from_secs(self.config.route_ttl_seconds * 2),
                    read_frame(&mut reader),
                )
                .await??;
                // Old generations cannot mutate a replacement connection.
                if !self
                    .clients
                    .lock()
                    .unwrap()
                    .get(&name)
                    .map(|c| c.generation == generation)
                    .unwrap_or(false)
                {
                    return Err(error("superseded Client connection"));
                }
                match frame["type"].as_str() {
                    Some("sdk.request") => {
                        let request_id=text(&frame,"id")?.to_string();
                        let server=self.clone();let output=channel.clone();let owner=name.clone();let connection=generation.clone();
                        let management=grant.management;
                        let permit=self.executing.clone().try_acquire_owned();
                        if let Ok(permit)=permit {
                            tokio::spawn(async move {
                                let _permit=permit;
                                let outcome=server.client_request(&owner,&connection,management,&frame,output.clone()).await;
                                let answer=match outcome {Ok(data)=>json!({"type":"sdk.result","id":request_id,"data":data}),Err(err)=>json!({"type":"sdk.result","id":request_id,"error":err.to_string()})};
                                let _=output.frame(&answer);
                            });
                        }else{channel.frame(&json!({"type":"sdk.result","id":request_id,"error":"management backpressure"}))?;}
                    }
                    Some("routes.snapshot") => {
                        let seq = frame["seq"]
                            .as_u64()
                            .ok_or_else(|| error("invalid route sequence"))?;
                        let ttl = frame["ttl_seconds"]
                            .as_u64()
                            .ok_or_else(|| error("invalid route ttl"))?;
                        if frame["session_id"] != generation
                            || ttl == 0
                            || ttl > self.config.route_ttl_seconds
                        {
                            return Err(error("invalid route epoch/ttl"));
                        }
                        let values = frame["reachable"]
                            .as_array()
                            .ok_or_else(|| error("missing route snapshot"))?;
                        if values.len() > 128 {
                            return Err(error("route capacity exceeded"));
                        }
                        let mut routes = HashSet::new();
                        for value in values {
                            let node = value.as_str().ok_or_else(|| error("invalid route Node"))?;
                            if !self.config.peers.iter().any(|p| p.node_id == node) {
                                return Err(error("unauthorized route"));
                            }
                            routes.insert(node.to_string());
                        }
                        {
                            let mut clients = self.clients.lock().unwrap();
                            let client = clients.get_mut(&name).unwrap();
                            if seq <= client.seq {
                                continue;
                            }
                            client.seq = seq;
                            client.routes = routes;
                            client.expires = Instant::now() + Duration::from_secs(ttl);
                        }
                        self.notify.notify_one();
                    }
                    Some("resolve.answer") => {
                        if let Some(qid) = frame["id"].as_str() {
                            let query = {
                                let mut queries = self.queries.lock().unwrap();
                                if queries
                                    .get(qid)
                                    .map(|q| q.client == name && q.generation == generation)
                                    .unwrap_or(false)
                                {
                                    queries.remove(qid)
                                } else {
                                    None
                                }
                            };
                            if let Some(query) = query {
                                let _ = query.sender.send(frame);
                            }
                        }
                    }
                    Some("forward.error") => {
                        let mid = text(&frame, "message_id")?;
                        let status = text(&frame, "status")?;
                        if !["not_forwarded", "unknown"].contains(&status) {
                            return Err(error("invalid forwarding status"));
                        }
                        let original = self
                            .store
                            .original(&self.config.node_id, mid)?
                            .ok_or_else(|| error("unknown forwarding message"))?;
                        if original["target"]["node_id"] != frame["target_node"] {
                            return Err(error("forwarding destination mismatch"));
                        }
                        self.store
                            .forward_error(&self.config.node_id, mid, &name, status)?;
                        self.notify.notify_one();
                    }
                    Some("message") => {
                        let source = text(&frame["source"], "node_id")?;
                        if source == self.config.node_id
                            || !grant.source_nodes.iter().any(|n| n == source)
                            || !self.config.permitted(source, text(&frame, "operation")?)
                        {
                            return Err(error("unauthorized message source/operation"));
                        }
                        self.accept(&frame, &raw)?;
                    }
                    Some("ping") => channel.frame(&json!({"type":"pong"}))?,
                    _ => return Err(error("unsupported forwarding frame")),
                }
            }
        }
        .await;
        self.terminals.disconnect(&generation);
        {
            let mut clients = self.clients.lock().unwrap();
            if clients
                .get(&name)
                .map(|c| c.generation == generation)
                .unwrap_or(false)
            {
                clients.remove(&name);
            }
        }
        self.notify.notify_one();
        outcome
    }

    async fn client_request(
        &self,
        client: &str,
        generation: &str,
        management: bool,
        frame: &Value,
        channel: Channel,
    ) -> Result<Value> {
        let action = text(frame, "action")?;
        if action == "health" {
            return self.inspect();
        }
        if !management {
            return Err(error("Client management capability is not granted"));
        }
        let backend = self
            .backend
            .as_ref()
            .ok_or_else(|| error("UNSUPPORTED_OPERATION: no native backend"))?;
        let mut args = frame.get("args").cloned().unwrap_or_else(|| json!({}));
        match action {
            "agents.snapshot" => backend.control(action, json!({})).await,
            "agent.update" => backend.control(action, args).await,
            "terminal.attach" => {
                self.terminals
                    .open(backend, generation, &args, channel)
                    .await
            }
            "terminal.input" | "terminal.resize" | "terminal.detach" => {
                self.terminals.command(generation, action, &args)
            }
            "agent.operation" | "loop.deliver" => {
                let operation = if action == "loop.deliver" {
                    "agent.send"
                } else {
                    text(&args, "operation")?
                };
                if !cam_protocol::AGENT_OPERATIONS.contains(&operation) {
                    return Err(error("UNSUPPORTED_OPERATION"));
                }
                let agent = text(&args, "agent_id")?;
                if !cam_protocol::agent_id(agent) {
                    return Err(error("specific agent ID required"));
                }
                let mid = text(frame, "id")?;
                if !cam_protocol::valid_id(mid) {
                    return Err(error("invalid operation ID"));
                }
                let message = json!({"type":"message","version":1,"message_id":mid,
                    "source":{"node_id":format!("client.{}",config::node_key(client))},"target":{"node_id":self.config.node_id,"agent_id":agent},
                    "operation":operation,"payload":{"args":args.get("options").cloned().unwrap_or_else(||json!({}))},"expires_at":stamp(now()+120)});
                if action == "loop.deliver" {
                    args["message"] = message;
                    args["owner"] = json!(client);
                    backend.control(action, args).await
                } else {
                    backend
                        .control("agent.operation", json!({"message":message}))
                        .await
                }
            }
            _ => Err(error("UNSUPPORTED_MANAGEMENT_ACTION")),
        }
    }
}

fn public_error(reason: &str) -> &'static str {
    match reason {
        "QUEUE_FULL" => "QUEUE_FULL: message was not admitted",
        "MESSAGE_CONFLICT" => "MESSAGE_CONFLICT: same ID has different content",
        "TARGET_UNRESOLVED or ambiguous agent" | "TARGET_UNRESOLVED or ambiguous Node" => {
            "TARGET_UNRESOLVED or ambiguous target"
        }
        _ => "request rejected or unresolved",
    }
}

pub async fn serve(config: Config) -> Result<()> {
    let instance = Instance::acquire(&config)?;
    serve_instance(config, instance).await
}
#[cfg(feature = "test-harness")]
pub async fn serve_test_node(config: Config) -> Result<()> {
    let instance = Instance::acquire_test_node(&config)?;
    serve_instance(config, instance).await
}
async fn serve_instance(config: Config, instance: Instance) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let listener = UnixListener::bind(&instance.socket)?;
    fs::set_permissions(&instance.socket, fs::Permissions::from_mode(0o600))?;
    let server = Server::new(config.clone(), instance.epoch.clone()).await?;
    let card = if config.discover_camc {
        Some(
            config::home()?
                .join(".cam/server")
                .join(format!("endpoint-{}.json", config::hostname()?)),
        )
    } else {
        None
    };
    if let Some(card) = &card {
        config::private_write(
            card,
            &encode(
                &json!({"socket":instance.socket,"node_id":config.node_id,"server_epoch":instance.epoch}),
            )?,
        )?;
    }
    eprintln!(
        "cam-server: ready node={} socket={}",
        config.node_id,
        instance.socket.display()
    );
    let running = server.clone();
    let pump = tokio::spawn(async move {
        let mut last_error = None;
        loop {
            if running.pump().await.is_err()
                && last_error
                    .map(|t: Instant| t.elapsed() >= Duration::from_secs(30))
                    .unwrap_or(true)
            {
                eprintln!("cam-server: queue processing failed; retained for recovery");
                last_error = Some(Instant::now());
            }
            tokio::select! {_=running.notify.notified()=>{},_=tokio::time::sleep(Duration::from_millis(100))=>{}}
        }
    });
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    loop {
        tokio::select! {
            accepted=listener.accept()=>{let (stream,_)=accepted?;let server=server.clone();tokio::spawn(async move {let _=server.connection(stream).await;});},
            _=tokio::signal::ctrl_c()=>break,
            _=terminate.recv()=>break,
        }
    }
    pump.abort();
    if let Some(card) = card {
        let _ = fs::remove_file(card);
    }
    drop(server);
    drop(instance);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io,
        pin::Pin,
        task::{Context, Poll},
    };
    struct PartialWriter(bool);
    impl AsyncWrite for PartialWriter {
        fn poll_write(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
            data: &[u8],
        ) -> Poll<io::Result<usize>> {
            if self.0 {
                Poll::Ready(Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "test transport failed",
                )))
            } else {
                self.0 = true;
                Poll::Ready(Ok(data.len().min(12)))
            }
        }
        fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
        fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }
    #[tokio::test]
    async fn partial_write_reports_unknown_and_unwritten_queue_reports_not_forwarded() {
        let budget = Arc::new(Semaphore::new(cam_protocol::FRAME_LIMIT));
        let (writer, reader) = tokio::io::duplex(4096);
        let ingress = Channel::start(writer, budget.clone());
        let destination = Channel::start(PartialWriter(false), budget);
        destination
            .forward(vec![b'x'; 100], ingress.clone(), &json!("first"), "node-b")
            .unwrap();
        destination
            .forward(vec![b'y'; 100], ingress, &json!("second"), "node-b")
            .unwrap();
        let mut reader = BufReader::new(reader);
        let (first, _) = read_frame(&mut reader).await.unwrap();
        let (second, _) = read_frame(&mut reader).await.unwrap();
        assert_eq!(
            first,
            json!({"type":"forward.error","message_id":"first","target_node":"node-b","status":"unknown"})
        );
        assert_eq!(second["message_id"], "second");
        assert_eq!(second["status"], "not_forwarded");
        assert!(destination.closed());
    }
}
