# Per-Agent System Prompt (dev note)

Status: **not implemented** — design draft, 2026-06-11.

## Motivation

Today `camc run` launches an agent with only the user's task prompt.
The agent's "persona" or system instructions are governed entirely by
whatever `CLAUDE.md` / `AGENTS.md` happens to sit in the working
directory. There is no first-class way to:

- Inject a per-agent persona without permanently editing the workdir
- Carry that persona across `camc reboot` / host moves
- Inspect what persona an agent was launched with via `camc status`
- Clean up the injection when the agent is removed

## Underlying mechanism (no new tool support needed)

Each supported tool natively auto-loads a file from its workdir:

| Tool        | File          |
|-------------|---------------|
| Claude Code | `CLAUDE.md`   |
| Codex       | `AGENTS.md`   |
| Cursor      | `AGENTS.md` (and `.cursorrules` if present) |

So the cleanest implementation is to **write the system prompt to that
file before launch**. The tool picks it up on its own.

## Proposed CLI

```bash
# Inline
camc run "fix this bug" --system-prompt "You are a Rust expert..." -n bug-rust

# From a file (preferred for non-trivial prompts)
camc run "review the PR" --system-file ~/prompts/code-reviewer.md -n review-1
```

Both flags resolve to the same internal field: `agent.task.system_prompt`.

## Behavior

### On `camc run`

1. Resolve target file:
    - `claude` → `<workdir>/CLAUDE.md`
    - `codex` → `<workdir>/AGENTS.md`
    - `cursor` → `<workdir>/AGENTS.md`
2. Append a marker-delimited block:
   ```
   <!-- camc:<agent_id> begin -->
   <prompt text>
   <!-- camc:<agent_id> end -->
   ```
3. If the file already contains a block for the same agent_id, replace it
   in place (idempotent re-runs).
4. Record the prompt + the file path on the agent record:
   ```json
   "task": {
     "system_prompt": "...",
     "system_prompt_file": "/path/to/CLAUDE.md"
   }
   ```
5. Spawn the tool as usual — it loads the file automatically.

### On `camc reboot`

If `task.system_prompt` is set and the marker block is missing from the
file (e.g. user wiped it manually, or rebooting on a different host),
re-inject before relaunching.

### On `camc rm`

Strip just the marker-delimited block. Pre-existing content above/below
is preserved. If the block was the only content, leave the file empty
but DO NOT delete it (file may be tracked in source control).

### On `camc status`

Show the system prompt (truncated to ~200 chars by default; `--full` for
the whole thing):

```
System: You are a Rust expert. When suggesting code...
```

## Append vs overwrite

**Default: append (with markers).** Reason:
- Workdirs often already have a project-wide `CLAUDE.md` / `AGENTS.md`
- Stomping that file is a footgun
- Markers let `camc rm` clean up our part precisely

`--system-overwrite` flag could be offered later for the "I want a clean
slate" case, but should NOT be the default.

## Conflict handling: multiple agents same workdir

Two agents in the same workdir share `CLAUDE.md` / `AGENTS.md`. Each
gets its own marker-delimited block, both appended. The tool reads the
whole file, so both personas merge.

If that's undesirable, the operator's escape hatch is: use separate
workdirs (which is the recommended pattern for camc anyway — agents are
typically tied to a context_path).

## Tracking

Store on the agent record:
```json
"task": {
  ...,
  "system_prompt": "...full text or null...",
  "system_prompt_file": "/abs/path/to/CLAUDE.md",
  "system_prompt_source": "inline" | "file:/path/to/source"
}
```

This is enough to display, reboot, and rm cleanly.

## Open questions

- Should `camc update --system-prompt "..."` allow editing a running
  agent's persona? It would only take effect on next reboot / restart.
- Should we support per-tool overrides? e.g. an agent that may be
  rebooted as either claude or codex (rare, but happens) needs both
  files written. Could just default to "write the file matching the
  current tool" and rewrite on reboot if tool changes.
- Should `cam release` / `cam sync` propagate system_prompt files to
  remote hosts? No — they live in the per-agent workdir, which the
  monitor manages locally.

## Out of scope (deliberately)

- Direct injection via the tool's API (Claude Agent SDK / OpenAI
  Chat Completions `system:`). camc is process-level; staying out
  of the tool's wire protocol keeps it portable.
- Persona libraries / templates. Operator can just point
  `--system-file` at any file.
- Server-side persona governance (RBAC over personas). cam serve
  could add this later if needed.

## Effort estimate

Small. Roughly:
- `camc_pkg/cli.py` — parse `--system-prompt` / `--system-file`,
  call new helper before launch (~30 lines)
- `camc_pkg/system_prompt.py` — new module, write/replace/strip block
  helpers (~80 lines)
- Schema bump in `agent_schema.py` — add three fields (~5 lines)
- `camc rm` — call strip helper (~5 lines)
- `camc reboot` — call write helper if record has system_prompt (~5 lines)
- `camc status` — render the prompt (~10 lines)
- Tests — file-write + rm round-trip (~50 lines)

Total: maybe 200 lines of code + tests. Half a day.
