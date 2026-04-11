# CAM — Coding Agent Manager

PM2 for AI coding agents. Manage Claude Code, Codex, Cursor, and other AI CLI tools via TMUX sessions, with auto-confirm, completion detection, background monitoring, and remote execution.

## Architecture

```
Mobile APP / CLI / Web
        ↓
   cam serve (API + Relay)
        ↓
   Core: AgentManager
        ↓
   CamcDelegate ──SSH (ControlMaster)──→ remote camc
        │                                    ↓
   local camc                          camc manages locally:
        │                              - tmux sessions
        │                              - monitor loops
   CamcPoller ←──SSH polling (5s)────  - agents.json
        ↓                              - auto-confirm/completion
   Storage: import to SQLite
```

**cam** is the server/orchestrator. **camc** is the standalone agent manager deployed to each machine. All agent operations are delegated from cam to camc.

- **camc** is the source of truth — each machine's `~/.cam/agents.json` is authoritative
- **cam serve** aggregates into SQLite for the API/mobile to query via CamcPoller
- **CamcDelegate** wraps `camc` CLI calls over SSH, using ControlMaster to piggyback on persistent connections

## Features

- **Multi-tool support** — Claude Code, Codex, Cursor, and any CLI tool via TOML adapter configs (no Python code needed)
- **Auto-confirm** — automatically approve permission prompts (trust dialogs, tool approvals, y/n prompts)
- **Completion detection** — pattern-based detection of when an agent finishes its task
- **Background monitor** — per-agent subprocess with auto-confirm, state tracking, idle detection, and stuck fallback
- **Auto-exit** — automatically kill idle agents after task completion
- **Remote execution** — run agents on remote machines via SSH with ControlMaster connection pooling
- **NFS cluster support** — hostname-based agent isolation for shared-disk environments
- **REST API** — FastAPI server with token auth, WebSocket events, hash-based conditional responses
- **WebSocket relay** — zero-dependency relay server for NAT traversal (mobile → relay → home server), with HTTP proxy fallback
- **Mobile app** — Android PWA/WebView app for managing agents from your phone
- **DAG scheduler** — define multi-agent workflows with dependencies in YAML
- **Self-healing** — monitors auto-restart on crash, `cam heal` / `camc heal` restart dead monitors

## Install

```bash
pip install -e ".[all,dev]"    # Full install with all deps + pytest
```

Or selectively:

```bash
pip install -e .               # Core CLI only
pip install -e ".[server]"     # API server (fastapi, uvicorn)
pip install -e ".[yaml]"       # DAG scheduler (pyyaml)
pip install -e ".[dev]"        # Test suite (pytest)
```

Requirements: Python 3.10+, tmux.

## Quick Start

```bash
cam doctor                                          # Check environment
cam context add my-project /path/to/project         # Register a project
cam run claude "Add error handling to the API"      # Launch an agent
cam list                                            # List all agents
cam logs <agent-id> -f                              # Follow live output
cam attach <agent-id>                               # Attach to TMUX session
cam stop <agent-id>                                 # Graceful stop
```

### Background Mode

```bash
cam run claude "Refactor the auth module" --detach  # Launch and detach
cam list                                            # Check status anytime
cam attach <agent-id>                               # Reattach to session
```

### Auto-confirm and Auto-exit

```bash
cam run claude "Fix all lint errors" --auto-confirm --auto-exit
# Agent runs unattended: approves prompts, detects completion, exits
```

### Sync and Health Check

```bash
cam sync                     # Deploy camc + configs to all remote machines
cam sync my-context          # Deploy to a specific context
cam heal                     # Check all agents, restart dead monitors (local + remote)
```

### DAG Workflows

```bash
cam apply tasks.yaml         # Run a multi-agent workflow
```

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
  - id: review
    prompt: "Review all changes"
    depends_on: [tests]
