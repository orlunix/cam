"""Hierarchical configuration system for CAM.

Loads configuration from multiple sources in priority order:
1. Built-in defaults (in code)
2. Global config: ~/.config/cam/config.toml
3. Project config: .cam/config.toml (searched in CWD and parents)
4. Environment variables: CAM_* prefix
5. CLI overrides (passed as kwargs)
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from cam.constants import (
    CONFIG_DIR,
    DATA_DIR,
    DEFAULT_HEALTH_CHECK_INTERVAL,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PROBE_COOLDOWN,
    DEFAULT_PROBE_STABLE_SECONDS,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    GLOBAL_CONFIG,
    LOG_DIR,
    PROJECT_CONFIG,
)


class GeneralConfig(BaseModel):
    """General CAM settings."""

    default_tool: str = "claude"
    default_timeout: str = "30m"
    auto_confirm: bool = True
    log_level: str = "info"


class MonitorConfig(BaseModel):
    """Process monitoring settings."""

    poll_interval: int = DEFAULT_POLL_INTERVAL
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    health_check_interval: int = DEFAULT_HEALTH_CHECK_INTERVAL
    probe_detection: bool = True
    probe_stable_seconds: int = DEFAULT_PROBE_STABLE_SECONDS
    probe_cooldown: int = DEFAULT_PROBE_COOLDOWN


class RetryConfig(BaseModel):
    """Retry and backoff settings."""

    max_retries: int = 0
    backoff_base: float = 2.0
    backoff_max: float = 300.0


class DisplayConfig(BaseModel):
    """Display and output settings."""

    color: bool = True
    unicode: bool = True
    compact: bool = False


class SecurityConfig(BaseModel):
    """Security and sandboxing settings."""

    encrypt_tokens: bool = True
    sandbox: bool = False


class PathsConfig(BaseModel):
    """Path configuration."""

    data_dir: str = str(DATA_DIR)
    log_dir: str = str(LOG_DIR)


class ServerConfig(BaseModel):
    """API server settings."""

    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
    auth_token: str | None = None
    log_level: str = "info"
    relay_url: str | None = None
    relay_token: str | None = None


class ToolConfig(BaseModel):
    """Per-tool configuration."""

    default_args: list[str] = Field(default_factory=list)
    auto_confirm_patterns: list[str] = Field(default_factory=list)


class CamConfig(BaseModel):
    """Root configuration model."""

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    tools: dict[str, ToolConfig] = Field(default_factory=dict)


def parse_duration(s: str | None) -> int | None:
    """Parse duration string into seconds.

    Args:
        s: Duration string like "30m", "2h", "1d", or "300" (plain seconds)

    Returns:
        Number of seconds

    Raises:
        ValueError: If the duration format is invalid

    Examples:
        >>> parse_duration("30")
        30
        >>> parse_duration("30s")
        30
        >>> parse_duration("5m")
        300
        >>> parse_duration("2h")
        7200
        >>> parse_duration("1d")
        86400
    """
    if not s:
        return None

    s = s.strip()
    if not s:
        return None

    # Plain number (seconds)
    if s.isdigit():
        return int(s)

    # Parse with unit
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd])$", s.lower())
    if not match:
        raise ValueError(
            f"Invalid duration format: {s}. "
            "Expected format: '30', '30s', '5m', '2h', or '1d'"
        )

    value, unit = match.groups()
    value = float(value)

    units = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }

    return int(value * units[unit])


def _find_project_config() -> Path | None:
    """Walk up from CWD looking for .cam/config.toml.

    Returns:
        Path to project config file if found, None otherwise
    """
    current = Path.cwd()

    # Walk up the directory tree
    for parent in [current, *current.parents]:
        config_path = parent / PROJECT_CONFIG
        if config_path.exists():
            return config_path

    return None


def _load_toml(path: Path) -> dict:
    """Load a TOML file.

    Args:
        path: Path to TOML file

    Returns:
        Parsed TOML data as dict, or {} if file doesn't exist
    """
    if not path.exists():
        return {}

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        # Log warning but don't fail - just skip this config source
        print(f"Warning: Failed to load {path}: {e}")
        return {}


def _merge_dicts(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries.

    Args:
        base: Base dictionary
        override: Dictionary with overriding values

    Returns:
        Merged dictionary (creates a new dict)
    """
    result = base.copy()

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Recursively merge nested dicts
            result[key] = _merge_dicts(result[key], value)
        else:
            # Override scalar values or lists
            result[key] = value

    return result


