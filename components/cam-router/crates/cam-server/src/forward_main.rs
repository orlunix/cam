#[tokio::main(worker_threads = 2)]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--help"] {
        println!("cam-forwarder --config FILE\nHeadless reference Client forwarding backend; no business return table.");
        return;
    }
    let result = async {
        if args.len() != 2 || args[0] != "--config" {
            return Err(cam_protocol::error("usage: cam-forwarder --config FILE"));
        }
        cam_server::relay::run(cam_server::relay::Config::load(std::path::Path::new(
            &args[1],
        ))?)
        .await
    }
    .await;
    if let Err(err) = result {
        eprintln!("cam-forwarder: {}", err);
        std::process::exit(2);
    }
}
