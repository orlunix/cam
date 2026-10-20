//! The shared message envelope. Relays keep the original encoded frame.
use chrono::{DateTime, TimeZone, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    io,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWrite, AsyncWriteExt};

pub const FRAME_LIMIT: usize = 4 * 1024 * 1024;
pub const AGENT_OPERATIONS: &[&str] = &[
    "message.send",
    "agent.send",
    "agent.capture",
    "agent.run",
    "agent.stop",
    "agent.remove",
    "agent.kill",
    "agent.reboot",
    "agent.key",
    "agent.status",
];
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub fn error(text: &str) -> Box<dyn std::error::Error + Send + Sync> {
    io::Error::new(io::ErrorKind::InvalidData, text).into()
}
pub fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}
pub fn id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}
pub fn stamp(seconds: i64) -> String {
    Utc.timestamp_opt(seconds, 0)
        .single()
        .expect("valid UTC timestamp")
        .format("%Y-%m-%dT%H:%M:%SZ")
        .to_string()
}
pub fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| error("missing string field"))
}
pub fn valid_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}
pub fn agent_id(value: &str) -> bool {
    valid_id(value) && !value.contains('.')
}
pub fn expiry(value: &Value) -> Result<i64> {
    let raw = text(value, "expires_at")?;
    if raw.len() != 20 || !raw.ends_with('Z') {
        return Err(error("invalid expiry"));
    }
    Ok(DateTime::parse_from_rfc3339(raw)?.timestamp())
}
pub fn validate(value: &Value) -> Result<()> {
    if value["version"].as_u64() != Some(1)
        || value["type"] != "message"
        || !valid_id(text(value, "message_id")?)
    {
        return Err(error("invalid message envelope"));
    }
    for field in ["reply_to", "thread_id"] {
        if !value[field].is_null() && !valid_id(text(value, field)?) {
            return Err(error("invalid correlation ID"));
        }
    }
    for side in ["source", "target"] {
        let node = text(&value[side], "node_id")?;
        if node.is_empty()
            || node.len() > 128
            || node.chars().any(char::is_whitespace)
            || node.chars().any(char::is_control)
        {
            return Err(error("invalid Node"));
        }
        if let Some(agent) = value[side].get("agent_id") {
            if !agent.as_str().map(agent_id).unwrap_or(false) {
                return Err(error("invalid agent ID"));
            }
        }
    }
    if !value["payload"].is_object() {
        return Err(error("invalid payload"));
    }
    let op = text(value, "operation")?;
    if AGENT_OPERATIONS.contains(&op) {
        if !value["target"]["agent_id"]
            .as_str()
            .map(agent_id)
            .unwrap_or(false)
        {
            return Err(error("specific agent ID required"));
        }
    } else if op == "test.echo" {
        if value["target"]["receiver_id"] != "test.echo" {
            return Err(error("invalid echo target"));
        }
    } else if op == "operation.result" || op == "operation.delivery" {
        let receiver = if op == "operation.result" {
            "operation.results"
        } else {
            "delivery.status"
        };
        if value["target"]["receiver_id"] != receiver || !value["reply_to"].is_string() {
            return Err(error("invalid internal result"));
        }
    } else {
        return Err(error("UNSUPPORTED_OPERATION"));
    }
    let end = expiry(value)?;
    if end <= now() || end > now() + 90000 {
        return Err(error("expired or excessive message deadline"));
    }
    Ok(())
}
pub fn encode(value: &Value) -> Result<Vec<u8>> {
    let mut raw = serde_json::to_vec(value)?;
    raw.push(b'\n');
    if raw.len() > FRAME_LIMIT {
        return Err(error("frame limit exceeded"));
    }
    Ok(raw)
}
pub fn digest(value: &Value) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).expect("JSON value"))
    )
}

pub async fn read_frame<R: AsyncBufRead + Unpin + ?Sized>(
    reader: &mut R,
) -> Result<(Value, Vec<u8>)> {
    let mut raw = Vec::new();
    loop {
        let available = reader.fill_buf().await?;
        if available.is_empty() {
            return Err(error("connection closed"));
        }
        let used = available
            .iter()
            .position(|b| *b == b'\n')
            .map(|n| n + 1)
            .unwrap_or(available.len());
        if raw.len() + used > FRAME_LIMIT {
            return Err(error("frame limit exceeded"));
        }
        let complete = available[used - 1] == b'\n';
        raw.extend_from_slice(&available[..used]);
        reader.consume(used);
        if complete {
            break;
        }
    }
    let value: Value = serde_json::from_slice(&raw)?;
    if !value.is_object() {
        return Err(error("object frame required"));
    }
    Ok((value, raw))
}
pub async fn write_frame<W: AsyncWrite + Unpin + ?Sized>(
    writer: &mut W,
    value: &Value,
) -> Result<()> {
    writer.write_all(&encode(value)?).await?;
    writer.flush().await?;
    Ok(())
}
pub fn result(request: &Value, stdout: &str, stderr: &str, code: u8, status: &str) -> Value {
    json!({"version":1,"type":"message","message_id":id(),"reply_to":request["message_id"],
        "source":request["target"],"target":{"node_id":request["source"]["node_id"],"receiver_id":"operation.results"},
        "operation":"operation.result","payload":{"status":status,"stdout":stdout,"stderr":stderr,"exit_code":code},
        "expires_at":stamp(now()+86400)})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn contract_and_digest() {
        let v = json!({"version":1,"type":"message","message_id":"a","source":{"node_id":"a"},
            "target":{"node_id":"b","agent_id":"12345678"},"operation":"agent.capture","payload":{},"expires_at":stamp(now()+60)});
        assert!(validate(&v).is_ok());
        let mut bad = v.clone();
        bad["target"]["agent_id"] = json!("*");
        assert!(validate(&bad).is_err());
        bad = v.clone();
        bad["operation"] = json!("agent.list");
        assert!(validate(&bad).is_err());
        assert_eq!(
            digest(&v),
            digest(&serde_json::from_slice::<Value>(&encode(&v).unwrap()).unwrap())
        );
    }
    #[tokio::test]
    async fn framing_is_bounded_and_preserves_bytes() {
        let raw = b" { \"type\" : \"message\", \"text\":\"\\u4e2d\" } \n";
        let mut r = &raw[..];
        let (_, same) = read_frame(&mut r).await.unwrap();
        assert_eq!(same, raw);
        let mut r = &b"{\"x\":NaN}\n"[..];
        assert!(read_frame(&mut r).await.is_err());
        let large = vec![b'x'; FRAME_LIMIT + 1];
        let mut r = &large[..];
        assert!(read_frame(&mut r).await.is_err());
    }
}
