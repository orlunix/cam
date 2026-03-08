"""TOML-configurable adapter for AI coding tools.

Allows defining new tool adapters declaratively via TOML files,
without writing Python code. Implements the full ToolAdapter interface.

Detection logic (state, completion, auto-confirm) is delegated to
``cam.client``, which is the single source of truth.  This adapter
is a thin wrapper that converts plain-string returns → cam enums.
"""

from __future__ import annotations

import logging
from pathlib import Path

from cam.adapters.base import ConfirmAction, ProbeAction, ToolAdapter
from cam.client import (
    AdapterConfig,
    detect_completion as _detect_completion,
    detect_state as _detect_state,
    is_ready_for_input as _is_ready_for_input,
    should_auto_confirm as _should_auto_confirm,
)
from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Valid AgentState values for state patterns
_VALID_STATES = {s.value: s for s in AgentState}

# Valid completion / state strategies (validated at init time)
_VALID_STATE_STRATEGIES = ("first", "last")
_VALID_COMPLETION_STRATEGIES = ("pattern", "prompt_count", "process_exit")


class ConfigurableAdapter(ToolAdapter):
    """TOML-driven adapter implementing the full ToolAdapter interface.

    All detection logic is delegated to ``cam.client`` functions which
    operate on a shared ``AdapterConfig`` parsed from the same TOML dict.
    """

    def __init__(self, config: dict) -> None:
        self._config = config  # Stored for to_dict() serialization

        adapter = config.get("adapter", {})
        if not adapter.get("name"):
            raise ValueError("adapter.name is required")
        if not adapter.get("display_name"):
            raise ValueError("adapter.display_name is required")

        self.name: str = adapter["name"]
        self.display_name: str = adapter["display_name"]

        # Validate strategies before delegating to AdapterConfig
        state_strategy = config.get("state", {}).get("strategy", "first")
        if state_strategy not in _VALID_STATE_STRATEGIES:
            raise ValueError(
                f"Unknown state.strategy: {state_strategy!r} (valid: 'first', 'last')"
            )

        comp_strategy = config.get("completion", {}).get("strategy", "process_exit")
        if comp_strategy not in _VALID_COMPLETION_STRATEGIES:
            raise ValueError(
                f"Unknown completion.strategy: {comp_strategy!r} "
                f"(valid: 'pattern', 'prompt_count', 'process_exit')"
            )

        # Validate state names
        for entry in config.get("state", {}).get("patterns", []):
            state_str = entry["state"]
            if state_str not in _VALID_STATES:
                raise ValueError(
                    f"Unknown state: {state_str!r} (valid: {list(_VALID_STATES)})"
                )

        # Delegate all pattern compilation + detection config to client.py
        self._ac = AdapterConfig(config)

        # Launch config (kept here — not needed by client.py detection)
        launch = config.get("launch", {})
        self._command: list[str] = launch.get("command", [])

    def to_dict(self) -> dict:
        """Return the raw adapter config for serialization to cam-client."""
        return self._config

    @classmethod
    def from_toml(cls, path: Path) -> ConfigurableAdapter:
        """Load an adapter from a TOML file."""
        with open(path, "rb") as f:
            config = tomllib.load(f)
        return cls(config)

    def get_launch_command(
        self, task: TaskDefinition, context: Context
    ) -> list[str]:
        """Build launch command, replacing placeholders.

        Supported placeholders:
            {prompt} - task prompt text
            {path}   - context working directory path

        Uses single-pass replacement to avoid double-substitution
        (e.g. {path} inside a prompt wouldn't get replaced).
        """
        replacements = {"{prompt}": task.prompt, "{path}": context.path}
        result = []
        for part in self._command:
            for key, value in replacements.items():
                if key in part:
                    part = part.replace(key, value)
                    break  # One placeholder per part
            result.append(part)
        return result

    def detect_state(self, output: str) -> AgentState | None:
        s = _detect_state(output, self._ac)
        return AgentState(s) if s else None

    def should_auto_confirm(self, output: str) -> ConfirmAction | None:
        r = _should_auto_confirm(output, self._ac)
        if r is None:
            return None
        return ConfirmAction(response=r[0], send_enter=r[1])

    def detect_completion(self, output: str) -> AgentStatus | None:
        r = _detect_completion(output, self._ac)
        if r == "completed":
            return AgentStatus.COMPLETED
        if r == "failed":
            return AgentStatus.FAILED
        return None

    def is_ready_for_input(self, output: str) -> bool:
        return _is_ready_for_input(output, self._ac)

    def get_startup_wait(self) -> float:
        return self._ac.startup_wait

    def needs_prompt_after_launch(self) -> bool:
        return self._ac.prompt_after_launch

    def get_probe_action(self, auto_confirm: bool) -> ProbeAction:
        if auto_confirm:
            return ProbeAction(
                char=self._ac.probe_confirm_response,
                send_enter=self._ac.probe_confirm_send_enter,
                is_confirm=True,
            )
        return ProbeAction(char=self._ac.probe_char, send_enter=False, is_confirm=False)

    def get_confirm_cooldown(self) -> float:
        return self._ac.confirm_cooldown

    def get_confirm_sleep(self) -> float:
        return self._ac.confirm_sleep

    def get_completion_stable(self) -> float:
        return self._ac.completion_stable

    def get_probe_wait(self) -> float:
        return self._ac.probe_wait

    def get_probe_idle_threshold(self) -> int:
        return self._ac.probe_idle_threshold

    def get_auto_exit(self) -> bool:
        return self._ac.auto_exit

    def get_exit_action(self) -> str:
        return self._ac.exit_action

    def get_exit_command(self) -> str:
        return self._ac.exit_command
