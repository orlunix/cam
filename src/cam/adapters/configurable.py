"""TOML-configurable adapter for AI coding tools.

Allows defining new tool adapters declaratively via TOML files,
without writing Python code. Implements the full ToolAdapter interface.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from cam.adapters.base import ConfirmAction, ToolAdapter
from cam.core.models import AgentState, AgentStatus, Context, TaskDefinition

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Map string flag names to re constants
_RE_FLAGS = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
}

# Valid AgentState values for state patterns
_VALID_STATES = {s.value: s for s in AgentState}


def _compile_flags(flag_names: list[str]) -> int:
    """Combine a list of regex flag names into a single flags int."""
    flags = 0
    for name in flag_names:
        upper = name.upper()
        if upper not in _RE_FLAGS:
            raise ValueError(f"Unknown regex flag: {name!r} (valid: {list(_RE_FLAGS)})")
        flags |= _RE_FLAGS[upper]
    return flags


def _compile(pattern: str, flags: list[str] | None = None) -> re.Pattern[str]:
    """Compile a regex pattern with optional flag names."""
    return re.compile(pattern, _compile_flags(flags or []))


class ConfigurableAdapter(ToolAdapter):
    """TOML-driven adapter implementing the full ToolAdapter interface.

    All regex patterns are precompiled at init time for zero runtime overhead.
    """

    def __init__(self, config: dict) -> None:
        adapter = config.get("adapter", {})
        if not adapter.get("name"):
            raise ValueError("adapter.name is required")
        if not adapter.get("display_name"):
            raise ValueError("adapter.display_name is required")

        self.name: str = adapter["name"]
        self.display_name: str = adapter["display_name"]

        # Launch config
        launch = config.get("launch", {})
        self._command: list[str] = launch.get("command", [])
        self._prompt_after_launch: bool = launch.get("prompt_after_launch", False)
        self._startup_wait: float = float(launch.get("startup_wait", 2.0))
        self._strip_ansi: bool = launch.get("strip_ansi", False)

        # Ready pattern (optional)
        rp = launch.get("ready_pattern")
        self._ready_pattern: re.Pattern[str] | None = (
            _compile(rp, launch.get("ready_flags")) if rp else None
        )

        # State detection
        state_cfg = config.get("state", {})
        self._state_strategy: str = state_cfg.get("strategy", "first")
        if self._state_strategy not in ("first", "last"):
            raise ValueError(
                f"Unknown state.strategy: {self._state_strategy!r} (valid: 'first', 'last')"
            )
        self._state_recent_chars: int = state_cfg.get("recent_chars", 2000)
        self._state_patterns: list[tuple[AgentState, re.Pattern[str]]] = []
        for entry in state_cfg.get("patterns", []):
            state_str = entry["state"]
            if state_str not in _VALID_STATES:
                raise ValueError(
                    f"Unknown state: {state_str!r} (valid: {list(_VALID_STATES)})"
                )
            self._state_patterns.append((
                _VALID_STATES[state_str],
                _compile(entry["pattern"], entry.get("flags")),
            ))

        # Completion detection
        comp = config.get("completion", {})
        self._completion_strategy: str = comp.get("strategy", "process_exit")
        if self._completion_strategy not in ("pattern", "prompt_count", "process_exit"):
            raise ValueError(
                f"Unknown completion.strategy: {self._completion_strategy!r} "
                f"(valid: 'pattern', 'prompt_count', 'process_exit')"
            )
        self._completion_recent_chars: int = comp.get("recent_chars", 500)
        self._min_output_length: int = comp.get("min_output_length", 100)
        self._error_search_full: bool = comp.get("error_search_full", True)

        # Pattern strategy
        cp = comp.get("completion_pattern")
        self._completion_pattern: re.Pattern[str] | None = (
            _compile(cp, comp.get("completion_flags")) if cp else None
        )
        ep = comp.get("error_pattern")
        self._error_pattern: re.Pattern[str] | None = (
            _compile(ep, comp.get("error_flags")) if ep else None
        )
        sp = comp.get("shell_prompt_pattern")
        self._shell_prompt_pattern: re.Pattern[str] | None = (
            _compile(sp, comp.get("shell_prompt_flags")) if sp else None
        )

        # Prompt-count strategy
        pp = comp.get("prompt_pattern")
        self._prompt_pattern: re.Pattern[str] | None = (
            _compile(pp, comp.get("prompt_flags")) if pp else None
        )
        self._prompt_count_threshold: int = comp.get("prompt_count_threshold", 2)
        fp = comp.get("fallback_summary_pattern")
        self._fallback_summary_pattern: re.Pattern[str] | None = (
            _compile(fp, comp.get("fallback_summary_flags")) if fp else None
        )

        # Auto-confirm rules (ordered)
        self._confirm_rules: list[tuple[re.Pattern[str], ConfirmAction]] = []
        for rule in config.get("confirm", []):
            self._confirm_rules.append((
                _compile(rule["pattern"], rule.get("flags")),
                ConfirmAction(
                    response=rule.get("response", ""),
                    send_enter=rule.get("send_enter", True),
                ),
            ))

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
        """Detect state using configured strategy."""
        recent = output[-self._state_recent_chars:] if len(output) > self._state_recent_chars else output
        if self._strip_ansi:
            from cam.utils.ansi import strip_ansi
            recent = strip_ansi(recent)

        if self._state_strategy == "last":
            # Find the last matching pattern (most recent activity wins)
            last_pos = -1
            last_state: AgentState | None = None
            for state, pattern in self._state_patterns:
                for m in pattern.finditer(recent):
                    if m.start() > last_pos:
                        last_pos = m.start()
                        last_state = state
            return last_state
        else:
            # "first" strategy: return first matching pattern
            for state, pattern in self._state_patterns:
                if pattern.search(recent):
                    return state
            return None

    def should_auto_confirm(self, output: str) -> ConfirmAction | None:
        """Check confirm rules in order, return first match."""
        if self._strip_ansi:
            from cam.utils.ansi import strip_ansi
            output = strip_ansi(output)
        # Strip trailing whitespace per line (SSH captures pad lines to
        # terminal width), then take last 500 chars for pattern matching.
        clean = "\n".join(line.rstrip() for line in output.splitlines()).rstrip()
        recent = clean[-500:] if len(clean) > 500 else clean

        for pattern, action in self._confirm_rules:
            if pattern.search(recent):
                return action
        return None

    def detect_completion(self, output: str) -> AgentStatus | None:
        """Detect completion using configured strategy."""
        if self._completion_strategy == "process_exit":
            return None  # Rely on session exit

        if self._completion_strategy == "prompt_count":
            return self._detect_completion_prompt_count(output)

        # Default: "pattern" strategy
        return self._detect_completion_pattern(output)

    def _detect_completion_pattern(self, output: str) -> AgentStatus | None:
        """Pattern-based completion detection (like Codex/Aider)."""
        if self._strip_ansi:
            from cam.utils.ansi import strip_ansi
            output = strip_ansi(output)

        # Check errors (optionally in full output)
        if self._error_pattern:
            search_text = output if self._error_search_full else (
                output[-self._completion_recent_chars:] if len(output) > self._completion_recent_chars else output
            )
            if self._error_pattern.search(search_text):
                return AgentStatus.FAILED

        recent = output[-self._completion_recent_chars:] if len(output) > self._completion_recent_chars else output

        if self._completion_pattern and self._completion_pattern.search(recent):
            return AgentStatus.COMPLETED

        if (
            self._shell_prompt_pattern
            and self._shell_prompt_pattern.search(recent)
            and len(output) > self._min_output_length
        ):
            return AgentStatus.COMPLETED

        return None

    def _detect_completion_prompt_count(self, output: str) -> AgentStatus | None:
        """Prompt-count completion detection (like Claude)."""
        if not self._prompt_pattern:
            return None

        if self._strip_ansi:
            from cam.utils.ansi import strip_ansi
            clean = strip_ansi(output)
        else:
            clean = output

        count = len(self._prompt_pattern.findall(clean))
        if count >= self._prompt_count_threshold:
            return AgentStatus.COMPLETED

        # Fallback: single prompt + summary pattern
        if (
            count == 1
            and self._fallback_summary_pattern
            and self._fallback_summary_pattern.search(clean)
        ):
            return AgentStatus.COMPLETED

        return None

    def is_ready_for_input(self, output: str) -> bool:
        if not self._ready_pattern:
            return True
        if self._strip_ansi:
            from cam.utils.ansi import strip_ansi
            output = strip_ansi(output)
        return bool(self._ready_pattern.search(output))

    def get_startup_wait(self) -> float:
        return self._startup_wait

    def needs_prompt_after_launch(self) -> bool:
        return self._prompt_after_launch
