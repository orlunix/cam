# CAM skills fleet sync — 2026-10-20

The canonical source is `src/camc_pkg/skills/`: six skills and twelve Markdown
files, including all reference documents:

- `managing-camc`
- `managing-cam-tui`
- `camc-diagnose`
- `camc-messaging`
- `camc-cron-loop`
- `camc-goal-loop`

## Scope

Updated Home installs for Claude, Codex, Cursor and the canonical agents
directory, existing tool profiles, each Node's active skillm library, and
managed-agent workspace copies. The Server SDK skill trees already matched the
source and required no replacement or Server restart.

| Node | Verified skill roots |
| --- | ---: |
| PDX046 | 26 |
| DC7-26 | 13 |
| KIX001 | 5 |
| KIX009 | 5 |
| PDX064 | 9 |
| PDX086 | 8 |
| PDX106 | 38 |
| Total | 104 |

Roots are logical installation paths; some share physical directories through
Home mounts or symlinks. Two historical PDX106 workspaces (`gsp-riscv-pilot-001`
and `gsp-riscv-pilot-002` controller archives) have mode-0555 directories and no
skills installed. No new skill directory was created inside those archives.

Changed copies were backed up under
`~/.cam/backups/skills-sync-20261020-*/<hostname>/`, with private manifests.
Unrelated skillm changes were preserved; no library was pushed. Existing agent
processes were not restarted. New sessions can discover the updated skills;
already loaded skill descriptions may remain cached in an existing session.

## Bundled documents and verification

The camc builder previously stripped Python `import` statements from Markdown
examples during import consolidation. Embedded documents now use escaped,
single-line Python literals, preserving their exact source content.

Personal `~/.cam/camc` and the shared `/home/prgn_share/bin/camc` entry point
were updated to 1.2.193, with SHA-256
`cf173e8a4d0958f4ec996b0c7b13e4c59f2b9f80df8ed9bc0efddd96eb414667`.
Old pinned release directories were retained.

Validation:

- Build/version, built-in inventory and installation-path tests: 26 passed.
- Built artifact preserves all twelve source documents and installs them
  unchanged into `.claude`, `.codex`, `.cursor` and `.agents` skill directories.
- Built artifact passes Python 3.6 grammar parsing.
- Per-Node verification checks installed files, active library files, Server
  SDK skill files, embedded documents and personal/shared artifact hashes.

Shared skillm requires Python 3.12 syntax; its updates were run with an already
installed Python 3.12 interpreter. System Python and agent environments were
not modified.
