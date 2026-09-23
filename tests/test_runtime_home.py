"""Shared Codex home with adapter-declared host-local runtime paths."""

import os
import stat
from types import SimpleNamespace

import pytest

from camc_pkg import cli, runtime_env
from camc_pkg.adapters import _load_config


def _spec(shared_home, local_root, overlay_root=None):
    spec = {
        "environment": "CODEX_HOME",
        "shared_home": str(shared_home),
        "local_links": ["tmp/arg0=%s" % (local_root / "arg0")],
        "config_local_dirs": ["sqlite_home"],
        "prune_path_links": ["tmp/arg0"],
        "clear_environment": ["CODEX_SQLITE_HOME"],
        "overlay": {
            "root": str(overlay_root or shared_home),
            "config_file": "config.toml", "config_format": "codex",
            "wire_api": "responses", "catalog_file": "catalog.json",
            "shared": ["auth.json", "skills"], "shared_globs": ["*"],
            "shared_exclude": ["tmp", "config.toml", "catalog.json"],
        },
    }
    return spec


def _runtime(home, path="/usr/bin"):
    return runtime_env.RuntimeEnv(
        env={"HOME": str(home), "USER": "tester", "PATH": path,
             "CODEX_HOME": "/inherited/wrong",
             "CODEX_SQLITE_HOME": "/inherited/sqlite"},
        source="test", shell="", path=path)


def _adapter(spec, isolate_tool_home=True):
    return type("Adapter", (), {
        "runtime": spec, "isolate_tool_home": isolate_tool_home})()


def test_codex_adapter_declares_shared_vs_local_runtime():
    config = _load_config("codex")
    assert config.runtime["environment"] == "CODEX_HOME"
    assert config.runtime["shared_home"] == "~/.codex"
    assert config.isolate_tool_home is True
    assert config.runtime["local_links"] == [
        "tmp/arg0=/tmp/{user}/codex-arg0"]
    assert config.runtime["config_local_dirs"] == ["sqlite_home"]
    assert config.runtime["prune_path_links"] == ["tmp/arg0"]
    assert config.runtime["overlay"]["root"] == "~/.codex"
    assert config.runtime["overlay"]["shared_globs"] == ["*"]
    assert config.runtime["overlay"]["shared_exclude"] == [
        "tmp", "config.toml", "camc-model-catalog.json"]


@pytest.mark.parametrize("tool,expected", [
    ("codex", True),
    ("claude", False),
    ("cursor", False),
    ("pi", False),
])
def test_each_tool_adapter_declares_home_isolation_default(tool, expected):
    assert _load_config(tool).isolate_tool_home is expected


def test_login_uses_thin_agent_home_and_bootstraps_local_paths(tmp_path):
    home = tmp_path / "home"
    shared = home / ".codex"
    local = tmp_path / "host-a"
    sqlite = local / "sqlite"
    shared.mkdir(parents=True)
    (shared / "config.toml").write_text(
        'model = "login"\nsqlite_home = "%s"\n' % sqlite)
    overlay = shared / "agent-a"
    stale_arg0_path = str(overlay / "tmp" / "arg0" / "codex-arg0-old")
    runtime = _runtime(home, stale_arg0_path + os.pathsep + "/usr/bin")

    info = runtime_env.configure_agent_runtime_home(
        "agent-a", _adapter(_spec(shared, local)), runtime)

    assert runtime.env["CODEX_HOME"] == str(overlay)
    assert "CODEX_SQLITE_HOME" not in runtime.env
    assert runtime.env["PATH"] == "/usr/bin"
    assert os.path.islink(overlay / "tmp" / "arg0")
    assert os.path.realpath(overlay / "tmp" / "arg0") == str(local / "arg0")
    assert (local / "arg0").is_dir()
    assert sqlite.is_dir()
    assert stat.S_IMODE((local / "arg0").stat().st_mode) == 0o700
    assert stat.S_IMODE(sqlite.stat().st_mode) == 0o700
    assert info["overlay"]["path"] == str(overlay)
    assert (overlay / "config.toml").read_text() == (shared / "config.toml").read_text()
    assert not (shared / "tmp").exists()


def test_dangling_link_target_is_created_before_validation(tmp_path):
    home = tmp_path / "home"
    shared = home / ".codex"
    target = tmp_path / "host-local" / "arg0"
    (shared / "tmp").mkdir(parents=True)
    os.symlink(str(target), shared / "tmp" / "arg0")
    assert not (shared / "tmp" / "arg0").exists()

    runtime_env.ensure_runtime_split(
        _spec(shared, target.parent), _runtime(home))

    assert target.is_dir()
    assert (shared / "tmp" / "arg0").is_dir()


