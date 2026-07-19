"use strict";

const fs = require("fs");
const path = require("path");

let pass = 0;
let fail = 0;
function ok(name, condition, detail = "") {
  if (condition) pass++;
  else {
    fail++;
    console.error("FAIL " + name + (detail ? " - " + detail : ""));
  }
}

const sourcePath = path.join(__dirname, "..", "..", "..", "web", "js", "mobile", "views", "agent-detail.js");
const source = fs.readFileSync(sourcePath, "utf8");
const mobileHtmlPath = path.join(__dirname, "..", "..", "..", "web", "mobile.html");
const mobileHtml = fs.readFileSync(mobileHtmlPath, "utf8");
const terminalMountPath = path.join(__dirname, "..", "..", "..", "web", "js", "shared", "terminal-mount.js");
const terminalMount = fs.readFileSync(terminalMountPath, "utf8");
const mobileCssPath = path.join(__dirname, "..", "..", "..", "web", "css", "mobile.css");
const mobileCss = fs.readFileSync(mobileCssPath, "utf8");

function fn(name, next) {
  const start = source.indexOf("function " + name + "(");
  const end = source.indexOf("function " + next + "(", start);
  return start >= 0 ? source.slice(start, end >= 0 ? end : undefined) : "";
}
const predicate = fn("canUseAttachedTerminalInput", "sendAgentInput");
const sendInput = fn("sendAgentInput", "sendAgentKey");
const sendKey = fn("sendAgentKey", "prefetchCapturePreview");
const loadOutput = fn("loadOutput", "loadLogs");
const terminalMode = fn("isTerminalMode", "isTerminalDisplayMode");
const terminalDisplay = fn("isTerminalDisplayMode", "mobileTerminalInput");
const ensureAttach = fn("ensureAgentTerminalAttach", "applyOutputMode");
const applyMode = fn("applyOutputMode", "updateOutputModeMenu");
const switchMode = fn("switchOutputMode", "restartOutputPoll");
const restartPoll = fn("restartOutputPoll", "inputHTML");
const wireEvents = fn("wireEvents", "onMobileAppearance");
const wireInput = fn("wireInputButtons", "closeFullscreen");
const keybar = fn("wireTerminalKeyBar", "terminalMenuHTML");
const termFocus = (() => {
  const start = terminalMount.indexOf("export function focusTerminalForAgent(");
  const end = terminalMount.indexOf("export async function sendTerminalRaw", start);
  return start >= 0 ? terminalMount.slice(start, end >= 0 ? end : undefined) : "";
})();
const terminalReadOnly = (() => {
  const start = terminalMount.indexOf("export function setTerminalReadOnly(");
  const end = terminalMount.indexOf("export function setTerminalStatus", start);
  return start >= 0 ? terminalMount.slice(start, end >= 0 ? end : undefined) : "";
})();
const resumeTerminal = (() => {
  const start = terminalMount.indexOf("export async function resumeTerminalForAgent(");
  const end = terminalMount.indexOf("export function terminalPoolSize", start);
  return start >= 0 ? terminalMount.slice(start, end >= 0 ? end : undefined) : "";
})();
const openTerminal = (() => {
  const start = terminalMount.indexOf("export async function openTerminalForAgent(");
  const end = terminalMount.indexOf("export function scheduleTerminalFit", start);
  return start >= 0 ? terminalMount.slice(start, end >= 0 ? end : undefined) : "";
})();

ok("attached-terminal predicate exists", predicate.length > 0);
ok("predicate requires native mobile bridge", predicate.includes("mobileTerminalInput()"));
ok("predicate requires ready terminal session", predicate.includes("terminalSessionReady(agentId)"));
ok("predicate is independent from visible output mode", !predicate.includes("isTerminalMode()"));

ok("sendAgentInput exists", sendInput.length > 0);
ok("sendAgentInput uses attached predicate", sendInput.includes("canUseAttachedTerminalInput()"));
ok("sendAgentInput uses terminal input fast path", sendInput.includes("sendTerminalInput(agentId, text, { enter: withEnter })"));
ok("sendAgentInput preserves api fallback", sendInput.includes("api.sendInput(agentId, text, withEnter, agentHints())"));
ok("sendAgentInput no longer gates terminal input on output mode", !sendInput.includes("isTerminalMode()"));

