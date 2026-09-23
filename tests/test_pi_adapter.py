"""Pi adapter contract derived from the approved Pi 0.85.x TUI/CLI."""

from camc_pkg.adapters import _load_config
from camc_pkg.detection import is_ready_for_input
from camc_pkg.system_prompt import target_file


def test_pi_launch_and_launcher_trust_policy():
    config = _load_config("pi")
    assert config.command == ["pi"]
    assert config.prompt_after_launch is True
    assert config.launcher_autorun_args == ["--approve"]
    assert config.readiness["binary"] == "pi"


def test_pi_idle_editor_is_ready_but_working_editor_is_not():
    config = _load_config("pi")
    idle = "header\n" + "─" * 80 + "\n \n" + "─" * 80 + "\n~/repo\n0.0%/200k"
    busy = "header\n── Working (esc to interrupt) " + "─" * 40 + "\n \n" + "─" * 80

    assert is_ready_for_input(idle, config, cursor_flag=1) is True
    assert is_ready_for_input(idle, config, cursor_flag=0) is False
    assert is_ready_for_input(busy, config, cursor_flag=1) is False


def test_pi_home_policy_and_system_prompt_mapping():
    config = _load_config("pi")
    assert config.isolate_tool_home is False
    assert config.runtime["environment"] == "PI_CODING_AGENT_DIR"
    assert config.runtime["shared_home"] == "~/.pi/agent"
    assert "sessions" in config.runtime["overlay"]["shared_exclude"]
    assert target_file("pi", "/work") == "/work/AGENTS.md"
