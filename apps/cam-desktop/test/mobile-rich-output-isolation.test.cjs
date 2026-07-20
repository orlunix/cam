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
const detailPath = path.join(root, "web", "js", "mobile", "views", "agent-detail.js");
const terminalMountPath = path.join(root, "web", "js", "shared", "terminal-mount.js");
const richPath = path.join(root, "web", "js", "mobile", "rich-output.js");
const oldRichPath = path.join(root, "web", "js", "mobile", "rich-output", "rich-output.js");
const embeddedHubPath = path.join(root, "android", "app", "src", "main", "java", "com", "cam", "app", "MobileEmbeddedHub.java");
const outputSessionsPath = path.join(root, "android", "app", "src", "main", "java", "com", "cam", "app", "MobileAgentOutputSessions.java");
const buildPath = path.join(root, "android", "build.sh");

const detail = fs.readFileSync(detailPath, "utf8");
const terminalMount = fs.readFileSync(terminalMountPath, "utf8");
const richExists = fs.existsSync(richPath);
const rich = richExists ? fs.readFileSync(richPath, "utf8") : "";
const embeddedHub = fs.readFileSync(embeddedHubPath, "utf8");
const outputSessionsExists = fs.existsSync(outputSessionsPath);
const outputSessions = outputSessionsExists ? fs.readFileSync(outputSessionsPath, "utf8") : "";
const build = fs.readFileSync(buildPath, "utf8");

ok("Raw output is the capture-mode label", detail.includes(">Raw output</button>"));
ok("Live output label is no longer exposed", !detail.includes(">Live output</button>"));
ok("Rich output menu item is exposed", detail.includes("id=\"toggle-rich\"") && detail.includes(">Rich output</button>"));
ok("Rich output mode can be persisted", detail.includes("v === 'rich'"));
ok("Rich renderer lives in the normal mobile module path", richExists);
ok("Old Rich output module directory is removed", !fs.existsSync(oldRichPath));
ok("agent detail imports the mobile Rich renderer", detail.includes("../rich-output.js"));

ok("Rich output is a text renderer, not xterm", rich && !rich.includes("window.Terminal") && !rich.includes("FitAddon") && !rich.includes("xterm") && !rich.includes("scrollLines"));
ok("Rich output does not subscribe to terminal data", rich && !rich.includes(".onData(") && !rich.includes("__camNativeTerm") && !rich.includes("CamBridge.term"));
ok("Rich output no longer keeps ANSI stream buffers", rich && !rich.includes("RICH_OUTPUT_RING_BUFFER") && !rich.includes("ansiBuffer"));
ok("Rich output renderer exposes render/set/dispose functions", rich.includes("export function renderRichOutput") && rich.includes("export function setRichOutputVisible") && rich.includes("export function disposeRichOutput"));
ok("Rich output uses lightweight semantic text decoration", rich.includes("classifyRichLine") && rich.includes("renderInlineRichText"));
ok("Rich output detects repeated-character separator lines", rich.includes("function isRichSeparatorLine") && rich.includes("normalized.length >= 4") && rich.includes("return 'separator'"));
ok("Rich output renders separators with a dedicated divider class", rich.includes("rich-line-separator") && rich.includes("if (kind === 'separator')"));

ok("Terminal core remains without Rich output coupling", !terminalMount.includes("rich-output") && !terminalMount.includes("Rich output"));
ok("Raw/Rich output input uses camc API with Terminal-only terminal input", detail.includes("if (isTerminalMode() && mobileTerminalInput() && terminalSessionReady(agentId))") && !detail.includes("function canUseAttachedTerminalInput()"));
ok("Rich output uses the Raw capture fetch path", !detail.includes("if (isTerminalMode() || isRichOutputMode()) return;") && detail.includes("if (isRichOutputMode())") && detail.includes("renderRichOutput(agentId, container.querySelector('#rich-output-host'), data.output)"));
ok("Rich output polling is allowed", detail.includes("if (isTerminalMode() || useFullOutput) return;") && !detail.includes("if (isTerminalMode() || isRichOutputMode() || useFullOutput) return;"));
ok("Raw/Rich output polls every 2s", detail.includes("let _outputPollMs = 2000") && detail.includes("if (_outputPollMs !== 2000)"));
ok("Rich output switching restarts output polling", detail.includes("if ((outputMode === 'live' || outputMode === 'rich') && active)"));
ok("Rich output does not use Terminal stream sync", !detail.includes("syncRichOutputStream") && !detail.includes("refreshRichOutput"));

ok("Android long-lived output session manager exists", outputSessionsExists);
ok("Android output session manager has a 10 minute idle timeout", outputSessions.includes("IDLE_MS = 10L * 60L * 1000L"));
ok("Android output session manager uses long-lived SSH sessions", outputSessions.includes("MobileSshExec.openSession(opts, true)"));
ok("Android output capture uses long-lived output sessions", embeddedHub.includes("MobileAgentOutputSessions.exec("));
ok("Android input send uses long-lived output sessions", embeddedHub.includes("MobileAgentOutputSessions.execStdin("));
ok("Android key send uses long-lived output sessions", embeddedHub.includes("MobileAgentOutputSessions.exec(\n            auth, id, MobileSshExec.camcKeyCommand(id, key), SEND_TIMEOUT_MS)"));
ok("Android build includes long-lived output session manager", build.includes("MobileAgentOutputSessions.java"));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