```

## Standalone CLI (camc)

Single-file, zero-dependency CLI for managing agents on any machine. Python 3.6+ stdlib only — no pip install needed. This is the core execution engine that runs on every machine.

```bash
camc run claude "Fix the bug"       # Launch an agent
camc run claude                     # Interactive mode
camc list                           # List agents (filtered by hostname)
camc logs <id> -f                   # Follow output
camc attach <id>                    # Attach to tmux session
camc stop <id>                      # Stop agent
camc heal                           # Restart dead monitors
camc capture <id>                   # Capture terminal output
camc send <id> --text "hello"       # Send text to agent
```

Deploy via `cam sync` or manually: `scp dist/camc remote:~/.local/bin/camc`

Storage: `~/.cam/agents.json` (source of truth), `~/.cam/logs/monitor-<id>.log`, `~/.cam/events.jsonl`

## Monitor Loop

The monitor is a pure screen-based, tool-agnostic design. No characters sent for idle detection — only for auto-confirm and stuck fallback. Runs as a background subprocess per agent, polling every ~1 second:

1. **Health check** (every 15s) — is the tmux session alive?
2. **Capture + hash** — MD5 of terminal output (last line stripped to avoid status bar flicker)
3. **Auto-confirm** (5s cooldown) — detect permission dialogs, send response per TOML rules
4. **State detection** — regex on recent output → planning/editing/testing/committing
5. **Idle detection** — screen stable 60s + prompt visible → idle confirmed (fast-track: 5s after done pattern)
6. **Stuck fallback** — screen frozen 120s + no prompt → send probe character
7. **Auto-exit** — idle + user not attached + auto_exit enabled → kill session

## Adapter Configuration

Each tool is configured via TOML in `src/cam/adapters/configs/`. Key sections:

- `[launch]` — command, prompt_after_launch, startup_wait, ready_pattern
- `[state]` — `[[state.patterns]]` mapping regex → agent state
- `[completion]` — strategy, prompt_pattern, threshold
- `[[confirm]]` — auto-confirm rules: pattern, response, send_enter
- `[monitor]` — cooldowns, busy_pattern, done_pattern, auto_exit

To add a new tool, create a `.toml` file — no Python code required.

## API Server

```bash
cam serve --host 0.0.0.0 --port 8420 --token <secret>
```

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List agents (supports `?status=running&limit=50`) |
| POST | `/api/agents` | Start an agent |
| GET | `/api/agents/:id` | Agent details |
| DELETE | `/api/agents/:id` | Stop agent |
| GET | `/api/agents/:id/output` | Terminal output (hash-based conditional) |
| POST | `/api/agents/:id/input` | Send text input |
| POST | `/api/agents/:id/key` | Send special key (Ctrl-C, Escape) |
| GET | `/api/contexts` | List contexts |
| GET | `/api/health` | Server health + adapters |
| WS | `/api/ws` | Real-time event stream |

### Relay Server

For accessing your home server from mobile over the internet:

```bash
# On public-facing server:
python relay/relay.py --port 8001 --token <relay-token> --web-root /path/to/web

# On home server:
cam serve --port 8420 --token <api-token> \
    --relay ws://relay-host:8001 --relay-token <relay-token>
```

The relay is a standalone Python script with zero dependencies (stdlib-only RFC 6455 WebSocket). It bridges REST-over-WebSocket between mobile clients and your CAM server. Also supports HTTP proxy mode for environments where WebSocket is blocked.

## Mobile App

The `web/` directory contains a PWA that works as both a browser app and an Android WebView wrapper (`android/`).

- **Dashboard** — running/completed agents with live status
- **Start Agent** — select context, tool, enter prompt, toggle auto-confirm/auto-exit
- **Agent Detail** — live terminal output, send input/keys
- **File Browser** — browse and read files in agent contexts
- **Contexts / Machines** — manage project directories and remote machines
- **Settings** — connection profiles, relay protocol (WS/HTTP), cache

## Machines and Contexts

Machines are defined in `~/.cam/machines.json`. Contexts reference machines.

```bash
# Remote context
cam context add my-project /home/user/project \
    --host server.example.com --user deploy --port 22
```

On NFS clusters where multiple machines share `~/.cam/agents.json`, camc uses hostname-based filtering to ensure each machine only manages its own agents.

## Directory Structure

```
cam/
├── src/cam/               # cam server package
│   ├── cli/               # Typer commands
│   ├── api/               # FastAPI server, routes, relay connector
│   ├── core/              # AgentManager, CamcDelegate, CamcPoller, Scheduler
│   ├── transport/         # SSH, Local, Agent transports
│   ├── storage/           # SQLite stores (agents, contexts, events)
│   └── adapters/configs/  # TOML adapter configs (claude, codex, cursor)
├── src/camc_pkg/          # camc source (built to dist/camc)
├── src/cam-agent/         # Go binary for standardized remote protocol
├── relay/                 # Zero-dep WebSocket relay server
├── web/                   # PWA frontend
│   ├── js/                # API client, app shell, view modules
│   └── css/               # Styles
├── android/               # Android WebView wrapper
├── tests/                 # pytest suite (~450 tests)
├── dist/                  # Built artifacts (camc)
└── docs/                  # Architecture docs, case studies
```

## Storage

| | cam serve | camc (per machine) |
|---|---|---|
| **Storage** | SQLite (`~/.local/share/cam/cam.db`) | JSON (`~/.cam/agents.json`) |
| **Agent ID** | 8-char hex (from camc) or full UUID (legacy) | 8-char hex via `uuid5(hostname+time+random)` |
| **Role** | Aggregated cache for API/mobile | Source of truth per machine |

## Testing

```bash
pip install -e ".[all,dev]"
pytest                    # Full suite (~450 tests)
pytest tests/test_foo.py  # Single file
pytest -k "test_name"     # By name
```

## License

MIT
