//! Simulate separate Linux Nodes in process tests on one host. Not shipped.
#[tokio::main(worker_threads = 2)]
async fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let outcome = async {
        if args.len() != 3 || args[0] != "serve" || args[1] != "--config" {
            return Err(cam_protocol::error(
                "test fixture requires serve --config FILE",
            ));
        }
        cam_server::server::serve_test_node(cam_server::config::Config::load(
            std::path::Path::new(&args[2]),
        )?)
        .await
    }
    .await;
    if let Err(err) = outcome {
        eprintln!("test Node: {}", err);
        std::process::exit(2);
    }
}
