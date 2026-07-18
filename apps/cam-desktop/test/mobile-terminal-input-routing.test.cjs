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

function fn(name, next) {
  const start = source.indexOf("function " + name + "(");
  const end = source.indexOf("function " + next + "(", start);
  return start >= 0 ? source.slice(start, end >= 0 ? end : undefined) : "";
}

const predicate = fn("canUseAttachedTerminalInput", "sendAgentInput");
const sendInput = fn("sendAgentInput", "sendAgentKey");
const sendKey = fn("sendAgentKey", "prefetchCapturePreview");
const loadOutput = fn("loadOutput", "loadLogs");

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

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
