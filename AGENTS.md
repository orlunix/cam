# Agent Workflow Notes

## Cam Project Collaboration

When working in this repository, default to a reviewer/integrator split
when the sibling `cam-dev` agent is available:

- `cam-dev` implements changes.
- Codex reviews the diff, runs focused and full verification, asks for
  revisions when needed, and summarizes the result to the user.

Current known local sibling:

- name: `cam-dev`
- agent id: `f1a1a661`
- expected path: `/home/hren/.openclaw/workspace/cam`

Use `camc list`, `camc status f1a1a661`, and `camc capture f1a1a661
--lines N` to verify the agent is available before delegating. Send
implementation requests with `camc msg send f1a1a661 -t "..."`, using
clear scope, expected files, tests, and a parseable completion format.
For planner-style requests, prefix the prompt with an explicit preflight:
use `ls`, `grep`, and `find` first to inspect relevant files, paths, and
attributes before proposing or changing implementation details.

Direct local edits are acceptable for tiny mechanical changes, urgent
debugging/unblocks, or when `cam-dev` is unavailable. If bypassing
`cam-dev`, state the reason briefly.

## Ownership Split (user instruction, 2026-07-23)

- **Kimi (this assistant) owns DESKTOP work only** — `apps/cam-desktop/`
  and `web/` — because this machine has the desktop build/test/release
  environment (electron-builder, MSI/DMG, Release flow).
- **camc is owned by another agent** — do NOT edit `src/camc`,
  `src/camc_pkg/`, `dist/camc`, or `build_camc.py` on this machine.
  Route camc changes to the owning agent (see the cam-dev sibling
  above) instead of implementing them here.
- Exception: only when the user explicitly asks for a change outside
  the desktop scope may Kimi edit those files, and only for that
  specific ask.

Do not push unless the user explicitly authorizes it. Commit only when
the user asks for a commit or clearly approves the completed change.
