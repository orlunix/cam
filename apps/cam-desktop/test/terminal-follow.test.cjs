"use strict";

const fs = require("fs");
const path = require("path");
let pass = 0;
let fail = 0;
function ok(name, condition, detail = "") {
  if (condition) pass++;
  else { fail++; console.error("FAIL " + name + (detail ? " — " + detail : "")); }
}
function atBottom(buffer) {
  if (!buffer || buffer.viewportY == null || buffer.baseY == null) return true;
  return buffer.viewportY >= buffer.baseY;
}

ok("scrollback defaults to follow", atBottom(null));
ok("bottom viewport follows", atBottom({ viewportY: 12, baseY: 12 }));
ok("past-bottom viewport follows", atBottom({ viewportY: 13, baseY: 12 }));
ok("scrolled-up viewport is preserved", !atBottom({ viewportY: 4, baseY: 12 }));

const sourcePath = path.join(__dirname, "..", "..", "..", "web", "js", "desktop", "agent-console.js");
const source = fs.readFileSync(sourcePath, "utf8");
function fn(name, next) {
  const start = source.indexOf("function " + name + "(");
  const end = source.indexOf("function " + next + "(", start);
  return start >= 0 ? source.slice(start, end >= 0 ? end : undefined) : "";
}
const show = fn("showTerminalEntry", "prepareThenShowTerminalEntry");
const fit = fn("scheduleTerminalFit", "hideTerminalEntries");
const open = fn("openTerminalForSelected", "renderCachedOutput");
const sync = fn("syncActiveTerminalEntry", "terminalEntryBySession");
const cssPath = path.join(__dirname, "..", "..", "..", "web", "css", "desktop.css");
const css = fs.readFileSync(cssPath, "utf8");
const terminalChromeCss = css.slice(css.indexOf(".terminal-tmux-chrome"), css.indexOf("#mode-agents .agent-composer[hidden]"));
const wheelStart = source.indexOf("entry._wheelHandler = (ev) =>");
const wheelEnd = source.indexOf("container.addEventListener('wheel'", wheelStart);
const wheel = wheelStart >= 0 && wheelEnd >= 0 ? source.slice(wheelStart, wheelEnd) : "";
const keyStart = source.indexOf("entry.term.attachCustomKeyEventHandler((ev) =>");
const keyEnd = source.indexOf("entry._wheelHandler = (ev) =>", keyStart);
const keyHandler = keyStart >= 0 && keyEnd >= 0 ? source.slice(keyStart, keyEnd) : "";
const historyStart = source.indexOf("terminalHistoryBtn.addEventListener('click'");
const historyEnd = source.indexOf("terminalBottomBtn.addEventListener('click'", historyStart);
const historyClick = historyStart >= 0 && historyEnd >= 0 ? source.slice(historyStart, historyEnd) : "";
const bottomStart = historyEnd;
const bottomEnd = source.indexOf("terminalTabsEl.addEventListener('click'", bottomStart);
const bottomClick = bottomStart >= 0 && bottomEnd >= 0 ? source.slice(bottomStart, bottomEnd) : "";
const refreshStart = source.indexOf("terminalRefreshBtn.addEventListener('click'");
const refreshEnd = source.indexOf("terminalTabsEl.addEventListener('click'", refreshStart);
const refreshClick = refreshStart >= 0 && refreshEnd >= 0 ? source.slice(refreshStart, refreshEnd) : "";
const actionBarStart = source.indexOf('<div class="terminal-action-bar"');
const attachSlot = source.indexOf('terminal-attach-icon', actionBarStart);
const historySlot = source.indexOf('terminal-history-btn', actionBarStart);
const bottomSlot = source.indexOf('terminal-bottom-btn', actionBarStart);
const refreshSlot = source.indexOf('terminal-refresh-btn', actionBarStart);
const hubPath = path.join(__dirname, "..", "electron", "embedded-hub.cjs");
const hub = fs.readFileSync(hubPath, "utf8");
const preloadPath = path.join(__dirname, "..", "electron", "preload.cjs");
const preload = fs.readFileSync(preloadPath, "utf8");
const mainPath = path.join(__dirname, "..", "electron", "main.cjs");
const main = fs.readFileSync(mainPath, "utf8");
const transportPath = path.join(__dirname, "..", "electron", "ssh-transport.cjs");
const transport = fs.readFileSync(transportPath, "utf8");

