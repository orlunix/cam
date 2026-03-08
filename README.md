# CAM — Coding Agent Manager

PM2 for AI coding agents. Manage Claude Code, Codex, Cursor, and other AI coding tools from a unified CLI, REST API, mobile app, or DAG scheduler.

CAM wraps each agent in a TMUX session, monitors its output for state changes, handles permission prompts automatically, and detects task completion — so you can launch agents, walk away, and check results later.

## Features

- **Multi-tool support** — Claude Code, Codex, Cursor, and any CLI tool via TOML adapter configs
- **Auto-confirm** — automatically approve permission prompts (trust dialogs, tool approvals)
- **Completion detection** — pattern-based detection of when an agent finishes its task
- **Background monitor** — detach mode with a per-agent subprocess that handles auto-confirm and state tracking
- **DAG scheduler** — define multi-agent workflows with dependencies in YAML
- **Remote execution** — run agents on remote machines via SSH with ControlMaster connection pooling
- **REST API** — FastAPI server with token auth, WebSocket events, file management
- **WebSocket relay** — zero-dependency relay server for NAT traversal (phone → relay → home server)
- **Mobile app** — Android PWA/WebView app for managing agents from your phone
- **Probe-based idle detection** — send invisible probe characters to confirm agent is truly idle

## Install

```bash
pip install -e .
```

Optional dependencies:

```bash
pip install -e ".[dev]"      # pytest
pip install -e ".[yaml]"     # DAG scheduler (pyyaml)
pip install -e ".[remote]"   # WebSocket transport (websockets)
pip install -e ".[server]"   # API server (fastapi, uvicorn)
pip install -e ".[all]"      # Everything
```

Requirements: Python 3.10+, tmux installed and on PATH.

## Quick Start

```bash
cam doctor                                          # Check environment
cam context add my-project /path/to/project         # Register a project directory
cam run claude "Add error handling to the API"      # Launch an agent
cam list                                            # List all agents
cam logs <agent-id> -f                              # Follow live output
cam status <agent-id>                               # Detailed agent status
cam stop <agent-id>                                 # Graceful stop
```

### Background Mode

```bash
cam run claude "Refactor the auth module" --detach  # Launch and detach
cam list                                            # Check status anytime
cam attach <agent-id>                               # Reattach to TMUX session
```

### Auto-confirm and Auto-exit

```bash
cam run claude "Fix all lint errors" --auto-confirm --auto-exit
# Agent runs unattended: approves prompts, detects completion, finalizes
```

### DAG Workflows

```bash
cam apply tasks.yaml                                # Run a multi-agent workflow
```

Example `tasks.yaml`:
```yaml
version: 1
defaults:
  tool: claude
  timeout: 600
tasks:
  - id: lint
    prompt: "Fix all lint errors"
  - id: tests
    prompt: "Add missing unit tests"
    depends_on: [lint]
  - id: docs
    prompt: "Update API documentation"
    depends_on: [lint]
  - id: review
    prompt: "Review all changes and summarize"
    depends_on: [tests, docs]
```

## Contexts

Contexts are named project directories that agents work in.

```bash
cam context add frontend /home/user/projects/webapp
cam context add backend /home/user/projects/api
cam context list

# Remote context (SSH)
cam context add remote-box /home/user/project \
    --host server.example.com --user deploy --port 22

# cam-agent transport (standardized binary protocol)
cam context add managed /home/user/project \
    --transport agent --host server.example.com --user deploy
```

## Adapter Configuration

Each tool is configured via a TOML file in `src/cam/adapters/configs/`. The adapter config defines:

- **Launch command** and startup behavior
- **Ready detection** — regex patterns to detect when the agent is ready for input
- **State detection** — patterns that map output to states (planning, editing, testing, committing)
- **Completion detection** — how to tell when a task is finished
- **Auto-confirm rules** — patterns and responses for permission prompts
- **Probe settings** — idle detection via invisible characters
- **Monitor settings** — cooldowns, thresholds, auto-exit behavior

Example (abbreviated `claude.toml`):

```toml
[adapter]
name = "claude"
display_name = "Claude Code"

[launch]
command = ["claude", "--allowed-tools", "Bash,Edit,Read,Write,Glob,Grep"]
prompt_after_launch = true
startup_wait = 30.0

ready_pattern = "^[❯>]"
ready_flags = ["MULTILINE"]

[completion]
strategy = "prompt_count"
prompt_pattern = "^[❯>]"
prompt_count_threshold = 2

[[confirm]]
pattern = "Enter to (confirm|select).*Esc to cancel"
response = ""
send_enter = true
```

