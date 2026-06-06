# CAM Desktop Rich Renderer v2 Spec

Status: draft for implementation

## Purpose

Rich mode is a local renderer over the same low-bandwidth plain `camc capture`
string used by Plain mode. It must make long agent transcripts scannable without
requiring remote rich text, markdown JSON, or a heavy client-side markdown
engine. The renderer should avoid a flat wall of white text by assigning each
line/block a visual role: command, output, prose, status, path, quote, list,
code, table, separator, or low-signal log.

The design references three stable patterns:

- Agent TUI transcript style, as seen in Codex / Claude Code: commands are
  distinct from prose, command output is indented/dimmed where appropriate,
  status/progress is visible, and separators preserve rhythm.
- Markdown semantics, as seen in GitHub / ChatGPT: headings, lists, quotes,
  fenced code, tables, dividers, inline code, bold/italic, and links are familiar
  and should render predictably.
- CI/log viewers, as seen in GitHub Actions or build logs: `ERROR`, `WARN`,
  `PASS`, `FAIL`, exit codes, HTTP statuses, and build/install/test labels get
  stable severity treatment; repetitive dependency/progress noise is dimmed.

## Non-goals

- Do not transmit rich text, block JSON, or agent event streams in this slice.
- Do not introduce markdown-it, highlight.js, xterm.js, or another heavy parser.
- Do not execute commands or create an interactive terminal.
- Do not hide output. Low-signal output may be visually dimmed, but remains
  visible, selectable, and copyable.
- Do not make ASCII-art boxes or text dividers part of the rendered structure.
  Use HTML/CSS structure, borders, accents, badges, and spans.

## Pipeline

1. Normalize line endings to `LF`.
2. Split into line records `{ raw, plain }`, where `plain` strips ANSI only for
   classification.
3. Run a deterministic block classifier in the order below.
4. Run a bounded inline classifier inside prose/list/quote/status/output blocks.
5. Emit escaped HTML fragments with `.rich-*` classes only.
6. Fall back to escaped plain lines on parser failure.

Classifier order is fixed. Earlier rules win; later rules must not reinterpret
blocks that were already consumed.

## Block Taxonomy And Priority

### 1. Fenced Code

Pattern:

- Triple backtick or triple tilde, optional language label.
- Consumes until matching fence or EOF.

Render:

- `.rich-code-block`, optional `.rich-code-lang`, nested `pre.rich-code > code`.
- Apply lightweight code tokenizer per line.
- Diff-looking lines inside code get diff classes.

Never:

- Never parse markdown/list/status inside fenced code.

### 2. Table

Pattern:

- Pipe table with at least two columns and a separator row like `---`, `:---`,
  `---:`.

Render:

- `.rich-table-wrap > table.rich-table`.
- Header row is stronger than body rows.
- Inline classifier may run inside cells, but no block detection inside cells.

### 3. Terminal Separator / Markdown Divider

Pattern:

- Short markdown divider: `---`, `***`, `___`, `===`.
- Long terminal separator: full-width `_`, `-`, `=`, `-`, etc.

Render:

- Short markdown divider: `.rich-divider`.
- Long terminal separator: full-width `.rich-terminal-rule` using CSS border.

Never:

- Never render separator text inside `pre` or code.
- Never create horizontal scrollbar from separator content.

### 4. Markdown Heading

Pattern:

- `#`, `##`, `###` followed by text.
- Keep shallow only; deeper headings render as normal prose.

Render:

- `.rich-heading.rich-heading-N`.
- Apply inline classifier to heading text.

### 5. Agent Event / Approval / Progress Line

Pattern examples:

- `You approved codex to run ... this time`
- `Ran ssh -p ...`
- `Working (1m 09s | esc to interrupt)`
- `Brewed for 10s`
- `Auto-update failed ...`

Render:

- `.rich-event` with optional severity/accent classes:
  - `.rich-event-approved`
  - `.rich-event-ran`
  - `.rich-event-working`
  - `.rich-event-failed`
- Important words (`approved`, `failed`, `running`, elapsed time) get inline
  accent spans.

Reason:

- This is the main TUI clue that separates agent narration from ordinary prose.

### 6. Terminal Command

Pattern:

- Shell prompts: `$`, `>`, `PS>`, `cmd>`.
- Command-looking `>` only when followed by known commands such as `ssh`, `cam`,
  `camc`, `npm`, `node`, `python`, `git`, `cd`, `ls`, `cat`, `echo`, `make`,
  `pytest`, `docker`, `kubectl`, `powershell`, `cmd`.