ok("show function exists", show.length > 0);
const reveal = show.indexOf("classList.remove('parked')");
const fitCall = show.indexOf("scheduleTerminalFit({ keepBottom: opts.keepBottom !== false })");
ok("show reveals before fitting", reveal >= 0 && fitCall >= 0 && reveal < fitCall, "reveal=" + reveal + " fit=" + fitCall);
ok("stable fitter exists", fit.length > 0);
ok("stable fitter includes second animation frame", fit.includes("raf(() =>") && fit.includes("raf(pass)"));
ok("stable fitter includes 80ms fallback", fit.includes("window.setTimeout(pass, 80)"));
ok("stable fitter includes 220ms fallback", fit.includes("window.setTimeout(pass, 220)"));
ok("stable fitter ignores stale hidden panes", fit.includes("if (!terminalEntryCanAutoResize(ent)) return;"));
ok("cached session branch exists", open.includes("ent.opening || ent.sessionId"));
ok("cached session switches immediately", open.includes("showTerminalEntry(agent.id, { keepBottom: false })"));
const cachedStart = open.indexOf("if (!force && (ent.opening || ent.sessionId))");
const cachedEnd = open.indexOf("prepareThenShowTerminalEntry(agent.id, 120)", cachedStart);
const cachedBlock = cachedStart >= 0 && cachedEnd >= 0 ? open.slice(cachedStart, cachedEnd) : "";
ok("cached session has no hidden-delay prep", !cachedBlock.includes("prepareThenShowTerminalEntry"));
ok("cached session does not reconnect", !cachedBlock.includes("bridge.open"));
// Bug 2 (2026-07-18): the open must wait for real layout before
// measuring the grid, so a first attach never opens at the xterm 80x24
// default while the pane is still hidden.
const layoutWait = open.indexOf("await waitForTerminalLayout(ent)");
const openCall = open.indexOf("bridge.open({ agentId: agent.id, cols: openCols, rows: openRows })");
ok("open waits for layout before measuring", layoutWait >= 0 && openCall > layoutWait,
  "layoutWait=" + layoutWait + " openCall=" + openCall);
ok("layout wait helper exists", source.includes("async function waitForTerminalLayout("));
ok("cache limit remains six", source.includes("const TERMINAL_CACHE_LIMIT = 6"));
ok("history follow uses viewport state", source.includes("terminalShouldForceBottom(ent) || terminalIsAtBottom(ent)"));
ok("history write does not recheck and yank viewport", source.includes("if (shouldFollow) terminalScrollToBottom(ent);"));
ok("tmux tab control refresh exists", source.includes("refreshTerminalTmuxControls"));
ok("terminal has mouse-first history control", source.includes("terminal-history-btn"));
ok("terminal has a safe history return control", source.includes("terminal-bottom-btn"));
ok("terminal attach is icon-only in terminal chrome", source.includes("terminal-attach-icon"));
ok("terminal action bar has four stable ordered slots",
  actionBarStart >= 0 && attachSlot < historySlot && historySlot < bottomSlot && bottomSlot < refreshSlot);
ok("terminal action slots stay visible and use disabled state",
  source.includes("terminalActionBar.hidden = !terminalVisible")
    && !source.includes("terminalHistoryBtn.hidden =")
    && !source.includes("terminalBottomBtn.hidden ="));
ok("terminal action bar is centered and lifted off the input line",
  terminalChromeCss.includes(".terminal-action-bar {")
    && terminalChromeCss.includes("left: 50%;")
    && terminalChromeCss.includes("transform: translateX(-50%);")
    && terminalChromeCss.includes("bottom: var(--terminal-chrome-bottom-inset);")
    && terminalChromeCss.includes("display: flex;")
    && terminalChromeCss.includes("gap: 5px;"));
