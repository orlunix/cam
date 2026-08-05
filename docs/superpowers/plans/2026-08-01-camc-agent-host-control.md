# CAMC Agent Host Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every newly started or healed Codex CAMC agent durable guidance to retry sandbox-denied host operations once with `require_escalated`, while preserving existing workspace instructions and documenting a future MCP replacement.

**Architecture:** Codex skill installation remains the single lifecycle hook used by both `camc run` and skill refresh during `camc heal`. When the selected project config directory is `.codex`, the installer calls a focused `system_prompt` helper that idempotently ensures one CAMC-managed block in the workspace `AGENTS.md`; it skips insertion when equivalent user guidance already exists. Claude, Cursor, and generic tools are unchanged.

**Tech Stack:** Python stdlib, pytest, existing embedded-skill generator.

## Global Constraints

- Preserve all user-authored `AGENTS.md` content outside the CAMC marker block.
- Do not retry ordinary command failures; guidance applies only to explicit sandbox/access denials.
- Retry the exact command at most once with `sandbox_permissions: require_escalated` and a concise justification.
- Do not add the rule to Claude, Cursor, or generic-tool workspaces.
- No new runtime dependency, daemon, network listener, commit, or push.

---

### Task 1: Idempotent Codex Host-Escalation Guidance

**Files:**
- Modify: `src/camc_pkg/system_prompt.py`
- Modify: `src/camc_pkg/skills.py`
- Test: `tests/test_skill_install_paths.py`

**Interfaces:**
- Produces: `ensure_codex_host_escalation_guidance(workdir) -> str`, returning `created`, `updated`, `existing`, or `user_defined`.
- Consumes: `install_manifest_skills(workdir, force=False, config_dir=".claude")`.

- [x] Add tests proving `.codex` installation creates one marked block, repeated installation is byte-idempotent, equivalent user guidance prevents insertion, and non-Codex config directories remain untouched.
- [x] Run the focused tests and capture RED because the helper does not exist.
- [x] Implement the marker-delimited helper and invoke it only for `config_dir == ".codex"` after embedded skills are installed.
- [x] Run focused skill-install and system-prompt tests to GREEN.

### Task 2: Bundle and Regression Verification

**Files:**
- Generated: `dist/camc`
- Generated: `dist/BUILD_LOG.md`
- Generated: `src/camc_pkg/__init__.py`

- [x] Run the built-in skill inventory and relevant CAMC run/heal tests.
- [x] Build with `PYTHONPATH=src python3 build_camc.py`.
- [x] Verify bundled skill installation creates the managed guidance once and preserves user content.
- [x] Run `git diff --check` and report unrelated baseline failures separately.

Verification note: the focused skill-install suite passed 17/17 and the
combined run/heal suite passed 54 tests. The inventory check retains one
pre-existing failure because `managing-camc/SKILL.md` contains a bare
`camc heal`; this plan intentionally does not edit built-in skill content.

## Deferred MCP Host-Control Design

### Goal and ownership boundary

The durable end state replaces prompt-driven shell escalation with one CAMC
MCP server shared by MCP-capable clients. The server is host-owned and embedded
in the single-file `~/.cam/camc`; projects do not receive a copied server.
Codex, Claude, and Cursor register the same absolute command:

```text
~/.cam/camc mcp-server
```

One host server instance serves agents from every project on that host. Each
tool call carries its own `sender`, `target`, and, where applicable, `msg_id` or
`workdir`; the MCP connection itself is never an identity or routing source.
Remote hosts run their own CAMC MCP server. Cross-host routing remains a later
CAM/CAMC feature and is not part of the first implementation.

### Installation and upgrades

No Python package, Node package, daemon, network listener, or separate binary
is installed. `camc init` installs a user-level MCP entry named `camc-host` for
each detected supported tool. The first `camc run` repairs a missing entry for
the selected tool, and `camc heal --mcp` explicitly repairs all detected tool
configs and performs a connectivity probe. Installation must atomically merge
only the CAMC-owned entry, preserve every user-owned MCP server, and be
byte-idempotent when already current.

