"""Tests for camc shell prelude hook registry and generated camc embedding."""

import json
import os
import socket
import subprocess
import sys
import time

import pytest


@pytest.fixture
def prelude_registry():
    from camc_pkg.prelude import registry
    return registry


def test_parse_disable_env_empty(prelude_registry):
    assert prelude_registry.parse_disable_env("") == set()
    assert prelude_registry.parse_disable_env(None) == set()


def test_parse_disable_env_single_hook(prelude_registry):
    assert prelude_registry.parse_disable_env("capture") == {"capture"}


def test_parse_disable_env_all(prelude_registry):
    disabled = prelude_registry.parse_disable_env("all")
    assert "capture" in disabled


def test_parse_disable_env_csv(prelude_registry):
    assert prelude_registry.parse_disable_env("capture, capture") == {"capture"}


def test_render_prelude_includes_capture_by_default(prelude_registry):
    text = prelude_registry.render_prelude_text()
    assert "_camc_prelude_capture" in text
    assert "_camc_prelude_send_text_to_tmux" in text
    assert "_camc_prelude_dispatch" in text
    assert "agents.json" in text
    assert "exec python3" in text


def test_render_prelude_disable_capture(prelude_registry):
    text = prelude_registry.render_prelude_text(disabled={"capture"})
    assert "_camc_prelude_capture" not in text
    assert "_camc_prelude_dispatch" not in text
    assert "exec python3" in text


def test_render_prelude_disable_all_matches_capture_only(prelude_registry):
    all_text = prelude_registry.render_prelude_text(disabled=prelude_registry.parse_disable_env("all"))
    cap_text = prelude_registry.render_prelude_text(disabled={"capture"})
    assert all_text == cap_text


def test_build_camc_respects_disable_capture(monkeypatch, tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build_py = os.path.join(repo, "build_camc.py")
    out = tmp_path / "camc"
    monkeypatch.setenv("CAMC_PRELUDE_DISABLE", "capture")
    proc = subprocess.run(
        [sys.executable, build_py, "--output", str(out)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    body = out.read_text(encoding="utf-8")
    assert "_camc_prelude_capture" not in body
    assert "exec python3" in body


def test_build_camc_default_includes_capture_hook(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build_py = os.path.join(repo, "build_camc.py")
    out = tmp_path / "camc"
    env = os.environ.copy()
    env.pop("CAMC_PRELUDE_DISABLE", None)
    proc = subprocess.run(
        [sys.executable, build_py, "--output", str(out)],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    body = out.read_text(encoding="utf-8")
    assert "_camc_prelude_capture" in body


@pytest.fixture
def prelude_bench_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cam_dir = home / ".cam"
    cam_dir.mkdir(parents=True)
    sock = tmp_path / "test.sock"
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "cam_dir": cam_dir, "sock": sock}


def test_shell_capture_fast_with_agents_json(prelude_bench_env):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    camc = os.path.join(repo, "dist", "camc")
    if not os.path.exists(camc):
        pytest.skip("dist/camc not built")

    sock = prelude_bench_env["sock"]
    subprocess.run(
        ["tmux", "-S", str(sock), "kill-server"],
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "-S", str(sock), "new-session", "-d", "-s", "cam-bench01", "sleep 600"],
        check=True,
    )
    host = socket.gethostname().split(".", 1)[0]
    (prelude_bench_env["cam_dir"] / "agents.json").write_text(
        json.dumps([
            {
                "id": "deadbeef",
                "tmux_session": "cam-bench01",
                "tmux_socket": str(sock),
                "tmux_bin": "tmux",
                "hostname": host,
                "status": "running",
            }
        ], indent=2),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [camc, "capture", "deadbeef"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(prelude_bench_env["home"])},
    )
    assert proc.returncode == 0, proc.stderr


def test_shell_capture_fallback_preserves_argv(prelude_bench_env):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    camc = os.path.join(repo, "dist", "camc")
    if not os.path.exists(camc):
        pytest.skip("dist/camc not built")

    (prelude_bench_env["cam_dir"] / "agents.json").write_text("[]", encoding="utf-8")

    trace = subprocess.run(
        ["sh", "-x", camc, "capture", "deadbeef"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(prelude_bench_env["home"])},
    )
    assert 'exec python3 "' in trace.stderr or "exec python3 " in trace.stderr
    assert "capture" in trace.stderr
    assert "deadbeef" in trace.stderr

    proc = subprocess.run(
        [camc, "capture", "deadbeef"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(prelude_bench_env["home"])},
    )
    assert proc.returncode != 0 or proc.stdout == "" or "usage:" not in proc.stderr


def _write_fast_agent(cam_dir, agent_id, session, sock):
    host = socket.gethostname().split(".", 1)[0]
    (cam_dir / "agents.json").write_text(
        json.dumps([
            {
                "id": agent_id,
                "tmux_session": session,
                "tmux_socket": str(sock),
                "tmux_bin": "tmux",
                "hostname": host,
                "status": "running",
            }
        ], indent=2),
        encoding="utf-8",
    )


def test_shell_send_text_fast_with_enter(prelude_bench_env):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    camc = os.path.join(repo, "dist", "camc")
    if not os.path.exists(camc):
        pytest.skip("dist/camc not built")

    sock = prelude_bench_env["sock"]
    session = "cam-send01"
    subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
    try:
        subprocess.run(["tmux", "-S", str(sock), "new-session", "-d", "-s", session, "sh"], check=True)
        _write_fast_agent(prelude_bench_env["cam_dir"], "feedface", session, sock)

        proc = subprocess.run(
            [camc, "send", "feedface", "--text", "printf fastsendok"],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(prelude_bench_env["home"])},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "Sent.\n"
        time.sleep(0.2)
        cap = subprocess.run(
            ["tmux", "-S", str(sock), "capture-pane", "-p", "-J", "-t", session + ":0.0", "-S", "-20"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "fastsendok" in cap.stdout
    finally:
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)


def test_shell_send_text_fast_no_enter(prelude_bench_env):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    camc = os.path.join(repo, "dist", "camc")
    if not os.path.exists(camc):
        pytest.skip("dist/camc not built")

    sock = prelude_bench_env["sock"]
    session = "cam-send02"
    subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
    try:
        subprocess.run(["tmux", "-S", str(sock), "new-session", "-d", "-s", session, "sh"], check=True)
        _write_fast_agent(prelude_bench_env["cam_dir"], "cafebabe", session, sock)

        proc = subprocess.run(
            [camc, "send", "cafebabe", "-t", "typed-no-enter", "--no-enter"],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(prelude_bench_env["home"])},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "Sent.\n"
        time.sleep(0.2)
        cap = subprocess.run(
            ["tmux", "-S", str(sock), "capture-pane", "-p", "-J", "-t", session + ":0.0", "-S", "-20"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "typed-no-enter" in cap.stdout
        assert "not found" not in cap.stdout
    finally:
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