ok("terminal action buttons are scaled 1.2x",
  terminalChromeCss.includes("width: 34px;") && terminalChromeCss.includes("height: 34px;") && terminalChromeCss.includes("font-size: 13px;"));
ok("copy browsing suppresses terminal auto-follow", source.includes("!ent.copyBrowsing"));
ok("tmux controls have a bounded passive refresh", source.includes("terminalTmuxRefreshPending"));
ok("terminal status retains its full text as a tooltip", source.includes("terminalAttachStatus.title = text || ''"));
ok("switching back immediately restores the cached agent tabs", sync.includes("renderTerminalTabs(ent)"));
ok("showing a cached terminal refreshes its tmux state", show.includes("void refreshTerminalTmuxControls()"));
ok("terminal chrome is centered on the shared agent control rail", css.includes("width: min(100%, var(--desktop-rail-wide));") && css.includes("left: 50%;") && css.includes("transform: translateX(-50%);"));
ok("terminal status is under the tabs at the upper left", css.includes(".terminal-tmux-chrome .terminal-attach-status") && css.includes("top: 44px;") && css.includes("left: 16px;"));
ok("terminal bottom controls retain a terminal-font-relative margin", source.includes("Math.round(fontSize * 2.3)") && source.includes("--terminal-chrome-bottom-inset") && css.includes("bottom: var(--terminal-chrome-bottom-inset);"));
ok("terminal chrome is translucent but stays identifiable",
  terminalChromeCss.includes("color-mix(in srgb, var(--output-float-bg) 55%, transparent)")
    && terminalChromeCss.includes("backdrop-filter: blur(6px)"));
ok("terminal controls use tab-shaped corners", terminalChromeCss.includes("border-radius: var(--radius-sm);"));
ok("current nested camc tmux socket is normalized", hub.includes("rec.tmux_socket || (rec.runtime && rec.runtime.tmux && rec.runtime.tmux.socket) || ''"));
ok("terminal chrome keeps info status color", css.includes(".terminal-tmux-chrome .terminal-attach-status.is-info"));
ok("terminal chrome keeps ok status color", css.includes(".terminal-tmux-chrome .terminal-attach-status.is-ok"));
ok("terminal chrome keeps error status color", css.includes(".terminal-tmux-chrome .terminal-attach-status.is-error"));
ok("normal wheel scrolls local xterm viewport", wheel.includes("entry.term.scrollLines(lines)"));
ok("normal wheel pauses terminal auto-follow", wheel.includes("entry.needsBottom = false;") && wheel.includes("entry.forceBottomUntil = 0;"));
ok("History wheel sends five line keys through the live PTY", wheel.includes("if (entry.copyBrowsing)") && wheel.includes("sequence.repeat(5)") && wheel.includes("bridge.input({ sessionId: entry.sessionId"));
ok("History maps PageUp and PageDown through the live PTY", source.includes("PageUp: '\\x1b[5~'") && source.includes("PageDown: '\\x1b[6~'") && keyHandler.includes("bridge.input({ sessionId: entry.sessionId"));
ok("History leaves Up and Down on xterm's native input path", !source.includes("ArrowUp: -1") && !source.includes("ArrowDown: 1"));
ok("History enters copy mode via the live stream (prefix+[), optimistic state",
  historyClick.includes("bridge.input({ sessionId: ent.sessionId, data: '\\x02[' })")
    && historyClick.indexOf("data: '\\x02['") < historyClick.indexOf("ent.copyBrowsing = true")
    && !historyClick.includes("await bridge.copyMode") && !historyClick.includes("term.scrollLines"));
ok("History button pages up via the live stream while in copy mode",
  historyClick.includes("if (ent.copyBrowsing) {") && historyClick.includes("bridge.input({ sessionId: ent.sessionId, data: '\\x1b[5~' })"));
