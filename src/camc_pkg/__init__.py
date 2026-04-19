"""camc — Standalone coding agent manager.

Manages AI coding agents (Claude Code, Codex, Cursor) via tmux sessions.
Auto-confirm, state detection, completion detection, background monitoring.
"""

import logging
import os
import sys

__version__ = "1.2.0"
__build__ = ""  # populated by build_camc.py: "git-hash date"

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
EVENTS_FILE = os.path.join(CAM_DIR, "events.jsonl")
MACHINES_FILE = os.path.join(CAM_DIR, "machines.json")
CONTEXTS_FILE = os.path.join(CAM_DIR, "contexts.json")
PIDS_DIR = os.path.join(CAM_DIR, "pids")
CONTEXT_FILE = os.path.join(CAM_DIR, "context.json")  # Legacy single-machine context
SOCKETS_DIR = "/tmp/cam-sockets"

_DEFAULT_CONTEXT = {"name": None, "host": None, "port": None, "env_setup": None}
