# Cross-Server skills validation — 2026-10-20

Updated `managing-camc`, `camc-messaging` and `managing-cam-tui` to describe
the implemented cross-Server CLI path. The complete built-in inventory is
six skills and thirteen files, including the new `reference/cross-server.md`.

The guides distinguish local-first lookup from explicit `Node:agent`
addressing, explain `run --node`, list the ten supported per-agent operations,
and separate those from TUI fleet discovery/terminal attach. They also cover
stdout/stderr/exit status, stable logical threads versus individual transport
message IDs, explicit remote replies, and recovery without resubmitting an
operation whose outcome is unknown.

The builder preserves skill files byte-for-byte, including Python import
statements inside Markdown examples. The regression test compares every
embedded file with its source. Package and built-in metadata remain compatible
with the existing Claude-specific frontmatter extensions.

## Validation

- Full Python suite: **1744 passed, 1 skipped** (210.48 seconds).
- Focused build/skill/protocol/worker tests: **75 passed**.
- Separate Server/Client process tests: **14 passed**, including real tmux
  disposable agents, all ten operations, stdout/exit-code forwarding,
  ambiguous-name rejection, local precedence, independent replies through
  another Client, deduplication and uncertain-result handling.
- The production-singleton test was excluded from this run because the user's
  production Server is already active. This skill change does not alter it.
- All six skills pass YAML/common-format and reference validation; existing
  Claude extension fields are preserved and omitted only in the validator's
  temporary common-format copy.
- The built artifact installs all thirteen files unchanged into Claude,
  Codex, Cursor and canonical agents skill directories and parses as Python 3.6.
- An isolated SDK rollout fixture verifies Server reconnection, unchanged
  non-backend configuration, preservation of the original SDK release and
  loading of all thirteen files by a fresh worker. Its processes are reclaimed.

## Global rollout

Release: **camc 1.2.194**. Targets are PDX046, PDX064, PDX086, PDX106,
DC7-26, KIX001 and KIX009. Scope includes Home/tool profiles, active skillm
libraries, installed workspace copies, personal/shared camc, and Server SDK
skills used when remotely starting agents.

For SDKs, create a derived release with updated skills and reconnect the
Server against it. Preserve old releases, Node identities, credentials,
operation state and agent sessions. Do not replace the Server/TUI binaries
as part of this skills rollout.

Verify installed documents, both CLI artifact hashes, the active SDK and
Server readiness on every Node after deployment. Leave the two historical
mode-0555 PDX106 controller archives without new skill installations, as in
the preceding [fleet sync](cam-skills-sync-20261020.md). Back up changed copies
under each Home's private `~/.cam/backups/skills-sync-20261020-*` directory.