ok("To Bottom safely cancels tmux copy mode before local follow", bottomClick.includes("await bridge.cancelCopyMode({ sessionId: ent.sessionId })") && bottomClick.indexOf("await bridge.cancelCopyMode") < bottomClick.indexOf("ent.copyBrowsing = false"));
ok("To Bottom keeps an immediate local fast path outside copy mode", bottomClick.includes("if (!ent.copyBrowsing) {") && bottomClick.indexOf("if (!ent.copyBrowsing)") < bottomClick.indexOf("await bridge.cancelCopyMode"));
ok("Refresh reuses the force-open lifecycle", refreshClick.includes("await openTerminalForSelected({ force: true })"));
ok("force refresh recreates even an already detached xterm",
  open.includes("if (force) {") && !open.includes("if (force && ent.sessionId)"));
ok("Refresh prevents overlapping reconnects",
  source.includes("let terminalRefreshPending = false") && refreshClick.includes("if (terminalRefreshPending"));
ok("Refresh reports progress and completion",
  refreshClick.includes("Re-attaching terminal...") && refreshClick.includes("Terminal re-attached."));
ok("Refresh receives the force-open failure detail",
  open.includes("return res;")
    && refreshClick.includes("const result = await openTerminalForSelected({ force: true })")
    && refreshClick.includes("result?.detail || result?.error"));
ok("Refresh adds no second main-process protocol",
  !preload.includes("refresh(payload)") && !main.includes("term:refresh"));
ok("stale old-session status cannot detach the replacement",
  source.includes("const ent = terminalEntryBySession(msg.sessionId)") && source.includes("if (!ent) return;"));
ok("Refresh blocks every action and exposes progress styling",
  source.includes("const actionsBlocked = terminalRefreshPending")
    && source.includes("terminalRefreshBtn.classList.toggle('is-refreshing', terminalRefreshPending)")
    && terminalChromeCss.includes(".terminal-refresh-btn.is-refreshing")
    && terminalChromeCss.includes("@keyframes terminal-refresh-spin"));
ok("terminal attachment pending state survives shared control refreshes",
  source.includes("let terminalAttachmentPending = false")
    && source.includes("terminalAttachmentPending = terminalDelivery")
    && source.includes("terminalAttachmentPending = false"));
