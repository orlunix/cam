# Claude Code Permission Dialog Patterns

Captured from Claude Code v2.1.91 on 2026-04-03. These are the actual terminal
screens that camc's auto-confirm must match.

## Dialog Structure

All permission dialogs share this structure:

```
────────────────────────────────── (full-width separator)
 <Title>                           (e.g. "Bash command", "Create file")

   <Details>                       (tool-specific content)

 <Permission rule or context>      (optional, e.g. "Permission rule Bash requires...")

 Do you want to proceed?           (or "Do you want to create/make this edit...")
 ❯ 1. Yes                          (highlighted option, cursor here)
   2. <session-level option>        (optional, depends on tool)
   3. No

 Esc to cancel · Tab to amend · ctrl+e to explain   (footer)
```

Key: the dialog always occupies the **bottom portion** of the screen, starting
with a full-width `─` separator line.

## Approval Method

Claude Code's Ink TUI is a **select menu**, not a text input. The highlighted
option (`❯`) is selected by pressing **Enter**. Pressing **"1"** also works
because the Ink menu consumes single-digit keypresses to jump to that option.

Both Enter and "1" approve the default (first) option.

---

## Pattern 1: Read File

**Trigger**: `Read(path)` on a file outside the project directory.

```
────────────────────────────────────────────────────
 Read file

  Read(/etc/hostname)

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, allow reading from etc/ during this session
   3. No

 Esc to cancel · Tab to amend
```

**Options**: 3 (Yes / Allow directory for session / No)
**Notes**: Option 2 grants session-wide read access to the parent directory.
Read within the project directory is auto-approved and shows no dialog.

## Pattern 2: Create File (Write)

**Trigger**: `Write(path)` creating a new file.

```
────────────────────────────────────────────────────
 Create file
 hello.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hello world
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create hello.txt?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
```

**Options**: 3 (Yes / Allow all edits for session / No)
**Notes**: Shows file content preview between `╌` dashed separators.
The question text is "Do you want to **create** <filename>?" (not "proceed").

## Pattern 3: Edit File

**Trigger**: `Edit(path)` modifying an existing file.

```
────────────────────────────────────────────────────
 Edit file
 hello.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 1 -hello world
 1   No newline at end of file
 2 +hello world
 3 +line 2
 4   No newline at end of file
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to make this edit to hello.txt?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
```

**Options**: 3 (Yes / Allow all edits for session / No)
**Notes**: Shows unified diff between `╌` separators (- removed, + added).
Question: "Do you want to **make this edit** to <filename>?"

## Pattern 4: Create File (Outside Project)

**Trigger**: `Write(path)` to a file outside project directory.

```
────────────────────────────────────────────────────
 Create file
 ../cam-perm-new-dir/test.py
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 import sys
  2
  3 def greet(name):
  ...
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create test.py?
 ❯ 1. Yes
   2. Yes, allow all edits in cam-perm-new-dir/ during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
```

