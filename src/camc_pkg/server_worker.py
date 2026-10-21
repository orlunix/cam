"""Optional private stdio worker owned by the Rust CAM Server.

This is an SDK boundary, not a camc CLI or a second listening daemon. Business
messages go straight to the native adapter; only the Server owns routing.
"""
import concurrent.futures
import os
import signal
import sys
import threading

from camc_pkg.server_operations import CamOperationReceiver
from camc_pkg.server_protocol import cam_encode_frame, cam_read_frame


def main():
    # Management helpers also launch tmux subprocesses. Their inherited
    # stdin/stdout must never share the Server's framed control stream:
    # a child can consume requests or print unframed output into replies.
    incoming = os.fdopen(os.dup(sys.stdin.fileno()), "rb")
    outgoing = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    with open(os.devnull, "r+b", buffering=0) as null:
        os.dup2(null.fileno(), sys.stdin.fileno())
        os.dup2(null.fileno(), sys.stdout.fileno())

    def terminate(unused_signum, unused_frame):
        # This worker and its SDK children have a dedicated process group.
        # Tools in tmux and runtime monitors have their own sessions.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        os._exit(0)
    signal.signal(signal.SIGTERM, terminate)
    node = os.environ["CAM_SERVER_WORKER_NODE"]
    path = os.environ["CAM_SERVER_WORKER_STATE"]
    receiver = CamOperationReceiver(node, path)
    lock = threading.Lock()
    capacity = threading.BoundedSemaphore(8)

    def write(value):
        with lock:
            outgoing.write(cam_encode_frame(value))
            outgoing.flush()

    def execute(message):
        try:
            if message.get("type") == "backend.control":
                from camc_pkg.server_management import management_request
                try:
                    result = management_request(message["action"], message.get("args") or {},
                                                node, path, receiver)
                except Exception as exc:
                    result = {"error": str(exc)}
            else:
                result = receiver.execute(message)
            write({"type": "backend.result", "source_node": message["source"]["node_id"],
                   "request_id": message["message_id"], "message": result})
        except Exception:
            # Avoid logging exception text that could contain payload data.
            try:
                write({"type": "backend.error", "source_node": message.get("source", {}).get("node_id"),
                       "request_id": message.get("message_id"), "error": "native backend failed"})
            except (OSError, ValueError):
                pass
        finally:
            capacity.release()

    write({"type": "backend.ready", "version": 1, "node_id": node})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            try:
                message = cam_read_frame(incoming)
            except (OSError, ValueError):
                break
            capacity.acquire()
            pool.submit(execute, message)
    incoming.close()
    outgoing.close()


if __name__ == "__main__":
    main()
