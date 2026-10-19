//! A supervised stdio SDK worker; never dispatches business via camc argv.
use crate::config::{Backend as BackendConfig, Config};
use cam_protocol::{encode, error, read_frame, Result};
use serde_json::Value;
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncWriteExt, BufReader},
    process::{Child, Command},
    sync::{mpsc, oneshot},
};

type Reply = oneshot::Sender<Value>;
type Key = (String, String);
pub struct Backend {
    sender: mpsc::Sender<Value>,
    pending: Arc<Mutex<HashMap<Key, Reply>>>,
    process: Mutex<Child>,
}
impl Backend {
    pub async fn start(config: &BackendConfig, node: &Config) -> Result<Arc<Self>> {
        use std::os::unix::process::CommandExt;
        let mut command = std::process::Command::new(&config.python);
        command
            .args(["-m", "camc_pkg.server_worker"])
            .env("PYTHONPATH", &config.module_path)
            .env("CAM_SERVER_WORKER_NODE", &node.node_id)
            .env(
                "CAM_SERVER_WORKER_STATE",
                node.runtime_dir.join("operations.db"),
            )
            .env("CAM_SERVER_SOCKET", node.runtime_dir.join("server.sock"))
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null());
        command.process_group(0);
        let owner = unsafe { libc::getpid() };
        unsafe {
            command.pre_exec(move || {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getppid() != owner {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::BrokenPipe,
                        "Server exited before backend startup",
                    ));
                }
                Ok(())
            });
        }
        let mut process = Command::from(command).kill_on_drop(true).spawn()?;
        let mut input = process
            .stdin
            .take()
            .ok_or_else(|| error("backend stdin unavailable"))?;
        let output = process
            .stdout
            .take()
            .ok_or_else(|| error("backend stdout unavailable"))?;
        let mut reader = BufReader::new(output);
        let (hello, _) =
            tokio::time::timeout(Duration::from_secs(10), read_frame(&mut reader)).await??;
        if hello["type"] != "backend.ready"
            || hello["node_id"] != node.node_id
            || hello["version"] != 1
        {
            return Err(error("incompatible native backend"));
        }
        let (sender, mut requests) = mpsc::channel::<Value>(8);
        let pending: Arc<Mutex<HashMap<Key, Reply>>> = Arc::new(Mutex::new(HashMap::new()));
        let waiting = pending.clone();
        tokio::spawn(async move {
            while let Some(message) = requests.recv().await {
                let raw = match encode(&message) {
                    Ok(raw) => raw,
                    Err(_) => break,
                };
                if input.write_all(&raw).await.is_err() {
                    break;
                }
            }
            waiting.lock().unwrap().clear();
        });
        let waiting = pending.clone();
        tokio::spawn(async move {
            while let Ok((answer, _)) = read_frame(&mut reader).await {
                if let (Some(source), Some(mid)) = (
                    answer["source_node"].as_str(),
                    answer["request_id"].as_str(),
                ) {
                    if let Some(sender) = waiting
                        .lock()
                        .unwrap()
                        .remove(&(source.to_string(), mid.to_string()))
                    {
                        let _ = sender.send(answer);
                    }
                }
            }
            waiting.lock().unwrap().clear();
        });
        Ok(Arc::new(Self {
            sender,
            pending,
            process: Mutex::new(process),
        }))
    }
    pub async fn execute(&self, message: Value) -> Result<Value> {
        let mid = message["message_id"]
            .as_str()
            .ok_or_else(|| error("missing backend message ID"))?
            .to_string();
        let source = message["source"]["node_id"]
            .as_str()
            .ok_or_else(|| error("missing backend source Node"))?
            .to_string();
        let key = (source, mid);
        let (sender, receiver) = oneshot::channel();
        self.pending.lock().unwrap().insert(key.clone(), sender);
        let outcome = async {
            self.sender
                .send(message)
                .await
                .map_err(|_| error("backend unavailable"))?;
            let answer = tokio::time::timeout(Duration::from_secs(70), receiver).await??;
            if answer["type"] != "backend.result" {
                return Err(error("native backend failed; outcome unknown"));
            }
            Ok(answer["message"].clone())
        }
        .await;
        self.pending.lock().unwrap().remove(&key);
        outcome
    }
}
impl Drop for Backend {
    fn drop(&mut self) {
        if let Ok(process) = self.process.lock() {
            if let Some(pid) = process.id() {
                unsafe {
                    libc::kill(-(pid as i32), libc::SIGTERM);
                }
            }
        }
    }
}