def test_same_shared_home_maps_to_host_local_targets(tmp_path):
    shared = tmp_path / "nfs" / ".codex"
    shared.mkdir(parents=True)
    first = _spec(shared, tmp_path / "host-a")
    runtime_env.ensure_runtime_split(first, _runtime(tmp_path / "home-a"))
    assert os.path.realpath(shared / "tmp" / "arg0") == str(
        tmp_path / "host-a" / "arg0")

    # A shared symlink has one textual target.  Hosts see the same pathname on
    # their own local /tmp; model that with two isolated roots using the same
    # adapter declaration rather than replacing a live user's link.
    shared_b = tmp_path / "nfs-b" / ".codex"
    shared_b.mkdir(parents=True)
    second = _spec(shared_b, tmp_path / "host-b")
    runtime_env.ensure_runtime_split(second, _runtime(tmp_path / "home-b"))
    assert os.path.realpath(shared_b / "tmp" / "arg0") == str(
        tmp_path / "host-b" / "arg0")


def test_conflicting_link_fails_without_replacing_user_content(tmp_path):
    shared = tmp_path / ".codex"
    (shared / "tmp" / "arg0").mkdir(parents=True)
    marker = shared / "tmp" / "arg0" / "keep"
    marker.write_text("owned")

    with pytest.raises(IOError, match="already exists"):
        runtime_env.ensure_runtime_split(
            _spec(shared, tmp_path / "local"), _runtime(tmp_path))

    assert marker.read_text() == "owned"