To add a new tool, create a `.toml` file in the configs directory — no Python code required.

## API Server

```bash
cam serve --host 0.0.0.0 --port 8420 --token <secret>
```

REST endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List agents |
| POST | `/api/agents` | Start an agent |
| GET | `/api/agents/:id` | Agent details |
| DELETE | `/api/agents/:id` | Stop agent |
| GET | `/api/agents/:id/output` | Terminal output (hash-based conditional) |
| POST | `/api/agents/:id/input` | Send text input |
| POST | `/api/agents/:id/key` | Send special key (Ctrl-C, Escape) |
| GET | `/api/contexts` | List contexts |
| POST | `/api/contexts` | Create context |
| WS | `/api/ws` | Real-time event stream |

### Relay Server

For accessing your home server from mobile over the internet:

```bash
# On public server:
python relay/relay.py --port 8001 --token <relay-token>

# On home server:
cam serve --port 8420 --token <api-token> \
    --relay ws://relay-host:8001 --relay-token <relay-token>
```

The relay is a standalone Python script with zero dependencies (stdlib-only WebSocket implementation with manual RFC 6455 framing). It bridges REST-over-WebSocket requests between mobile clients and your CAM server.

## Mobile App

The `web/` directory contains a PWA that works as both a browser app and an Android WebView app.

- **Dashboard** — list running/completed agents with live status updates
- **Start Agent** — select context, tool, enter prompt, toggle auto-confirm/auto-exit
- **Agent Detail** — live terminal output, send input/keys, view logs
- **File Browser** — browse and read files in agent contexts
- **Settings** — server connection, relay configuration, cache management

Build the Android APK:

```bash
cd android && ./build.sh
```

## cam-agent

A Go binary (`cam-agent/`) that provides a standardized target protocol for running agents on remote machines. It wraps tmux on Linux and provides a uniform CLI interface:

```bash
cam-agent ping
cam-agent session create --name <id> -- <command>
cam-agent session capture --name <id>
cam-agent session send --name <id> --text "hello"
cam-agent session exists --name <id>
cam-agent session kill --name <id>
```

Build:

```bash
cd cam-agent && make build
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                    CLI (Typer)                   │
│  run · list · status · logs · stop · attach      │
├─────────────────────────────────────────────────┤
│                   Core Layer                     │
│  AgentManager · AgentMonitor · EventBus          │
│  Scheduler · Config · Models · Probe             │
├─────────────────────────────────────────────────┤
│                 Transport Layer                  │
│  Local · SSH · Agent · Docker · WebSocket        │
├─────────────────────────────────────────────────┤
│                 Adapter Layer                    │
│  ConfigurableAdapter (TOML-driven)               │
│  Claude · Codex · Cursor · Generic               │
├─────────────────────────────────────────────────┤
│              Session Backend                     │
│  tmux (Linux/macOS) · wintmux (Windows)          │
└─────────────────────────────────────────────────┘
```

## Directory Structure

```
cam/
├── src/cam/
│   ├── cli/             # Typer commands (run, list, stop, context, etc.)
│   ├── core/            # Agent manager, monitor, scheduler, models, config
│   ├── adapters/        # Tool adapters and TOML configs
│   │   └── configs/     # claude.toml, codex.toml, cursor.toml
│   ├── transport/       # Local, SSH, Agent, Docker, WebSocket transports
│   ├── storage/         # SQLite database, agent/context/history stores
│   ├── api/             # FastAPI server, routes, WebSocket, relay connector
│   └── utils/           # ANSI stripping, doctor, shell helpers
├── cam-agent/           # Go binary for standardized remote protocol
├── relay/               # Zero-dep WebSocket relay server
├── web/                 # PWA / Android WebView frontend
│   ├── js/              # API client, app shell, view modules
│   └── css/             # Styles
├── android/             # Android WebView app (Java)
├── tests/               # pytest test suite
├── scripts/             # Helper scripts (daemon launcher, test tools)
└── docs/                # Architecture documentation
```

## Configuration

Global config at `~/.config/cam/config.toml`:

```toml
[cam]
data_dir = "~/.local/share/cam"

[cam.defaults]
tool = "claude"
auto_confirm = false
auto_exit = false
timeout = 0

[cam.env_setup]
# Shell commands run before agent launch (e.g., activate virtualenv)
# commands = ["source ~/.nvm/nvm.sh"]
```

Data stored at `~/.local/share/cam/`:
- `cam.db` — SQLite database (agents, contexts, history)
- `pids/` — PID files for background monitor subprocesses

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