Render:

- `.rich-shell` row containing `.rich-shell-prompt` and
  `.rich-shell-command`.
- Tokenize command segment:
  - first non-env executable: `.rich-cmd-name`
  - leading `KEY=value`: `.rich-cmd-env` + `.rich-cmd-env-val`
  - `--flag`, `-x`, `--flag=value`: `.rich-cmd-flag` and optional value
  - filesystem paths: `.rich-path` or `.rich-cmd-path`
  - URLs: `.rich-link`

Special case:

- Prompt followed only by paths/attachments is a path/output line, not a command
  execution. Do not make the first path look like an executable.

### 7. Command Output / Subblock

Pattern:

- Lines after a command that are indented, start with ellipsis markers, show
  snippets, or appear to be command stdout/stderr but are not code/status.
- Examples: `... +3 lines`, line-numbered snippets, grep result fragments,
  `stdout:` / `stderr:` labels.

Render:

- `.rich-subblock` or `.rich-output-line`.
- `... +N lines`, `ctrl + t to view transcript`, and similar helper text are
  `.rich-dim` inside the subblock.
- File paths and line references get inline path/link treatment.

### 8. Status / Result / Severity Line

Pattern:

- Anchored labels: `PASS`, `FAIL`, `ERROR`, `WARN`, `STATUS`, `REQ_STATUS`,
  `FILES_CHANGED`, `IMPLEMENTATION`, `TESTS`, `SMOKE`, `BUILD`, `INSTALL`,
  `BLOCKERS`, `NOTES`, `MOBILE_COMPAT`, `REVIEW_STATUS`.
- Exit codes: `exit code N`, `Exit Code: N`, `exit status N`.
- HTTP: `HTTP/1.1 404`, `HTTP/2 200`.
- Common final summaries: `SUMMARY N/M`, `N/N PASS`, `0 FAIL`.

Render:

- `.rich-status.rich-status-{pass|fail|error|warn|info}`.
- Leading compact `.rich-status-badge-*`.
- Rest of the line runs inline classifier.

### 9. Quote

Pattern:

- Markdown quote: `>` followed by text.
- Consecutive quote lines form one block.

Render:

- `blockquote.rich-quote` with left border.
- Inline classifier inside quote text.
- Quote is medium emphasis, not as loud as status or command.

### 10. List

Pattern:

- Bullets: `-`, `*`, `+`.
- Ordered: `1.`, `2.`.
- Checkboxes: `[ ]`, `[x]` after bullet.
- Shallow indentation levels only.

Render:

- `.rich-list`, `.rich-list-item`, `.rich-list-marker`.
- Ordered list markers should be visually distinct enough to scan.
- Inline classifier inside list content.

### 11. Real Indented Code

Pattern:

- Four-space-indented line with a code-like signal:
  keyword, assignment, braces, semicolon, function call, diff prefix, shell-ish
  token.

Render:

- Same as code block.

Never:

- Path-only lines, attachment paths, whitespace-only terminal residue, or
  ordinary indented prose must not become code. This avoids empty scrollable
  panels and fake code blocks.

### 12. Low-signal Log Line

Pattern examples:

- `npm WARN`, `npm notice`, `npm info`, `npm http`, `npm verb`
- `electron-builder`, `app-builder`, `gyp info`, `node_modules/...`
- node deprecation warnings
- spinner/progress bar lines
- repeated dependency install traces

Render:

- `.rich-dim`.
- Still visible and selectable.
- If a low-signal line contains `ERROR`, `FAIL`, nonzero exit code, or HTTP 4xx
  / 5xx, severity wins and it must not be dimmed.

### 13. Normal Prose

Pattern:

- Anything not matched earlier.

Render:

- `.rich-line`.
- Inline classifier gives structure to important phrases, paths, URLs, quoted
  strings, inline code, list-like references, and emphasis.

## Inline Classifier

Run only on non-code text. It must be bounded and local to the line.

Priority:

