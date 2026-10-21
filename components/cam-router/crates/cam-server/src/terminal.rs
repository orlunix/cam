//! Private tmux control clients. Raw pane output, never a nested attach TUI.
use crate::{backend::Backend, server::Channel};
use base64::{engine::general_purpose::STANDARD, Engine};
use cam_protocol::{error, id, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::Command,
    sync::mpsc,
};

enum Input {
    Data(Vec<u8>),
    Resize(u16, u16),
    Stop,
}
struct Attached {
    owner: String,
    agent: String,
    pane: String,
    primary: bool,
    input: mpsc::Sender<Input>,
}
#[derive(Default)]
pub struct Terminals {
    sessions: Mutex<HashMap<String, Attached>>,
}
impl Terminals {
    pub async fn open(
        self: &Arc<Self>,
        backend: &Arc<Backend>,
        owner: &str,
        args: &Value,
        channel: Channel,
    ) -> Result<Value> {
        let agent = args["agent_id"]
            .as_str()
            .ok_or_else(|| error("agent ID required"))?;
        {
            let sessions = self.sessions.lock().unwrap();
            if sessions.len() >= 32 {
                return Err(error("terminal capacity reached"));
            }
        }
        let info = backend
            .control("terminal.resolve", json!({"agent_id":agent,"pane_id":args["pane_id"],"launch_id":args["launch_id"]}))
            .await?;
        let argv: Vec<String> = serde_json::from_value(info["argv"].clone())?;
        let pane = info["pane"]
            .as_str()
            .ok_or_else(|| error("missing pane"))?
            .to_string();
        let session = info["session"]
            .as_str()
            .ok_or_else(|| error("missing session"))?;
        let window = info["window"].as_str().unwrap_or("").to_string();
        let rows = info["rows"].as_u64().unwrap_or(24);
        let cols = info["cols"].as_u64().unwrap_or(80);
        let mut child = Command::new(&argv[0])
            .args(&argv[1..])
            .args(["-C", "attach-session", "-t", session])
            .env_remove("TMUX")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true)
            .spawn()?;
        let mut write = child
            .stdin
            .take()
            .ok_or_else(|| error("control stdin missing"))?;
        let mut read = BufReader::new(
            child
                .stdout
                .take()
                .ok_or_else(|| error("control stdout missing"))?,
        );
        // A fresh control client otherwise defaults to 80x24 and constrains
        // every window in this session, including the agent's existing pane.
        write.write_all(format!("refresh-client -C {cols},{rows}\ncapture-pane -p -e -t {pane} -S -2000\ndisplay-message -p -t {pane} '#{{cursor_y}};#{{cursor_x}};#{{bracketed_paste_flag}}'\n").as_bytes()).await?;
        let tid = id();
        let (sender, mut input) = mpsc::channel(32);
        {
            let mut sessions = self.sessions.lock().unwrap();
            if sessions.values().any(|t| t.agent == agent && t.pane == pane) {
                return Err(error("terminal was concurrently attached"));
            }
            sessions.insert(
                tid.clone(),
                Attached {
                    owner: owner.into(),
                    agent: agent.into(),
                    pane: pane.clone(),
                    primary: info["primary"] == true,
                    input: sender,
                },
            );
        }
        let broker = self.clone();
        let identity = tid.clone();
        let backend = backend.clone();
        let probe_agent = agent.to_string();
        tokio::spawn(async move {
            let mut block = 0;
            let mut capture = Vec::new();
            let mut after = Vec::new();
            let mut cursor = String::new();
            let mut ready = false;
            let mut seq = 0u64;
            let mut line = Vec::new();
            let mut in_block = false;
            let mut block_end = Vec::new();
            let mut block_error = Vec::new();
            let outcome:Result<()>=async {loop {
                tokio::select! {
                    result=read.read_until(b'\n',&mut line)=>{
                        if result?==0 {break;}
                        if line.len()>4*1024*1024 {return Err(error("terminal frame exceeded limit"));}
                        if in_block&&!ready&&(block==3||block==4)&&line!=block_end&&line!=block_error {
                            // Command-result blocks are opaque. A tool may print
                            // literal %begin/%output text inside capture-pane.
                            if block==3{capture.extend(&line);if capture.len()>2*1024*1024{return Err(error("terminal history exceeded limit"));}}
                            else{cursor.push_str(&String::from_utf8_lossy(&line));}
                        } else if line.starts_with(b"%output ") {
                            let mut parts=line.splitn(3,|b|*b==b' ');parts.next();
                            if parts.next()==Some(pane.as_bytes()) {
                                let bytes=decode_output(parts.next().unwrap_or_default().strip_suffix(b"\n").unwrap_or_default())?;
                                if ready {channel.frame(&json!({"type":"terminal.output","terminal_id":identity,"seq":seq,"data":STANDARD.encode(&bytes)}))?;seq+=bytes.len()as u64;}
                                else if block>=3 {after.extend(bytes);if after.len()>1024*1024{return Err(error("terminal bootstrap overflow"));}}
                            }
                        } else if line.starts_with(b"%begin ") {block+=1;in_block=true;block_end=[b"%end ".as_slice(),&line[7..]].concat();block_error=[b"%error ".as_slice(),&line[7..]].concat();}
                        else if in_block&&line==block_error {return Err(error("tmux control command failed"));}
                        else if line.starts_with(b"%exit") || line.strip_prefix(b"%window-close ").map(|s|s.strip_suffix(b"\n").unwrap_or(s))==Some(window.as_bytes()) {break;}
                        else if in_block&&line==block_end {
                            in_block=false;
                            if block==4&&!ready {
                            let pos:Vec<_>=cursor.trim().split(';').collect();
                            let row=pos.first().and_then(|v|v.parse::<u16>().ok()).unwrap_or(0);
                            let col=pos.get(1).and_then(|v|v.parse::<u16>().ok()).unwrap_or(0);
                            let mut bracketed=pos.get(2)==Some(&"1");
                            if pos.get(2).map(|v|v.is_empty()).unwrap_or(true) {
                                // Older tmux cannot expose this mode as a format.
                                // Probe after the control client is listening, so
                                // subsequent mode changes remain in its output.
                                if let Ok(modes)=backend.control("terminal.resolve",json!({"agent_id":probe_agent,"pane_id":pane,"probe_modes":true})).await {
                                    bracketed=modes["bracketed_paste"]==true;
                                }
                            }
                            // Drop only capture-pane's framing newline. Blank
                            // screen rows keep history and the cursor aligned.
                            let captured=String::from_utf8_lossy(&capture);
                            let mut initial=captured.strip_suffix('\n').unwrap_or(&captured).replace('\n',"\r\n").into_bytes();
                            initial.extend(format!("\x1b[{};{}H{}",row+1,col+1,if bracketed{"\x1b[?2004h"}else{""}).as_bytes());
                            channel.frame(&json!({"type":"terminal.snapshot","terminal_id":identity,"seq":0,"rows":rows,"cols":cols,"data":STANDARD.encode(initial)}))?;
                            ready=true;
                            if !after.is_empty(){channel.frame(&json!({"type":"terminal.output","terminal_id":identity,"seq":seq,"data":STANDARD.encode(&after)}))?;seq+=after.len()as u64;after.clear();}
                            }
                        }
                        line.clear();
                    },
                    command=input.recv()=>{match command {
                        Some(Input::Data(bytes))=>{write.write_all(&input_commands(&pane,&bytes)).await?;},
                        // Control clients have no status line. Adding the
                        // normal client's status height gives the tool an
                        // extra row outside the visible CAM terminal.
                        Some(Input::Resize(cols,rows))=>{write.write_all(format!("refresh-client -C {cols},{rows}\n").as_bytes()).await?;},
                        _=>break,
                    }},
                    _=tokio::time::sleep(Duration::from_secs(5)),if !ready=>{return Err(error("terminal bootstrap timed out"));}
                }
            }Ok(())}.await;
            let closed = json!({"type":"terminal.closed","terminal_id":identity,"error":outcome.err().map(|e|e.to_string())});
            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            while channel.frame(&closed).is_err() {
                if channel.closed() || std::time::Instant::now() >= deadline {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
            broker.sessions.lock().unwrap().remove(&identity);
            let _ = child.kill().await;
            let _ = child.wait().await;
        });
        Ok(json!({"terminal_id":tid,"rows":rows,"cols":cols,"pane_id":info["pane"],"primary":info["primary"]}))
    }
    pub fn close_pane(&self, agent: &str, pane: &str) {
        self.sessions.lock().unwrap().retain(|_, t| {
            if t.agent == agent && t.pane == pane { let _ = t.input.try_send(Input::Stop); false } else { true }
        });
    }
    pub fn command(&self, owner: &str, action: &str, args: &Value) -> Result<Value> {
        let mut sessions = self.sessions.lock().unwrap();
        let tid = args["terminal_id"]
            .as_str()
            .ok_or_else(|| error("terminal ID required"))?;
        let terminal = sessions
            .get(tid)
            .filter(|t| t.owner == owner)
            .ok_or_else(|| error("terminal unavailable or owned by another Client"))?;
        let command = match action {
            "terminal.input" => {
                let data = STANDARD.decode(args["data"].as_str().unwrap_or(""))?;
                if data.is_empty() || data.len() > 65536 {
                    return Err(error("invalid terminal input size"));
                }
                Input::Data(data)
            }
            "terminal.resize" => {
                let cols = args["cols"].as_u64().unwrap_or(0);
                let rows = args["rows"].as_u64().unwrap_or(0);
                if !(2..=500).contains(&cols) || !(2..=200).contains(&rows) {
                    return Err(error("invalid terminal geometry"));
                }
                if sessions.values().any(|t| t.agent == terminal.agent && t.primary && t.owner != owner) {
                    return Err(error("terminal geometry is controlled by the agent view's Client"));
                }
                // tmux 2.7 uses the smallest attached client. All of this
                // Client's pane streams must agree on the session geometry.
                for sibling in sessions.values().filter(|t| t.agent == terminal.agent && t.owner == owner) {
                    sibling.input.try_send(Input::Resize(cols as u16, rows as u16))
                        .map_err(|_| error("terminal resize backpressure"))?;
                }
                return Ok(json!({"ok":true}));
            }
            "terminal.detach" => Input::Stop,
            _ => return Err(error("unsupported terminal action")),
        };
        terminal
            .input
            .try_send(command)
            .map_err(|_| error("terminal input backpressure"))?;
        if action == "terminal.detach" {
            sessions.remove(tid);
        }
        Ok(json!({"ok":true}))
    }
    pub fn disconnect(&self, owner: &str) {
        self.sessions
            .lock()
            .unwrap()
            .retain(|_, t| t.owner != owner);
    }
}

pub fn decode_output(raw: &[u8]) -> Result<Vec<u8>> {
    let mut output = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        if raw[i] == b'\\' {
            if i + 3 >= raw.len() || !raw[i + 1..i + 4].iter().all(|b| (b'0'..=b'7').contains(b)) {
                return Err(error("invalid tmux output escape"));
            }
            let value = ((raw[i + 1] - b'0') as u16) * 64
                + ((raw[i + 2] - b'0') as u16) * 8
                + (raw[i + 3] - b'0') as u16;
            if value > 255 {
                return Err(error("invalid tmux output byte"));
            }
            output.push(value as u8);
            i += 4;
        } else {
            output.push(raw[i]);
            i += 1;
        }
    }
    Ok(output)
}

