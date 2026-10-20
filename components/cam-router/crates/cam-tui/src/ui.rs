use crate::{
    config::Config,
    ipc,
    terminal::{self, Terminal as Model, View},
};
use base64::{engine::general_purpose::STANDARD, Engine};
use cam_protocol::{error, id, Result};
use crossterm::{
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event, KeyCode, KeyEventKind, KeyModifiers, MouseButton, MouseEventKind,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::Rect,
    style::{Color, Modifier, Style},
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
    Frame, Terminal,
};
use serde_json::{json, Value};
use std::{
    io,
    path::PathBuf,
    sync::mpsc,
    time::{Duration, Instant},
};
use unicode_width::UnicodeWidthStr;

const BG: Color = Color::Rgb(17, 20, 27);
const PANEL: Color = Color::Rgb(27, 31, 41);
const ACCENT: Color = Color::Rgb(115, 211, 190);
struct Tab {
    key: String,
    agent: Value,
    tid: Option<String>,
    model: Model,
    cursor: u64,
    initial: bool,
    pending: bool,
    error: Option<String>,
    geometry: (u16, u16),
    read_only: bool,
    attach_generation: String,
    retry_after: Instant,
}
enum Dialog {
    Nodes,
    Settings,
    Menu(Value),
    Form {
        kind: String,
        title: String,
        fields: Vec<(String, String)>,
        focus: usize,
    },
    Info {
        title: String,
        text: String,
        scroll: u16,
    },
}
type Reply = (String, std::result::Result<Value, String>);
pub struct App {
    socket: PathBuf,
    handle: tokio::runtime::Handle,
    tx: mpsc::Sender<Reply>,
    rx: mpsc::Receiver<Reply>,
    input_tx: tokio::sync::mpsc::Sender<Value>,
    snapshot: Value,
    tabs: Vec<Tab>,
    active: usize,
    filter: String,
    filter_focus: bool,
    list_offset: usize,
    dialog: Option<Dialog>,
    hits: Vec<(Rect, String)>,
    main: Rect,
    status: String,
    node: String,
    job: String,
    snapshot_pending: bool,
    daemon_connected: bool,
    last_snapshot: Instant,
    mouse_passthrough: bool,
    history: usize,
    view_id: String,
    editing_job: Option<Value>,
    pending_open: Option<(String, String)>,
}
impl App {
    pub fn new(config: &Config, handle: tokio::runtime::Handle) -> Self {
        let (tx, rx) = mpsc::channel();
        // Preserve keystroke order even when IPC replies have different
        // latency. Independent spawned requests can reorder typed commands.
        let (input_tx, mut input_rx) = tokio::sync::mpsc::channel::<Value>(256);
        let input_socket = config.runtime_dir.join("client.sock");
        let input_replies = tx.clone();
        handle.spawn(async move {
            let mut pending = None;
            loop {
                let mut args = match pending.take() {
                    Some(args) => args,
                    None => match input_rx.recv().await {
                        Some(args) => args,
                        None => break,
                    },
                };
                let mut bytes = STANDARD
                    .decode(args["data"].as_str().unwrap_or(""))
                    .unwrap_or_default();
                // Batch keys accumulated during a remote round trip, keeping
                // tab boundaries and the Server's input limit intact.
                while let Ok(next) = input_rx.try_recv() {
                    let more = STANDARD
                        .decode(next["data"].as_str().unwrap_or(""))
                        .unwrap_or_default();
                    if next["terminal_id"] != args["terminal_id"]
                        || next["view_id"] != args["view_id"]
                        || bytes.len() + more.len() > 65536
                    {
                        pending = Some(next);
                        break;
                    }
                    bytes.extend(more);
                }
                args["data"] = json!(STANDARD.encode(bytes));
                let result = ipc::request(&input_socket, "terminal.input", args)
                    .await
                    .map_err(|e| e.to_string());
                let _ = input_replies.send(("input".into(), result));
            }
        });
        Self {
            socket: config.runtime_dir.join("client.sock"),
            handle,
            tx,
            rx,
            input_tx,
            snapshot: json!({"agents":[],"nodes":[],"jobs":[]}),
            tabs: vec![],
            active: 0,
            filter: String::new(),
            filter_focus: false,
            list_offset: 0,
            dialog: None,
            hits: vec![],
            main: Rect::default(),
            status: "Connecting to local Client daemon…".into(),
            node: String::new(),
            job: String::new(),
            snapshot_pending: false,
            daemon_connected: false,
            last_snapshot: Instant::now() - Duration::from_secs(2),
            mouse_passthrough: false,
            history: config.scrollback_lines,
            view_id: id(),
            editing_job: None,
            pending_open: None,
        }
    }
    fn request(&self, kind: String, action: &str, mut args: Value) {
        if action.starts_with("terminal.") {
            args["view_id"] = json!(self.view_id);
        }
        if action == "terminal.input" {
            if let Err(err) = self.input_tx.try_send(args) {
                let _ = self.tx.send((
                    kind,
                    Err(format!("terminal input queue unavailable: {err}")),
                ));
            }
            return;
        }
        let socket = self.socket.clone();
        let action = action.to_string();
        let tx = self.tx.clone();
        self.handle.spawn(async move {
            let result = ipc::request(&socket, &action, args)
                .await
                .map_err(|e| e.to_string());
            let _ = tx.send((kind, result));
        });
    }
    fn agents(&self) -> Vec<Value> {
        let filter = self.filter.to_lowercase();
        self.snapshot["agents"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|a| {
                filter.is_empty()
                    || ["name", "id", "node_id", "state", "status", "tool"]
                        .iter()
                        .any(|k| {
                            a[*k]
                                .as_str()
                                .unwrap_or("")
                                .to_lowercase()
                                .contains(&filter)
                        })
            })
            .cloned()
            .collect()
    }
    fn active_tab(&mut self) -> Option<&mut Tab> {
        self.tabs.get_mut(self.active)
    }
    fn open(&mut self, agent: Value) {
        let key = format!(
            "{}:{}",
            agent["node_id"].as_str().unwrap_or(""),
            agent["id"].as_str().unwrap_or("")
        );
        if let Some(index) = self.tabs.iter().position(|t| t.key == key) {
            self.active = index;
            return;
        }
        if self.tabs.len() >= 24 {
            self.status = "Close a tab before opening another (24-tab limit)".into();
            return;
        }
        let generation = id();
        self.request(
            format!("attach:{generation}:{key}"),
            "terminal.attach",
            json!({"node_id":agent["node_id"],"agent_id":agent["id"]}),
        );
        self.tabs.push(Tab {
            key,
            agent,
            tid: None,
            model: Model::new(24, 80, self.history),
            cursor: 0,
            initial: true,
            pending: true,
            error: None,
            geometry: (80, 24),
            read_only: false,
            attach_generation: generation,
            retry_after: Instant::now(),
        });
        self.active = self.tabs.len() - 1;
        self.filter_focus = false;
    }
    fn reattach(&mut self, key: &str) {
        if let Some(tab) = self.tabs.iter_mut().find(|t| t.key == key) {
            tab.attach_generation = id();
            tab.tid = None;
            tab.pending = true;
            tab.initial = true;
            tab.cursor = 0;
            tab.error = None;
            tab.retry_after = Instant::now() + Duration::from_secs(5);
            let generation = tab.attach_generation.clone();
            let args = json!({"node_id":tab.agent["node_id"],"agent_id":tab.agent["id"]});
            self.request(
                format!("attach:{generation}:{key}"),
                "terminal.attach",
                args,
            );
        }
    }
    fn input(&mut self, bytes: Vec<u8>) {
        if bytes.is_empty() {
            return;
        }
        if let Some(tab) = self.tabs.get(self.active) {
            if tab.read_only {
                self.status =
                    "Read-only view: close/release the writer view to take control".into();
                return;
            }
            if let Some(tid) = &tab.tid {
                self.request(
                    "input".into(),
                    "terminal.input",
                    json!({"terminal_id":tid,"data":STANDARD.encode(bytes)}),
                );
            }
        }
    }
    fn paste(&mut self, text: &str) {
        let data = self.tabs.get(self.active).map(|t| t.model.paste(text));
        match data {
            Some(Ok(bytes)) => self.input(bytes),
            Some(Err(err)) => self.status = err.to_string(),
            None => {}
        }
    }
    fn copy(&mut self) {
        let text = self
            .active_tab()
            .map(|t| t.model.selection_text())
            .unwrap_or_default();
        let tx = self.tx.clone();
        self.handle.spawn_blocking(move || {
            let _ = tx.send((
                "clipboard".into(),
                terminal::copy(&text)
                    .map(Value::String)
                    .map_err(|e| e.to_string()),
            ));
        });
    }
    fn clipboard_paste(&self) {
        let tx = self.tx.clone();
        self.handle.spawn_blocking(move || {
            let _ = tx.send((
                "paste".into(),
                terminal::read_clipboard()
                    .map(Value::String)
                    .map_err(|e| e.to_string()),
            ));
        });
    }
    fn form(&mut self, kind: &str, title: &str, fields: Vec<(&str, String)>) {
        self.dialog = Some(Dialog::Form {
            kind: kind.into(),
            title: title.into(),
            fields: fields.into_iter().map(|(a, b)| (a.into(), b)).collect(),
            focus: 0,
        });
    }
    fn new_agent(&mut self) {
        let node = if self.node.is_empty() {
            self.snapshot["nodes"]
                .as_array()
                .and_then(|n| n.first())
                .and_then(|n| n["node_id"].as_str())
                .unwrap_or("")
                .to_string()
        } else {
            self.node.clone()
        };
        self.form(
            "new",
            "New agent",
            vec![
                ("Node ID", node),
                ("Name", String::new()),
                ("Tool", "codex".into()),
                ("Workspace", String::new()),
                ("Launcher (blank=default, none=local)", String::new()),
                ("Prompt (optional)", String::new()),
            ],
        );
    }
    fn node_form(&mut self, edit: bool) {
        let existing = if edit {
            self.snapshot["connections"]
                .as_array()
                .and_then(|entries| entries.iter().find(|e| e["node_id"] == self.node))
                .cloned()
                .unwrap_or(Value::Null)
        } else {
            Value::Null
        };
        let node = if edit {
            self.node.clone()
        } else {
            String::new()
        };
        self.form(
            "node",
            "Node connection",
            vec![
                ("Node ID", node),
                (
                    "Unix socket (local)",
                    existing["socket"].as_str().unwrap_or("").into(),
                ),
                (
                    "Transport argv JSON (remote)",
                    if existing["command"]
                        .as_array()
                        .map(|a| !a.is_empty())
                        .unwrap_or(false)
                    {
                        existing["command"].to_string()
                    } else {
                        String::new()
                    },
                ),
                (
                    "Token file (absolute)",
                    existing["token_file"].as_str().unwrap_or("").into(),
                ),
            ],
        );
    }
    fn job_form(&mut self, edit: bool) {
        let existing = if edit {
            self.snapshot["jobs"]
                .as_array()
                .and_then(|jobs| jobs.iter().find(|job| job["id"] == self.job))
                .cloned()
        } else {
            None
        };
        if edit && existing.is_none() {
            self.status = "Select a task or loop first".into();
            return;
        }
        self.editing_job = existing.clone();
        let job = existing.unwrap_or(Value::Null);
        let agent = self
            .tabs
            .get(self.active)
            .map(|t| t.agent.clone())
            .unwrap_or(Value::Null);
        self.form(
            "job",
            "Scheduled task / engineering loop",
            vec![
                ("Name", job["name"].as_str().unwrap_or("").into()),
                (
                    "Kind: loop / prompt / command",
                    job["kind"].as_str().unwrap_or("loop").into(),
                ),
                (
                    "Every seconds; or at UNIX time",
                    if job.is_null() {
                        "300".into()
                    } else if job["every_seconds"].as_u64().unwrap_or(0) > 0 {
                        job["every_seconds"].to_string()
                    } else {
                        format!("at {}", job["next_due"].as_i64().unwrap_or(0))
                    },
                ),
                (
                    "Node ID",
                    job["node_id"]
                        .as_str()
                        .or_else(|| agent["node_id"].as_str())
                        .unwrap_or(&self.node)
                        .into(),
                ),
                (
                    "Agent ID",
                    job["agent_id"]
                        .as_str()
                        .or_else(|| agent["id"].as_str())
                        .unwrap_or("")
                        .into(),
                ),
                (
                    "Prompt; or command argv JSON",
                    if job["kind"] == "command" {
                        job["argv"].to_string()
                    } else {
                        job["prompt"].as_str().unwrap_or("").into()
                    },
                ),
            ],
        );
    }
    fn submit_form(&mut self) {
        let Some(Dialog::Form{kind,fields,..})=self.dialog.take()else{return};
        let v: Vec<_> = fields.iter().map(|(_, v)| v.trim().to_string()).collect();
        let result: Result<()> = (|| {
            match kind.as_str() {
                "new" => {
                    if v[0].is_empty() || v[2].is_empty() || !v[3].starts_with('/') {
                        return Err(error("Node, tool and absolute workspace are required"));
                    }
                    let agent = id()[..8].to_string();
                    self.pending_open = Some((v[0].clone(), agent.clone()));
                    let mut options =
                        json!({"name":v[1],"tool":v[2],"path":v[3],"prompt":v[5],"json":true});
                    if !v[4].is_empty() {
                        options["tool_launcher"] = json!(v[4]);
                    }
                    self.request("created".into(),"agent.operation",json!({"node_id":v[0],"agent_id":agent,"operation":"agent.run","options":options}));
                    self.status = format!(
                        "Creating {agent}; follow initializing → started in the agent list"
                    );
                }
                "node" => {
                    let command: Vec<String> = if v[2].is_empty() {
                        vec![]
                    } else {
                        serde_json::from_str(&v[2])?
                    };
                    self.request("command".into(),"node.upsert",json!({"node_id":v[0],"socket":if v[1].is_empty(){Value::Null}else{json!(v[1])},"command":command,"token_file":v[3]}));
                    self.node = v[0].clone();
                }
                "job" => {
                    let mut job = self.editing_job.clone().unwrap_or_else(|| json!({}));
                    for (key, value) in [
                        ("name", &v[0]),
                        ("kind", &v[1]),
                        ("node_id", &v[3]),
                        ("agent_id", &v[4]),
                        ("prompt", &v[5]),
                    ] {
                        job[key] = json!(value);
                    }
                    job["last_status"] = json!("scheduled");
                    job["attempts"] = json!(0);
                    job["last_error"] = Value::Null;
                    if let Some(at) = v[2].strip_prefix("at ") {
                        job["next_due"] = json!(at.parse::<i64>()?);
                        job["every_seconds"] = json!(0);
                    } else {
                        let every = v[2].parse::<i64>()?;
                        if every <= 0 {
                            return Err(error("use a positive interval or at UNIX_SECONDS"));
                        }
                        job["every_seconds"] = json!(every);
                        job["next_due"] = json!(cam_protocol::now()
                            .checked_add(every)
                            .ok_or_else(|| error("interval is too large"))?);
                    }
                    if v[1] == "command" {
                        job["argv"] = serde_json::from_str::<Value>(&v[5])?;
                    }
                    self.request("command".into(), "job.save", job);
                    self.editing_job = None;
                }
                "message" => {
                    self.request("operation".into(),"agent.operation",json!({"node_id":v[0],"agent_id":v[1],"operation":"agent.send","options":{"text":v[2]}}));
                }
                _ => {}
            }
            Ok(())
        })();
        if let Err(err) = result {
            self.status = err.to_string();
            self.form(
                &kind,
                "Correct the highlighted request",
                fields
                    .iter()
                    .map(|(k, v)| (k.as_str(), v.clone()))
                    .collect(),
            );
        }
    }
    fn operation(&mut self, operation: &str, agent: Value) {
        self.dialog = None;
        if operation == "open" {
            self.open(agent);
            return;
        }
        if operation == "reset" {
            let key = format!(
                "{}:{}",
                agent["node_id"].as_str().unwrap_or(""),
                agent["id"].as_str().unwrap_or("")
            );
            if let Some(tid) = self
                .tabs
                .iter()
                .find(|t| t.key == key)
                .and_then(|t| t.tid.clone())
            {
                self.request(
                    format!("reset:{key}"),
                    "terminal.detach",
                    json!({"terminal_id":tid}),
                );
            }
            return;
        }
        if operation == "message" {
            self.form(
                "message",
                "Send agent prompt",
                vec![
                    ("Node ID", agent["node_id"].as_str().unwrap_or("").into()),
                    ("Agent ID", agent["id"].as_str().unwrap_or("").into()),
                    ("Prompt", String::new()),
                ],
            );
            return;
        }
        self.request("operation".into(),"agent.operation",json!({"node_id":agent["node_id"],"agent_id":agent["id"],"operation":format!("agent.{operation}"),"options":{"json":true}}));
        self.status = format!(
            "Submitted {operation} to {}:{}",
            agent["node_id"], agent["id"]
        );
    }
    fn action(&mut self, action: &str) {
        match action {
            "new" => self.new_agent(),
            "nodes" => self.dialog = Some(Dialog::Nodes),
            "settings" => self.dialog = Some(Dialog::Settings),
            "dismiss" => self.dialog = None,
            "form.submit" => self.submit_form(),
            "node.add" => self.node_form(false),
            "node.edit" => self.node_form(true),
            "job.add" => self.job_form(false),
            "job.edit" => self.job_form(true),
            "node.connect" | "node.disconnect" | "node.remove" => {
                self.request("command".into(), action, json!({"node_id":self.node}));
            }
            "job.toggle" | "job.remove" => {
                self.request("command".into(), action, json!({"id":self.job}))
            }
            "job.history" => self.request("history".into(), action, json!({"id":self.job})),
            "health.details" => self.request("health".into(), "health", json!({})),
            "tabs.prev" => self.active = self.active.saturating_sub(1),
            "tabs.next" => self.active = (self.active + 1).min(self.tabs.len().saturating_sub(1)),
            "bottom" => {
                if let Some(t) = self.active_tab() {
                    t.model.bottom();
                }
            }
            _ => {
                if let Some(key) = action.strip_prefix("agent:") {
                    if let Some(agent) = self.agents().into_iter().find(|a| {
                        format!(
                            "{}:{}",
                            a["node_id"].as_str().unwrap_or(""),
                            a["id"].as_str().unwrap_or("")
                        ) == key
                    }) {
                        self.open(agent);
                    }
                }
                if let Some(index) = action
                    .strip_prefix("tab:")
                    .and_then(|s| s.parse::<usize>().ok())
                {
                    if index < self.tabs.len() {
                        self.active = index;
                        self.filter_focus = false;
                    }
                }
                if let Some(index) = action
                    .strip_prefix("close:")
                    .and_then(|s| s.parse::<usize>().ok())
                {
                    if index < self.tabs.len() {
                        if let Some(tid) = &self.tabs[index].tid {
                            self.request(
                                "release".into(),
                                "terminal.release",
                                json!({"terminal_id":tid}),
                            );
                        }
                        self.tabs.remove(index);
                        self.active = self.active.min(self.tabs.len().saturating_sub(1));
                    }
                }
                if let Some(node) = action.strip_prefix("select-node:") {
                    self.node = node.into();
                }
                if let Some(job) = action.strip_prefix("select-job:") {
                    self.job = job.into();
                }
                if let Some(index) = action
                    .strip_prefix("field:")
                    .and_then(|s| s.parse::<usize>().ok())
                {
                    if let Some(Dialog::Form { focus, .. }) = &mut self.dialog {
                        *focus = index;
                    }
                }
                if let Some(op) = action.strip_prefix("op:") {
                    if let Some(Dialog::Menu(agent)) = &self.dialog {
                        self.operation(op, agent.clone());
                    }
                }
                if action == "filter" {
                    self.filter_focus = true;
                }
            }
        }
    }
    fn replies(&mut self) {
        while let Ok((kind, result)) = self.rx.try_recv() {
            if kind == "snapshot" {
                self.snapshot_pending = false;
                if let Ok(snapshot) = result {
                    if !self.daemon_connected {
                        self.status = "Client ready · F1 New · F2 Nodes · F3 Settings".into();
                    }
                    self.daemon_connected = true;
                    if !self.snapshot["epoch"].is_null()
                        && self.snapshot["epoch"] != snapshot["epoch"]
                    {
                        for t in &mut self.tabs {
                            t.error = Some("Client daemon restarted; resynchronizing".into());
                            t.pending = false;
                        }
                    }
                    self.snapshot = snapshot;
                    if self.node.is_empty() {
                        self.node = self.snapshot["nodes"]
                            .as_array()
                            .and_then(|n| n.first())
                            .and_then(|n| n["node_id"].as_str())
                            .unwrap_or("")
                            .into();
                    }
                    if let Some((node, agent)) = &self.pending_open {
                        let target = self.snapshot["agents"]
                            .as_array()
                            .and_then(|list| {
                                list.iter()
                                    .find(|a| a["node_id"] == *node && a["id"] == *agent)
                            })
                            .cloned();
                        if let Some(target) = target {
                            self.pending_open = None;
                            self.open(target);
                        }
                    }
                } else {
                    self.daemon_connected = false;
                    self.status = result.err().unwrap_or_default();
                }
                continue;
            }
            if let Some((generation, key)) = kind
                .strip_prefix("attach:")
                .and_then(|value| value.split_once(':'))
            {
                if let Some(t) = self
                    .tabs
                    .iter_mut()
                    .find(|t| t.key == key && t.attach_generation == generation)
                {
                    t.pending = false;
                    match result {
                        Ok(info) => {
                            t.tid = info["terminal_id"].as_str().map(str::to_string);
                            t.read_only = info["read_only"] == true;
                            let rows = info["rows"].as_u64().unwrap_or(24) as u16;
                            let cols = info["cols"].as_u64().unwrap_or(80) as u16;
                            t.model = Model::new(rows, cols, self.history);
                            t.geometry = (cols, rows);
                        }
                        Err(err) => {
                            t.error = Some(err);
                            t.retry_after = Instant::now() + Duration::from_secs(5);
                        }
                    }
                }
                continue;
            }
            if let Some(key) = kind.strip_prefix("reset:") {
                match result {
                    Ok(_) => self.reattach(key),
                    Err(err) => self.status = err,
                }
                continue;
            }
            if let Some(tid) = kind.strip_prefix("output:") {
                if let Some(t) = self.tabs.iter_mut().find(|t| t.tid.as_deref() == Some(tid)) {
                    t.pending = false;
                    match result {
                        Ok(output) => {
                            t.read_only = output["read_only"] == true;
                            if t.read_only && !t.model.paused {
                                let rows = output["rows"].as_u64().unwrap_or(24) as u16;
                                let cols = output["cols"].as_u64().unwrap_or(80) as u16;
                                t.model.parser.set_size(rows, cols);
                            }
                            if t.initial && !output["snapshot"].is_null() {
                                let snapshot = &output["snapshot"];
                                if let Some(data) = snapshot["data"]
                                    .as_str()
                                    .and_then(|s| STANDARD.decode(s).ok())
                                {
                                    t.model.process(&data);
                                    t.initial = false;
                                }
                            }
                            if !t.initial {
                                if let Some(data) = output["data"]
                                    .as_str()
                                    .and_then(|s| STANDARD.decode(s).ok())
                                {
                                    t.model.process(&data);
                                    t.cursor = output["cursor"].as_u64().unwrap_or(t.cursor);
                                }
                            }
                            if t.model.overflow {
                                t.error=Some("History backlog exceeded; right-click agent → Reconnect terminal".into());
                            }
                            if output["closed"] == true {
                                t.error = Some(
                                    output["error"]
                                        .as_str()
                                        .unwrap_or(
                                            "Terminal closed; waiting for live agent or reconnect",
                                        )
                                        .into(),
                                );
                                t.retry_after = Instant::now() + Duration::from_secs(5);
                            }
                        }
                        Err(err) => t.error = Some(err),
                    }
                }
                continue;
            }
            match result {
                Ok(result) if kind == "paste" => {
                    if let Some(text) = result.as_str() {
                        self.paste(text);
                    }
                }
                Ok(result) if kind == "clipboard" => {
                    self.status = result.as_str().unwrap_or("").into()
                }
                Ok(result) if kind == "history" || kind == "health" => {
                    self.dialog = Some(Dialog::Info {
                        title: if kind == "health" {
                            "Component health"
                        } else {
                            "Task execution history"
                        }
                        .into(),
                        text: serde_json::to_string_pretty(&result).unwrap_or_default(),
                        scroll: 0,
                    })
                }
                Ok(result) if kind == "created" && result["payload"]["exit_code"] == 0 => {
                    self.status = "Agent created; following launch in its terminal".into();
                    self.last_snapshot = Instant::now() - Duration::from_secs(2);
                }
                Ok(result) if kind == "operation" || kind == "created" => {
                    let text = result["payload"]["stdout"].as_str().unwrap_or("");
                    let stderr = result["payload"]["stderr"].as_str().unwrap_or("");
                    // Capture's envelope contains escaped multiline text.
                    // Render its content so paging can reach all the output.
                    let content = serde_json::from_str::<Value>(text)
                        .ok()
                        .and_then(|value| value["content"].as_str().map(str::to_owned));
                    self.dialog = Some(Dialog::Info {
                        title: format!(
                            "Operation: {}",
                            result["payload"]["status"].as_str().unwrap_or("result")
                        ),
                        text: format!("{}\n{stderr}", content.as_deref().unwrap_or(text)),
                        scroll: 0,
                    });
                    self.last_snapshot = Instant::now() - Duration::from_secs(2);
                }
                Ok(_) if kind == "command" => {
                    self.status = "Saved / submitted".into();
                    self.last_snapshot = Instant::now() - Duration::from_secs(2);
                }
                Err(err) => self.status = err,
                _ => {}
            }
        }
    }
    fn tick(&mut self) {
        self.replies();
        if !self.snapshot_pending && self.last_snapshot.elapsed() > Duration::from_secs(1) {
            self.snapshot_pending = true;
            self.last_snapshot = Instant::now();
            self.request("snapshot".into(), "snapshot", json!({}));
        }
        let retry: Vec<_> = self
            .tabs
            .iter()
            .filter(|t| {
                t.error.is_some()
                    && !t.pending
                    && !t.model.paused
                    && Instant::now() >= t.retry_after
                    && self.snapshot["agents"]
                        .as_array()
                        .map(|agents| {
                            agents.iter().any(|a| {
                                a["node_id"] == t.agent["node_id"]
                                    && a["id"] == t.agent["id"]
                                    && a["status"] == "alive"
                                    && a["stale"] == false
                            })
                        })
                        .unwrap_or(false)
            })
            .map(|t| t.key.clone())
            .collect();
        for key in retry {
            self.reattach(&key);
        }
        let requests: Vec<_> = self
            .tabs
            .iter_mut()
            .filter_map(|t| {
                if t.pending || t.error.is_some() {
                    None
                } else {
                    t.tid.as_ref().map(|tid| {
                        t.pending = true;
                        (
                            format!("output:{tid}"),
                            json!({"terminal_id":tid,"cursor":t.cursor,"initial":t.initial}),
                        )
                    })
                }
            })
            .collect();
        for (kind, args) in requests {
            self.request(kind, "terminal.read", args);
        }
        let cols = self.main.width.max(2).min(500);
        let rows = self.main.height.max(2).min(200);
        if let Some(t) = self.tabs.get_mut(self.active) {
            if !t.initial && !t.read_only && t.geometry != (cols, rows) {
                t.model.parser.set_size(rows, cols);
                t.geometry = (cols, rows);
                if let Some(tid) = t.tid.clone() {
                    self.request(
                        "resize".into(),
                        "terminal.resize",
                        json!({"terminal_id":tid,"cols":cols,"rows":rows}),
                    );
                }
            }
        }
    }
    fn button(&mut self, f: &mut Frame, rect: Rect, label: &str, action: &str, active: bool) {
        if rect.width == 0
            || rect.height == 0
            || rect.x >= f.size().width
            || rect.y >= f.size().height
        {
            return;
        }
        let rect = Rect::new(
            rect.x,
            rect.y,
            rect.width.min(f.size().width - rect.x),
            rect.height.min(f.size().height - rect.y),
        );
        f.render_widget(
            Paragraph::new(label).style(
                Style::default()
                    .fg(if active { Color::Black } else { ACCENT })
                    .bg(if active { ACCENT } else { PANEL }),
            ),
            rect,
        );
        self.hits.push((rect, action.into()));
    }
    pub fn draw(&mut self, f: &mut Frame) {
        self.hits.clear();
        let area = f.size();
        f.render_widget(
            Block::default().style(Style::default().bg(BG).fg(Color::White)),
            area,
        );
        if area.width < 48 || area.height < 12 {
            f.render_widget(
                Paragraph::new("CAM TUI needs at least 48×12 terminal cells"),
                area,
            );
            return;
        }
        let left = area.width.min(32).max(22).min(area.width / 2);
        let right = area.width - left;
        f.render_widget(
            Block::default().style(Style::default().bg(PANEL)),
            Rect::new(0, 0, left, area.height - 1),
        );
        self.button(f, Rect::new(1, 1, 5, 1), " New ", "new", false);
        self.button(f, Rect::new(7, 1, 10, 1), " Settings ", "settings", false);
        self.button(f, Rect::new(18, 1, 7, 1), " Nodes ", "nodes", false);
        let filter = Rect::new(1, 3, left - 2, 3);
        f.render_widget(
            Paragraph::new(self.filter.as_str()).block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(if self.filter_focus {
                        "Filter · typing"
                    } else {
                        "Filter agents /"
                    }),
            ),
            filter,
        );
        self.hits.push((filter, "filter".into()));
        let agents = self.agents();
        let height = area.height.saturating_sub(9) as usize;
        for (i, agent) in agents
            .iter()
            .skip(self.list_offset)
            .take(height / 2)
            .enumerate()
        {
            let y = 7 + i as u16 * 2;
            let key = format!(
                "{}:{}",
                agent["node_id"].as_str().unwrap_or(""),
                agent["id"].as_str().unwrap_or("")
            );
            let selected = self
                .tabs
                .get(self.active)
                .map(|t| t.key == key)
                .unwrap_or(false);
            let alive = agent["status"] == "alive";
            let stale = agent["stale"] == true;
            let color = if stale {
                Color::DarkGray
            } else if alive {
                if agent["state"] == "idle" {
                    ACCENT
                } else {
                    Color::LightYellow
                }
            } else {
                Color::LightRed
            };
            let title = format!(
                "{} {}",
                if alive { "●" } else { "○" },
                agent["name"].as_str().unwrap_or("agent")
            );
            f.render_widget(
                Paragraph::new(title).style(Style::default().fg(color).bg(if selected {
                    Color::Rgb(48, 64, 70)
                } else {
                    PANEL
                })),
                Rect::new(1, y, left - 2, 1),
            );
            f.render_widget(
                Paragraph::new(format!(
                    "  {} · {}{}",
                    agent["node_id"].as_str().unwrap_or(""),
                    agent["state"].as_str().unwrap_or(""),
                    if stale {
                        " · stale"
                    } else if agent["phase"] == "initializing" {
                        " · initializing"
                    } else {
                        ""
                    }
                ))
                .style(Style::default().fg(Color::Gray)),
                Rect::new(1, y + 1, left - 2, 1),
            );
            self.hits
                .push((Rect::new(1, y, left - 2, 2), format!("agent:{key}")));
        }
        let visible = ((right.saturating_sub(8)) / 19).max(1) as usize;
        let start = (self.active / visible) * visible;
        if start > 0 {
            self.button(f, Rect::new(left + 1, 1, 2, 1), "‹", "tabs.prev", false);
        }
        if start + visible < self.tabs.len() {
            self.button(
                f,
                Rect::new(area.width - 3, 1, 2, 1),
                "›",
                "tabs.next",
                false,
            );
        }
        let mut x = left + 4;
        for i in start..(start + visible).min(self.tabs.len()) {
            let title = self.tabs[i].agent["name"].as_str().unwrap_or("agent");
            let label = format!(" {} ", title.chars().take(12).collect::<String>());
            let width = 14.min(area.width.saturating_sub(x + 3));
            self.button(
                f,
                Rect::new(x, 1, width, 1),
                &label,
                &format!("tab:{i}"),
                self.active == i,
            );
            self.button(
                f,
                Rect::new(x + width, 1, 2, 1),
                "× ",
                &format!("close:{i}"),
                false,
            );
            x += 19;
        }
        let term = Rect::new(left, 3, right, area.height - 4);
        f.render_widget(
            Block::default()
                .borders(Borders::ALL)
                .title("Agent terminal"),
            term,
        );
        self.main = Rect::new(term.x + 1, term.y + 1, term.width - 2, term.height - 2);
        if let Some(tab) = self.tabs.get(self.active) {
            f.render_widget(View(&tab.model), self.main);
            if tab.read_only {
                f.render_widget(
                    Paragraph::new("Read-only: another view controls input/size")
                        .style(Style::default().fg(Color::LightYellow)),
                    Rect::new(self.main.x, self.main.y, self.main.width, 1),
                );
            }
            if let Some(error) = &tab.error {
                f.render_widget(
                    Paragraph::new(error.as_str()).style(Style::default().fg(Color::LightRed)),
                    Rect::new(
                        self.main.x,
                        self.main.y,
                        self.main.width,
                        self.main.height.min(3),
                    ),
                );
            }
            if tab.model.paused {
                self.button(
                    f,
                    Rect::new(
                        self.main.x + self.main.width.saturating_sub(24),
                        self.main.y,
                        24.min(self.main.width),
                        1,
                    ),
                    " History · End: return ",
                    "bottom",
                    true,
                );
            }
        } else {
            f.render_widget(Paragraph::new("Select an agent on the left, or click New.\n\nConnect CAM Servers in Nodes.\n\nPageUp/Down · Wheel · Drag to select · Ctrl+C to copy\nTerminal Paste shortcut · Ctrl+Q to leave UI\nF1 New · F2 Nodes · F3 Settings · F8 mouse passthrough").style(Style::default().fg(Color::Gray)),self.main);
        }
        let status = format!(
            "{}{}",
            if self.mouse_passthrough {
                "[TOOL MOUSE] "
            } else {
                ""
            },
            self.status
        );
        f.render_widget(
            Paragraph::new(status).style(Style::default().fg(Color::Gray).bg(PANEL)),
            Rect::new(0, area.height - 1, area.width, 1),
        );
        self.draw_dialog(f);
        if self.dialog.is_none() && !self.filter_focus {
            if let Some(t) = self.tabs.get(self.active) {
                if !t.model.paused && !t.model.parser.screen().hide_cursor() {
                    let (r, c) = t.model.parser.screen().cursor_position();
                    if r < self.main.height && c < self.main.width {
                        f.set_cursor(self.main.x + c, self.main.y + r);
                    }
                }
            }
        }
    }
    fn draw_dialog(&mut self, f: &mut Frame) {
        let Some(dialog)=self.dialog.take()else{return};
        let area = f.size();
        let width = area.width.saturating_sub(6).min(100);
        let height = area.height.saturating_sub(4).min(27);
        let rect = Rect::new(
            (area.width - width) / 2,
            (area.height - height) / 2,
            width,
            height,
        );
        f.render_widget(Clear, rect);
        f.render_widget(
            Block::default()
                .borders(Borders::ALL)
                .style(Style::default().bg(PANEL)),
            rect,
        );
        self.hits.clear();
        let inner = Rect::new(rect.x + 2, rect.y + 1, rect.width - 4, rect.height - 2);
        let close = Rect::new(rect.x + rect.width - 4, rect.y, 3, 1);
        self.button(f, close, " × ", "dismiss", false);
        match &dialog {
            Dialog::Form {
                title,
                fields,
                focus,
                ..
            } => {
                f.render_widget(
                    Paragraph::new(title.as_str())
                        .style(Style::default().fg(ACCENT).add_modifier(Modifier::BOLD)),
                    Rect::new(inner.x, inner.y, inner.width, 1),
                );
                for (i, (label, value)) in fields.iter().enumerate() {
                    let y = inner.y + 2 + i as u16 * 2;
                    if y + 1 >= rect.y + rect.height - 2 {
                        break;
                    }
                    let field = Rect::new(inner.x, y, inner.width, 2);
                    f.render_widget(
                        Paragraph::new(format!("{label}\n{value}")).style(
                            Style::default().fg(if i == *focus { ACCENT } else { Color::Gray }),
                        ),
                        field,
                    );
                    self.hits.push((field, format!("field:{i}")));
                    if i == *focus {
                        f.set_cursor(
                            (inner.x + value.width().min(inner.width as usize) as u16)
                                .min(inner.x + inner.width - 1),
                            y + 1,
                        );
                    }
                }
                self.button(
                    f,
                    Rect::new(inner.x, rect.y + rect.height - 2, 20, 1),
                    " Save (Ctrl+Enter) ",
                    "form.submit",
                    true,
                );
            }
            Dialog::Menu(agent) => {
                f.render_widget(
                    Paragraph::new(format!(
                        "{} · {}",
                        agent["name"].as_str().unwrap_or("agent"),
                        agent["node_id"].as_str().unwrap_or("")
                    )),
                    Rect::new(inner.x, inner.y, inner.width, 1),
                );
                for (i, (label, op)) in [
                    ("Open terminal", "open"),
                    ("Send prompt", "message"),
                    ("Status", "status"),
                    ("Capture output", "capture"),
                    ("Reconnect terminal (keep tool)", "reset"),
                    ("Reboot (fresh tool, same ID)", "reboot"),
                    ("Stop tool", "stop"),
                    ("Kill session", "kill"),
                    ("Remove agent", "remove"),
                ]
                .iter()
                .enumerate()
                {
                    self.button(
                        f,
                        Rect::new(inner.x, inner.y + 2 + i as u16, inner.width, 1),
                        label,
                        &format!("op:{op}"),
                        false,
                    );
                }
            }
            Dialog::Nodes => {
                f.render_widget(
                    Paragraph::new("Nodes · authenticated CAM Server connections")
                        .style(Style::default().fg(ACCENT)),
                    Rect::new(inner.x, inner.y, inner.width, 1),
                );
                let nodes = self.snapshot["health"]["nodes"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default();
                for (i, node) in nodes
                    .iter()
                    .take(inner.height.saturating_sub(6) as usize / 2)
                    .enumerate()
                {
                    let identity = node["node_id"].as_str().unwrap_or("");
                    let y = inner.y + 2 + i as u16 * 2;
                    let label = format!(
                        "{} {} · tunnel {} · status {}",
                        if self.node == identity { "›" } else { " " },
                        identity,
                        node["tunnel_health"].as_str().unwrap_or("unknown"),
                        node["status_health"].as_str().unwrap_or("unknown")
                    );
                    self.button(
                        f,
                        Rect::new(inner.x, y, inner.width, 1),
                        &label,
                        &format!("select-node:{identity}"),
                        self.node == identity,
                    );
                    f.render_widget(
                        Paragraph::new(
                            node["last_error"]
                                .as_str()
                                .unwrap_or("Forwarding: no active end-to-end probe"),
                        )
                        .style(Style::default().fg(Color::Gray)),
                        Rect::new(inner.x, y + 1, inner.width, 1),
                    );
                }
                let y = rect.y + rect.height - 3;
                for (i, (label, action)) in [
                    ("+ Add", "node.add"),
                    ("Edit", "node.edit"),
                    ("Connect", "node.connect"),
                    ("Disconnect", "node.disconnect"),
                    ("Remove", "node.remove"),
                ]
                .iter()
                .enumerate()
                {
                    let x = inner.x + i as u16 * 14;
                    if x + 12 < rect.x + rect.width {
                        self.button(f, Rect::new(x, y, 12, 1), label, action, false);
                    }
                }
            }
            Dialog::Settings => {
                f.render_widget(
                    Paragraph::new("Settings · scheduled tasks / engineering loops")
                        .style(Style::default().fg(ACCENT)),
                    Rect::new(inner.x, inner.y, inner.width, 1),
                );
                self.button(
                    f,
                    Rect::new(inner.x + inner.width.saturating_sub(16), inner.y, 16, 1),
                    "Health details",
                    "health.details",
                    false,
                );
                f.render_widget(
                    Paragraph::new(format!(
                        "Daemon {} · status poll {} · scheduler tick {}",
                        self.snapshot["health"]["daemon"]
                            .as_str()
                            .unwrap_or("unknown"),
                        self.snapshot["health"]["status_poll_at"],
                        self.snapshot["health"]["scheduler_tick_at"]
                    ))
                    .style(Style::default().fg(Color::Gray)),
                    Rect::new(inner.x, inner.y + 1, inner.width, 1),
                );
                let jobs = self.snapshot["jobs"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default();
                for (i, job) in jobs
                    .iter()
                    .take(inner.height.saturating_sub(5) as usize)
                    .enumerate()
                {
                    let identity = job["id"].as_str().unwrap_or("");
                    let label = format!(
                        "{} {} · {} · {} · due {}",
                        if job["enabled"] == true { "●" } else { "○" },
                        job["name"].as_str().unwrap_or(identity),
                        job["kind"].as_str().unwrap_or(""),
                        job["last_status"].as_str().unwrap_or("scheduled"),
                        job["next_due"]
                    );
                    self.button(
                        f,
                        Rect::new(inner.x, inner.y + 3 + i as u16, inner.width, 1),
                        &label,
                        &format!("select-job:{identity}"),
                        self.job == identity,
                    );
                }
                let y = rect.y + rect.height - 3;
                self.button(
                    f,
                    Rect::new(inner.x, y, 14, 1),
                    "+ Task / loop",
                    "job.add",
                    false,
                );
                self.button(
                    f,
                    Rect::new(inner.x + 16, y, 8, 1),
                    "Edit",
                    "job.edit",
                    false,
                );
                self.button(
                    f,
                    Rect::new(inner.x + 26, y, 16, 1),
                    "Enable / pause",
                    "job.toggle",
                    false,
                );
                self.button(
                    f,
                    Rect::new(inner.x + 44, y, 12, 1),
                    "Remove",
                    "job.remove",
                    false,
                );
                if inner.width >= 70 {
                    self.button(
                        f,
                        Rect::new(inner.x + 58, y, 12, 1),
                        "History",
                        "job.history",
                        false,
                    );
                }
            }
            Dialog::Info {
                title,
                text,
                scroll,
            } => {
                f.render_widget(
                    Paragraph::new(text.as_str())
                        .wrap(Wrap { trim: false })
                        .scroll((*scroll, 0))
                        .block(Block::default().title(title.as_str())),
                    inner,
                );
            }
        }
        self.dialog = Some(dialog);
    }
    fn event(&mut self, event: Event) -> bool {
        match event {
            Event::Key(key) if key.kind != KeyEventKind::Release => {
                if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('q') {
                    return false;
                }
                if self.dialog.is_some() {
                    if key.code == KeyCode::Esc {
                        self.dialog = None;
                        return true;
                    }
                    if key.code == KeyCode::Enter && key.modifiers.contains(KeyModifiers::CONTROL) {
                        self.submit_form();
                        return true;
                    }
                    if let Some(Dialog::Form { fields, focus, .. }) = &mut self.dialog {
                        match key.code {
                            KeyCode::Tab | KeyCode::Down => *focus = (*focus + 1) % fields.len(),
                            KeyCode::BackTab | KeyCode::Up => {
                                *focus = (*focus + fields.len() - 1) % fields.len()
                            }
                            KeyCode::Enter => {
                                if *focus + 1 == fields.len() {
                                    self.submit_form();
                                } else {
                                    *focus += 1;
                                }
                            }
                            KeyCode::Backspace => {
                                fields[*focus].1.pop();
                            }
                            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                                fields[*focus].1.push(c)
                            }
                            _ => {}
                        }
                    } else if let Some(Dialog::Info { scroll, .. }) = &mut self.dialog {
                        match key.code {
                            KeyCode::PageDown => *scroll = scroll.saturating_add(10),
                            KeyCode::PageUp => *scroll = scroll.saturating_sub(10),
                            _ => {}
                        }
                    }
                    return true;
                }
                if self.filter_focus {
                    match key.code {
                        KeyCode::Esc | KeyCode::Enter => self.filter_focus = false,
                        KeyCode::Backspace => {
                            self.filter.pop();
                        }
                        KeyCode::Char(c) => self.filter.push(c),
                        _ => {}
                    }
                    self.list_offset = 0;
                    return true;
                }
                match key.code {
                    KeyCode::F(1) => self.new_agent(),
                    KeyCode::F(2) => self.dialog = Some(Dialog::Nodes),
                    KeyCode::F(3) => self.dialog = Some(Dialog::Settings),
                    KeyCode::F(8) => self.mouse_passthrough = !self.mouse_passthrough,
                    KeyCode::PageUp => {
                        let rows = self.main.height as i32;
                        if let Some(t) = self.active_tab() {
                            t.model.scroll(rows);
                        }
                    }
                    KeyCode::PageDown => {
                        let rows = self.main.height as i32;
                        if let Some(t) = self.active_tab() {
                            t.model.scroll(-rows);
                        }
                    }
                    KeyCode::End
                        if self
                            .tabs
                            .get(self.active)
                            .map(|t| t.model.paused)
                            .unwrap_or(false) =>
                    {
                        if let Some(t) = self.active_tab() {
                            t.model.bottom();
                        }
                    }
                    KeyCode::Char('c')
                        if key.modifiers.contains(KeyModifiers::CONTROL)
                            && self
                                .tabs
                                .get(self.active)
                                .map(|t| t.model.selection.is_some())
                                .unwrap_or(false) =>
                    {
                        self.copy()
                    }
                    KeyCode::Char('v') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.clipboard_paste()
                    }
                    KeyCode::Char('/') if self.tabs.is_empty() => self.filter_focus = true,
                    _ => {
                        if let Some(t) = self.active_tab() {
                            if t.model.paused && t.model.selection.is_none() {
                                t.model.bottom();
                            }
                        }
                        self.input(terminal::encode_key(key));
                    }
                }
            }
            Event::Paste(text) => {
                if let Some(Dialog::Form { fields, focus, .. }) = &mut self.dialog {
                    fields[*focus].1.push_str(&text);
                } else if self.filter_focus {
                    self.filter.push_str(&text);
                } else {
                    self.paste(&text);
                }
            }
            Event::Mouse(mouse) => {
                if mouse.kind == MouseEventKind::Up(MouseButton::Left)
                    && !(self.mouse_passthrough
                        && self.dialog.is_none()
                        && contains(self.main, mouse.column, mouse.row))
                {
                    if let Some(t) = self.active_tab() {
                        t.model.dragging = false;
                        if t.model.selection.map(|(a, b)| a == b).unwrap_or(false) {
                            t.model.bottom();
                        }
                    }
                    return true;
                }
                if mouse.kind == MouseEventKind::Down(MouseButton::Left)
                    && self
                        .hits
                        .iter()
                        .any(|(r, a)| a == "bottom" && contains(*r, mouse.column, mouse.row))
                {
                    self.action("bottom");
                    return true;
                }
                let inside = contains(self.main, mouse.column, mouse.row);
                if self.dialog.is_none() && inside {
                    let row = mouse.row - self.main.y;
                    let col = mouse.column - self.main.x;
                    if self.mouse_passthrough {
                        let code = match mouse.kind {
                            MouseEventKind::Down(MouseButton::Left) => Some((0, 'M')),
                            MouseEventKind::Up(MouseButton::Left) => Some((0, 'm')),
                            MouseEventKind::Drag(MouseButton::Left) => Some((32, 'M')),
                            MouseEventKind::ScrollUp => Some((64, 'M')),
                            MouseEventKind::ScrollDown => Some((65, 'M')),
                            _ => None,
                        };
                        if let Some((code, end)) = code {
                            self.input(
                                format!("\x1b[<{code};{};{}{end}", col + 1, row + 1).into_bytes(),
                            );
                        }
                        return true;
                    }
                    match mouse.kind {
                        MouseEventKind::ScrollUp => {
                            if let Some(t) = self.active_tab() {
                                t.model.scroll(3);
                            }
                        }
                        MouseEventKind::ScrollDown => {
                            if let Some(t) = self.active_tab() {
                                t.model.scroll(-3);
                            }
                        }
                        MouseEventKind::Down(MouseButton::Left) => {
                            if let Some(t) = self.active_tab() {
                                t.model.select(row, col, true);
                            }
                        }
                        MouseEventKind::Drag(MouseButton::Left) => {
                            if let Some(t) = self.active_tab() {
                                if t.model.dragging {
                                    t.model.select(row, col, false);
                                }
                            }
                        }
                        MouseEventKind::Up(MouseButton::Left) => {
                            if let Some(t) = self.active_tab() {
                                t.model.dragging = false;
                            }
                        }
                        MouseEventKind::Down(MouseButton::Right) => {
                            if self
                                .tabs
                                .get(self.active)
                                .map(|t| t.model.selection.is_some())
                                .unwrap_or(false)
                            {
                                self.copy();
                            } else {
                                self.clipboard_paste();
                            }
                        }
                        _ => {}
                    }
                } else {
                    match mouse.kind {
                        MouseEventKind::Down(MouseButton::Left | MouseButton::Right) => {
                            let action = self
                                .hits
                                .iter()
                                .rev()
                                .find(|(rect, _)| contains(*rect, mouse.column, mouse.row))
                                .map(|(_, action)| action.clone());
                            if let Some(action) = action {
                                if mouse.kind == MouseEventKind::Down(MouseButton::Right)
                                    && action.starts_with("agent:")
                                {
                                    let key = &action[6..];
                                    if let Some(agent) = self.agents().into_iter().find(|a| {
                                        format!(
                                            "{}:{}",
                                            a["node_id"].as_str().unwrap_or(""),
                                            a["id"].as_str().unwrap_or("")
                                        ) == key
                                    }) {
                                        self.dialog = Some(Dialog::Menu(agent));
                                    }
                                } else {
                                    self.action(&action);
                                }
                            }
                        }
                        MouseEventKind::ScrollDown => {
                            self.list_offset = self.list_offset.saturating_add(1)
                        }
                        MouseEventKind::ScrollUp => {
                            self.list_offset = self.list_offset.saturating_sub(1)
                        }
                        _ => {}
                    }
                }
            }
            _ => {}
        }
        true
    }
}
fn contains(rect: Rect, x: u16, y: u16) -> bool {
    x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height
}
struct Restore;
impl Drop for Restore {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            DisableMouseCapture,
            DisableBracketedPaste,
            LeaveAlternateScreen
        );
    }
}
pub fn run(config: Config, handle: tokio::runtime::Handle) -> Result<()> {
    // Crossterm 0.27 emits bare SGR resets for suppressed color commands,
    // clearing selection attributes too. The full-screen UI needs its palette
    // even when a parent set NO_COLOR for command output.
    crossterm::style::force_color_output(true);
    enable_raw_mode()?;
    let _restore = Restore;
    execute!(
        io::stdout(),
        EnterAlternateScreen,
        EnableMouseCapture,
        EnableBracketedPaste
    )?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    terminal.clear()?;
    let mut app = App::new(&config, handle);
    loop {
        app.tick();
        terminal.draw(|f| app.draw(f))?;
        if event::poll(Duration::from_millis(40))? && !app.event(event::read()?) {
            break;
        }
    }
    let socket = app.socket.clone();
    let view = app.view_id.clone();
    let terminals: Vec<_> = app.tabs.iter().filter_map(|t| t.tid.clone()).collect();
    app.handle.block_on(async move {
        let mut tasks = tokio::task::JoinSet::new();
        for tid in terminals {
            let socket = socket.clone();
            let view = view.clone();
            tasks.spawn(async move {
                let _ = tokio::time::timeout(
                    Duration::from_secs(2),
                    ipc::request(
                        &socket,
                        "terminal.release",
                        json!({"terminal_id":tid,"view_id":view}),
                    ),
                )
                .await;
            });
        }
        while tasks.join_next().await.is_some() {}
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;
    #[tokio::test]
    async fn typed_input_waits_for_prior_input_before_sending_enter() {
        use cam_protocol::{read_frame, write_frame};
        use tokio::{io::BufReader, net::UnixListener};
        let runtime = std::env::temp_dir().join(format!("cam-ui-input-{}", id()));
        std::fs::create_dir(&runtime).unwrap();
        let socket = runtime.join("client.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let config = Config {
            runtime_dir: runtime.clone(),
            ..Config::default()
        };
        let app = App::new(&config, tokio::runtime::Handle::current());
        app.request(
            "input".into(),
            "terminal.input",
            json!({"terminal_id":"one","data":STANDARD.encode(b"command")}),
        );
        let (first, _) = listener.accept().await.unwrap();
        let (read, mut write) = first.into_split();
        let (request, _) = read_frame(&mut BufReader::new(read)).await.unwrap();
        assert_eq!(request["args"]["data"], STANDARD.encode(b"command"));
        app.request(
            "input".into(),
            "terminal.input",
            json!({"terminal_id":"one","data":STANDARD.encode(b"\r")}),
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(100), listener.accept())
                .await
                .is_err(),
            "Enter was sent before the preceding input completed"
        );
        write_frame(&mut write, &json!({"id":request["id"],"data":{"ok":true}}))
            .await
            .unwrap();
        let (second, _) = listener.accept().await.unwrap();
        let (read, mut write) = second.into_split();
        let (request, _) = read_frame(&mut BufReader::new(read)).await.unwrap();
        assert_eq!(request["args"]["data"], STANDARD.encode(b"\r"));
        write_frame(&mut write, &json!({"id":request["id"],"data":{"ok":true}}))
            .await
            .unwrap();
        drop(app);
        drop(listener);
        std::fs::remove_file(socket).unwrap();
        std::fs::remove_dir(runtime).unwrap();
    }
    #[tokio::test]
    async fn daemon_connection_status_recovers_without_overwriting_actions() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        app.tx
            .send(("snapshot".into(), Ok(json!({"epoch":"one"}))))
            .unwrap();
        app.replies();
        assert!(app.status.starts_with("Client ready"));
        app.status = "Agent created".into();
        app.tx
            .send(("snapshot".into(), Ok(json!({"epoch":"one"}))))
            .unwrap();
        app.replies();
        assert_eq!(app.status, "Agent created");
        app.tx
            .send(("snapshot".into(), Err("Connection lost".into())))
            .unwrap();
        app.replies();
        assert_eq!(app.status, "Connection lost");
        app.tx
            .send(("snapshot".into(), Ok(json!({"epoch":"two"}))))
            .unwrap();
        app.replies();
        assert!(app.status.starts_with("Client ready"));
    }
    #[tokio::test]
    async fn releasing_a_single_wide_character_drag_retains_selection() {
        let mut config = Config::default();
        config.runtime_dir = std::env::temp_dir().join(format!("cam-no-daemon-{}", id()));
        let mut app = App::new(&config, tokio::runtime::Handle::current());
        app.open(json!({"node_id":"test","id":"11111111","name":"test"}));
        app.main = Rect::new(32, 4, 20, 3);
        app.tabs[0].model.process("中".as_bytes());
        for (kind, column) in [
            (MouseEventKind::Down(MouseButton::Left), 32),
            (MouseEventKind::Drag(MouseButton::Left), 33),
            (MouseEventKind::Up(MouseButton::Left), 33),
        ] {
            app.event(Event::Mouse(event::MouseEvent {
                kind,
                column,
                row: 4,
                modifiers: KeyModifiers::NONE,
            }));
        }
        assert_eq!(app.tabs[0].model.selection_text(), "中");
    }
    #[tokio::test]
    async fn desktop_layout_renders_commands_filter_and_main() {
        let config = Config::default();
        let mut app = App::new(&config, tokio::runtime::Handle::current());
        let mut term = Terminal::new(TestBackend::new(120, 32)).unwrap();
        term.draw(|f| app.draw(f)).unwrap();
        let text = term
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol.as_str())
            .collect::<String>();
        for word in ["New", "Settings", "Nodes", "Filter", "Agent terminal"] {
            assert!(text.contains(word), "missing {word}");
        }
    }
    #[tokio::test]
    async fn settings_nodes_and_forms_survive_small_terminal_resize() {
        for (width, height) in [(48, 12), (80, 24), (120, 32)] {
            let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            for action in ["new", "settings", "nodes"] {
                app.action(action);
                terminal.draw(|f| app.draw(f)).unwrap();
            }
        }
    }
    #[tokio::test]
    async fn filtering_does_not_close_tabs() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        app.snapshot = json!({"agents":[{"node_id":"a","id":"11111111","name":"foo"},{"node_id":"b","id":"22222222","name":"bar"}]});
        app.filter = "bar".into();
        assert_eq!(app.agents().len(), 1);
        assert_eq!(app.agents()[0]["node_id"], "b");
    }
}
