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

ok("show function exists", show.length > 0);
const reveal = show.indexOf("ent.container.hidden = false");
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
ok("cache limit remains six", source.includes("const TERMINAL_CACHE_LIMIT = 6"));
ok("history follow uses viewport state", source.includes("terminalShouldForceBottom(ent) || terminalIsAtBottom(ent)"));
ok("history write does not recheck and yank viewport", source.includes("if (shouldFollow) terminalScrollToBottom(ent);"));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
