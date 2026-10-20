//! Node-local durable queues. A Client never owns this state.
use cam_protocol::{digest, encode, error, expiry, now, result, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{path::Path, sync::Mutex};

pub struct Store {
    db: Mutex<Connection>,
    max_messages: usize,
    max_bytes: usize,
}
#[derive(Clone)]
pub struct Pending {
    pub message: Value,
    pub raw: Vec<u8>,
    pub attempts: i64,
    pub last_client: String,
}
impl Store {
    pub fn open(path: &Path, max_messages: usize, max_bytes: usize) -> Result<Self> {
        let db = Connection::open(path)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS messages(source TEXT,id TEXT,digest TEXT,raw BLOB,direction TEXT,state TEXT,expires INTEGER,next INTEGER DEFAULT 0,attempts INTEGER DEFAULT 0,last_client TEXT DEFAULT '',error TEXT DEFAULT '',PRIMARY KEY(source,id,direction));
          CREATE TABLE IF NOT EXISTS results(request_id TEXT,result_id TEXT,kind TEXT,raw BLOB,PRIMARY KEY(request_id,result_id));
          CREATE TABLE IF NOT EXISTS answers(source TEXT,request_id TEXT,answer_id TEXT,PRIMARY KEY(source,request_id,answer_id));
          CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER);")?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
        Ok(Self {
            db: Mutex::new(db),
            max_messages,
            max_bytes,
        })
    }
    fn capacity(&self, db: &Connection, bytes: usize, completion: bool) -> Result<()> {
        let (count,size):(i64,i64)=db.query_row("SELECT count(*),coalesce(sum(length(raw)),0)+(SELECT coalesce(sum(length(raw)),0) FROM results) FROM messages",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
        // Reserve bounded space for acknowledgements/results of admitted work.
        // Admission stops at the configured budget; completion cannot grow
        // without bound or prevent a full queue from being inspected.
        let count_limit = if completion {
            self.max_messages.saturating_mul(3).saturating_add(16)
        } else {
            self.max_messages
        };
        let byte_limit = if completion {
            self.max_bytes
                .saturating_add(16 * cam_protocol::FRAME_LIMIT)
        } else {
            self.max_bytes
        };
        if count as usize >= count_limit || size as usize + bytes > byte_limit {
            return Err(error("QUEUE_FULL"));
        }
        Ok(())
    }
    fn maintenance(db: &Connection) -> Result<()> {
        db.execute("UPDATE messages SET state='expired',error='EXPIRED_UNCONFIRMED' WHERE state IN ('queued','sent') AND expires<=?",[now()])?;
        // Keep conflict/dedup tombstones longer than the maximum message TTL.
        db.execute("DELETE FROM results WHERE request_id IN (SELECT id FROM messages WHERE direction='out' AND expires<? AND state IN ('done','received','expired'))",[now()-7*86400])?;
        db.execute("DELETE FROM answers WHERE NOT EXISTS (SELECT 1 FROM messages WHERE messages.source=answers.source AND messages.id=answers.request_id AND direction='in' AND expires>=?)",[now()-7*86400])?;
        db.execute(
            "DELETE FROM messages WHERE expires<? AND state IN ('done','received','expired')",
            [now() - 7 * 86400],
        )?;
        Ok(())
    }
    pub fn save(&self, message: &Value, raw: &[u8], direction: &str) -> Result<bool> {
        let mut db = self.db.lock().unwrap();
        let tx = db.transaction()?;
        let source = message["source"]["node_id"].as_str().unwrap();
        let mid = message["message_id"].as_str().unwrap();
        let old: Option<String> = tx
            .query_row(
                "SELECT digest FROM messages WHERE source=? AND id=? AND direction=?",
                params![source, mid, direction],
                |r| r.get(0),
            )
            .optional()?;
        let hash = digest(message);
        if let Some(old) = old {
            if old != hash {
                return Err(error("MESSAGE_CONFLICT"));
            }
            return Ok(false);
        }
        Self::maintenance(&tx)?;
        self.capacity(
            &tx,
            raw.len(),
            message["operation"] == "operation.result"
                || message["operation"] == "operation.delivery",
        )?;
        tx.execute("INSERT INTO messages(source,id,digest,raw,direction,state,expires) VALUES(?,?,?,?,?,'queued',?)",params![source,mid,hash,raw,direction,expiry(message)?])?;
        tx.commit()?;
        Ok(true)
    }
    pub fn due(&self, direction: &str) -> Result<Vec<Pending>> {
        let db = self.db.lock().unwrap();
        Self::maintenance(&db)?;
        let mut s=db.prepare("SELECT raw,attempts,last_client FROM messages WHERE direction=? AND state IN ('queued','sent') AND next<=? AND expires>? ORDER BY next,id LIMIT 8")?;
        let items = s
            .query_map(params![direction, now(), now()], |r| {
                Ok((r.get::<_, Vec<u8>>(0)?, r.get(1)?, r.get(2)?))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        items
            .into_iter()
            .map(|(raw, attempts, last_client)| {
                Ok(Pending {
                    message: serde_json::from_slice(&raw)?,
                    raw,
                    attempts,
                    last_client,
                })
            })
            .collect()
    }
    pub fn mark(
        &self,
        message: &Value,
        direction: &str,
        state: &str,
        delay: i64,
        client: &str,
        reason: &str,
    ) -> Result<()> {
        self.db.lock().unwrap().execute("UPDATE messages SET state=?,next=?,attempts=attempts+1,last_client=?,error=? WHERE source=? AND id=? AND direction=?",params![state,now()+delay,client,reason,message["source"]["node_id"].as_str(),message["message_id"].as_str(),direction])?;
        Ok(())
    }
    pub fn received(&self, source: &str, mid: &str) -> Result<()> {
        self.db.lock().unwrap().execute(
            "UPDATE messages SET state='received' WHERE source=? AND id=? AND direction='out'",
            params![source, mid],
        )?;
        Ok(())
    }
    pub fn forward_error(&self, node: &str, mid: &str, client: &str, status: &str) -> Result<()> {
        self.db.lock().unwrap().execute("UPDATE messages SET state='queued',next=?,error=? WHERE source=? AND id=? AND direction='out' AND state='sent' AND last_client=?",params![now()+1,status,node,mid,client])?;
        Ok(())
    }
    pub fn original(&self, node: &str, mid: &str) -> Result<Option<Value>> {
        let raw: Option<Vec<u8>> = self
            .db
            .lock()
            .unwrap()
            .query_row(
                "SELECT raw FROM messages WHERE source=? AND id=? AND direction='out'",
                params![node, mid],
                |r| r.get(0),
            )
            .optional()?;
        raw.map(|r| serde_json::from_slice(&r).map_err(Into::into))
            .transpose()
    }
    pub fn record_result(&self, mid: &str, message: &Value, raw: &[u8]) -> Result<()> {
        let mut kind = message["payload"]["kind"].as_str().unwrap_or(
            if message["operation"] == "message.send" {
                "reply"
            } else {
                "result"
            },
        );
        if kind == "delivery" && message["payload"]["exit_code"] != 0 {
            kind = "error";
        }
        let db = self.db.lock().unwrap();
        let exists: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM results WHERE request_id=? AND result_id=?)",
            params![mid, message["message_id"].as_str()],
            |r| r.get(0),
        )?;
        if !exists {
            self.capacity(&db, raw.len(), true)?;
            db.execute(
                "INSERT INTO results VALUES(?,?,?,?)",
                params![mid, message["message_id"].as_str(), kind, raw],
            )?;
        }
        Ok(())
    }
    pub fn results(&self, mid: &str, reply: bool) -> Result<Vec<Vec<u8>>> {
        let db = self.db.lock().unwrap();
        let mut s=db.prepare("SELECT raw FROM results WHERE request_id=? AND (?=0 OR kind!='delivery') ORDER BY rowid")?;
        let rows = s
            .query_map(params![mid, reply as i64], |r| r.get(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }
    pub fn finish(&self, request: &Value, response: &Value) -> Result<()> {
        let raw = encode(response)?;
        let mut db = self.db.lock().unwrap();
        let tx = db.transaction()?;
        self.capacity(&tx, raw.len(), true)?;
        tx.execute("INSERT OR IGNORE INTO messages(source,id,digest,raw,direction,state,expires) VALUES(?,?,?,?,'out','queued',?)",params![response["source"]["node_id"].as_str(),response["message_id"].as_str(),digest(response),raw,expiry(response)?])?;
        tx.execute(
            "INSERT OR IGNORE INTO answers VALUES(?,?,?)",
            params![
                request["source"]["node_id"].as_str(),
                request["message_id"].as_str(),
                response["message_id"].as_str()
            ],
        )?;
        tx.execute(
            "UPDATE messages SET state='done' WHERE source=? AND id=? AND direction='in'",
            params![
                request["source"]["node_id"].as_str(),
                request["message_id"].as_str()
            ],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn receipt(&self, request: &Value, response: &Value) -> Result<()> {
        let raw = encode(response)?;
        self.save(response, &raw, "out")?;
        self.db.lock().unwrap().execute(
            "INSERT OR IGNORE INTO answers VALUES(?,?,?)",
            params![
                request["source"]["node_id"].as_str(),
                request["message_id"].as_str(),
                response["message_id"].as_str()
            ],
        )?;
        Ok(())
    }
    pub fn repeat_answers(&self, request: &Value, node: &str) -> Result<()> {
        self.db.lock().unwrap().execute("UPDATE messages SET state='queued',next=0 WHERE direction='out' AND source=? AND expires>? AND id IN (SELECT answer_id FROM answers WHERE source=? AND request_id=?)",params![node,now(),request["source"]["node_id"].as_str(),request["message_id"].as_str()])?;
        Ok(())
    }
    pub fn recover(&self) -> Result<()> {
        let requests: Vec<Value> = {
            let db = self.db.lock().unwrap();
            let mut s =
                db.prepare("SELECT raw FROM messages WHERE direction='in' AND state='executing'")?;
            let raw = s
                .query_map([], |r| r.get::<_, Vec<u8>>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            raw.into_iter()
                .map(|r| serde_json::from_slice(&r))
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        for request in requests {
            self.finish(
                &request,
                &result(
                    &request,
                    "",
                    "Server restarted during execution; outcome unknown\n",
                    2,
                    "unknown",
                ),
            )?;
        }
        Ok(())
    }
    pub fn echo(&self, request: &Value) -> Result<()> {
        let mut db = self.db.lock().unwrap();
        let tx = db.transaction()?;
        let state: String = tx.query_row(
            "SELECT state FROM messages WHERE source=? AND id=? AND direction='in'",
            params![
                request["source"]["node_id"].as_str(),
                request["message_id"].as_str()
            ],
            |r| r.get(0),
        )?;
        if state == "done" {
            return Ok(());
        }
        tx.execute(
            "INSERT INTO counters VALUES('echo',1) ON CONFLICT(name) DO UPDATE SET value=value+1",
            [],
        )?;
        let answer = result(
            request,
            request["payload"]["text"].as_str().unwrap_or(""),
            "",
            0,
            "ok",
        );
        let raw = encode(&answer)?;
        self.capacity(&tx, raw.len(), true)?;
        tx.execute("INSERT INTO messages(source,id,digest,raw,direction,state,expires) VALUES(?,?,?,?, 'out','queued',?)",params![answer["source"]["node_id"].as_str(),answer["message_id"].as_str(),digest(&answer),raw,expiry(&answer)?])?;
        tx.execute(
            "INSERT INTO answers VALUES(?,?,?)",
            params![
                request["source"]["node_id"].as_str(),
                request["message_id"].as_str(),
                answer["message_id"].as_str()
            ],
        )?;
        tx.execute(
            "UPDATE messages SET state='done' WHERE source=? AND id=? AND direction='in'",
            params![
                request["source"]["node_id"].as_str(),
                request["message_id"].as_str()
            ],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn snapshot(&self) -> Result<Value> {
        let db = self.db.lock().unwrap();
        let mut s =
            db.prepare("SELECT direction,state,count(*) FROM messages GROUP BY direction,state")?;
        let rows=s.query_map([],|r|Ok(json!({"direction":r.get::<_,String>(0)?,"state":r.get::<_,String>(1)?,"count":r.get::<_,i64>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        let count: i64 = db.query_row(
            "SELECT coalesce((SELECT value FROM counters WHERE name='echo'),0)",
            [],
            |r| r.get(0),
        )?;
        Ok(json!({"queues":rows,"echo_count":count}))
    }
    pub fn query(&self, node: &str, mid: &str) -> Result<Value> {
        let db = self.db.lock().unwrap();
        let row:Option<(String,i64,String)>=db.query_row("SELECT state,attempts,error FROM messages WHERE source=? AND id=? AND direction='out'",params![node,mid],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
        Ok(match row {
            Some((state, attempts, err)) => {
                json!({"message_id":mid,"state":state,"attempts":attempts,"error":err})
            }
            None => json!({"message_id":mid,"state":"not_found"}),
        })
    }
}
