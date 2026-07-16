# Desktop Terminal Regressions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore tmux controls, terminal status colors, and browser-like scrolling in CAM Desktop, then publish a verified Windows MSI.

**Architecture:** Preserve the existing Electron main-process ownership of tmux commands. Normalize the current camc nested socket field into the existing Desktop agent shape, use explicit CSS state selectors after the terminal-chrome base selector, and split wheel behavior between local xterm viewport scrolling and tmux copy-mode scroll commands.

**Tech Stack:** Electron, xterm.js, Node.js CommonJS tests, CSS, tmux control commands over pooled SSH.

## Global Constraints

- Do not modify tmux configuration.
- The renderer may only receive opaque terminal session IDs; tmux commands remain in Electron main.
- Normal wheel scrolling is local xterm scrollback and pauses auto-follow.
- Copy-mode wheel scrolling uses tmux `send-keys -X scroll-up|scroll-down`, never cursor-arrow input.
- Do not commit or push without explicit user approval.

---

### Task 1: Normalize current camc tmux socket metadata

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs:711-755`
- Modify: `apps/cam-desktop/electron/tmux-controls.cjs:14-24`
- Test: `apps/cam-desktop/test/tmux-controls.test.cjs`

- [ ] Add a failing test proving `tmuxMetadataForAgent` accepts `runtime.tmux.socket` when `tmux_socket` is absent.
- [ ] Add a failing source assertion proving `_normalizeAgent` maps the same fallback into `tmux_socket`.
- [ ] Map `rec.tmux_socket || rec.runtime?.tmux?.socket || ''` in the hub and accept the nested fallback in the helper.
- [ ] Run `npm run test:term` and confirm the new tests pass.

### Task 2: Restore terminal status state colors

**Files:**
- Modify: `web/css/desktop.css:2700-2750`
- Test: `apps/cam-desktop/test/terminal-follow.test.cjs`

- [ ] Add failing source assertions for terminal-chrome scoped info, ok, and error color selectors.
- [ ] Add three state selectors after the terminal-chrome base rule so they win the equal-specificity cascade.
- [ ] Run `npm run test:term` and confirm the color assertions pass.

### Task 3: Implement local and copy-mode scrolling

**Files:**
- Modify: `apps/cam-desktop/electron/tmux-controls.cjs`
- Modify: `apps/cam-desktop/electron/main.cjs:488-505, 720-740`
- Modify: `apps/cam-desktop/electron/preload.cjs:142-160`
- Modify: `web/js/desktop/agent-console.js:1934-2030, 2685-2705, 2750-2756`
- Test: `apps/cam-desktop/test/tmux-controls.test.cjs`
- Test: `apps/cam-desktop/test/terminal-follow.test.cjs`

- [ ] Add failing helper tests for validated tmux copy scroll arguments.
- [ ] Add failing renderer source assertions that normal wheels call `term.scrollLines`, clear follow state, and copy mode calls the dedicated `copyScroll` bridge instead of `bridge.input` cursor arrows.
- [ ] Add `term:copyScroll` IPC which validates direction and invokes tmux copy-mode `scroll-up` or `scroll-down` against the owned pane.
- [ ] Expose the narrow bridge method in preload.
- [ ] In the renderer, locally scroll xterm in normal mode and use the IPC only in copy mode; track xterm scroll events so To Bottom appears while normal scrollback is above live output.
- [ ] Make To Bottom exit copy mode only when needed, otherwise restore local live-follow.
- [ ] Run `npm run test:term` and confirm all terminal tests pass.

### Task 4: Verify and release

**Files:**
- Verify: `apps/cam-desktop`
- Artifact: Nutstore app release folder

- [ ] Run terminal tests, hub tests, Electron syntax checks, renderer syntax check, and `git diff --check`.
- [ ] Build the MSI in the clean Windows build directory.
- [ ] Extract the MSI and verify the changed renderer/CSS resource hashes plus the packaged Electron files.
- [ ] Copy a uniquely named MSI to Nutstore’s synced app folder and verify its SHA-256 matches the build artifact.

### Task 5: Recover tmux controls after a slow attach

**Files:**
- Modify: `apps/cam-desktop/electron/main.cjs:404-460, 563-580`
- Test: `apps/cam-desktop/test/terminal-follow.test.cjs`

- [ ] Add failing source assertions for a ten-attempt recovery limit and a retry from `_tmuxClientState`.
- [ ] Preserve the initial short discovery loop, then let each selected-terminal control refresh make at most one `list-clients` recovery probe when no tty was discovered.
- [ ] Store the pre-attach client set and recovery count with the terminal session; stop on success, close, or the tenth failed recovery probe.
- [ ] Run `npm run test:term` and confirm recovery regression coverage passes.

### Task 6: Keep History controls from closing the terminal channel

**Files:**
- Modify: `apps/cam-desktop/electron/ssh-transport.cjs:340-390`
- Modify: `apps/cam-desktop/electron/main.cjs:409-424`
- Test: `apps/cam-desktop/test/terminal-follow.test.cjs`

- [ ] Add failing source assertions that tmux control calls request a connection-preserving timeout and that the transport supports a per-command abort path.
- [ ] On an opted-in control timeout, close only the short SSH exec channel and retain the pooled SSH client carrying the long-lived terminal attach.
- [ ] Opt in `_tmuxExec` and `_tmuxClientSet`; leave regular remote operations unchanged.
- [ ] Run `npm run test:term` and confirm the terminal stays protected by regression coverage.
