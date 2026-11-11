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
    layout::{Alignment, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
    Frame, Terminal,
};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    io,
    path::PathBuf,
    sync::mpsc,
    time::{Duration, Instant},
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

const BG: Color = Color::Rgb(17, 20, 27);
const PANEL: Color = Color::Rgb(27, 31, 41);
const ACCENT: Color = Color::Rgb(115, 211, 190);
// Keep chrome readable when a terminal customizes its indexed ANSI palette.
const TEXT: Color = Color::Rgb(224, 230, 240);
const MUTED: Color = Color::Rgb(156, 167, 187);
const HINT: Color = Color::Rgb(118, 130, 150);
const KEY: Color = Color::Rgb(138, 217, 232);
const WARNING: Color = Color::Rgb(228, 201, 124);
const ERROR: Color = Color::Rgb(240, 133, 133);
#[derive(Clone, Copy)]
enum FilterKind { Node, Tool, Status }
impl FilterKind {
    fn label(self) -> &'static str {
        match self { Self::Node => "Node", Self::Tool => "Tool", Self::Status => "Status" }
    }
}
struct Tab {
    key: String,
    agent: Value,
    pane: Option<String>,
    primary: bool,
    title: String,
    tid: Option<String>,
    model: Model,
    cursor: u64,
    initial: bool,
    pending: bool,
    error: Option<String>,
    geometry: (u16, u16),
    read_only: bool,
    resize_read_only: bool,
    attach_generation: String,
    retry_after: Instant,
}
enum Dialog {
    Nodes,
    Help { scroll: u16 },
    Filters,
    ChooseFilter {
        kind: FilterKind,
        selected: usize,
        options: Vec<(String, String)>,
        return_to_panel: bool,
    },
    ChooseNode {
        form: Box<Dialog>,
        field: usize,
        selected: usize,
        nodes: Vec<String>,
    },
    Settings,
    Menu(Value),
    More(Value),
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
    visit_tx: tokio::sync::mpsc::Sender<Value>,
    snapshot: Value,
    tabs: Vec<Tab>,
    active: usize,
    filter: String,
    filter_node: String,
    filter_tool: String,
    filter_status: String,
    filter_focus: bool,
    list_offset: usize,
    agent_list: Rect,
    menu_position: (u16, u16),
    dialog: Option<Dialog>,
    hits: Vec<(Rect, String)>,
    main: Rect,
    status: String,
    node: String,
    local_host: String,
    job: String,
    snapshot_pending: bool,
    daemon_connected: bool,
    last_snapshot: Instant,
    mouse_passthrough: bool,
    history: usize,
    view_id: String,
    editing_job: Option<Value>,
    editing_agent: Option<Value>,
    pending_open: Option<(String, String)>,
    pending_ssh: Option<(crate::ssh::Connection, String)>,
    ssh_target: String,
    last_tabs: HashMap<String, (String, Instant)>,
    creating_terminal: HashSet<String>,
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
        // Preserve click order even if a previous visit write is slow.
        let (visit_tx, mut visit_rx) = tokio::sync::mpsc::channel::<Value>(64);
        let visit_socket = config.runtime_dir.join("client.sock");
        let visit_replies = tx.clone();
        handle.spawn(async move {
            while let Some(args) = visit_rx.recv().await {
                let result = ipc::request(&visit_socket, "agent.visit", args)
                    .await.map_err(|e| e.to_string());
                let _ = visit_replies.send(("visit".into(), result));
            }
        });
        Self {
            socket: config.runtime_dir.join("client.sock"),
            handle,
            tx,
            rx,
            input_tx,
            visit_tx,
            snapshot: json!({"agents":[],"nodes":[],"jobs":[]}),
            tabs: vec![],
            active: 0,
            filter: String::new(),
            filter_node: String::new(),
            filter_tool: String::new(),
            filter_status: String::new(),
            filter_focus: false,
            list_offset: 0,
            agent_list: Rect::default(),
            menu_position: (27, 6),
            dialog: None,
            hits: vec![],
            main: Rect::default(),
            status: "Connecting to local Client daemon…".into(),
            node: String::new(),
            local_host: cam_server::config::hostname().unwrap_or_else(|_| "Local".into()),
            job: String::new(),
            snapshot_pending: false,
            daemon_connected: false,
            last_snapshot: Instant::now() - Duration::from_secs(2),
            mouse_passthrough: false,
            history: config.scrollback_lines,
            view_id: id(),
            editing_job: None,
            editing_agent: None,
            pending_open: None,
            pending_ssh: None,
            ssh_target: String::new(),
            last_tabs: HashMap::new(),
            creating_terminal: HashSet::new(),
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
        let mut agents: Vec<Value> = self.snapshot["agents"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|a| {
                (self.filter_node.is_empty() || a["node_id"] == self.filter_node)
                    && (self.filter_tool.is_empty() || a["tool"] == self.filter_tool)
                    && (self.filter_status.is_empty()
                        || a["status"] == self.filter_status
                        || a["state"] == self.filter_status
                        || Self::agent_status(a) == self.filter_status
                        || (self.filter_status == "stale" && a["stale"] == true))
                    && (filter.is_empty()
                    || self
                        .node_label(a["node_id"].as_str().unwrap_or(""))
                        .to_lowercase()
                        .contains(&filter)
                    || self.short_node(a["node_id"].as_str().unwrap_or(""))
                        .to_lowercase().contains(&filter)
                    || ["name", "id", "node_id", "state", "status", "tool"]
                        .iter()
                        .any(|k| {
                            a[*k]
                                .as_str()
                                .unwrap_or("")
                                .to_lowercase()
                                .contains(&filter)
                        })
                    || a["tags"].as_array().into_iter().flatten().any(|tag|
                        tag.as_str().unwrap_or("").to_lowercase().contains(filter.trim_start_matches('#'))))
            })
            .cloned()
            .collect();
        let visit = |a: &Value| self.snapshot["recent_visits"][format!("{}:{}", a["node_id"].as_str().unwrap_or(""), a["id"].as_str().unwrap_or(""))].as_i64().unwrap_or(0);
        agents.sort_by(|a, b| visit(b).cmp(&visit(a))
            .then_with(|| a["name"].as_str().unwrap_or("").to_lowercase().cmp(&b["name"].as_str().unwrap_or("").to_lowercase()))
            .then_with(|| a["node_id"].as_str().cmp(&b["node_id"].as_str()))
            .then_with(|| a["id"].as_str().cmp(&b["id"].as_str())));
        agents
    }
    fn agent_status(agent: &Value) -> &str {
        if agent["phase"] == "initializing" && agent["status"] == "alive" {
            "starting"
        } else {
            agent["state"].as_str().filter(|s| !s.is_empty())
                .unwrap_or_else(|| agent["status"].as_str().unwrap_or("unknown"))
        }
    }
    fn short_node(&self, id: &str) -> String {
        let label = self.node_label(id);
        // Retain explicit aliases, including dots; abbreviate hostnames only.
        let alias = self.snapshot["connections"].as_array().into_iter().flatten()
            .find(|e| e["node_id"] == id)
            .and_then(|e| e["name"].as_str()).filter(|s| !s.trim().is_empty());
        if alias.is_some() { return label; }
        label.split('.').next().unwrap_or(&label)
            .replace("-container-xterm-", "-")
    }
    fn filter_value(&self, kind: FilterKind) -> &str {
        match kind {
            FilterKind::Node => &self.filter_node,
            FilterKind::Tool => &self.filter_tool,
            FilterKind::Status => &self.filter_status,
        }
    }
    fn filter_label(&self, kind: FilterKind) -> String {
        let value = self.filter_value(kind);
        if value.is_empty() {
            return match kind { FilterKind::Node => "All nodes", FilterKind::Tool => "All tools", FilterKind::Status => "All statuses" }.into();
        }
        match kind {
            FilterKind::Node => self.short_node(value),
            _ => value.into(),
        }
    }
    fn choose_filter(&mut self, kind: FilterKind) {
        let return_to_panel = matches!(self.dialog, Some(Dialog::Filters));
        let all = match kind { FilterKind::Node => "All nodes", FilterKind::Tool => "All tools", FilterKind::Status => "All statuses" };
        let mut options = vec![(String::new(), all.into())];
        match kind {
            FilterKind::Node => options.extend(self.node_options().into_iter()
                .map(|id| { let label = self.short_node(&id); (id, label) })),
            FilterKind::Tool => {
                let tools: std::collections::BTreeSet<_> = self.snapshot["agents"]
                    .as_array().into_iter().flatten()
                    .filter_map(|a| a["tool"].as_str()).filter(|s| !s.is_empty()).collect();
                options.extend(tools.into_iter().map(|tool| (tool.into(), tool.into())));
            }
            FilterKind::Status => options.extend(["alive", "idle", "running", "starting", "dead", "stopped", "failed", "killed", "stale"]
                .iter().map(|s| (s.to_string(), s.to_string()))),
        }
        let selected = options.iter().position(|(v, _)| v == self.filter_value(kind)).unwrap_or(0);
        self.filter_focus = false;
        self.dialog = Some(Dialog::ChooseFilter { kind, selected, options, return_to_panel });
    }
    fn select_filter(&mut self, index: usize) {
        let Some(Dialog::ChooseFilter { kind, options, return_to_panel, .. }) = self.dialog.take() else { return };
        if let Some((value, _)) = options.get(index) {
            match kind {
                FilterKind::Node => self.filter_node = value.clone(),
                FilterKind::Tool => self.filter_tool = value.clone(),
                FilterKind::Status => self.filter_status = value.clone(),
            }
            self.list_offset = 0;
        }
        if return_to_panel { self.dialog = Some(Dialog::Filters); }
    }
    fn clear_filters(&mut self) {
        self.filter.clear(); self.filter_node.clear(); self.filter_tool.clear(); self.filter_status.clear();
        self.list_offset = 0;
    }
    fn guide() -> Text<'static> {
        let heading = Style::default().fg(ACCENT).add_modifier(Modifier::BOLD);
        let key = Style::default().fg(KEY).add_modifier(Modifier::BOLD);
        let muted = Style::default().fg(MUTED);
        Text::from(vec![
            Line::from(Span::styled("  CAM  /  YOUR AGENT WORKSPACE", heading)),
            Line::from("  Click an agent to open a tab, or choose New to start one."),
            Line::from(vec![Span::raw("  "), Span::styled("Ctrl+Q", key), Span::raw("  Exit UI; agents keep running.    "), Span::styled("F4", key), Span::raw("  Open this guide anytime.")]),
            Line::from(""),
            Line::from(Span::styled("  HISTORY", heading)),
            Line::from(vec![Span::raw("  "), Span::styled("PageUp / PageDown", key), Span::raw("  Browse history.  "), Span::styled("Mouse wheel", key), Span::raw("  Scroll.")]),
            Line::from(vec![Span::raw("  "), Span::styled("End", key), Span::raw("  Return to live output and clear the selection.")]),
            Line::from(""),
            Line::from(Span::styled("  COPY & PASTE", heading)),
            Line::from(vec![Span::raw("  Drag to select, release, then "), Span::styled("Ctrl+C", key), Span::raw(" to copy to the system clipboard.")]),
            Line::from(Span::styled("  Ctrl+C without a selection interrupts the tool. Mouse copy remains terminal-dependent.", muted)),
            Line::from(vec![Span::raw("  "), Span::styled("Ctrl+V", key), Span::raw(" or your terminal's Paste shortcut. Paste does not press Enter.")]),
            Line::from(""),
            Line::from(Span::styled("  FIND & ORGANIZE", heading)),
            Line::from("  Filter by Node, Tool and Status; search names, IDs or #tags. Clear filters resets all."),
            Line::from("  Agents are sorted by your last visit, saved on this Client; background output does not reorder them."),
            Line::from("  Agent details: tool · status · node. Nodes → Edit → Name sets a short alias."),
            Line::from(""),
            Line::from(Span::styled("  COMMON ACTIONS", heading)),
            Line::from(vec![Span::raw("  "), Span::styled("F1", key), Span::raw(" New    "), Span::styled("F2", key), Span::raw(" Nodes    "), Span::styled("F3", key), Span::raw(" Settings    "), Span::styled("F8", key), Span::raw(" Tool mouse passthrough")]),
            Line::from("  Agent … menu: Settings for name/tags, Stop or Remove; More actions for other commands."),
            Line::from("  Select an agent on the left. Terminal 0 is fixed; + opens a shell tab, × closes only that shell."),
            Line::from(Span::styled("  Ctrl+Q leaves agents, shells and the Client daemon running. Nodes connects CAM Servers.", muted)),
        ])
    }
    fn node_options(&self) -> Vec<String> {
        let mut nodes = vec![];
        for key in ["connections", "nodes"] {
            for node in self.snapshot[key].as_array().into_iter().flatten() {
                if let Some(id) = node["node_id"].as_str() {
                    if !nodes.iter().any(|n| n == id) {
                        nodes.push(id.to_string());
                    }
                }
            }
        }
        nodes
    }
    fn node_label(&self, id: &str) -> String {
        let connections = self.snapshot["connections"].as_array();
        let endpoints: Vec<cam_server::relay::Endpoint> = connections
            .into_iter()
            .flatten()
            .filter_map(|value| serde_json::from_value(value.clone()).ok())
            .collect();
        let Some(endpoint) = endpoints.iter().find(|e| e.node_id == id) else {
            return if id.is_empty() { "Select a machine".into() } else { format!("Node {}", id.chars().take(8).collect::<String>()) };
        };
        let name = crate::config::endpoint_name(endpoint, &self.local_host);
        if endpoints
            .iter()
            .filter(|e| crate::config::endpoint_name(e, &self.local_host) == name)
            .count()
            > 1
        {
            format!("{name} [{}]", id.chars().take(8).collect::<String>())
        } else {
            name
        }
    }
    fn choose_node(&mut self, field: usize) {
        let Some(form @ Dialog::Form { .. }) = self.dialog.take() else { return };
        let current = match &form {
            Dialog::Form { fields, .. } => fields.get(field).map(|v| v.1.as_str()).unwrap_or(""),
            _ => "",
        };
        let nodes = self.node_options();
        let selected = nodes.iter().position(|n| n == current).unwrap_or(0);
        self.dialog = Some(Dialog::ChooseNode {
            form: Box::new(form),
            field,
            selected,
            nodes,
        });
    }
    fn select_node(&mut self, index: usize) {
        // Freeze choice identities while the dialog is open. A concurrent
        // connection edit must not turn a displayed row into another target.
        let id = match &self.dialog {
            Some(Dialog::ChooseNode { nodes, .. }) => nodes.get(index).cloned(),
            _ => None,
        };
        let Some(id) = id else { return };
        if !self.node_options().contains(&id) {
            self.status = "Machine was removed; reopen the machine selector".into();
            return;
        }
        if let Some(Dialog::ChooseNode {
            mut form, field, ..
        }) = self.dialog.take()
        {
            if let Dialog::Form { fields, focus, .. } = form.as_mut() {
                fields[field].1 = id;
                *focus = (field + 1).min(fields.len() - 1);
            }
            self.dialog = Some(*form);
        }
    }
    fn active_tab(&mut self) -> Option<&mut Tab> {
        self.tabs.get_mut(self.active)
    }
    fn activate(&mut self, index: usize) {
        let Some(tab) = self.tabs.get(index) else { return };
        if let Err(err) = self.visit_tx.try_send(json!({"node_id":tab.agent["node_id"],"agent_id":tab.agent["id"]})) {
            self.status = format!("Visit history queue unavailable: {err}");
        }
        self.active = index;
        let key = tab.key.clone();
        self.last_tabs.insert(agent_key(&tab.agent), (key.clone(), Instant::now()));
        let attach = tab.tid.is_none() && !tab.pending;
        self.filter_focus = false;
        self.list_offset = 0;
        if attach { self.reattach(&key); }
    }
    fn open(&mut self, agent: Value) {
        let group = agent_key(&agent);
        let key = self.last_tabs.get(&group).map(|(key, _)| key.as_str()).unwrap_or(&group);
        if let Some(index) = self.tabs.iter().position(|t| t.key == key)
            .or_else(|| self.tabs.iter().position(|t| t.key == group)) {
            self.activate(index);
            self.refresh_panes(&agent);
            return;
        }
        if let Some(index) = self.add_tab(agent.clone(), None, "Terminal 0".into()) {
            self.activate(index);
            self.refresh_panes(&agent);
        }
    }
    fn visible_tabs(&self) -> Vec<usize> {
        let Some(active) = self.tabs.get(self.active) else { return vec![] };
        let group = agent_key(&active.agent);
        self.tabs.iter().enumerate().filter_map(|(i, t)| (agent_key(&t.agent) == group).then_some(i)).collect()
    }
    fn discard_tabs(&mut self, keys: &HashSet<String>) {
        let old = self.tabs.get(self.active).map(|t| (t.key.clone(), agent_key(&t.agent)));
        for tab in self.tabs.iter().filter(|t| keys.contains(&t.key)) {
            if let Some(tid) = &tab.tid {
                self.request("discard".into(), if tab.read_only {"terminal.release"} else {"terminal.detach"}, json!({"terminal_id":tid}));
            }
        }
        self.tabs.retain(|t| !keys.contains(&t.key));
        self.active = old.and_then(|(key, group)| self.tabs.iter().position(|t| t.key == key)
            .or_else(|| self.tabs.iter().position(|t| t.primary && agent_key(&t.agent) == group))).unwrap_or(0);
    }
    fn add_tab(&mut self, agent: Value, pane: Option<String>, title: String) -> Option<usize> {
        let group = agent_key(&agent);
        let key = pane.as_ref().map(|p| format!("{group}/{p}")).unwrap_or_else(|| group.clone());
        if let Some(index) = self.tabs.iter().position(|t| t.key == key) { return Some(index); }
        if self.tabs.len() >= 24 {
            let groups: HashSet<_> = self.tabs.iter().map(|t| agent_key(&t.agent)).filter(|g| g != &group).collect();
            if let Some(old) = groups.iter().min_by_key(|g| self.last_tabs.get(*g).map(|(_, at)| *at)) {
                let keys = self.tabs.iter().filter(|t| agent_key(&t.agent) == *old).map(|t| t.key.clone()).collect();
                self.discard_tabs(&keys);
            } else {
                self.status = "Terminal view limit reached".into();
                return None;
            }
        }
        let generation = id();
        self.tabs.push(Tab {
            key,
            agent,
            primary: pane.is_none(),
            pane,
            title,
            tid: None,
            model: Model::new(24, 80, self.history),
            cursor: 0,
            initial: true,
            pending: false,
            error: None,
            geometry: (80, 24),
            read_only: false,
            resize_read_only: false,
            attach_generation: generation,
            retry_after: Instant::now(),
        });
        Some(self.tabs.len() - 1)
    }
    fn pane_capable(&self, agent: &Value) -> bool {
        self.snapshot["nodes"].as_array().into_iter().flatten().any(|n| n["node_id"] == agent["node_id"]
            && n["capabilities"].as_array().into_iter().flatten().any(|c| c == "terminal.panes"))
    }
    fn pane_kind(action: &str, agent: &Value) -> String {
        format!("{action}:{}|{}", agent["launch_id"].as_str().unwrap_or(""), agent_key(agent))
    }
    fn refresh_panes(&self, agent: &Value) {
        if self.pane_capable(agent) {
            self.request(Self::pane_kind("panes", agent), "terminal.list",
                json!({"node_id":agent["node_id"],"agent_id":agent["id"],"launch_id":agent["launch_id"]}));
        }
    }
    fn sync_panes(&mut self, agent: Value, info: &Value) {
        let group = agent_key(&agent);
        let panes = info["panes"].as_array().cloned().unwrap_or_default();
        let keys = self.tabs.iter().filter(|t| agent_key(&t.agent) == group && !t.primary
            && !panes.iter().any(|p| p["pane_id"].as_str() == t.pane.as_deref())).map(|t| t.key.clone()).collect();
        self.discard_tabs(&keys);
        for pane in panes.iter().filter(|p| p["primary"] != true) {
            if let Some(id) = pane["pane_id"].as_str() {
                self.add_tab(agent.clone(), Some(id.into()), pane["title"].as_str().unwrap_or("Terminal").into());
            }
        }
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
            let args = json!({"node_id":tab.agent["node_id"],"agent_id":tab.agent["id"],"pane_id":tab.pane,"launch_id":tab.agent["launch_id"]});
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
                ("Node", node),
                ("Name", String::new()),
                ("Tool", "codex".into()),
                ("Workspace", String::new()),
                ("Launcher (blank=default, none=local)", String::new()),
                ("Prompt (optional)", String::new()),
            ],
        );
    }
    fn node_form(&mut self, edit: bool) {
        let endpoint = self.snapshot["connections"].as_array().into_iter().flatten()
            .find(|e| e["node_id"] == self.node).cloned()
            .and_then(|e| serde_json::from_value::<cam_server::relay::Endpoint>(e).ok());
        let connection = if edit {
            match endpoint.as_ref().and_then(crate::ssh::profile) {
                Some(connection) => connection,
                None => { self.advanced_node_form(true); return; }
            }
        } else { crate::ssh::Connection { port:22, ..crate::ssh::Connection::default() } };
        self.ssh_target = connection.expected_node;
        self.form("node.ssh", "SSH connection", vec![
            ("Name (optional)", connection.name),
            ("Host", connection.host),
            ("Port", connection.port.to_string()),
            ("Username (optional)", connection.user),
            ("SSH key (optional)", connection.key),
        ]);
    }
    fn advanced_node_form(&mut self, edit: bool) {
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
            "Local / advanced connection",
            vec![
                (
                    "Name (optional)",
                    existing["name"].as_str().unwrap_or("").into(),
                ),
                ("Server ID (advanced)", node),
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
                    "Node",
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
                    let command: Vec<String> = if v[3].is_empty() {
                        vec![]
                    } else {
                        serde_json::from_str(&v[3])?
                    };
                    self.request("command".into(),"node.upsert",json!({"node_id":v[1],"name":v[0],"socket":if v[2].is_empty(){Value::Null}else{json!(v[2])},"command":command,"token_file":v[4]}));
                    self.node = v[1].clone();
                }
                "node.ssh" => {
                    let mut connection = crate::ssh::Connection {
                        name:v[0].clone(), host:v[1].clone(), port:if v[2].is_empty() {22} else {v[2].parse()?},
                        user:v[3].clone(), key:v[4].clone(), expected_node:self.ssh_target.clone(),
                    };
                    connection.validate()?;
                    self.pending_ssh = Some((connection, "node.ssh_add".into()));
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
                "agent.settings" => {
                    if v[0].is_empty() || v[0].chars().count() > 128 || v[0].chars().any(char::is_control) {
                        return Err(error("Name must contain 1-128 printable characters"));
                    }
                    let mut tags: Vec<&str> = vec![];
                    for tag in v[1].split(',').map(str::trim).filter(|s| !s.is_empty()) {
                        if tag.len() > 64 || !tag.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-') {
                            return Err(error("Tags use letters, digits, underscore or dash; separate them with commas"));
                        }
                        if !tags.contains(&tag) { tags.push(tag); }
                    }
                    if tags.len() > 32 { return Err(error("Use up to 32 tags")); }
                    let agent = self.editing_agent.as_mut().ok_or_else(|| error("Select an agent first"))?;
                    agent["name"] = json!(v[0]);
                    agent["tags"] = json!(tags);
                    let args = json!({"node_id":agent["node_id"],"agent_id":agent["id"],"name":v[0],"tags":tags});
                    self.request("attributes".into(), "agent.update", args);
                    self.status = "Saving agent name and tags".into();
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
        if operation == "settings" {
            let tags = agent["tags"].as_array().into_iter().flatten()
                .filter_map(Value::as_str).collect::<Vec<_>>().join(", ");
            self.form("agent.settings", &format!("Agent settings · {}", self.short_node(agent["node_id"].as_str().unwrap_or(""))), vec![
                ("Name", agent["name"].as_str().unwrap_or("").into()),
                ("Tags (comma-separated)", tags),
            ]);
            self.editing_agent = Some(agent);
            return;
        }
        if operation == "more" { self.dialog = Some(Dialog::More(agent)); return; }
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
                    ("Node", agent["node_id"].as_str().unwrap_or("").into()),
                    ("Agent ID", agent["id"].as_str().unwrap_or("").into()),
                    ("Prompt", String::new()),
                ],
            );
            return;
        }
        self.request("operation".into(),"agent.operation",json!({"node_id":agent["node_id"],"agent_id":agent["id"],"operation":format!("agent.{operation}"),"options":{"json":true}}));
        self.status = format!(
            "Submitted {operation} to {}:{}",
            self.node_label(agent["node_id"].as_str().unwrap_or("")),
            agent["id"]
        );
    }
    fn action(&mut self, action: &str) {
        match action {
            "new" => self.new_agent(),
            "nodes" => self.dialog = Some(Dialog::Nodes),
            "settings" => self.dialog = Some(Dialog::Settings),
            "help" => self.dialog = Some(Dialog::Help { scroll: 0 }),
            "filters" => { self.filter_focus = false; self.dialog = Some(Dialog::Filters); }
            "filter.node" => self.choose_filter(FilterKind::Node),
            "filter.tool" => self.choose_filter(FilterKind::Tool),
            "filter.status" => self.choose_filter(FilterKind::Status),
            "filter.clear" => self.clear_filters(),
            "dismiss" => {
                self.dialog = match self.dialog.take() {
                    Some(Dialog::ChooseNode { form, .. }) => Some(*form),
                    Some(Dialog::ChooseFilter { return_to_panel: true, .. }) => Some(Dialog::Filters),
                    _ => None,
                };
            }
            "form.submit" => self.submit_form(),
            "node.add" => self.node_form(false),
            "node.advanced" => self.advanced_node_form(false),
            "node.edit" => self.node_form(true),
            "node.details" => {
                self.dialog = Some(Dialog::Info {
                    title: "Node details".into(),
                    text: format!(
                        "{}\n\nServer ID: {}",
                        self.node_label(&self.node),
                        self.node
                    ),
                    scroll: 0,
                });
            }
            "job.add" => self.job_form(false),
            "job.edit" => self.job_form(true),
            "node.connect" | "node.disconnect" | "node.remove" => {
                if action == "node.connect" {
                    let connection = self.snapshot["connections"].as_array().into_iter().flatten()
                        .find(|e| e["node_id"] == self.node).cloned()
                        .and_then(|e| serde_json::from_value::<cam_server::relay::Endpoint>(e).ok())
                        .as_ref().and_then(crate::ssh::profile);
                    if let Some(connection) = connection {
                        self.pending_ssh = Some((connection, "node.connect".into()));
                        return;
                    }
                }
                self.request("command".into(), action, json!({"node_id":self.node}));
            }
            "job.toggle" | "job.remove" => {
                self.request("command".into(), action, json!({"id":self.job}))
            }
            "job.history" => self.request("history".into(), action, json!({"id":self.job})),
            "health.details" => self.request("health".into(), "health", json!({})),
            "tabs.prev" | "tabs.next" => {
                let tabs = self.visible_tabs();
                if let Some(index) = tabs.iter().position(|i| *i == self.active) {
                    let next = if action == "tabs.prev" { index.saturating_sub(1) } else { (index+1).min(tabs.len()-1) };
                    self.activate(tabs[next]);
                }
            }
            "terminals.new" | "terminals.refresh" => {
                if let Some(agent) = self.tabs.get(self.active).map(|t| t.agent.clone()) {
                    if !self.pane_capable(&agent) {
                        self.status = "Update this Node's CAM Server/SDK to use terminal tabs".into();
                    } else if action == "terminals.refresh" { self.refresh_panes(&agent); }
                    else if self.creating_terminal.insert(agent_key(&agent)) {
                        self.request(Self::pane_kind("panes+", &agent), "terminal.create",
                            json!({"node_id":agent["node_id"],"agent_id":agent["id"],"launch_id":agent["launch_id"]}));
                        self.status = "Creating terminal…".into();
                    }
                }
            }
            "bottom" => {
                if let Some(t) = self.active_tab() {
                    t.model.bottom();
                }
            }
            _ => {
                if let Some(key) = action.strip_prefix("menu:") {
                    if let Some(agent) = self.agents().into_iter().find(|a|
                        format!("{}:{}", a["node_id"].as_str().unwrap_or(""), a["id"].as_str().unwrap_or("")) == key) {
                        self.menu_position = self.hits.iter().find(|(_, action)| action == &format!("menu:{key}"))
                            .map(|(r, _)| (r.x, r.y)).unwrap_or((27, 6));
                        self.filter_focus = false;
                        self.dialog = Some(Dialog::Menu(agent));
                    }
                    return;
                }
                if let Some(index) = action.strip_prefix("pick-filter:").and_then(|v| v.parse().ok()) {
                    self.select_filter(index);
                    return;
                }
                if let Some(index) = action
                    .strip_prefix("pick-node:")
                    .and_then(|v| v.parse::<usize>().ok())
                {
                    self.select_node(index);
                    return;
                }
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
                        self.activate(index);
                    }
                }
                if let Some(index) = action
                    .strip_prefix("close:")
                    .and_then(|s| s.parse::<usize>().ok())
                {
                    if index < self.tabs.len() {
                        let tab = &self.tabs[index];
                        if tab.primary {
                            self.status = "Agent terminal is fixed; use Stop or Kill in the agent menu".into();
                        } else {
                            self.request(Self::pane_kind("panes-", &tab.agent), "terminal.close",
                                json!({"node_id":tab.agent["node_id"],"agent_id":tab.agent["id"],"pane_id":tab.pane,"launch_id":tab.agent["launch_id"]}));
                        }
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
                    let node_field = matches!(&self.dialog, Some(Dialog::Form { fields, .. }) if fields.get(index).map(|v| v.0.as_str()) == Some("Node"));
                    if let Some(Dialog::Form { focus, .. }) = &mut self.dialog {
                        *focus = index;
                    }
                    if node_field {
                        self.choose_node(index);
                    }
                }
                if let Some(op) = action.strip_prefix("op:") {
                    if let Some(Dialog::Menu(agent) | Dialog::More(agent)) = &self.dialog {
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
            if let Some((action, target)) = kind.split_once(':').filter(|(action, _)| ["panes", "panes+", "panes-"].contains(action)) {
                if let Some((launch, group)) = target.split_once('|') {
                    if action == "panes+" { self.creating_terminal.remove(group); }
                    let agent = self.tabs.iter().find(|t| t.primary && agent_key(&t.agent) == group
                        && t.agent["launch_id"].as_str().unwrap_or("") == launch).map(|t| t.agent.clone());
                    if let Some(agent) = agent {
                        match result {
                            Ok(info) => {
                                let selected = self.tabs.get(self.active).map(|t| agent_key(&t.agent) == group).unwrap_or(false);
                                self.sync_panes(agent, &info);
                                if action == "panes+" { self.status = "Terminal created".into(); }
                                else if action == "panes-" { self.status = "Terminal closed".into(); }
                                if action == "panes+" && selected {
                                    if let Some(index) = self.tabs.iter().position(|t| agent_key(&t.agent) == group && t.pane.as_deref() == info["pane_id"].as_str()) {
                                        self.activate(index);
                                    }
                                } else if selected && self.tabs.get(self.active).map(|t| t.tid.is_none() && !t.pending).unwrap_or(false) {
                                    self.activate(self.active);
                                }
                            }
                            Err(err) => self.status = err,
                        }
                    }
                }
                continue;
            }
            if kind == "discard" { continue; }
            if kind == "visit" {
                self.last_snapshot = Instant::now() - Duration::from_secs(2);
                if let Err(err) = result { self.status = format!("Visit history: {err}"); }
                continue;
            }
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
                    let mut restarted = HashSet::new();
                    for tab in &mut self.tabs {
                        if let Some(agent) = self.snapshot["agents"].as_array().into_iter().flatten()
                            .find(|a| a["id"] == tab.agent["id"] && a["node_id"] == tab.agent["node_id"]) {
                            if !tab.agent["launch_id"].is_null() && tab.agent["launch_id"] != agent["launch_id"] {
                                restarted.insert(agent_key(agent));
                            }
                            tab.agent = agent.clone();
                        }
                    }
                    if !restarted.is_empty() {
                        let keys = self.tabs.iter().filter(|t| !t.primary && restarted.contains(&agent_key(&t.agent)))
                            .map(|t| t.key.clone()).collect();
                        self.discard_tabs(&keys);
                        let mut old = vec![];
                        for tab in self.tabs.iter_mut().filter(|t| restarted.contains(&agent_key(&t.agent))) {
                            if let Some(tid) = tab.tid.take() { old.push(tid); }
                            tab.attach_generation = id();
                            tab.pending = false;
                            tab.error = Some("Agent restarted; resynchronizing its terminal".into());
                            tab.retry_after = Instant::now();
                        }
                        for tid in old { self.request("discard".into(), "terminal.detach", json!({"terminal_id":tid})); }
                    }
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
                let mut refresh = None;
                if let Some(t) = self.tabs.iter_mut().find(|t| t.tid.as_deref() == Some(tid)) {
                    t.pending = false;
                    match result {
                        Ok(output) => {
                            t.read_only = output["read_only"] == true;
                            t.resize_read_only = output["resize_read_only"] == true;
                            if !t.model.paused {
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
                                if !t.primary { refresh = Some(t.agent.clone()); }
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
                        Err(err) => { if !t.primary { refresh = Some(t.agent.clone()); } t.error = Some(err); }
                    }
                }
                if let Some(agent) = refresh { self.refresh_panes(&agent); }
                continue;
            }
            match result {
                Ok(result) if kind == "attributes" => {
                    let agent = &result["agent"];
                    if let Some(current) = self.snapshot["agents"].as_array_mut().into_iter().flatten()
                        .find(|a| a["id"] == agent["id"] && a["node_id"] == agent["node_id"]) {
                        *current = agent.clone();
                    }
                    for tab in &mut self.tabs {
                        if tab.agent["id"] == agent["id"] && tab.agent["node_id"] == agent["node_id"] {
                            tab.agent = agent.clone();
                        }
                    }
                    self.status = "Saved agent name and tags".into();
                    self.last_snapshot = Instant::now() - Duration::from_secs(2);
                }
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
                Ok(result) if kind == "paired" => {
                    if let Some(node) = result["node_id"].as_str() { self.node = node.into(); }
                    self.status = "SSH connection saved; connecting to CAM Server".into();
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
            if !t.initial && !t.read_only && !t.resize_read_only && t.geometry != (cols, rows) {
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
            Block::default().style(Style::default().bg(BG).fg(TEXT)),
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
        let compact = area.height < 24;
        let filters = Rect::new(1, 3, left - 2, if compact { 4 } else { 8 });
        f.render_widget(Block::default().borders(Borders::ALL).title("Filters /")
            .border_style(Style::default().fg(if self.filter_focus { ACCENT } else { HINT })), filters);
        let search = Rect::new(2, 4, left - 4, 1);
        f.render_widget(Paragraph::new(if self.filter.is_empty() && !self.filter_focus {
            "Search name, ID or #tag"
        } else { self.filter.as_str() }).style(Style::default().fg(if self.filter.is_empty() { HINT } else { TEXT })), search);
        self.hits.push((search, "filter".into()));
        if compact {
            self.button(f, Rect::new(2, 5, left - 4, 1), "Node / Tool / Status ▾", "filters", false);
        } else {
            for (row, kind, action) in [(6, FilterKind::Node, "filter.node"), (7, FilterKind::Tool, "filter.tool"), (8, FilterKind::Status, "filter.status")] {
                let label = format!("{:<7} {} ▾", kind.label(), self.filter_label(kind));
                self.button(f, Rect::new(2, row, left - 4, 1), &label, action, !self.filter_value(kind).is_empty());
            }
            self.button(f, Rect::new(2, 9, left - 4, 1), "Clear filters", "filter.clear", false);
        }
        let agents = self.agents();
        let list_top = if compact { 7 } else { 12 };
        self.agent_list = Rect::new(1, list_top, left - 2, area.height - list_top - 1);
        let total = self.snapshot["agents"].as_array().map(Vec::len).unwrap_or(0);
        let list_block = Block::default().borders(Borders::ALL)
            .title(format!("Agents  {}/{}", agents.len(), total)).border_style(Style::default().fg(HINT));
        let body = list_block.inner(self.agent_list);
        f.render_widget(list_block, self.agent_list);
        let visible = body.height as usize / 2;
        self.list_offset = self.list_offset.min(agents.len().saturating_sub(visible.max(1)));
        for (i, agent) in agents.iter().skip(self.list_offset).take(visible).enumerate() {
            let y = body.y + i as u16 * 2;
            let key = format!("{}:{}", agent["node_id"].as_str().unwrap_or(""), agent["id"].as_str().unwrap_or(""));
            let selected = self.tabs.get(self.active).map(|t| agent_key(&t.agent) == key).unwrap_or(false);
            let alive = agent["status"] == "alive";
            let color = if agent["stale"] == true { HINT } else if !alive { ERROR }
                else if agent["state"] == "idle" { ACCENT } else { WARNING };
            let background = if selected { Color::Rgb(38, 53, 62) } else { PANEL };
            let base = Style::default().bg(background);
            f.render_widget(Block::default().style(base), Rect::new(body.x, y, body.width, 2));
            f.render_widget(Paragraph::new(if alive { "●" } else { "○" }).style(base.fg(color)), Rect::new(body.x, y, 1, 1));
            f.render_widget(Paragraph::new(elide(agent["name"].as_str().unwrap_or("agent"), body.width.saturating_sub(5)))
                .style(base.fg(TEXT).add_modifier(Modifier::BOLD)), Rect::new(body.x + 2, y, body.width.saturating_sub(5), 1));
            self.hits.push((Rect::new(body.x, y, body.width, 2), format!("agent:{key}")));
            let menu = Rect::new(body.x + body.width - 3, y, 3, 1);
            f.render_widget(Paragraph::new(" … ").style(base.fg(MUTED)), menu);
            self.hits.push((menu, format!("menu:{key}")));
            let node_width = (body.width / 3).min(12);
            let detail_width = body.width.saturating_sub(node_width + 1);
            let detail = format!("{:<6} · {}", elide(agent["tool"].as_str().unwrap_or("tool"), 6),
                if agent["stale"] == true { "stale" } else { Self::agent_status(agent) });
            f.render_widget(Paragraph::new(elide(&detail, detail_width)).style(base.fg(HINT)),
                Rect::new(body.x, y + 1, detail_width, 1));
            let node = self.short_node(agent["node_id"].as_str().unwrap_or(""));
            f.render_widget(Paragraph::new(elide(&node, node_width)).alignment(Alignment::Right).style(base.fg(HINT)),
                Rect::new(body.x + body.width - node_width, y + 1, node_width, 1));
        }
        if agents.is_empty() && body.height >= 2 {
            f.render_widget(Paragraph::new("No matching agents\nClear filters or choose New").style(Style::default().fg(HINT)), body);
        }
        let show_help = right >= 42;
        if show_help { self.button(f, Rect::new(area.width - 9, 1, 8, 1), " Help F4", "help", false); }
        let tabs = self.visible_tabs();
        let selected = tabs.iter().position(|i| *i == self.active).unwrap_or(0);
        let visible = ((right.saturating_sub(if show_help { 26 } else { 16 })) / 19).max(1) as usize;
        let start = (selected / visible) * visible;
        if start > 0 {
            self.button(f, Rect::new(left + 1, 1, 2, 1), "‹", "tabs.prev", false);
        }
        if start + visible < tabs.len() {
            self.button(
                f,
                Rect::new(area.width - if show_help { 22 } else { 12 }, 1, 2, 1),
                "›",
                "tabs.next",
                false,
            );
        }
        let mut x = left + 4;
        for &i in tabs.iter().skip(start).take(visible) {
            let title = &self.tabs[i].title;
            let label = format!(" {} ", title.chars().take(12).collect::<String>());
            let width = 14.min(area.width.saturating_sub(x + if show_help { 12 } else { 3 }));
            self.button(
                f,
                Rect::new(x, 1, width, 1),
                &label,
                &format!("tab:{i}"),
                self.active == i,
            );
            if !self.tabs[i].primary {
                self.button(f, Rect::new(x + width, 1, 2, 1), "× ", &format!("close:{i}"), false);
            }
            x += 19;
        }
        if !tabs.is_empty() {
            self.button(f, Rect::new(area.width - if show_help { 18 } else { 11 }, 1, 3, 1), " ↻ ", "terminals.refresh", false);
            self.button(f, Rect::new(area.width - if show_help { 14 } else { 7 }, 1, 3, 1), " + ", "terminals.new", false);
        }
        let term = Rect::new(left, 3, right, area.height - 4);
        let title = self.tabs.get(self.active).map(|t| format!("{} · {} · {}", t.agent["name"].as_str().unwrap_or("agent"), t.title,
            self.short_node(t.agent["node_id"].as_str().unwrap_or("")))).unwrap_or_else(|| "Agent terminal".into());
        f.render_widget(
            Block::default()
                .borders(Borders::ALL)
                .title(title),
            term,
        );
        self.main = Rect::new(term.x + 1, term.y + 1, term.width - 2, term.height - 2);
        if let Some(tab) = self.tabs.get(self.active) {
            f.render_widget(View(&tab.model), self.main);
            if tab.read_only {
                f.render_widget(
                    Paragraph::new("Read-only: another view controls input/size")
                        .style(Style::default().fg(WARNING)),
                    Rect::new(self.main.x, self.main.y, self.main.width, 1),
                );
            }
            if let Some(error) = &tab.error {
                f.render_widget(
                    Paragraph::new(error.as_str()).style(Style::default().fg(ERROR)),
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
            f.render_widget(Paragraph::new(Self::guide()).wrap(Wrap { trim: false })
                .style(Style::default().fg(TEXT)), self.main);
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
            Paragraph::new(status).style(Style::default().fg(MUTED).bg(PANEL)),
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
        let attributes = matches!(&dialog, Dialog::Form { kind, .. } if kind == "agent.settings");
        let width = area.width.saturating_sub(6).min(if attributes { 76 } else { 100 });
        let height = area.height.saturating_sub(if attributes { 2 } else { 4 }).min(if attributes { 12 } else { 27 });
        let rect = if matches!(dialog, Dialog::Menu(_)) {
            let width = area.width.saturating_sub(4).min(36);
            let height = area.height.saturating_sub(2).min(9);
            Rect::new(self.menu_position.0.min(area.width - width - 1),
                (self.menu_position.1 + 1).min(area.height - height - 1), width, height)
        } else { Rect::new((area.width - width) / 2, (area.height - height) / 2, width, height) };
        f.render_widget(Clear, rect);
        f.render_widget(
            Block::default()
                .borders(Borders::ALL)
                .style(Style::default().bg(PANEL).fg(TEXT)),
            rect,
        );
        self.hits.clear();
        let inner = Rect::new(rect.x + 2, rect.y + 1, rect.width - 4, rect.height - 2);
        let close = Rect::new(rect.x + rect.width - 4, rect.y, 3, 1);
        self.button(f, close, " × ", "dismiss", false);
        match &dialog {
            Dialog::Help { scroll } => {
                f.render_widget(Paragraph::new(Self::guide()).wrap(Wrap { trim: false })
                    .style(Style::default().fg(TEXT)).scroll((*scroll, 0)), inner);
            }
            Dialog::Filters => {
                f.render_widget(Paragraph::new("Filters · combine Node, Tool and Status")
                    .style(Style::default().fg(ACCENT)), Rect::new(inner.x, inner.y, inner.width, 1));
                for (row, kind, action) in [(1, FilterKind::Node, "filter.node"), (2, FilterKind::Tool, "filter.tool"), (3, FilterKind::Status, "filter.status")] {
                    self.button(f, Rect::new(inner.x, inner.y + row, inner.width, 1),
                        &format!("{:<7} {} ▾", kind.label(), self.filter_label(kind)), action, !self.filter_value(kind).is_empty());
                }
                self.button(f, Rect::new(inner.x, inner.y + 5, inner.width, 1), " Clear filters ", "filter.clear", false);
            }
            Dialog::ChooseFilter { kind, selected, options, .. } => {
                f.render_widget(Paragraph::new(format!("{} filter · ↑/↓ Enter · Esc back", kind.label()))
                    .style(Style::default().fg(ACCENT)), Rect::new(inner.x, inner.y, inner.width, 1));
                let visible = inner.height.saturating_sub(3).max(1) as usize;
                let start = selected.saturating_sub(visible - 1);
                for (row, (index, (_, label))) in options.iter().enumerate().skip(start).take(visible).enumerate() {
                    self.button(f, Rect::new(inner.x, inner.y + 2 + row as u16, inner.width, 1),
                        &format!("{} {}", if index == *selected { "›" } else { " " }, label),
                        &format!("pick-filter:{index}"), index == *selected);
                }
            }
            Dialog::Form {
                kind,
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
                if kind == "node.ssh" {
                    f.render_widget(Paragraph::new("Passwords / key passphrases are prompted by SSH; never saved.")
                        .style(Style::default().fg(HINT)), Rect::new(inner.x, inner.y+1, inner.width, 1));
                }
                for (i, (label, value)) in fields.iter().enumerate() {
                    let y = inner.y + 2 + i as u16 * 2;
                    if y + 1 >= rect.y + rect.height - 2 {
                        break;
                    }
                    let field = Rect::new(inner.x, y, inner.width, 2);
                    let display = if label == "Node" {
                        format!("{}  ▾", self.node_label(value))
                    } else {
                        value.clone()
                    };
                    f.render_widget(
                        Paragraph::new(format!("{label}\n{display}")).style(
                            Style::default().fg(if i == *focus { ACCENT } else { MUTED }),
                        ),
                        field,
                    );
                    self.hits.push((field, format!("field:{i}")));
                    if i == *focus && label != "Node" {
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
            Dialog::ChooseNode {
                selected, nodes, ..
            } => {
                f.render_widget(
                    Paragraph::new("Choose machine · ↑/↓ Enter · Esc back")
                        .style(Style::default().fg(ACCENT)),
                    Rect::new(inner.x, inner.y, inner.width, 1),
                );
                let visible = inner.height.saturating_sub(3).max(1) as usize;
                let start = selected.saturating_sub(visible - 1);
                if nodes.is_empty() {
                    f.render_widget(
                        Paragraph::new("No machines configured. Add a connection in Nodes."),
                        Rect::new(inner.x, inner.y + 2, inner.width, 2),
                    );
                }
                for (row, (index, node)) in nodes
                    .iter()
                    .enumerate()
                    .skip(start)
                    .take(visible)
                    .enumerate()
                {
                    let ready = self.snapshot["nodes"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .any(|n| n["node_id"] == *node && n["ready"] == true);
                    let label = format!(
                        "{} {} · {}",
                        if index == *selected { "›" } else { " " },
                        self.node_label(node),
                        if ready { "connected" } else { "offline" }
                    );
                    self.button(
                        f,
                        Rect::new(inner.x, inner.y + 2 + row as u16, inner.width, 1),
                        &label,
                        &format!("pick-node:{index}"),
                        index == *selected,
                    );
                }
            }
            Dialog::Menu(agent) => {
                f.render_widget(Paragraph::new(elide(agent["name"].as_str().unwrap_or("agent"), inner.width))
                    .style(Style::default().fg(TEXT).add_modifier(Modifier::BOLD)), Rect::new(inner.x, inner.y, inner.width, 1));
                for (row, label, op) in [(2, "Settings · name / tags", "settings"), (3, "Stop tool", "stop"),
                    (4, "Remove agent", "remove"), (6, "More actions…", "more")] {
                    let button = Rect::new(inner.x, inner.y + row, inner.width, 1);
                    self.button(f, button, label, &format!("op:{op}"), false);
                    f.render_widget(Paragraph::new(label).style(Style::default().fg(
                        if op == "stop" || op == "remove" { ERROR } else { TEXT }).bg(PANEL)), button);
                }
            }
            Dialog::More(agent) => {
                f.render_widget(
                    Paragraph::new(format!(
                        "{} · {}",
                        agent["name"].as_str().unwrap_or("agent"),
                        self.node_label(agent["node_id"].as_str().unwrap_or(""))
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
                    ("Kill session", "kill"),
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
                        self.node_label(identity),
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
                        .style(Style::default().fg(MUTED)),
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
                    ("Details", "node.details"),
                ]
                .iter()
                .enumerate()
                {
                    let x = inner.x + i as u16 * 14;
                    if x + 12 < rect.x + rect.width {
                        self.button(f, Rect::new(x, y, 12, 1), label, action, false);
                    }
                }
                self.button(f, Rect::new(inner.x, y + 1, 20.min(inner.width), 1),
                    "Local / advanced", "node.advanced", false);
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
                    .style(Style::default().fg(MUTED)),
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
                        self.action("dismiss");
                        return true;
                    }
                    if matches!(self.dialog, Some(Dialog::ChooseFilter { .. })) {
                        if let Some(Dialog::ChooseFilter { selected, options, .. }) = &mut self.dialog {
                            match key.code {
                                KeyCode::Up => *selected = selected.saturating_sub(1),
                                KeyCode::Down => *selected = (*selected + 1).min(options.len().saturating_sub(1)),
                                KeyCode::Enter => { let index = *selected; self.select_filter(index); }
                                _ => {}
                            }
                        }
                        return true;
                    }
                    if matches!(self.dialog, Some(Dialog::ChooseNode { .. })) {
                        if let Some(Dialog::ChooseNode {
                            selected, nodes, ..
                        }) = &mut self.dialog
                        {
                            let count = nodes.len();
                            match key.code {
                                KeyCode::Up => *selected = selected.saturating_sub(1),
                                KeyCode::Down => {
                                    *selected = (*selected + 1).min(count.saturating_sub(1))
                                }
                                KeyCode::Enter => {
                                    let index = *selected;
                                    self.select_node(index);
                                }
                                _ => {}
                            }
                        }
                        return true;
                    }
                    if key.code == KeyCode::Enter && key.modifiers.contains(KeyModifiers::CONTROL) {
                        self.submit_form();
                        return true;
                    }
                    let node_field = match &self.dialog {
                        Some(Dialog::Form { fields, focus, .. }) if fields[*focus].0 == "Node" => {
                            Some(*focus)
                        }
                        _ => None,
                    };
                    if let Some(field) = node_field {
                        if matches!(key.code, KeyCode::Enter | KeyCode::Char(' ')) {
                            self.choose_node(field);
                            return true;
                        }
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
                            KeyCode::Backspace if node_field.is_none() => {
                                fields[*focus].1.pop();
                            }
                            KeyCode::Char(c)
                                if node_field.is_none()
                                    && !key.modifiers.contains(KeyModifiers::CONTROL) =>
                            {
                                fields[*focus].1.push(c)
                            }
                            _ => {}
                        }
                    } else if let Some(Dialog::Info { scroll, .. } | Dialog::Help { scroll }) = &mut self.dialog {
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
                        KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => self.filter.push(c),
                        _ => {}
                    }
                    self.list_offset = 0;
                    return true;
                }
                match key.code {
                    KeyCode::F(1) => self.new_agent(),
                    KeyCode::F(2) => self.dialog = Some(Dialog::Nodes),
                    KeyCode::F(3) => self.dialog = Some(Dialog::Settings),
                    KeyCode::F(4) => self.dialog = Some(Dialog::Help { scroll: 0 }),
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
                    if fields[*focus].0 != "Node" {
                        fields[*focus].1.push_str(&text);
                    }
                } else if self.dialog.is_some() {
                    // Dialog input must never leak into an attached tool.
                } else if self.filter_focus {
                    self.filter.push_str(&text);
                    self.list_offset = 0;
                } else {
                    self.paste(&text);
                }
            }
            Event::Mouse(mouse) => {
                if let Some(Dialog::ChooseFilter { selected, options, .. }) = &mut self.dialog {
                    match mouse.kind {
                        MouseEventKind::ScrollUp => { *selected = selected.saturating_sub(1); return true; }
                        MouseEventKind::ScrollDown => { *selected = (*selected + 1).min(options.len().saturating_sub(1)); return true; }
                        _ => {}
                    }
                }
                if let Some(Dialog::ChooseNode {
                    selected, nodes, ..
                }) = &mut self.dialog
                {
                    let node_count = nodes.len();
                    match mouse.kind {
                        MouseEventKind::ScrollUp => {
                            *selected = selected.saturating_sub(1);
                            return true;
                        }
                        MouseEventKind::ScrollDown => {
                            *selected = (*selected + 1).min(node_count.saturating_sub(1));
                            return true;
                        }
                        _ => {}
                    }
                }
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
                                        let key = format!("{}:{}", agent["node_id"].as_str().unwrap_or(""), agent["id"].as_str().unwrap_or(""));
                                        self.action(&format!("menu:{key}"));
                                    }
                                } else {
                                    self.action(&action);
                                }
                            }
                        }
                        MouseEventKind::ScrollDown if contains(self.agent_list, mouse.column, mouse.row) => {
                            self.list_offset = self.list_offset.saturating_add(1)
                        }
                        MouseEventKind::ScrollUp if contains(self.agent_list, mouse.column, mouse.row) => {
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
fn elide(text: &str, width: u16) -> String {
    if text.width() <= width as usize { return text.into(); }
    if width == 0 { return String::new(); }
    let mut result = String::new();
    let mut cells = 0;
    for ch in text.chars() {
        let next = ch.width().unwrap_or(0);
        if cells + next > width as usize - 1 { break; }
        result.push(ch);
        cells += next;
    }
    result.push('…');
    result
}
fn agent_key(agent: &Value) -> String {
    format!("{}:{}", agent["node_id"].as_str().unwrap_or(""), agent["id"].as_str().unwrap_or(""))
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
        if let Some((connection, action)) = app.pending_ssh.take() {
            disable_raw_mode()?;
            execute!(io::stdout(), DisableMouseCapture, DisableBracketedPaste, LeaveAlternateScreen)?;
            let result = crate::ssh::authenticate(&connection, &config.runtime_dir);
            enable_raw_mode()?;
            execute!(io::stdout(), EnterAlternateScreen, EnableMouseCapture, EnableBracketedPaste)?;
            terminal.clear()?;
            match result {
                Ok(()) if action == "node.connect" => app.request("command".into(), &action, json!({"node_id":connection.expected_node})),
                Ok(()) => {
                    app.status = "SSH authenticated; checking, setting up and connecting CAM Server…".into();
                    app.request("paired".into(), &action, serde_json::to_value(connection)?);
                }
                Err(err) => app.status = err.to_string(),
            }
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
    #[test]
    fn labels_fit_terminal_cells_without_splitting_wide_characters() {
        assert_eq!(elide("中文name", 5), "中文…");
        assert_eq!(elide("中文", 4), "中文");
        assert_eq!(elide("abc", 0), "");
        assert_eq!(elide("abc", 1), "…");
    }
    #[tokio::test]
    async fn sidebar_has_separate_regions_aligned_muted_details_and_a_menu() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        app.snapshot = json!({"connections":[{"node_id":"a","name":"SITE-A046","socket":"/tmp/a","token_file":"/tmp/token"}],
            "agents":[{"id":"one","node_id":"a","name":"中文 agent with a long name","tool":"claude","status":"alive","state":"idle","tags":["review"]}]});
        let mut terminal = Terminal::new(TestBackend::new(150, 38)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let buffer = terminal.backend().buffer();
        let row = |y| (0..32).map(|x| buffer.get(x, y).symbol.as_str()).collect::<String>();
        assert!(row(3).contains("Filters"));
        assert!(row(app.agent_list.y).contains("Agents  1/1"));
        let y = app.agent_list.y + 1;
        assert_eq!(buffer.get(4, y).symbol, "中");
        assert_eq!(buffer.get(6, y).symbol, "文");
        assert_eq!(buffer.get(28, y).symbol, "…");
        assert_eq!(buffer.get(2, y + 1).fg, HINT);
        assert_eq!(buffer.get(29, y + 1).symbol, "6");
        assert!(row(y + 1).contains("claude · idle"));
        assert_eq!(app.main.x, 33);
        app.filter = "#review".into();
        assert_eq!(app.agents().len(), 1);
        app.action("menu:a:one");
        assert!(matches!(app.dialog, Some(Dialog::Menu(_))));
        assert!(app.tabs.is_empty());
        terminal.draw(|f| app.draw(f)).unwrap();
        let text: String = terminal.backend().buffer().content.iter().map(|c| c.symbol.as_str()).collect();
        for label in ["Settings · name / tags", "Stop tool", "Remove agent", "More actions"] { assert!(text.contains(label)); }
        app.action("op:settings");
        assert!(matches!(app.dialog, Some(Dialog::Form { .. })));
        assert_eq!(app.editing_agent.as_ref().unwrap()["id"], "one");
        for (width, height) in [(150, 38), (60, 14), (48, 12)] {
            terminal.resize(Rect::new(0, 0, width, height)).unwrap();
            terminal.draw(|f| app.draw(f)).unwrap();
            let text: String = terminal.backend().buffer().content.iter().map(|c| c.symbol.as_str()).collect();
            assert!(text.contains("Tags (comma-separated)"));
            assert!(text.contains("Save"));
        }
    }
    #[tokio::test]
    async fn sidebar_filters_combine_and_clear_without_changing_identity() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        app.snapshot = json!({"connections":[
            {"node_id":"a","name":"farm.alpha","socket":"/tmp/a","token_file":"/tmp/token"},
            {"node_id":"b","command":["ssh","site-a-container-xterm-064.example.com"],"token_file":"/tmp/token"}],
            "agents":[
                {"id":"one","name":"review","node_id":"a","tool":"claude","status":"alive","state":"idle"},
                {"id":"two","name":"build","node_id":"b","tool":"codex","status":"alive","state":"running","phase":"initializing"},
                {"id":"three","name":"failed-test","node_id":"a","tool":"codex","status":"dead","state":"failed","stale":true}]});
        assert_eq!(app.short_node("a"), "farm.alpha");
        assert_eq!(app.short_node("b"), "site-a-064");
        app.filter = "site-a-064".into();
        assert_eq!(app.agents().len(), 1);
        app.clear_filters();
        app.filter_node = "a".into();
        app.filter_tool = "codex".into();
        app.filter_status = "failed".into();
        assert_eq!(app.agents()[0]["id"], "three");
        app.filter_status = "stale".into();
        assert_eq!(app.agents().len(), 1);
        app.filter_status = "alive".into();
        assert!(app.agents().is_empty());
        app.clear_filters();
        app.filter_status = "running".into();
        assert_eq!(app.agents()[0]["id"], "two");
        app.filter_status = "starting".into();
        assert_eq!(app.agents().len(), 1);
        app.clear_filters();
        assert_eq!(app.agents().len(), 3);
        app.choose_filter(FilterKind::Node);
        app.snapshot["connections"].as_array_mut().unwrap().reverse();
        app.select_filter(2);
        assert_eq!(app.filter_node, "b");
    }
    #[tokio::test]
    async fn guide_filter_controls_and_agent_details_render_with_color() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        app.snapshot = json!({"nodes":[],"connections":[
            {"node_id":"a","name":"SITE-A046","socket":"/tmp/a","token_file":"/tmp/token"}],
            "agents":[{"id":"one","name":"review","node_id":"a","tool":"claude","status":"alive","state":"idle"}]});
        let mut terminal = Terminal::new(TestBackend::new(150, 38)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let buffer = terminal.backend().buffer();
        let text: String = buffer.content.iter().map(|c| c.symbol.as_str()).collect();
        for label in ["HISTORY", "COPY & PASTE", "Ctrl+Q", "All nodes", "All tools", "All statuses", "claude · idle", "SITE-A046"] {
            assert!(text.contains(label), "missing {label}");
        }
        assert!(buffer.content.iter().any(|c| c.symbol == "H" && c.fg == ACCENT));
        app.event(Event::Key(event::KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(matches!(app.dialog, Some(Dialog::Help { .. })));
        app.action("dismiss");
        terminal.resize(Rect::new(0, 0, 60, 14)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        app.action("filters");
        app.action("filter.node");
        app.select_filter(1);
        assert_eq!(app.filter_node, "a");
        assert!(matches!(app.dialog, Some(Dialog::Filters)));
        terminal.draw(|f| app.draw(f)).unwrap();
    }
    #[tokio::test]
    async fn node_picker_shows_hostnames_and_preserves_routing_ids() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        let a = "0123456789abcdef0123456789abcdef";
        let b = "fedcba9876543210fedcba9876543210";
        app.snapshot = json!({"nodes":[{"node_id":a,"ready":true},{"node_id":b,"ready":false}],
            "connections":[{"node_id":a,"name":"SITE-A046","socket":"/tmp/a","token_file":"/tmp/token"},
                           {"node_id":b,"name":"SITE-C","socket":"/tmp/b","token_file":"/tmp/token"}],
            "agents":[{"node_id":b,"id":"11111111","name":"agent-one"}]});
        app.new_agent();
        let mut terminal = Terminal::new(TestBackend::new(120, 32)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol.as_str())
            .collect();
        assert!(text.contains("SITE-A046"));
        assert!(!text.contains(a));
        app.event(Event::Paste(b.into()));
        app.action("field:0");
        app.snapshot["connections"]
            .as_array_mut()
            .unwrap()
            .reverse();
        app.event(Event::Key(event::KeyEvent::new(
            KeyCode::Down,
            KeyModifiers::NONE,
        )));
        app.event(Event::Key(event::KeyEvent::new(
            KeyCode::Enter,
            KeyModifiers::NONE,
        )));
        match &app.dialog {
            Some(Dialog::Form { fields, .. }) => assert_eq!(fields[0].1, b),
            _ => panic!("picker must return to the form"),
        };
        app.action("field:0");
        app.event(Event::Key(event::KeyEvent::new(
            KeyCode::Esc,
            KeyModifiers::NONE,
        )));
        assert!(matches!(&app.dialog, Some(Dialog::Form { fields, .. }) if fields[0].1 == b));
        app.filter = "site-c".into();
        assert_eq!(app.agents().len(), 1);
    }
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
    async fn terminal_tabs_belong_to_one_agent_and_keep_the_agent_pane_fixed() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        let a = json!({"node_id":"a","id":"one","name":"alpha","launch_id":"first"});
        let b = json!({"node_id":"a","id":"two","name":"beta","launch_id":"other"});
        app.snapshot = json!({"agents":[a,b]});
        app.open(a.clone());
        app.creating_terminal.insert(agent_key(&a));
        app.status = "Creating terminal…".into();
        app.tx.send((App::pane_kind("panes+", &a), Ok(json!({"pane_id":"%1","panes":[
            {"pane_id":"%0","primary":true},{"pane_id":"%1","title":"Terminal 1"}]})))).unwrap();
        app.replies();
        assert!(!app.creating_terminal.contains(&agent_key(&a)));
        assert_eq!(app.status, "Terminal created");
        assert_eq!(app.visible_tabs().len(), 2);
        app.action("tab:1");
        assert_eq!(app.tabs[app.active].pane.as_deref(), Some("%1"));
        app.open(b.clone());
        assert_eq!(app.visible_tabs().len(), 1);
        app.open(a.clone());
        assert_eq!(app.tabs[app.active].pane.as_deref(), Some("%1"));
        let mut terminal = Terminal::new(TestBackend::new(150, 38)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let row: String = (32..150).map(|x| terminal.backend().buffer().get(x,1).symbol.as_str()).collect();
        assert!(row.contains("Terminal 0") && row.contains("Terminal 1"));
        assert!(!row.contains("alpha") && !row.contains("beta"));
        app.action("close:0");
        assert!(app.status.contains("fixed"));
        app.sync_panes(a.clone(), &json!({"panes":[{"pane_id":"%0","primary":true}]}));
        assert!(app.tabs[app.active].primary);
        assert_eq!(app.visible_tabs().len(), 1);
        let mut new = a;
        new["launch_id"] = json!("second");
        app.tx.send(("snapshot".into(), Ok(json!({"agents":[new,b]})))).unwrap();
        app.replies();
        assert!(app.tabs[app.active].primary);
        assert_eq!(app.tabs[app.active].agent["launch_id"], "second");
    }
    #[tokio::test]
    async fn add_node_uses_ssh_fields_with_optional_username_and_advanced_fallback() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        app.action("node.add");
        let Some(Dialog::Form { kind, fields, .. }) = &mut app.dialog else { panic!("SSH form required") };
        assert_eq!(kind, "node.ssh");
        assert_eq!(fields[2].1, "22");
        assert_eq!(fields[3].1, "");
        assert_eq!(fields[4].1, "");
        assert!(!fields.iter().any(|(label, _)| label.contains("Server ID") || label.contains("Token") || label.contains("JSON")));
        fields[1].1 = "farm.example".into();
        app.submit_form();
        let (connection, _) = app.pending_ssh.take().unwrap();
        assert!(connection.user.is_empty());
        assert_eq!(connection.port, 22);
        app.action("node.advanced");
        assert!(matches!(app.dialog, Some(Dialog::Form { kind, .. }) if kind == "node"));
    }
    #[tokio::test]
    async fn recent_visits_sort_across_nodes_without_following_runtime_activity() {
        let mut app = App::new(&Config::default(), tokio::runtime::Handle::current());
        app.snapshot = json!({"agents":[
            {"node_id":"a","id":"same","name":"Zulu","tool":"codex","updated_at":99999},
            {"node_id":"b","id":"same","name":"beta","tool":"claude","updated_at":1},
            {"node_id":"a","id":"other","name":"Alpha","tool":"codex","updated_at":2}],
            "recent_visits":{"b:same":10,"a:same":9}});
        let order = |app: &App| app.agents().iter().map(|a| (a["node_id"].clone(), a["id"].clone())).collect::<Vec<_>>();
        assert_eq!(order(&app), vec![(json!("b"),json!("same")),(json!("a"),json!("same")),(json!("a"),json!("other"))]);
        app.snapshot["agents"][0]["updated_at"] = json!(999999);
        app.snapshot["agents"][0]["state"] = json!("running");
        assert_eq!(app.agents()[0]["node_id"], "b");
        app.filter_tool = "codex".into();
        assert_eq!(app.agents()[0]["name"], "Zulu");
        app.snapshot["recent_visits"] = json!({});
        assert_eq!(app.agents()[0]["name"], "Alpha");
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
