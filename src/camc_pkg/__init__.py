"""camc — Standalone coding agent manager.

Manages AI coding agents (Claude Code, Codex, Cursor) via tmux sessions.
Auto-confirm, state detection, completion detection, background monitoring.
"""

import logging
import os
import sys

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [camc] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("camc")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAM_DIR = os.path.expanduser("~/.cam")
CONFIGS_DIR = os.path.join(CAM_DIR, "configs")
LOGS_DIR = os.path.join(CAM_DIR, "logs")
AGENTS_FILE = os.path.join(CAM_DIR, "agents.json")
CONTEXT_FILE = os.path.join(CAM_DIR, "context.json")
SOCKETS_DIR = "/tmp/cam-sockets"

_DEFAULT_CONTEXT = {"name": None, "host": None, "port": None, "env_setup": None}
