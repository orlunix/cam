# `camc archive` — capture an agent's full history

`camc archive` packages a single agent's tmux scrollback + Claude session
JSONL + monitor logs into a tar.gz, plus three subcommands to inspect
existing archives.

## Create an archive

```bash
camc archive <agent>                   # tar.gz under ~/.cam/archives/
camc archive <agent> -o /tmp/foo.tgz   # custom output path
camc archive <agent> --session-id <sid> # resume a specific Claude session jsonl
```

Archive filename: `<agent-id>-<session-id>-<YYYYMMDDHHMMSS>[-<name>].tar.gz`.
The name suffix is appended (not overriding) so multiple archives at the
same time keep distinct files.

`camc rm <agent> --archive` runs the archive step before removing.
Default `rm` does NOT archive (workflows produce too much noise).

## Inspect existing archives

```bash
camc archive list                      # all archives as a table
camc archive info <archive-name>       # header + manifest + last assistant text
camc archive summary <archive-name>    # per-prompt table (line + summary col)
camc archive show <archive-name>       # full conversation Q/A order (pipe to less)
```

Examples:

```bash
camc archive list
# Date         Agent     Session-id  Name          Size
# 2026-04-23   8d5c354d  8c84ca2e    aicli         1.2M
# ...

camc archive info 8d5c354d-8c84ca2e-...tar.gz
# (header) cwd: /home/hren/test/aicli  tool: claude  prompts: 17
# (manifest) capture-pane.txt, session.jsonl, monitor-8d5c354d.log
# (tail)    last assistant text — 12 lines

camc archive summary 8d5c354d-...tar.gz
# Q#   PROMPT (line)              SUMMARY (one line)
# 1    @file.py 改成 ...           Updated file.py to ...
# 2    跑一下 tests              Ran pytest, 3 fails, ...

camc archive show 8d5c354d-...tar.gz | less
# Q1: ...
# A1: ...
# Q2: ...
```

## When to archive

- Before `camc rm` if the conversation is worth keeping
- End of a long debugging session
- Before cross-machine reboot (so context survives if anything goes wrong)

Archive is **opt-in by default** — short-lived workflow agents shouldn't
auto-archive (would generate hundreds of tarballs). For agents you care
about, run `camc archive <id>` explicitly or `camc rm <id> --archive`.