ok("tmux refresh reconciles renderer history with pane state", source.includes("ent.copyBrowsing = !!result.copyMode"));
ok("copy actions invalidate stale tmux refresh state", source.includes("tmuxControlRevision: 0") && source.includes("const tmuxControlRevision = ent.tmuxControlRevision") && source.includes("if (ent.tmuxControlRevision !== tmuxControlRevision) return;"));
ok("xterm scroll state updates the bottom control", source.includes("entry.term.onScroll(() =>"));
ok("To Bottom is available above live output outside copy mode", source.includes("!ent?.copyBrowsing && terminalIsAtBottom(ent)"));
ok("normal wheel remains local outside copy mode", wheel.indexOf("entry.term.scrollLines(lines)") > wheel.indexOf("if (entry.copyBrowsing)"));
ok("preload exposes narrow tmux copy-mode channels", preload.includes("copyMode(payload)") && preload.includes("cancelCopyMode(payload)") && !preload.includes("copyScroll(payload)"));
ok("main owns native tmux copy-mode IPC", main.includes("term:copyMode") && main.includes("term:cancelCopyMode") && !main.includes("term:copyScroll"));
ok("copy-mode entry uses one-page-up on the owned pane", main.includes("['copy-mode', '-u', '-t', state.paneId]"));
ok("copy-mode cancel puts tmux 2.7 options before the command", main.includes("['send-keys', '-X', '-t', state.paneId, 'cancel']"));
ok("copy-mode cancel checks pane state before sending", main.includes("if (!state.copyMode) return { ok: true, copyMode: false, paneId: state.paneId };"));
ok("main never writes a raw q to exit copy mode", !main.includes("termInput(event, { sessionId: payload.sessionId, data: 'q'"));
ok("tmux client recovery falls back only to a sole client without a baseline", main.includes("selectOnlyClient(afterClients)"));
ok("tmux client recovery pauses and resumes after bounded probes", main.includes("const TMUX_CLIENT_RECOVERY_RETRY_DELAY_MS = 30000;") && main.includes("tmuxClientRecoveryNextAt: 0"));
ok("tmux client recovery makes a single post-attach probe", main.includes("async function _retryTmuxClientDiscovery(ent)"));
ok("client tty is ensured (with retry) only where switch-client needs it", main.includes("async function _ensureTmuxClientTty(ent)") && main.includes("await _retryTmuxClientDiscovery(ent)"));
ok("window listing never blocks on client tty discovery", main.includes("if (!ent.tmuxClientTty) void _retryTmuxClientDiscovery(ent).catch(() => {});"));
ok("switch-client paths gate on an ensured client tty", (main.match(/const client = await _ensureTmuxClientTty\(ent\);/g) || []).length === 2);
ok("terminal state stores the client set and recovery schedule", main.includes("tmuxBeforeClients: beforeClients") && main.includes("tmuxClientRecoveryAttempts: 0") && main.includes("tmuxClientRecoveryNextAt: 0"));
ok("initial discovery keeps trying when its baseline probe was unavailable", !main.includes("if (!beforeClients) { ent.tmuxInitialDiscoveryPending = false; return; }"));
ok("tmux control failures identify the failing stage", main.includes("function _tmuxFailure(stage") && main.includes("_tmuxFailure('client_discovery'") && main.includes("_tmuxFailure('client_state'") && main.includes("_tmuxFailure('list_windows'"));
ok("tmux diagnostics expose safe discovery state", main.includes("recoveryAttempts:") && main.includes("initialPending:") && main.includes("beforeClientCount:") && main.includes("lastProbeError:"));
ok("failed tmux probes retain their underlying error", main.includes("tmuxLastProbeError") && main.includes("tmuxLastProbeMs"));
ok("renderer degrades tmux control failures quietly (no persistent error)", source.includes("Quiet degradation") && !source.includes("setTerminalAttachStatus(message, 'error', 0)"));
ok("tmux control timeouts drop the exec connection for zombie recovery", !main.includes("preserve_connection_on_timeout: true"));
// Rationale: after the terminal/exec pool split, tmux controls live on
// the exec pool (no PTY at stake). Dropping on timeout discards a
// possibly-zombie connection so the next op reconnects — one
// sacrificed op instead of a connection that never recovers.
ok("transport aborts opted-in timed-out command channels", transport.includes("preserve_connection_on_timeout") && transport.includes("abortOperation"));
ok("preserved tmux command timeouts do not retry by dropping the pooled terminal connection", transport.includes("!preserveConnectionTimeout && _isRetryableChannelError(first)"));
// tmux 3.2a (hlren) rejects `display-message -p -c <client>` as a usage
// error — the probe failed 100% there and the tab strip never appeared.
// The portable form targets the session instead.
ok("client state probe uses the portable session-targeted display-message",
  main.includes("['display-message', '-p', '-t', ent.tmux.session, '#{window_index}:#{pane_id}:#{pane_in_mode}']"));
ok("client state probe never combines -p with -c (usage error on tmux < 3.3)",
  !main.includes("['display-message', '-p', '-c',"));
ok("tab strip state refresh is fully event-driven (no interval poll)",
  !/setInterval[\s\S]{0,200}refreshTerminalTmuxControls/.test(source));
ok("exhausted tmux retries stop remote polling too",
  source.includes("|| ent.tmuxHintState === 'hidden')"));
ok("terminal tabs are a per-agent opt-in in agent Settings > Attributes",
  source.includes("cam_terminal_tabs_enabled:${agentId}")
    && fs.readFileSync(path.join(__dirname, "..", "..", "..", "web", "desktop.html"), "utf8").includes('id="agent-settings-terminal-tabs"')
    && !fs.readFileSync(path.join(__dirname, "..", "..", "..", "web", "desktop.html"), "utf8").includes('id="appearance-terminal-tabs"')
    && fs.readFileSync(path.join(__dirname, "..", "..", "..", "web", "js", "desktop", "shell.js"), "utf8").includes("terminalTabsKeyFor"));
ok("terminal tabs default on with per-agent opt-out ('0'), silent fallback",
  source.includes("!== '0'") && source.includes("cam_terminal_tabs_enabled:${agentId}")
    && fs.readFileSync(path.join(__dirname, "..", "..", "..", "web", "js", "desktop", "shell.js"), "utf8").includes("!== '0'"));