**Notes**: Option 2 specifies the **directory name** ("allow all edits in
<dirname>/ during this session"). Path shown as relative (`../`).

## Pattern 5: Bash Command

**Trigger**: `Bash(command)` for any command not pre-authorized.

```
────────────────────────────────────────────────────
 Bash command

   echo test
   Echo test

 Permission rule Bash requires confirmation for this command.

 Do you want to proceed?
 ❯ 1. Yes
   2. No

 Esc to cancel · Tab to amend · ctrl+e to explain
```

**Options**: 2 only (Yes / No) — no session-level option for Bash.
**Notes**:
- Shows the raw command AND a human-readable description below it.
- Has "Permission rule Bash requires confirmation for this command." context line.
- Footer includes `ctrl+e to explain` (unique to Bash).
- This is the most common dialog — all non-pre-authorized Bash commands trigger it.

### Bash Variants Observed

All Bash dialogs have the same structure, only the command differs:

| Command | Description shown |
|---------|------------------|
| `echo test` | Echo test |
| `ls -la /tmp/` | List files in /tmp/ |
| `cat /tmp/.../hello.txt` | Cat hello.txt |
| `rm /tmp/.../hello.txt` | Remove hello.txt |
| `ls ... \|\| mkdir -p ...` | Create directory if needed |
| `python3 .../test.py` | Run test.py |
| `pip list \| head -5` | List first 5 pip packages |

---

## Auto-Approved Tools (No Dialog)

These tools are pre-authorized via `--allowed-tools` and show NO permission dialog:

- `Read` (within project directory)
- `Glob`
- `Grep`
- `WebFetch`
- `WebSearch`

Note: `Read` outside the project directory DOES show a dialog (Pattern 1).

## Status Bar (Not a Dialog)

The last line of the screen is always the status bar:

| State | Text |
|-------|------|
| Idle, no focus | `? for shortcuts` |
| Active / user focused | `esc to interrupt` |
| Generating | `esc to interrupt` |

The status bar alternates between these values and **is not a permission dialog**.
It causes hash changes that trigger false probe cycles (see `auto-confirm-strategy.md`).

## Screen Layout Summary

```
Line 1-N:     Conversation content (scrollable)
Line N+1:     ─────────────── (separator, optional ▪▪▪ context indicator)
Line N+2:     ❯ <user input>  (prompt line)
Line N+3:     ─────────────── (separator)
Line N+4:     <status bar>    (? for shortcuts / esc to interrupt)
```

When a dialog is showing, it replaces the bottom portion:

```
Line 1-N:     Conversation content
Line N+1:     ─────────────── (dialog separator — ALWAYS full-width ─)
Line N+2-M:   Dialog content (title, details, options)
Line M+1:     Footer (Esc to cancel · Tab to amend)
```

## Matching Strategy

### Current TOML Rules (camc v1.1.0)

```toml
# Matches all "Do you want to proceed/create/edit" dialogs
[[confirm]]
pattern = "Do\\s+you\\s+want\\s+to\\s+proceed"
response = "1"
send_enter = false

# Matches "1. Yes" or "1. Allow" in numbered menus
[[confirm]]
pattern = "1\\.\\s*(Yes|Allow)"
response = "1"
send_enter = false

# Matches "Allow once" / "Always allow" (Claude 4.x+)
[[confirm]]
pattern = "Allow\\s+(once|always)"
response = "1"
send_enter = false

# y/n prompts (rare in Claude Code)
[[confirm]]
pattern = "\\(y/n\\)|\\[Y/n\\]|\\[y/N\\]"
response = "y"
send_enter = false
```

### Gap Analysis

| Dialog | Matched by rule | Notes |
|--------|----------------|-------|
| Read file — "Do you want to proceed?" | `Do\s+you\s+want\s+to\s+proceed` | ✓ |
| Create file — "Do you want to create X?" | `1\.\s*(Yes\|Allow)` | ✓ (matches option text) |
| Edit file — "Do you want to make this edit?" | `1\.\s*(Yes\|Allow)` | ✓ (matches option text) |
| Bash — "Do you want to proceed?" | `Do\s+you\s+want\s+to\s+proceed` | ✓ |
| Trust folder (startup) | Not captured in this test | Needs `Enter to confirm` pattern |

**Missing pattern**: The first-time directory trust dialog ("Do you trust the
files in this folder?") with `Enter to confirm · Esc to cancel` was not
triggered in this test because the directory was already trusted. This dialog
uses Enter (not "1") to confirm.

### Recommended Response

All captured dialogs use the Ink select menu with `❯ 1. Yes` as the default.
Both **Enter** and **"1"** work to select it. Using **Enter** is safer because:
1. "1" may echo at prompt if no dialog is showing (probe problem)
2. Enter has no visible side effect when no dialog is showing
3. The `❯` cursor is always on option 1 by default

However, Enter at an idle prompt submits an empty message, which is also
undesirable. The safest approach is **PreToolUse hook** (no dialog at all).