/// tmux 2.7 has no send-keys -H. Quote one literal argument for its command
/// parser, including its second-stage trailing-semicolon handling. No shell.
pub fn input_commands(pane: &str, bytes: &[u8]) -> Vec<u8> {
    let mut commands = Vec::new();
    for (i, part) in bytes.split(|b| *b == 0).enumerate() {
        if i > 0 {
            commands.extend(format!("send-keys -t {pane} C-Space\n").as_bytes());
        }
        if part.is_empty() {
            continue;
        }
        commands.extend(format!("send-keys -t {pane} -l \"").as_bytes());
        for (index, b) in part.iter().enumerate() {
            if *b == b';' && index + 1 == part.len() {
                commands.extend(b"\\\\");
            }
            match b {
                b'\\' => commands.extend(b"\\\\"),
                b'"' => commands.extend(b"\\\""),
                b'$' => commands.extend(b"\\$"),
                b'\n' => commands.extend(b"\\n"),
                b'\r' => commands.extend(b"\\r"),
                b'\t' => commands.extend(b"\\t"),
                27 => commands.extend(b"\\e"),
                _ => commands.push(*b),
            }
        }
        commands.extend(b"\"\n");
    }
    commands
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn escaped_pane_bytes_are_not_shell_text() {
        assert_eq!(
            decode_output(b"hi\\015\\012\\033[31m\\134").unwrap(),
            b"hi\r\n\x1b[31m\\"
        );
        assert!(decode_output(b"\\999").is_err());
    }
    #[test]
    fn input_is_literal_on_old_tmux() {
        let commands = input_commands("%0", b"$HOME;\"\r\n\0;");
        assert_eq!(commands.iter().filter(|b| **b == b'\n').count(), 3);
        assert!(!commands.contains(&0));
        assert!(String::from_utf8(commands).unwrap().contains("\\$HOME"));
    }
}
