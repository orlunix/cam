# CAM — Coding Agent Manager

PM2 for AI coding agents. Manage Claude Code, Codex, Aider, and other AI coding tools from a unified CLI.

## Install

```bash
pip install -e .
```

## Quick Start

```bash
cam doctor                                          # Check environment
cam context add my-project /path/to/project         # Add a context
cam run claude "Add error handling to the API"      # Run an agent
cam list                                            # List agents
cam logs <agent-id> -f                              # Follow output
cam stop <agent-id>                                 # Stop agent
```
