# CAM Desktop Rich Renderer v2 Spec

Status: implemented (renderer landed; reviewer pass complete)

Implementation notes (do not remove the spec; this section just
records where the spec lives in code):

- Block taxonomy lives in `web/js/desktop/agent-console.js` inside
  `renderRichOutput`. Priority order matches the table below.
  Subblock state (`inCmdOutput`) tags lines that follow a
  `.rich-shell` row as `.rich-line.rich-cmd-output`; any block
  detector resets the flag.
- Agent event / progress detector is `richEventMatch(plain)`; the
  verb set is defined in `RICH_EVENT_VERBS`. Bullet glyphs (✦ ✻
  ⏺ • …) are optional.
- Inline taxonomy lives in `enrichInline` /
  `enrichProseSegment` / `INLINE_RE`. The single combined regex
  exposes named groups (`url`, `fileref`, `path`, `quoted`,
  `metric`, `kwattn`, `tag`); `classifyInlineToken` dispatches.
- Per-line bailout at `LINE_HL_MAX = 500` chars keeps the
  renderer linear on pathological captures. Long ASCII dividers
  use `.rich-terminal-rule` (a CSS rule) so they never create
  horizontal scrollbars.
- Golden fixture lives at `/tmp/rich-render-v2-smoke.mjs` and
  covers all block + inline cases plus the negative cases from
  the "Important Negative Cases" section.
- CSS additions are scoped under `.agent-output-rich .rich-*` in
  `web/css/desktop.css`. No global selectors.

This spec defines the next Rich mode pass for CAM Desktop agent output. The
goal is to avoid a flat wall of equally bright text while keeping the renderer
fast, stable, selectable, and driven by the same plain `camc capture` string
used by Plain mode.

## Design References

The renderer should borrow structure from three familiar surfaces:

- Agent TUIs such as Codex / Claude Code: command lines, progress events,
  separators, tool results, and approval prompts are visually distinct.
- Markdown readers such as GitHub / ChatGPT: headings, lists, quotes, code
  blocks, tables, and links create hierarchy without hiding content.
- CI / log viewers: PASS / FAIL / WARN / ERROR lines, durations, counts, paths,
  and low-signal build noise are easy to scan.

This is not a full terminal emulator and not a full CommonMark parser. It is a
deterministic local renderer for terminal transcripts.

## Non-Goals

- Do not transfer rich text from remote machines.
- Do not require a remote `--format` flag; Rich and Plain share one capture.
- Do not add a heavy markdown, syntax-highlighter, or xterm dependency in v2.
- Do not hide or collapse output by default. Dim low-signal content instead.
- Do not make structure with ASCII art. Use spacing, borders, badges, muted
  backgrounds, and restrained color.
- Do not break selection/copy. Rendered text must remain selectable with
  Ctrl+C and the browser context menu.

## Rendering Pipeline

1. Normalize line endings to LF.
2. Strip ANSI only for detection; preserve ANSI spans for visual rendering.
3. Build line records: raw text, plain text, indent, blank/nonblank, length.
4. Run block classification in a fixed priority order.
5. Run bounded inline classification inside prose, lists, quotes, shell
   commands, status lines, and code blocks.
6. Emit safe escaped HTML using only `.rich-*` CSS classes.
7. If any classifier fails, render escaped plain text for that region.

The block pass owns vertical rhythm. The inline pass owns local emphasis.

## Block Taxonomy

Classifiers run in this order. Earlier matches win.

| Priority | Pattern | Visual Role | Notes |
| --- | --- | --- | --- |
| 1 | Fenced code block | Strong code card | Triple backtick / tilde, optional language label. |
| 2 | Table | Structured data block | Pipe table with separator row; align cells and keep selectable text. |
| 3 | Terminal / markdown divider | Section boundary | Long repeated rule characters, `---`, `===`, `***`; render as CSS rule, never as a scrollable code block. |
| 4 | Markdown heading | Section title | `#`, `##`, `###`; also detect compact all-caps labels only when followed by `:`. |
| 5 | Agent event / progress | Timeline event row | Examples: `Brewed for 10s`, `Cooked for 2m`, `Working (...)`, `Tool call`, `Reading`, `Edited`, `Wrote`, `Asked`. |
| 6 | Shell command | Command row | Prompts `$`, `PS>`, `cmd>`, `>`, and common chevron prompts. Command is emphasized; output stays separate. |
| 7 | Command output / subblock | Muted terminal output | Lines immediately after a command that look like stdout/stderr. Keep monospaced, lower contrast than commands. |
| 8 | Status / result | Scan badge row | PASS, FAIL, ERROR, WARN, STATUS, BUILD, INSTALL, TESTS, SMOKE, BLOCKERS, REQ_STATUS, exit codes, HTTP statuses. |
| 9 | Quote | Quoted / secondary reasoning | `>` markdown quotes and quoted transcript blocks. Left border, muted background. |
| 10 | List | Scannable list | Bullets, numbers, checkboxes, shallow indentation. List marker gets accent; text uses prose inline highlights. |
| 11 | Real indented code | Code card | Four-space lines only when code-like; path-only attachment lines must not become code. |
| 12 | Low-signal log | Dim line | npm/electron-builder/gyp/deprecation/progress noise. Still visible, just lower contrast. |
| 13 | Normal prose | Readable paragraph | Inline highlights applied; no background card by default. |