def test_login_and_api_use_same_overlay_shape_api_only_changes_its_config(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    shared = home / ".codex"
    shared.mkdir(parents=True)
    (shared / "config.toml").write_text('model = "login"\n')
    (shared / "auth.json").write_text("credential")
    (shared / "skills").mkdir()
    (shared / "sessions").mkdir()
    spec = _spec(shared, tmp_path / "local")
    runtime = _runtime(home)
    monkeypatch.setattr(
        "camc_pkg.api_metadata.resolve_api_metadata", lambda _name: {})
    monkeypatch.setattr(
        "camc_pkg.api_metadata.write_codex_model_catalog",
        lambda path, *_a, **_kw: open(path, "w").write("{}\n"))

    login_runtime = _runtime(home)
    login_info = runtime_env.configure_agent_runtime_home(
        "agent-login", _adapter(spec), login_runtime)
    info = runtime_env.configure_agent_runtime_home(
        "agent-api", _adapter(spec), runtime,
        api_plan={"name": "glm", "model": "model"},
        api_base_url="http://127.0.0.1:1234/v1")
    overlay = shared / "agent-api"
    login_overlay = shared / "agent-login"

    assert runtime.env["CODEX_HOME"] == str(overlay)
    assert info["overlay"]["path"] == str(overlay)
    assert login_runtime.env["CODEX_HOME"] == str(login_overlay)
    assert login_info["overlay"]["root"] == info["overlay"]["root"]
    assert login_overlay.joinpath("config.toml").read_text() == 'model = "login"\n'
    assert "camc-ihub" in overlay.joinpath("config.toml").read_text()
    assert (shared / "config.toml").read_text() == 'model = "login"\n'
    assert (overlay / "config.toml").is_file()
    assert (overlay / "auth.json").is_symlink()
    assert (overlay / "skills").is_symlink()
    assert (overlay / "sessions").is_symlink()
    assert os.path.realpath(overlay / "sessions") == str(shared / "sessions")
    assert os.path.realpath(overlay / "sessions").startswith(str(shared))
    assert (overlay / "tmp" / "arg0").is_symlink()
    assert (overlay / "catalog.json").is_file()
    assert runtime_env.remove_agent_runtime_home(
        info["overlay"], owner_id="agent-api")
    assert shared.is_dir()


def test_donor_is_non_destructive_and_never_links_agent_overlays(tmp_path):
    shared = tmp_path / ".codex"
    shared.mkdir()
    (shared / "config.toml").write_text('model = "login"\n')
    (shared / "auth.json").write_text("secret")
    (shared / "sessions").mkdir()
    runtime = _runtime(tmp_path)
    spec = _spec(shared, tmp_path / "local")

    first = runtime_env.configure_agent_runtime_home(
        "agent-a", _adapter(spec), runtime)
    second = runtime_env.configure_agent_runtime_home(
        "agent-b", _adapter(spec), _runtime(tmp_path))
    assert (shared / "auth.json").read_text() == "secret"
    assert (shared / "sessions").is_dir()
    assert not (shared / "share").exists()
    assert not (shared / "agent-b" / "agent-a").exists()
    assert os.path.realpath(shared / "agent-a" / "auth.json") == str(
        shared / "auth.json")
    assert first["overlay"]["donor"] == str(shared)
    assert second["overlay"]["donor"] == str(shared)


def test_unmanaged_direct_id_directory_is_never_reused_or_removed(tmp_path):
    shared = tmp_path / ".codex"
    occupied = shared / "agent-a"
    occupied.mkdir(parents=True)
    (occupied / "keep").write_text("user")
    spec = _spec(shared, tmp_path / "local")

    with pytest.raises(ValueError, match="not owned"):
        runtime_env.configure_agent_runtime_home(
            "agent-a", _adapter(spec), _runtime(tmp_path))
    assert not runtime_env.remove_agent_runtime_home(
        {"path": str(occupied), "root": str(shared), "owner_id": "agent-a"})
    assert (occupied / "keep").read_text() == "user"


def test_existing_owned_overlay_survives_refresh_failure(tmp_path, monkeypatch):
    donor = tmp_path / ".widget"
    donor.mkdir()
    (donor / "settings.ini").write_text("mode=login\n")
    spec = {"environment": "WIDGET_HOME", "donor": str(donor),
            "root": str(donor), "config_file": "settings.ini",
            "shared": ["history.jsonl"], "shared_globs": [],
            "shared_exclude": []}
    runtime = _runtime(tmp_path)
    info = runtime_env.ensure_agent_runtime_home("agent-a", spec, runtime)
    overlay = donor / "agent-a"
    (donor / "history.jsonl").write_text("event\n")
    monkeypatch.setattr(runtime_env, "_link_runtime_home_entry",
                        lambda _source, _target: "error")

    with pytest.raises(IOError, match="cannot link"):
        runtime_env.ensure_agent_runtime_home("agent-a", spec, runtime)

    assert overlay.is_dir()
    assert (overlay / ".camc-runtime-home.json").is_file()
    assert info["path"] == str(overlay)


def test_synthetic_adapter_uses_same_tool_neutral_overlay_mechanism(tmp_path):
    donor = tmp_path / ".widget"
    donor.mkdir()
    (donor / "settings.ini").write_text("mode=login\n")
    (donor / "history.jsonl").write_text("event\n")
    local = tmp_path / "widget-local"
    spec = {
        "environment": "WIDGET_HOME", "shared_home": str(donor),
        "local_links": ["tmp/arg0=%s" % local],
        "config_local_dirs": [], "prune_path_links": ["tmp/arg0"],
        "clear_environment": [],
        "overlay": {"root": str(donor),
                    "config_file": "settings.ini",
                    "shared_globs": ["*"],
                    "shared_exclude": ["tmp", "settings.ini"]},
    }
    runtime = _runtime(tmp_path)

    info = runtime_env.configure_agent_runtime_home(
        "widget-1", _adapter(spec), runtime)

    overlay = donor / "widget-1"
    assert runtime.env["WIDGET_HOME"] == str(overlay)
    assert (overlay / "settings.ini").read_text() == "mode=login\n"
    assert (overlay / "history.jsonl").is_symlink()
    assert os.path.realpath(overlay / "tmp" / "arg0") == str(local)
    assert info["overlay"]["donor"] == str(donor)


@pytest.mark.parametrize("adapter_default,api_plan,use_overlay", [
    (True, None, True),
    (True, {"name": "x"}, True),
    (False, None, False),
    (False, {"name": "x"}, False),
])
def test_synthetic_adapter_isolation_default_is_metadata_driven(
        tmp_path, adapter_default, api_plan, use_overlay):
    donor = tmp_path / ("donor-" + str(adapter_default) +
                        ("-api" if api_plan else "-login"))
    donor.mkdir()
    (donor / "settings.ini").write_text("mode=shared\n")
    spec = {
        "environment": "WIDGET_HOME", "shared_home": str(donor),
        "local_links": [], "config_local_dirs": [],
        "prune_path_links": [], "clear_environment": [],
        "overlay": {"root": str(donor),
                    "config_file": "settings.ini", "shared_globs": ["*"],
                    "shared_exclude": ["settings.ini"]},
    }
    runtime = _runtime(tmp_path)

    info = runtime_env.configure_agent_runtime_home(
        "widget-2", _adapter(spec, adapter_default), runtime,
        api_plan=api_plan)

    expected = donor / "widget-2" if use_overlay else donor
    assert runtime.env["WIDGET_HOME"] == str(expected)
    assert ("overlay" in info) is use_overlay
    assert expected.is_dir()


@pytest.mark.parametrize("adapter_default,option,api_plan,use_overlay", [
    (False, True, None, True),
    (True, False, {"name": "x"}, False),
    (True, False, None, False),
    (False, True, {"name": "x"}, True),
])
def test_run_option_overrides_adapter_isolation_default(
        tmp_path, adapter_default, option, api_plan, use_overlay):
    donor = tmp_path / ("donor-%s-%s" % (adapter_default, option))
    donor.mkdir()
    (donor / "settings.ini").write_text("mode=shared\n")
    spec = {
        "environment": "WIDGET_HOME", "shared_home": str(donor),
        "local_links": [], "config_local_dirs": [],
        "prune_path_links": [], "clear_environment": [],
        "overlay": {"root": str(donor),
                    "config_file": "settings.ini", "shared_globs": ["*"],
                    "shared_exclude": ["settings.ini"]},
    }

    info = runtime_env.configure_agent_runtime_home(
        "widget-option", _adapter(spec, adapter_default), _runtime(tmp_path),
        api_plan=api_plan, isolate_tool_home=option)

    assert info["isolate_tool_home"] is option
    assert ("overlay" in info) is use_overlay


def test_api_config_writer_cannot_mutate_shared_home_when_isolation_disabled(
        tmp_path):
    donor = tmp_path / ".codex"
    donor.mkdir()
    config = donor / "config.toml"
    config.write_text('model = "login"\n')

    with pytest.raises(ValueError, match="requires an isolated tool home"):
        runtime_env.configure_agent_runtime_home(
            "agent-api", _adapter(_spec(donor, tmp_path / "local")),
            _runtime(tmp_path), api_plan={"name": "api"},
            isolate_tool_home=False)

    assert config.read_text() == 'model = "login"\n'
    assert not (donor / "agent-api").exists()


@pytest.mark.parametrize("command", ["kill", "rm"])
def test_terminal_cleanup_happens_after_tmux(command, monkeypatch, tmp_path):
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "config.toml").write_text("")
    overlay_info = runtime_env.ensure_agent_runtime_home(
        "agent-a", {"environment": "CODEX_HOME", "donor": str(root),
                    "root": str(root),
                    "config_file": "config.toml", "shared_globs": ["*"],
                    "shared_exclude": ["tmp", "config.toml"]},
        _runtime(tmp_path))
    overlay = root / "agent-a"
    agent = {
        "id": "agent-a", "tmux_session": "cam-agent-a",
        "runtime": {"storage": {"overlay": overlay_info}},
        "task": {},
    }
    order = []

    class Store(object):
        def get(self, _owner):
            return agent

        def update(self, *_a, **_kw):
            order.append("record")

        def remove(self, *_a):
            order.append("record")

    monkeypatch.setattr(cli, "AgentStore", Store)
    monkeypatch.setattr(cli, "_kill_monitor", lambda _a: order.append("monitor"))
    monkeypatch.setattr(
        cli, "_stop_agent_api_proxy",
        lambda _a, cleanup_home=True: order.append("proxy"))
    monkeypatch.setattr(cli, "tmux_kill_session",
                        lambda _s: order.append("tmux") or True)
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: False)
    monkeypatch.setattr(cli, "_find_tmux_socket", lambda _s: None)
    original_cleanup = cli._cleanup_agent_runtime_overlay

    def cleanup(item):
        order.append("cleanup")
        return original_cleanup(item)

    monkeypatch.setattr(cli, "_cleanup_agent_runtime_overlay", cleanup)
    getattr(cli, "cmd_" + command)(
        SimpleNamespace(id="agent-a", archive=False))

    assert order.index("tmux") < order.index("cleanup")
    assert not overlay.exists()


