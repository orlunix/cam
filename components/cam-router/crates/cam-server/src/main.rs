use cam_protocol::{error, id, read_frame, write_frame, Result};
use cam_server::{
    config::{self, Config},
    server,
};
use serde_json::json;
use std::{collections::HashMap, path::PathBuf};
use tokio::{io::BufReader, net::UnixStream};

fn options(args: &[String]) -> Result<HashMap<String, String>> {
    let mut values = HashMap::new();
    let mut i = 0;
    while i < args.len() {
        if !args[i].starts_with("--")
            || i + 1 >= args.len()
            || values
                .insert(args[i].clone(), args[i + 1].clone())
                .is_some()
        {
            return Err(error("expected unique --option value pairs"));
        }
        i += 2;
    }
    Ok(values)
}
async fn main_result() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args[0] == "--help" || args[0] == "-h" {
        println!("cam-server {}\n  version [--json] (also --version)\n  setup|update [--bundle FILE] [--config FILE]\n  init [--config FILE] [--node-id ID]\n  serve [--config FILE]\n  pair --client-id ID [--config FILE] (owner-only local pairing)\n  connect --socket PATH        (stdio transport for an existing Server)\n  status --socket PATH\n  query --socket PATH --message-id ID", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args[0] == "version" || args[0] == "--version" {
        if args.len() > 2 || (args.len() == 2 && args[1] != "--json") { return Err(error("version accepts only --json")); }
        if args.get(1).map(String::as_str) == Some("--json") {
            println!("{}", json!({"version":cam_server::VERSION,"target":format!("{}-unknown-linux-musl", std::env::consts::ARCH),"protocol":1}));
        } else { println!("cam-server {}", cam_server::VERSION); }
        return Ok(());
    }
    let opts = options(&args[1..])?;
    let allowed = match args[0].as_str() {
        "setup" | "update" => vec!["--bundle", "--config"],
        "init" => vec!["--config", "--node-id"],
        "serve" => vec!["--config"],
        "pair" => vec!["--config", "--client-id"],
        "connect" => vec!["--socket"],
        "status" => vec!["--socket", "--config"],
        "query" => vec!["--socket", "--message-id"],
        _ => return Err(error("unknown command")),
    };
    if opts.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(error("unknown option"));
    }
    match args[0].as_str() {
        "setup" | "update" => {
            let bundle = opts.get("--bundle").map(PathBuf::from).unwrap_or(std::env::current_exe()?.with_file_name("cam-server.bundle"));
            let request = json!({"bundle":bundle,"config":opts.get("--config"),"version":cam_server::VERSION});
            let output = std::process::Command::new("python3").args(["-c", cam_server::SETUP_SCRIPT, &request.to_string()]).output()?;
            if !output.status.success() {
                let code = serde_json::from_slice::<serde_json::Value>(&output.stdout).ok()
                    .and_then(|v| v["setup_error"].as_str().map(str::to_owned)).unwrap_or_else(|| "setup_failed".into());
                return Err(error(&format!("Server setup failed ({code}); verify the release bundle, Python 3.6+ and private Server log")));
            }
            print!("{}", String::from_utf8(output.stdout)?);
        }
        "pair" => {
            let path = opts.get("--config").map(PathBuf::from).map(Ok)
                .unwrap_or_else(config::default_config)?;
            let cfg = Config::load(&path)?;
            let client = opts.get("--client-id").ok_or_else(|| error("--client-id required"))?;
            let grant = cfg.pair(client)?;
            let token = String::from_utf8(config::private_read(&grant.token_file)?)?;
            let socket = cfg.runtime_dir.join("server.sock");
            let stream = UnixStream::connect(&socket).await?;
            if stream.peer_cred()?.uid() != config::uid() { return Err(error("foreign Server UID")); }
            let (read, mut write) = stream.into_split();
            write_frame(&mut write, &json!({"type":"hello","version":1,"role":"client","client_id":client,"token":token.trim()})).await?;
            let (hello, _) = tokio::time::timeout(std::time::Duration::from_secs(5), read_frame(&mut BufReader::new(read))).await??;
            if hello["type"] != "hello" || hello["node_id"] != cfg.node_id || hello["management"] != true {
                return Err(error("running Server must support owner pairing and grant management"));
            }
            // Consumed over an authenticated SSH stream, never displayed by UI.
            println!("{}", json!({"node_id":cfg.node_id,"token":token.trim(),"socket":socket,"binary":std::env::current_exe()?}));
        }
        "init" => {
            let path = opts
                .get("--config")
                .map(PathBuf::from)
                .map(Ok)
                .unwrap_or_else(config::default_config)?;
            if path.exists() {
                return Err(error("config already exists; refusing to overwrite"));
            }
            let mut cfg = Config::default();
            cfg.node_id = opts.get("--node-id").cloned().unwrap_or_else(id);
            cfg.alias = config::hostname()?;
            if !cam_protocol::valid_id(&cfg.node_id) {
                return Err(error("invalid Node ID"));
            }
            config::private_write(&path, &serde_json::to_vec_pretty(&cfg)?)?;
            println!("{}", path.display());
        }
        "serve" => {
            let path = opts
                .get("--config")
                .map(PathBuf::from)
                .map(Ok)
                .unwrap_or_else(config::default_config)?;
            server::serve(Config::load(&path)?).await?;
        }
        "connect" | "status" | "query" => {
            let socket = match opts.get("--socket") {
                Some(path) => PathBuf::from(path),
                None if args[0] == "status" => {
                    let config = opts.get("--config").map(PathBuf::from).map(Ok)
                        .unwrap_or_else(config::default_config)?;
                    Config::load(&config)?.runtime_dir.join("server.sock")
                }
                _ => return Err(error("--socket is required")),
            };
            let stream = UnixStream::connect(socket).await?;
            if stream.peer_cred()?.uid() != config::uid() {
                return Err(error("foreign Server UID"));
            }
            let (read, mut write) = stream.into_split();
            if args[0] == "connect" {
                use tokio::io::AsyncWriteExt;
                let mut read = read;
                let mut input = tokio::io::stdin();
                let mut output = tokio::io::stdout();
                let upload = async {
                    tokio::io::copy(&mut input, &mut write).await?;
                    write.shutdown().await
                };
                tokio::try_join!(upload, tokio::io::copy(&mut read, &mut output))?;
            } else {
                let mut reader = BufReader::new(read);
                write_frame(
                    &mut write,
                    &json!({"type":"hello","version":1,"role":"camc"}),
                )
                .await?;
                let _ = read_frame(&mut reader).await?;
                let frame = if args[0] == "status" {
                    json!({"type":"inspect"})
                } else {
                    json!({"type":"query","message_id":opts.get("--message-id").ok_or_else(||error("--message-id is required"))?})
                };
                write_frame(&mut write, &frame).await?;
                let (answer, _) = read_frame(&mut reader).await?;
                println!("{}", serde_json::to_string_pretty(&answer)?);
            }
        }
        _ => unreachable!(),
    }
    Ok(())
}
#[tokio::main(worker_threads = 2)]
async fn main() {
    if let Err(err) = main_result().await {
        eprintln!("cam-server: {}", err);
        std::process::exit(2);
    }
}
