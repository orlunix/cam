"""The SDK's framed stream cannot be inherited by management subprocesses."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest


INTERPRETERS = list(dict.fromkeys(str(Path(p).resolve()) for p in (sys.executable, "/bin/python3")
                                 if Path(p).is_file()))


@pytest.mark.parametrize("interpreter", INTERPRETERS)
def test_management_children_cannot_consume_or_corrupt_control_frames(tmp_path, interpreter):
    package = tmp_path / "overlay" / "camc_pkg"
    package.mkdir(parents=True)
    real = Path(__file__).parents[1] / "src" / "camc_pkg"
    (package / "__init__.py").write_text("__path__.append(%r)\n" % str(real))
    (package / "server_management.py").write_text('''import os,subprocess,sys

def management_request(action,args,node,state_path,receiver):
    if action == 'child-probe':
        print('UNFRAMED_HELPER_OUTPUT', flush=True)
        code = "import os;print('UNFRAMED_CHILD_OUTPUT',flush=True);assert os.read(0,1)==b''"
        child = subprocess.run([sys.executable,'-c',code],timeout=2)
        return {'child_exit':child.returncode}
    return {'alive':True}
''')
    env = dict(os.environ, PYTHONPATH=str(package.parent), CAM_SERVER_WORKER_NODE="test-node",
               CAM_SERVER_WORKER_STATE=str(tmp_path / "state.db"))
    process = subprocess.Popen([interpreter, "-m", "camc_pkg.server_worker"], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, bufsize=0)
    def read():
        assert select.select([process.stdout], [], [], 5)[0], "SDK response timed out"
        raw = process.stdout.readline()
        assert raw, "SDK exited before its response"
        return json.loads(raw)
    def frame(identity, action):
        return json.dumps(dict(type="backend.control", source={"node_id":"@management"},
                               message_id=identity, action=action, args={})).encode() + b"\n"
    try:
        assert read()["type"] == "backend.ready"
        process.stdin.write(frame("probe", "child-probe") + frame("concurrent", "check"))
        process.stdin.flush()
        replies = [read(), read()]
        results = {reply["request_id"]:reply["message"] for reply in replies}
        assert results == {"probe":{"child_exit":0}, "concurrent":{"alive":True}}
        process.stdin.write(frame("after", "check")); process.stdin.flush()
        assert read()["message"] == {"alive":True}
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