def test_failed_tmux_kill_preserves_overlay(monkeypatch, tmp_path):
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "config.toml").write_text("")
    overlay_info = runtime_env.ensure_agent_runtime_home(
        "agent-a", {"environment": "CODEX_HOME", "donor": str(root),
                    "root": str(root), "config_file": "config.toml",
                    "shared_globs": ["*"],
                    "shared_exclude": ["tmp", "config.toml"]},
        _runtime(tmp_path))
    agent = {"id": "agent-a", "tmux_session": "cam-agent-a",
             "runtime": {"storage": {"overlay": overlay_info}}, "task": {}}
    updated = []

    class Store(object):
        def get(self, _owner):
            return agent

        def update(self, *_a, **_kw):
            updated.append(_kw)

    monkeypatch.setattr(cli, "AgentStore", Store)
    monkeypatch.setattr(cli, "_kill_monitor", lambda _a: None)
    monkeypatch.setattr(cli, "_stop_agent_api_proxy", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "tmux_kill_session", lambda _s: False)
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: True)

    cli.cmd_kill(SimpleNamespace(id="agent-a"))

    assert (root / "agent-a").is_dir()
    assert not updated


def test_rm_keeps_record_when_tmux_survives_kill(monkeypatch, tmp_path):
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "config.toml").write_text("")
    overlay_info = runtime_env.ensure_agent_runtime_home(
        "agent-a", {"environment": "CODEX_HOME", "donor": str(root),
                    "root": str(root), "config_file": "config.toml",
                    "shared_globs": ["*"],
                    "shared_exclude": ["tmp", "config.toml"]},
        _runtime(tmp_path))
    agent = {"id": "agent-a", "tmux_session": "cam-agent-a",
             "runtime": {"storage": {"overlay": overlay_info}}, "task": {}}
    removed = []
    updated = []

    class Store(object):
        def get(self, _owner):
            return agent

        def update(self, *_a, **fields):
            updated.append(fields)

        def remove(self, owner):
            removed.append(owner)

    monkeypatch.setattr(cli, "AgentStore", Store)
    monkeypatch.setattr(cli, "_kill_monitor", lambda _a: None)
    monkeypatch.setattr(cli, "_stop_agent_api_proxy", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "tmux_kill_session", lambda _s: False)
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: True)
    monkeypatch.setattr(cli, "_find_tmux_socket", lambda _s: None)

    cli.cmd_rm(SimpleNamespace(id="agent-a", archive=False))

    assert not removed
    assert not updated
    assert (root / "agent-a").is_dir()


