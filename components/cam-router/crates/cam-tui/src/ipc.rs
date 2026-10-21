use cam_protocol::{error, id, read_frame, write_frame, Result};
use serde_json::{json, Value};
use std::{path::Path, time::Duration};
use tokio::{io::BufReader, net::UnixStream};

pub async fn request(socket: &Path, action: &str, args: Value) -> Result<Value> {
    request_id(socket, action, args, &id()).await
}
pub async fn request_id(socket: &Path, action: &str, args: Value, identity: &str) -> Result<Value> {
    tokio::time::timeout(Duration::from_secs(if action == "node.ssh_add" || action == "node.connect" { 200 } else { 80 }), async {
        let stream = UnixStream::connect(socket).await?;
        if stream.peer_cred()?.uid() != unsafe { libc::geteuid() } {
            return Err(error("foreign daemon UID"));
        }
        let (read, mut write) = stream.into_split();
        write_frame(
            &mut write,
            &json!({"version":1,"id":identity,"action":action,"args":args}),
        )
        .await?;
        let (answer, _) = read_frame(&mut BufReader::new(read)).await?;
        if answer["id"] != identity {
            return Err(error("daemon response ID mismatch"));
        }
        if let Some(message) = answer["error"].as_str() {
            return Err(error(message));
        }
        Ok(answer["data"].clone())
    })
    .await?
}