1. Existing ANSI spans, if present.
2. Inline code: `` `code` `` -> `.rich-inline-code`.
3. URL: `http://...`, `https://...` -> `.rich-link`.
4. Filesystem path:
   - Unix: `/home/...`, `~/...`, `./...`, `../...`
   - Windows: `C:\\...`, `\\\\server\\share\\...`
   - Extensions: `.js`, `.py`, `.cjs`, `.md`, `.json`, `.toml`, `.png`,
     `.log`, `.txt`, `.sh`, `.ps1`, `.c`, `.cpp`, `.h`, `.rs`, `.go`, `.ts`,
     `.tsx`, `.jsx`, `.html`, `.css`
   -> `.rich-path`.
5. Line refs: `file.js:123`, `line 42`, `L123` -> `.rich-line-ref`.
6. Quoted strings: `'...'`, `"..."` -> `.rich-quoted` unless already code.
7. Strong markdown: `**...**` -> `.rich-strong`.
8. Conservative italic: `*...*` -> `.rich-em`.
9. Important keywords in prose:
   - `important`, `blocked`, `blocker`, `failed`, `error`, `warning`, `pass`,
     `success`, `done`, `needs-review`, `installed`, `reinstalled`, `synced`,
     `updated`, `unchanged`, `credential_missing`, `auth_failed`,
     `connect_lost`, `connect_timeout`, `camc_missing`, `invalid_json`
   -> `.rich-keyword` with severity variants when obvious.
10. Parenthetical timing / progress: `(1m 09s)`, `53/53`, `11/11`, `0 FAIL`
    -> `.rich-metric`.

Long-line guard:

- If a line exceeds `LINE_HL_MAX`, skip inline tokenization except HTML escape.
- URLs/paths in very long lines may be missed; speed and stability win.

## Visual Hierarchy

Use restrained dark-theme CSS, not loud rainbow highlighting.

- `command`: strong text, prompt accent, subtle left rhythm if needed.
- `status`: compact badges; red/yellow/green/blue only for severity.
- `code`: panel background, syntax spans, horizontal scroll only for real code.
- `subblock/output`: slightly dimmer than prose but not hidden.
- `quote`: left border, muted text.
- `list`: visible marker column; ordered numbers use a muted accent.
- `path/url`: color accent so filesystem references stand out in prose.
- `important prose`: modest weight/color, not a badge unless line starts with a
  status keyword.
- `low-signal`: dim gray.
- `separator`: full-width CSS border with low emphasis.

## Golden Fixture Requirements

A renderer fixture must include at least one case for every block type and every
negative rule. The smoke should assert both positive classes and absence of bad
classes.

Positive cases:

- heading, divider, terminal separator
- approval/progress event line
- command with env, flags, path, URL
- path-only prompt line
- command output snippet with `... +N lines`
- status lines: PASS, FAIL, ERROR, WARN, STATUS, BUILD, INSTALL, BLOCKERS
- exit code 0 and nonzero
- HTTP 200, 404, 500
- quote block
- unordered, ordered, checkbox list
- fenced code with string/comment/keyword
- diff block
- markdown table
- prose with URL, Unix path, Windows path, line ref, quoted string, metric,
  important keyword
- low-signal npm/electron-builder/deprecation line

Negative cases:

- path-only / attachment line is not `rich-code`
- terminal separator is not inside `pre` and does not create scrollbar text
- `PASS` in the middle of prose is not a status badge
- `https://` is not split into a `//` comment in code/prose rendering
- long line falls back safely and does not lock the renderer
- raw HTML remains escaped
- Plain mode is unchanged

## Implementation Plan

1. Move the fixture into a focused smoke script or doc fixture under
   `docs/desktop/` plus `/tmp` smoke during development.
2. Refactor `renderRichOutput` around the block taxonomy above, preserving the
   existing v1 helper names where possible.
3. Replace `renderPlainInline` with a bounded inline tokenizer that emits URL,
   path, line-ref, quote, metric, keyword, code, bold/italic spans.
4. Add CSS only under `.agent-output-rich .rich-*`.
5. Keep old v0/v1 smoke cases and add the golden fixture smoke.
6. Rebuild/reinstall only after the smoke passes.

## Acceptance Criteria

- Rich output no longer appears as an undifferentiated wall of white text on the
  golden fixture.
- Important status/result lines are scannable without reading every line.
- Paths, URLs, line refs, metrics, and quoted strings stand out in prose.
- Command blocks are clearly distinct from command output and prose.
- Low-signal logs are visually quiet but still copyable.
- Separators preserve structure without scrollbars.
- No new dependency, no remote rich data, no remote format flag, no renderer
  command execution.