@pytest.mark.parametrize("command", ["stop", "exit"])
@pytest.mark.parametrize("api_mode", [False, True])
def test_nonterminal_stop_and_exit_preserve_overlay(
        command, api_mode, monkeypatch, tmp_path):
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "config.toml").write_text("")
    overlay_info = runtime_env.ensure_agent_runtime_home(
        "agent-a", {"environment": "CODEX_HOME", "donor": str(root),
                    "root": str(root), "config_file": "config.toml",
                    "shared_globs": ["*"],
                    "shared_exclude": ["tmp", "config.toml"]},
        _runtime(tmp_path))
    agent = {
        "id": "agent-a", "tmux_session": "cam-agent-a",
        "runtime": {"storage": {"overlay": overlay_info}},
        "task": {"tool": "codex"},
    }
    if api_mode:
        agent["api"] = {"proxy_pid": 0}

    class Store(object):
        def get(self, _owner):
            return agent

        def update(self, *_a, **_kw):
            pass

    monkeypatch.setattr(cli, "AgentStore", Store)
    monkeypatch.setattr(cli, "tmux_session_exists", lambda _s: True)
    monkeypatch.setattr(cli, "_find_claude_pid", lambda _s: None)
    monkeypatch.setattr(cli, "graceful_exit", lambda *_a, **_kw: True)
    monkeypatch.setattr(cli, "_kill_monitor", lambda _a: None)
    monkeypatch.setattr(cli, "_stop_agent_api_proxy", lambda *_a, **_kw: None)

    getattr(cli, "cmd_" + command)(SimpleNamespace(id="agent-a"))

    assert (root / "agent-a").is_dir()


def test_monitor_terminal_proxy_cleanup_removes_owned_overlay(tmp_path):
    from camc_pkg import api_proxy

    root = tmp_path / ".codex"
    root.mkdir()
    (root / "config.toml").write_text("")
    overlay_info = runtime_env.ensure_agent_runtime_home(
        "agent-a", {"environment": "CODEX_HOME", "donor": str(root),
                    "root": str(root),
                    "config_file": "config.toml", "shared_globs": ["*"],
                    "shared_exclude": ["tmp", "config.toml"]},
        _runtime(tmp_path))
    overlay = root / "agent-a"
    api_proxy.stop_agent_api_proxy({
        "id": "agent-a", "task": {"tool": "codex"},
        "runtime": {"storage": {"overlay": overlay_info}},
    })

    assert not overlay.exists()


@pytest.mark.parametrize("owner", ["", "../escape", "agent/a", "."])
def test_overlay_rejects_unsafe_owner(tmp_path, owner):
    with pytest.raises(ValueError):
        runtime_env.ensure_agent_runtime_home(
            owner, {"environment": "CODEX_HOME", "donor": str(tmp_path),
                    "root": str(tmp_path / "overlay")}, _runtime(tmp_path))