ok("sendAgentKey exists", sendKey.length > 0);
ok("sendAgentKey uses attached predicate", sendKey.includes("canUseAttachedTerminalInput()"));
ok("sendAgentKey uses terminal key fast path", sendKey.includes("sendTerminalKey(agentId, key)"));
ok("sendAgentKey preserves api fallback", sendKey.includes("api.sendKey(agentId, key, agentHints())"));
ok("sendAgentKey no longer gates terminal keys on output mode", !sendKey.includes("isTerminalMode()"));

ok("output polling defaults to two seconds", source.includes("let _outputPollMs = 2000;"));
ok("loadOutput exists", loadOutput.length > 0);
ok("successful output polling resets to two seconds", loadOutput.includes("if (_outputPollMs !== 2000)") && loadOutput.includes("_outputPollMs = 2000;"));
ok("output polling keeps failure backoff cap", loadOutput.includes("Math.min(30000, _outputPollMs * 2)"));
ok("Raw output fetch-active guard remains declared", source.includes("let _fetchActive = false;") && loadOutput.includes("if (_fetchActive) return;"));

ok("interactive Terminal mode remains terminal-only", terminalMode.includes("outputMode === 'terminal'") && !terminalMode.includes("outputMode === 'rich'"));
ok("terminal display helper exists", terminalDisplay.length > 0);
ok("Rich output uses the terminal display host", terminalDisplay.includes("outputMode === 'rich'") && terminalDisplay.includes("outputMode === 'terminal'"));
ok("agent-detail imports terminal read-only control", source.includes("setTerminalReadOnly,"));
ok("agent-detail no longer imports terminal rich HTML", !source.includes("getTerminalRichHtml"));
ok("agent-detail no longer imports raw terminal stream fallback for Rich HTML", !source.includes("getTerminalStreamText"));
ok("Rich output HTML renderer has been removed", !source.includes("function renderLiveStreamOutput") && !source.includes("compactTerminalRichRows"));
ok("mobile keeps xterm serialize addon for terminal parking snapshots", mobileHtml.includes("vendor/xterm/addon-serialize.js"));
ok("serialize addon constructor resolves UMD class", terminalMount.includes("window.SerializeAddon.SerializeAddon") && terminalMount.includes("new SerializeCtor()"));
ok("terminal session entries track read-only state", terminalMount.includes("readOnly: false"));
ok("terminal read-only setter exists", terminalReadOnly.length > 0);
ok("terminal read-only setter toggles disableStdin", terminalReadOnly.includes("ent.term.options.disableStdin = ent.readOnly"));
ok("terminal read-only setter blurs active terminal", terminalReadOnly.includes("ent.term.blur()"));
ok("terminal read-only setter suppresses Android soft keyboard", terminalReadOnly.includes("xterm-helper-textarea") && terminalReadOnly.includes("inputmode") && terminalReadOnly.includes("readonly"));
ok("terminal read-only setter syncs touch scroll bridge", terminalReadOnly.includes("syncTerminalReadOnlyScrollBridge(ent)"));
ok("read-only option is applied only during open lifecycle", openTerminal.includes("if (opts.readOnly != null) setTerminalReadOnly(agent.id, opts.readOnly);") && !resumeTerminal.includes("opts.readOnly"));
ok("Rich read-only follow respects manual scrollback while Terminal keeps live follow", terminalMount.includes("function terminalIsAtBottom") && terminalMount.includes("const shouldFollow = ent.readOnly") && terminalMount.includes("? (terminalShouldForceBottom(ent) || terminalIsAtBottom(ent))") && terminalMount.includes(": (ent.agentId === termAgentId || terminalShouldForceBottom(ent));") && terminalMount.includes("if (shouldFollow) terminalScrollToBottom(ent);"));
ok("Rich read-only touch scrolling bridge exists", terminalMount.includes("function syncTerminalReadOnlyScrollBridge") && terminalMount.includes("touchmove") && terminalMount.includes("ent.term.scrollLines(lines)") && terminalMount.includes("{ passive: false }") && terminalMount.includes("_readOnlyScrollCleanup"));
ok("Rich read-only touch scrolling pauses auto-follow", terminalMount.includes("ent.needsBottom = false;") && terminalMount.includes("ent.forceBottomUntil = 0;"));
ok("Rich read-only viewport advertises native touch scrolling", mobileCss.includes(".agent-terminal-pane.is-read-only .xterm-viewport") && mobileCss.includes("-webkit-overflow-scrolling: touch") && mobileCss.includes("touch-action: pan-y"));
ok("terminal onData is blocked while read-only", terminalMount.includes("if (entry.readOnly) return;") && terminalMount.indexOf("if (entry.readOnly) return;") < terminalMount.indexOf("globalBridge.input({ sessionId: entry.sessionId, data })"));
ok("terminal focus ignores read-only Rich output", termFocus.includes("ent.readOnly") && termFocus.includes("return false"));
ok("Rich output attaches and resumes the existing terminal host", ensureAttach.includes("isTerminalDisplayMode()") && ensureAttach.includes("setTerminalViewActive(agentId, true)"));
ok("Rich output does not focus terminal on attach", ensureAttach.includes("if (isTerminalMode()) focusTerminalForAgent(agentId)") || ensureAttach.includes("if (isTerminalMode()) {\n      focusTerminalForAgent(agentId);"));
ok("applyOutputMode uses display helper for terminal visibility", applyMode.includes("const showTerminal = isTerminalDisplayMode();") && applyMode.includes("setTerminalViewActive(agentId, showTerminal)"));
ok("applyOutputMode makes Rich output read-only", applyMode.includes("setTerminalReadOnly(agentId, outputMode === 'rich')"));
ok("applyOutputMode hides composer only for interactive Terminal", applyMode.includes("const hideInput = isTerminalMode();") && applyMode.includes("inputSection.classList.toggle('is-terminal-hidden', hideInput)"));
ok("initial render shows terminal host for Rich output", source.includes("const showTerminalOnRender = isActive && isTerminalDisplayMode();"));
ok("initial render hides composer only for interactive Terminal", source.includes("const hideInputOnRender = isActive && isTerminalMode();") && source.includes("input-section${hideInputOnRender"));
ok("Rich terminal host taps do not focus/open keyboard", keybar.includes("if (isTerminalMode()) focusTerminalForAgent(agentId)"));
ok("Rich output keeps to-bottom button available", source.includes("} else if (outputMode === 'rich') {") && source.includes("jumpBottom.classList.remove('hidden')"));
ok("Rich output bypasses capture load because xterm receives terminal data", loadOutput.includes("if (isTerminalDisplayMode()) return;") && loadOutput.indexOf("if (isTerminalDisplayMode()) return;") < loadOutput.indexOf("api.agentOutput"));
ok("Rich output is excluded from capture polling", restartPoll.length > 0 && restartPoll.includes("if (isTerminalDisplayMode() || useFullOutput) return;"));
ok("switching to Rich output does not start capture polling", !switchMode.includes("outputMode === 'rich'") && switchMode.includes("outputMode === 'live'") && switchMode.includes("restartOutputPoll();"));
ok("switching to Raw output starts existing capture poll", switchMode.includes("outputMode === 'live'") && switchMode.includes("restartOutputPoll();"));
ok("Rich output menu item is added", source.includes("id=\"toggle-rich\">Rich output</button>"));
ok("Raw output keeps old live mode menu id", source.includes("id=\"toggle-live\">Raw output</button>"));
ok("Raw output labels do not expose old Live output wording", !source.includes("Live output"));
ok("Raw output switches to old live capture mode", wireEvents.includes("#toggle-live") && wireEvents.includes("switchOutputMode('live')"));
ok("Rich output switches to new rich terminal mode", wireEvents.includes("#toggle-rich") && wireEvents.includes("switchOutputMode('rich')"));
ok("normal Send explicitly sends text plus Enter", wireInput.includes("sendAgentInput(text, true)"));
ok("normal Send allows empty input to send Enter", !wireInput.includes("(!text && !_directInput)"));
ok("empty normal Send uses Enter key path", wireInput.includes("if (_directInput || !text) {") && wireInput.includes("sendAgentKey('Enter')"));
ok("direct mode Send sends only Enter", wireInput.includes("if (_directInput) {") && wireInput.includes("sendAgentKey('Enter')"));
ok("direct mode Send does not resend mirrored text", wireInput.indexOf("sendAgentKey('Enter')") >= 0 && wireInput.indexOf("sendAgentKey('Enter')") < wireInput.indexOf("sendAgentInput(text, true)"));
console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