def _apply_env_vars(config: dict) -> dict:
    """Apply CAM_* environment variables to config.

    Environment variables use the format:
    - CAM_SECTION_KEY (e.g., CAM_GENERAL_LOG_LEVEL)
    - CAM_KEY (e.g., CAM_LOG_LEVEL) - applied to general section

    Args:
        config: Configuration dictionary

    Returns:
        Updated configuration dictionary
    """
    result = config.copy()

    # Mapping of env var patterns to config paths
    env_mappings = {
        # General settings
        "CAM_DEFAULT_TOOL": ("general", "default_tool"),
        "CAM_DEFAULT_TIMEOUT": ("general", "default_timeout"),
        "CAM_AUTO_CONFIRM": ("general", "auto_confirm"),
        "CAM_LOG_LEVEL": ("general", "log_level"),
        # Monitor settings
        "CAM_POLL_INTERVAL": ("monitor", "poll_interval"),
        "CAM_IDLE_TIMEOUT": ("monitor", "idle_timeout"),
        "CAM_HEALTH_CHECK_INTERVAL": ("monitor", "health_check_interval"),
        "CAM_PROBE_DETECTION": ("monitor", "probe_detection"),
        "CAM_PROBE_STABLE_SECONDS": ("monitor", "probe_stable_seconds"),
        "CAM_PROBE_COOLDOWN": ("monitor", "probe_cooldown"),
        # Retry settings
        "CAM_MAX_RETRIES": ("retry", "max_retries"),
        "CAM_BACKOFF_BASE": ("retry", "backoff_base"),
        "CAM_BACKOFF_MAX": ("retry", "backoff_max"),
        # Display settings
        "CAM_COLOR": ("display", "color"),
        "CAM_UNICODE": ("display", "unicode"),
        "CAM_COMPACT": ("display", "compact"),
        # Security settings
        "CAM_ENCRYPT_TOKENS": ("security", "encrypt_tokens"),
        "CAM_SANDBOX": ("security", "sandbox"),
        # Path settings
        "CAM_DATA_DIR": ("paths", "data_dir"),
        "CAM_LOG_DIR": ("paths", "log_dir"),
        # Server settings
        "CAM_SERVER_HOST": ("server", "host"),
        "CAM_SERVER_PORT": ("server", "port"),
        "CAM_SERVER_AUTH_TOKEN": ("server", "auth_token"),
        "CAM_SERVER_LOG_LEVEL": ("server", "log_level"),
        "CAM_RELAY_URL": ("server", "relay_url"),
        "CAM_RELAY_TOKEN": ("server", "relay_token"),
    }

    for env_var, (section, key) in env_mappings.items():
        value = os.environ.get(env_var)
        if value is not None:
            # Ensure section exists
            if section not in result:
                result[section] = {}

            # Parse value based on type
            if key in ("auto_confirm", "color", "unicode", "compact", "encrypt_tokens", "sandbox", "probe_detection"):
                # Boolean values
                value = value.lower() in ("true", "1", "yes", "on")
            elif key in ("poll_interval", "idle_timeout", "health_check_interval", "max_retries", "probe_stable_seconds", "probe_cooldown", "port"):
                # Integer values
                value = int(value)
            elif key in ("backoff_base", "backoff_max"):
                # Float values
                value = float(value)

            result[section][key] = value

    return result


def load_config(**cli_overrides: Any) -> CamConfig:
    """Load configuration from all sources and merge them.

    Sources are merged in priority order (later overrides earlier):
    1. Built-in defaults (Pydantic model defaults)
    2. Global config (~/.config/cam/config.toml)
    3. Project config (.cam/config.toml)
    4. Environment variables (CAM_*)
    5. CLI overrides (keyword arguments)

    Args:
        **cli_overrides: Configuration overrides from CLI
            Can use flat keys like log_level="debug" or nested dicts

    Returns:
        Validated CamConfig instance

    Examples:
        >>> config = load_config()
        >>> config = load_config(log_level="debug")
        >>> config = load_config(general={"log_level": "debug"})
    """
    # Start with empty dict (Pydantic defaults will fill in)
    config_dict: dict[str, Any] = {}

    # 1. Built-in defaults are handled by Pydantic

    # 2. Load global config
    global_config = _load_toml(GLOBAL_CONFIG)
    if global_config:
        config_dict = _merge_dicts(config_dict, global_config)

    # 3. Load project config
    project_config_path = _find_project_config()
    if project_config_path:
        project_config = _load_toml(project_config_path)
        if project_config:
            config_dict = _merge_dicts(config_dict, project_config)

    # 4. Apply environment variables
    config_dict = _apply_env_vars(config_dict)

    # 5. Apply CLI overrides
    if cli_overrides:
        # Handle both flat keys and nested dicts
        # Flat keys like log_level="debug" get moved to general section
        cli_config: dict[str, Any] = {}
        flat_keys = {
            "default_tool",
            "default_timeout",
            "auto_confirm",
            "log_level",
        }

        for key, value in cli_overrides.items():
            if key in flat_keys:
                # Move to general section
                if "general" not in cli_config:
                    cli_config["general"] = {}
                cli_config["general"][key] = value
            else:
                cli_config[key] = value

        config_dict = _merge_dicts(config_dict, cli_config)

    # Parse and validate with Pydantic
    return CamConfig(**config_dict)
