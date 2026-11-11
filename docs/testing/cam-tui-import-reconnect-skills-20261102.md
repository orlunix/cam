# CAM TUI import/reconnect skill verification — 2026-11-02 UTC

## Scope

Updated `managing-cam-tui` to execute progressive session import and SSH/Node
reconnection with existing CAM interfaces. This adds skill instructions and a
Python 3.6+ public-key preparation helper; it does not add a bulk-import UI or
a background VNC scanner. Source, builtin, project Codex and the existing local
skillm library copies are synchronized. No commit or push was performed.

## Automated verification

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m pytest tests/test_tui_skill_ssh_setup.py tests/test_builtin_skills_inventory.py tests/test_skill_install_paths.py -q`

Result: **26 passed**. One existing pytest warning reports an unknown
`asyncio_mode` configuration option. Tests cover repeated authorization,
restricted entries, symlinks, ownership, read-only checks, concurrent writers,
builtin skill inventory and installation paths. The helper was also exercised
with the real system Python 3.6 and disposable OpenSSH keys.

A local smoke test used real tmux and production Python camc with simulated
Claude/Codex/Cursor/Pi executables. All four sessions were adopted and captured
from a fresh shell without inherited TMUX, retaining original pane PIDs. All
four records and tmux servers were removed.

## Cross-account integration: svcuser@node.example.com:22

Used the existing approved SSH route and existing remote Home/Server. No private
key was copied and no host-key checks were disabled.

| Stage | Result |
| --- | --- |
| Local NFS Home | Existing own-key authorization was already ready |
| Different remote Home | Authorized its existing `id_ed25519_o3rv.pub`; repeat was unchanged |
| First remote self-SSH | Correctly failed because its known_hosts lacked the host key; authorization remained ready |
| Remote self-SSH after trust verification | Succeeded with StrictHostKeyChecking=yes after matching the host ECDSA public key to this Client's already-trusted entry |
| Current Client access | Existing SSH credentials reached the different account without key/agent forwarding |
| Node connection | Added RISCV-test to the current Client and verified ready through snapshots |
| Session import | Four real remote tmux sessions, one per simulated tool, adopted without replacing their processes |
| Return path | Client capture returned each tool's expected marker and exit code 0; Terminal 0 was listed as primary |
| Disconnect/reconnect | Only the new test Node was disconnected; reconnect retained all four agent IDs and original pane PIDs |
| Independent failed endpoint | A bounded probe to port 1 failed; completed authorization and imported sessions remained valid |
| Cleanup | Four test agents, panes, monitors, sockets and the temporary remote directory removed; remote registry returned to zero |
| Retained success | Self-authorization, the verified known_hosts entry and the connected Node remain useful |

Standard Node onboarding updated the remote CAM Server from **0.1.3 to 0.1.5**,
reusing its identity. Remote production Python camc remained **1.2.194**;
the temporary 1.2.204 candidate used for adoption testing was removed.
The current Client returned to **82 agents**, with no test agents left visible.

## Compatibility findings

- The test account uses csh. POSIX probe scripts must explicitly use `/bin/sh -s`.
- A connect RPC accepting a request is not proof of a ready tunnel; snapshots
  must confirm ready, expected agent IDs and terminal output.
- Installed Client 0.1.7 returned `members`/`updated_nodes` for `node.sync` even
  when a node_id was supplied. That was NV peer sync, not target agent refresh.
  The skill now distinguishes this older behavior from `agents_synced` and
  verifies visibility through normal snapshots instead of reporting false success.
- Immediate capture can precede the tool's first output. The remote fixture
  initially captured empty panes; later capture and PID checks verified the
  original processes without restarting them.
- Native skillm `check` currently fails with `TypeError: check_cmd() got an
  unexpected keyword argument 'scan/__no_scan'`. No skillm code was changed.
  The existing local skill library was synchronized directly; `skillm add`
  was avoided because it automatically commits.

## Limits

The remote executables were simulated tools, not authenticated LLM sessions.
This test verifies real tmux adoption, Server/Client transport and reconnect,
not a graphical VNC desktop, a live LSF container namespace or LLM completion.
Raw terminal processes outside tmux cannot currently be adopted in place;
non-primary panes and session-name collisions require explicit mapping or
remain individually blocked. Histories are resume candidates, not alive agents.

Python camc 1.2.205 packages the final compatibility notes. The builder's
`--verify` accepted help output but rejected the normal `(dev)` versus release
stamp in `version`; independent verification normalizes that stamp while
checking the exact version and remaining output, embedded files and Python 3.6.