## Inline Taxonomy

Inline classifiers are conservative. They should make important tokens easier
to spot without turning every line into rainbow text.

| Priority | Pattern | Class Intent |
| --- | --- | --- |
| 1 | ANSI SGR spans | Preserve existing terminal color. |
| 2 | Inline code / backticks | Code pill. |
| 3 | URLs | Link accent and underline on hover. |
| 4 | Filesystem paths | Warm path accent; supports Unix, Windows, UNC, relative paths. |
| 5 | File references | Path plus `:line` / `:line:col` gets stronger path styling. |
| 6 | Shell tokens | Command name, env assignment, flags, flag values, redirects, pipes. |
| 7 | Important keywords | `IMPORTANT`, `TODO`, `FIXME`, `BLOCKER`, `SECURITY`, `PASS`, `FAIL`, `ERROR`, `WARN`. |
| 8 | Metrics | Durations, counts, percentages, exit codes, ports, bytes. |
| 9 | Quoted strings | Slight contrast only; avoid over-highlighting prose. |
| 10 | Tags | `#tag` only when it is not a shell comment. |

## Visual Hierarchy

Use a restrained dark UI vocabulary:

- Commands: most prominent transcript line after headings. Prompt accent plus
  bold command token; flags and paths get secondary accents.
- Status: compact pill plus tinted text. PASS / success is green, WARN is
  amber, FAIL / ERROR is red, INFO is blue/gray.
- Code: card background one step darker/lighter than pane, small language
  label when known, syntax-lite tokens only.
- Subblocks / stdout: monospaced, muted, no heavy border unless grouped.
- Quotes: left border and soft background, lower contrast than normal prose.
- Lists: marker column aligned; nested lists use indentation, not nested cards.
- Tables: subtle cell borders and sticky-looking header tone, no giant card.
- Dividers: CSS rule with vertical spacing. Never create a horizontal scrollbar.
- Low-signal logs: dim color, same selectable text.

Avoid large uninterrupted blocks with identical contrast. A page should show a
visible rhythm of heading, command, output, status, and prose.

## Important Negative Cases

These must not be misclassified:

- A path-only attachment line must not become an indented code block.
- A long repeated separator must not create a scrollable code panel.
- A prose line containing `PASS` or `ERROR` mid-sentence must not become a
  status badge.
- `https://...` must not be split as a shell comment or path-only line.
- `#` in a shell script comment is a comment; `#tag` in prose is a tag.
- Very long minified lines fall back to escaped plain text.
- Plain mode output remains untouched.

## Golden Fixture Set

v2 implementation must be driven by one fixture file covering:

1. Markdown headings, dividers, quotes, ordered lists, unordered lists,
   checkboxes, nested list indentation.
2. Shell transcript: prompt command, stdout, stderr, path arguments, flags,
   env assignments, redirects, and pipes.
3. Code: fenced JavaScript, Python, shell, diff, and four-space indented code.
4. Table with header and alignment row.
5. Status lines: PASS / FAIL / ERROR / WARN / STATUS, HTTP status, exit code.
6. Low-signal build output: npm, electron-builder, node deprecation, progress.
7. Agent event lines: brewed/cooked/working/tool/read/edit/write/send.
8. Inline tokens: URLs, file paths, Windows paths, UNC paths, line references,
   tags, quoted strings, durations, percentages, ports.
9. Negative cases listed above.

The smoke test should assert class presence and class absence. It should not
assert exact pixel color.

## Implementation Plan

1. Add the golden fixture and smoke assertions first.
2. Refactor block classification into named classifier helpers with explicit
   priority.
3. Add inline token classifiers in one bounded pass per line.
4. Tune CSS only through `.agent-output-rich .rich-*` selectors.
5. Verify selection/copy still works and the output pane does not show unwanted
   horizontal scrollbars.
6. Rebuild/reinstall only after the renderer passes local smoke.

## Acceptance Criteria

- Rich mode visually separates commands, outputs, status, code, quotes, lists,
  tables, paths, URLs, important keywords, and low-signal logs.
- Rich and Plain use the same captured string; switching modes does not trigger
  a new remote capture when output is already loaded.
- No raw agent text enters `innerHTML` without escaping.
- Output remains selectable and copyable.
- Long separators render as CSS rules and never create horizontal scrollbars.
- The implementation remains fast on large captures by bailing out on very
  long lines and avoiding cross-document markdown parsing.
