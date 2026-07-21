"use strict";

const fs = require("fs");
const path = require("path");

let pass = 0;
let fail = 0;
function ok(name, condition, detail = "") {
  if (condition) pass++;
  else {
    fail++;
    console.error("FAIL " + name + (detail ? " — " + detail : ""));
  }
}

const root = path.join(__dirname, "..", "..", "..");
const mobile = fs.readFileSync(path.join(root, "web", "js", "mobile", "views", "start-agent.js"), "utf8");
const legacy = fs.readFileSync(path.join(root, "web", "js", "views", "start-agent.js"), "utf8");
const desktop = fs.readFileSync(path.join(root, "web", "js", "desktop", "start-agent-mode.js"), "utf8");
const desktopHtml = fs.readFileSync(path.join(root, "web", "desktop.html"), "utf8");

for (const [name, src] of [["mobile", mobile], ["legacy", legacy]]) {
  ok(`${name} Start default tools include others`, src.includes("const DEFAULT_TOOLS = ['claude', 'codex', 'cursor', 'others']"));
  ok(`${name} Start hides aider`, src.includes("new Set(['generic', 'aider'])") && !src.includes("'claude', 'cursor', 'codex', 'aider'"));
  ok(`${name} Start renders custom tool input`, src.includes('id="custom-tool"') && src.includes('id="custom-tool-group"'));
  ok(`${name} Start sends the custom command for Others`, src.includes("function selectedTool(container)") && src.includes("tool,") && src.includes("Enter a tool command for Others"));
  ok(`${name} Start makes Context optional`, src.includes('Context (optional)') && src.includes('<select id="context" class="form-input">') && !src.includes('<select id="context" class="form-input" required>'));
  ok(`${name} Start supports node/path fallback`, src.includes('id="node-path-fields"') && src.includes('id="remote-path"') && src.includes("function applyTarget(container, body)") && src.includes("body.node = node") && src.includes("body.path = path"));
}

ok("desktop Start default tools include others", desktop.includes("const DEFAULT_TOOLS = ['claude', 'codex', 'cursor', 'others']"));
ok("desktop Start hides aider", desktop.includes("new Set(['generic', 'aider'])") && !desktop.includes("'claude', 'codex', 'cursor', 'aider'"));
ok("desktop Start API support disables others", desktop.includes("others: false") && !desktop.includes("aider: false"));
ok("desktop hub API support disables others", fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8").includes("others: false") && !fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8").includes("aider: false"));
ok("desktop Start custom tool field exists", desktopHtml.includes('id="start-custom-tool-field"') && desktopHtml.includes('id="start-custom-tool"'));
ok("desktop Start sends custom command for Others", desktop.includes("function selectedToolCommand()") && desktop.includes("tool: selectedToolCommand()") && desktop.includes("Enter a tool command for Others."));


const androidHub = fs.readFileSync(path.join(root, "android", "app", "src", "main", "java", "com", "cam", "app", "MobileEmbeddedHub.java"), "utf8");
const mobileSshExec = fs.readFileSync(path.join(root, "android", "app", "src", "main", "java", "com", "cam", "app", "MobileSshExec.java"), "utf8");

ok("Android Hub exposes POST /api/agents", androidHub.includes('"POST".equals(method) && "/api/agents".equals(path)'));
ok("Android Hub starts agents through existing remote camc", androidHub.includes("startRemoteAgent") && mobileSshExec.includes("camcRunCommand") && mobileSshExec.includes("camcStatusCommand"));
ok("Android Hub supports context or node/path targets", androidHub.includes("resolveStartTarget") && androidHub.includes("node_not_registered") && androidHub.includes("missing_target"));

function between(src, start, end) {
  const a = src.indexOf(start);
  const b = src.indexOf(end, a + start.length);
  return a >= 0 && b >= 0 ? src.slice(a, b) : "";
}

const mobileAdvanced = between(mobile, '<details class="form-advanced">', '<button type="submit"');
const mobileTaskNameIndex = mobile.indexOf('<label for="name">Task name (optional)</label>');
const mobileAdvancedIndex = mobile.indexOf('<details class="form-advanced">');
ok("mobile Start keeps Task name outside Advanced", mobileTaskNameIndex >= 0 && mobileAdvancedIndex >= 0 && mobileTaskNameIndex < mobileAdvancedIndex);
ok("mobile Start moves Prompt into Advanced", mobileAdvanced.includes('<label for="prompt">Prompt (optional)</label>'));
ok("mobile Start moves Auto-confirm into Advanced", mobileAdvanced.includes('id="autoconfirm"'));
ok("mobile Start moves Auto-exit into Advanced", mobileAdvanced.includes('id="autoexit"'));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