The configured command always points to `~/.cam/camc`, so replacing CAMC also
upgrades the MCP server. Project-level registration is a fallback only when a
tool or user policy forbids user-level configuration. Existing running agents
normally read MCP configuration only at tool startup: installation marks them
as pending restart but does not interrupt them. A later, explicit one-at-a-time
`camc reboot <agent>` activates MCP while preserving the CAMC record, workdir,
and tmux environment. The current Codex host-escalation guidance remains the
fallback for old or non-MCP sessions.

### Protocol compatibility

The preferred protocol is the stable MCP `2026-07-28` revision. It removes the
`initialize` session handshake in favor of `server/discover` and puts protocol
version, client identity, and capabilities in per-request `_meta`. CAMC must
also support the `2025-11-25` `initialize` flow because client upgrades will
not be synchronized. For stdio, a modern client may probe `server/discover`;
legacy clients continue with `initialize`. The server selects one protocol era
per stdio connection and never mixes their lifecycle messages.

All CAMC tools remain application-stateless under both eras. CAMC does not use
the newly deprecated roots, sampling, or MCP logging features; diagnostics go
to stderr. Tool input schemas use the common JSON Schema object subset so both
protocol eras can consume them. Results use deterministic tool ordering and a
small object-shaped `structuredContent` contract such as
`{ok, error, detail, data}` even though `2026-07-28` permits any JSON value.

References:

- [MCP 2026-07-28 stable release](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28)
- [2026 stateless lifecycle overview](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
- [stdio discovery and legacy fallback](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)

### Tool rollout

Phase 1 exposes only the mailbox operations required for agent collaboration:

- `camc_msg_send(sender, target, text, msg_id?)`
- `camc_msg_reply(sender, msg_id, text)`
- `camc_msg_read(agent, unread_only?, limit?)`
- `camc_ping()` for installation and host-access verification

After Phase 1 is reliable across Codex, Claude, and Cursor, add `send`, `key`,
`capture`, `stop`, and `rm`. These later tools call existing CAMC functions;
they do not introduce arbitrary shell execution. Destructive operations retain
their explicit target validation and client-side confirmation semantics.

### Host-access gate and security

MCP is only the primary path when its process can access the host CAMC mailbox
and tmux sockets. The installer must run a real `camc_ping` from each client's
MCP subprocess, not merely check that configuration parses. If a client starts
stdio MCP inside its shell sandbox and the probe receives `Operation not
permitted`, that client stays on the escalation-guidance fallback until a true
host broker is available; relabeling a sandboxed subprocess as MCP does not fix
permissions.

The server exposes an allowlisted CAMC operation set, validates agent IDs and
message sizes, never accepts an arbitrary command, and writes protocol output
only to stdout. Logs and tracebacks go to stderr with secrets and message bodies
redacted. It binds no TCP port in Phase 1, so there is no remote authentication
surface.

### Verification gate

The MCP path is not considered installable until tests prove all of the
following:

1. Both `2026-07-28` discovery and `2025-11-25` initialization reach the same
   tool handlers and return equivalent results.
2. User-level config merge is atomic, idempotent, preserves unrelated entries,
   and uses the absolute `~/.cam/camc` path.
3. Codex, Claude, and Cursor can each launch the configured server and complete
   `camc_ping` against real host state.
4. Two real agents exchange a threaded message without shell execution or
   `require_escalated`.
5. Existing agents continue working before reboot and gain MCP only after an
   explicit reboot; installation never restarts them in bulk.
6. Malformed targets, oversized messages, protocol text on stderr, sandboxed
   socket denial, and unsupported protocol versions fail with structured,
   non-secret errors.

The existing `camc-messaging` skill remains optional policy documentation. MCP
performs reliable host execution; the skill explains thread semantics, reply
discipline, and the CLI fallback.