ok("tmux control failure disables the strip with one transient (<3s) note",
  source.includes("ent.tmuxHintState = 'hidden';")
    && source.includes("setTerminalAttachStatus('window controls unavailable', 'info', 2800, ent.agentId)")
    && !source.includes("const retries = [2000, 5000, 12000, 30000]"));
{
  const shellSrc = fs.readFileSync(path.join(__dirname, "..", "..", "..", "web", "js", "desktop", "shell.js"), "utf8");
  ok("agent settings form refill is guarded by attribute signature (no edit clobber on status_update)",
    shellSrc.includes("agentAttrSig(settingsTarget) !== _agentSettingsAttrSig")
      && shellSrc.includes("function agentAttrSig(agent)")
      && shellSrc.includes("_agentSettingsAttrSig = agentAttrSig(agent);"));
}
ok("tab strip never polls while feature-disabled",
  source.includes("if (!terminalTabsEnabled(termAgentId) || !ent || !bridge || !ent.sessionId"));
ok("attach open has a renderer-side watchdog deadline",
  source.includes("ATTACH_WATCHDOG_MS = 45000") && source.includes("Promise.race") && source.includes("watchdog_timeout"));
ok("watchdog timeout joins the transient-retry set",
  source.includes("'watchdog_timeout'"));
ok("late-opening channels are closed on watchdog and superseded paths",
  source.includes("late.sessionId") && source.includes("stale_open"));
ok("refresh stays clickable while an attach is opening",
  source.includes("terminalRefreshBtn.disabled = actionsBlocked || !terminalVisible || !selectedAgent()")
    && !source.includes("|| !canUseTerminalMode() || termOpening;"));
ok("app exposes a full in-app reset (hub restart, window stays)",
  preload.includes("resetApp") && main.includes("ipcMain.handle('app:reset'")
    && main.includes("sshTransport.closeAll()")
    && main.includes("await embeddedHub.restart({ dataDir: userDataDir() })"));
ok("resetApp force-releases wedged tmux discovery gates (reload-stuck root fix)",
  main.includes("function _resetTmuxDiscovery()")
    && main.includes("const nGates = _resetTmuxDiscovery()")
    && main.includes("_tmuxDiscoveryReleases.add(release)"));
ok("tmux discovery queue wait is bounded (15s) with self-release of abandoned links",
  main.includes("Promise.race([") && main.includes("tmux discovery queue wait exceeded 15s")
    && main.includes("void discoveryBeginP.then((releaseFn)"));
ok("window switch/create use the live attach stream first (pty fast path)",
  main.includes("function _ptySwitchWindow") && main.includes("via: 'pty'") && main.includes("ent.write('\\x02c')"));
ok("window switch drops the pre-switch listWindows validation",
  !main.includes("listed.windows.some"));
ok("tab clicks highlight optimistically and reconcile after pty switch",
  source.includes("result.via === 'pty'") && source.includes("active: w.index === index"));
ok("conn bar exposes a reload-app button wired to reset + reload",
  fs.readFileSync(path.join(__dirname, "..", "..", "..", "web", "desktop.html"), "utf8").includes('id="app-reload-btn"')
    && fs.readFileSync(path.join(__dirname, "..", "..", "..", "web", "js", "desktop", "app.js"), "utf8").includes("await window.CamBridge.resetApp()"));
ok("remote size repair is gated on an actual size change",
  main.includes("const sizeChanged = !!(existingEnt") && main.includes("if (existingEnt.opts) void _repairRemoteTerminalSize(existingEnt.opts, agentId, cols, rows);"));
ok("attach prefers the reachable context endpoint, machine fields only as fallback",
  hub.includes("const fallbackOpts = (machineOpts.host !== opts.host")
    && hub.includes("machineOpts.host = agent.machine_host")
    && main.includes("ATTACH_FALLBACK_ERRORS")
    && !main.includes("if (agent.machine_host) opts.host = agent.machine_host;"));
