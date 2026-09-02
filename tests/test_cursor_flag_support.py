"""Cursor does not expose a reliable native tmux cursor signal."""

from pathlib import Path

from camc_pkg.adapters import AdapterConfig, _parse_toml
from camc_pkg.detection import is_ready_for_boot, is_ready_for_input
from camc_pkg.detection import should_boot_confirm
from camc_pkg.detection import should_auto_confirm
from camc_pkg.monitor_features import AutoConfirmationFeature, FinalStaticFallbackFeature
from camc_pkg.monitor_features import MonitorRuntime, MonitorSnapshot
from camc_pkg.transport import insert_prompt


_CONFIGS = Path(__file__).resolve().parent.parent / "src" / "cam" / "adapters" / "configs"


def _config(name):
    return AdapterConfig(_parse_toml((_CONFIGS / name).read_text(encoding="utf-8")))


def _snapshot(output, *, cursor_flag, now=100.0, prompt_visible=False,
              hash0="screen", hash1="screen", idle_for=0.0,
              idle_for_hash1=0.0):
    return MonitorSnapshot(
        output=output, hash=hash0, prev_hash="previous", changed=False,
        now=now, cycle=1, prompt_visible=prompt_visible,
        screen_busy=False, screen_done=False, bare_prompt=False,
        tail_lines=output.splitlines()[-5:], idle_for=idle_for,
        hash0=hash0, hash1=hash1, idle_for_hash1=idle_for_hash1,
        cursor_flag=cursor_flag,
    )


def test_cursor_bypasses_native_cursor_guard_for_ready_and_confirmation():
    config = _config("cursor.toml")
    assert config.cursor_flag_support is False
    assert _config("cursor.boot.toml").cursor_flag_support is False

    screen = "→ Run this command? (y)\n"
    assert should_auto_confirm(screen, config, cursor_flag=1) is not None
    assert is_ready_for_input("→ Plan, search, build anything\n", config,
                              cursor_flag=0) is True
    boot = _config("cursor.boot.toml")
    assert is_ready_for_boot("→ Plan, search, build anything\n", boot, config,
                             cursor_flag=0) is True

    runtime = MonitorRuntime("cursor", config, now=0.0)
    actions = AutoConfirmationFeature().confirm(
        _snapshot(screen, cursor_flag=1), runtime)
    assert any(action["kind"] == "send_input" for action in actions)


def test_cursor_boot_trust_selects_with_enter():
    boot = _config("cursor.boot.toml")
    assert should_boot_confirm("│  ▶ [a] Trust this workspace\n", boot)
    assert should_boot_confirm("│    [a] Trust this workspace\n", boot) is None
    assert any(response == "" and send_enter is True
               for _pattern, response, send_enter in boot.confirm_rules)
    runtime = MonitorRuntime("cursor-boot", _config("cursor.toml"), now=0.0)
    runtime.boot_config = boot
    runtime.in_initializing = True
    actions = AutoConfirmationFeature().confirm(
        _snapshot("│  ▶ [a] Trust this workspace\n", cursor_flag=0), runtime)
    assert {action.get("key") for action in actions
            if action["kind"] == "send_key"} == {"Enter"}


def test_cursor_run_everything_shift_tab_precedes_run_once():
    config = _config("cursor.toml")
    for screen in (
            "Run this command?\n→ Run (once) (y)\n"
            "Run Everything (shift+tab)\n",
            "Auto-run everything (shift+tab)\n"
            "→ Run (once) (y)\n"):
        result = should_auto_confirm(screen, config, cursor_flag=1)
        assert result is not None
        assert result[0:2] == ("BTab", False)
    assert should_auto_confirm("→ Auto-run everything (shift+tab)\n",
                              config, cursor_flag=1) is None


def test_cursor_insert_prompt_does_not_require_native_cursor_flag():
    observations = iter([
        {"ready": True, "cursor_flag": 0, "cursor_flag_supported": False,
         "hash0": "ready", "hash1": "ready"},
        {"ready": True, "cursor_flag": 0, "cursor_flag_supported": False,
         "hash0": "typed", "hash1": "typed"},
        {"ready": False, "cursor_flag": 0, "cursor_flag_supported": False,
         "hash0": "submitted", "hash1": "submitted"},
    ])
    keys = []

    result = insert_prompt(
        "cam-cursor", "hello", observe_fn=lambda _sid: next(observations),
        send_input_fn=lambda *_args, **_kwargs: True,
        send_key_fn=lambda _sid, key: keys.append(key) or True,
        sleep_fn=lambda _seconds: None, monotonic_fn=lambda: 0.0,
        ready_timeout=0.1, submit_delay=0.0, ack_timeout=0.1,
    )

    assert result["ok"] is True
    assert keys == ["Enter"]


def test_cursor_static_fallback_does_not_require_native_cursor_flag():
    config = _config("cursor.toml")
    runtime = MonitorRuntime("cursor", config, now=0.0)
    runtime.final_fallback = {
        "kind": "confirm", "text": "1", "baseline_hash0": "same",
        "baseline_hash1": "same", "attempts": 0, "next_at": 0.0,
    }
    actions = FinalStaticFallbackFeature().after_confirm(_snapshot(
        "normal output", cursor_flag=1, now=100.0, prompt_visible=False,
        hash0="same", hash1="same", idle_for=60.0,
        idle_for_hash1=60.0), runtime)
    assert any(action["kind"] == "send_key" for action in actions)


def test_codex_and_claude_keep_cursor_flag_guard_enabled():
    assert _config("codex.toml").cursor_flag_support is True
    assert _config("claude.toml").cursor_flag_support is True
