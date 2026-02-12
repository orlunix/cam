"""Global constants and default paths."""

from __future__ import annotations

import os
from pathlib import Path

# Version
VERSION = "0.1.0"

# XDG-compliant default paths
DATA_DIR = Path(os.environ.get("CAM_DATA_DIR", "~/.local/share/cam")).expanduser()
CONFIG_DIR = Path(os.environ.get("CAM_CONFIG_DIR", "~/.config/cam")).expanduser()
LOG_DIR = DATA_DIR / "logs"
SOCKET_DIR = DATA_DIR / "sockets"
PID_DIR = DATA_DIR / "pids"
ADAPTER_DIR = DATA_DIR / "adapters"

# Database
DB_PATH = DATA_DIR / "cam.db"

# Config file names
GLOBAL_CONFIG = CONFIG_DIR / "config.toml"
PROJECT_CONFIG = ".cam/config.toml"

# Schema version (for migrations)
SCHEMA_VERSION = 1

# Monitor defaults
DEFAULT_POLL_INTERVAL = 2  # seconds
DEFAULT_IDLE_TIMEOUT = 300  # seconds (0 = disabled)
DEFAULT_HEALTH_CHECK_INTERVAL = 30  # seconds

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2
EXIT_TIMEOUT = 3
EXIT_KILLED = 4