ok("tmux binary is probed per endpoint, never hardcoded to /bin/tmux",
  main.includes("function _probeRemoteTmuxBin")
    && main.includes("command -v tmux || command -v /bin/tmux")
    && main.includes("tmux.bin = await _probeRemoteTmuxBin(resolved.opts)")
    && !require("fs").readFileSync(require("path").join(__dirname, "..", "electron", "tmux-controls.cjs"), "utf8").includes('|| "/bin/tmux"'));
ok("remote size repair resolves tmux binary from record with env fallback",
  main.includes('tmux_bin = (') && main.includes('shutil.which("tmux")') && !main.includes('["tmux", "-S", str(socket)]'));
// Desktop semantics 2026-07-18: the app never closes pooled SSH
// connections on its own while running — an open desktop terminal
// holds its sessions (unlike mobile).
ok("pooled SSH connections are never idle-closed",
  !transport.includes("_dropEntry(entry.key, 'idle')") && transport.includes("NEVER idle-close"));

// 2026-07-22 stability bundle: half-dead pooled sockets must not be
// reused, hung attaches must recover, and unexpected drops reconnect
// with bounded backoff instead of waiting for a keystroke.
ok("suspect pool entry is dropped on terminal open failure/timeout",
  transport.includes("_dropEntry(key, 'open_timeout')") && transport.includes("_dropEntry(key, 'open_failed')"));
ok("terminal channel open default timeout is tightened to 15s",
  transport.includes("Number(opts.timeout_ms) || 15000"));
ok("password auth falls back to keyboard-interactive with password answers",
  transport.includes("tryKeyboard:        authBuilt.auth === 'password'") && transport.includes("client.on('keyboard-interactive'"));
ok("encrypted key without passphrase falls back to ssh agent",
  transport.includes("falling back to agent") && transport.includes("ssh2.utils.parseKey(keyBuf)"));
ok("key_file_missing error carries fix guidance",
  transport.includes("not found — edit the host to select a valid key file or use password auth"));
ok("OS resume drops only idle pooled entries",
  transport.includes("function dropIdleEntries") && transport.includes("'resume_idle'") && main.includes("powerMonitor.on('resume'"));
ok("terminal repair evidence is persisted to a user-visible log",
  main.includes("function _diagLog") && main.includes("cam-desktop.log"));
ok("attach retries once on transient open failures",
  source.includes("TRANSIENT_ATTACH_ERRORS.has(res && res.error)") && source.includes("const TRANSIENT_ATTACH_ERRORS"));
ok("unexpected drops auto-reconnect with bounded backoff",
  source.includes("function _scheduleAutoReconnect") && source.includes("AUTO_RECONNECT_DELAYS_TRANSPORT") && source.includes("AUTO_RECONNECT_DELAYS_EXIT"));
ok("failure budget resets only after a sustained (>10s) reconnect",
  source.includes("RECONNECT_SUSTAIN_MS") && source.includes("ent._liveSince = Date.now();")
    && !source.includes("ent._autoReconnectAttempt = 0;\n        if (typeof bridge.ready === 'function')"));
ok("exhausted ladder continues as a background retry loop with guidance",
  source.includes("function _startBackgroundRetry")
    && source.includes("AUTO_RECONNECT_BG_FIRST_MS") && source.includes("AUTO_RECONNECT_BG_INTERVAL_MS")
    && !source.includes("auto-reconnect exhausted"));
ok("status pill is the visible, clickable retry affordance",
  source.includes("terminalAttachStatus.addEventListener('click'"));
ok("keystroke reconnect cancels the scheduled auto attempt",
  source.includes("if (ent._autoReconnectTimer) { clearTimeout(ent._autoReconnectTimer); ent._autoReconnectTimer = null; }"));
ok("reconnect path has the same watchdog (no 'reconnecting' latch)",
  fn("reconnectTerminalEntry", "_scheduleAutoReconnect").includes("ATTACH_WATCHDOG_MS"));
ok("persistent attach status is owned per agent and cleared on switch",
  source.includes("terminalAttachStatusOwner") && source.includes("ttl === 0) ? ownerId : null"));
ok("slow layouts get a late fit pass",
  source.includes("window.setTimeout(pass, 700)"));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
