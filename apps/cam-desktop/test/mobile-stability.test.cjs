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
const mobileRoot = path.join(root, "web", "js", "mobile");

function jsFiles(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((ent) => {
    const p = path.join(dir, ent.name);
    return ent.isDirectory() ? jsFiles(p) : (ent.name.endsWith(".js") ? [p] : []);
  });
}

const stampedImports = [];
for (const file of jsFiles(mobileRoot)) {
  const src = fs.readFileSync(file, "utf8");
  const re = /(?:from\s*|import\s*)["']([^"']+)["']/g;
  let m;
  while ((m = re.exec(src))) {
    if (m[1].startsWith(".") && m[1].includes("?")) {
      stampedImports.push(path.relative(root, file) + ": " + m[1]);
    }
  }
}
ok("mobile local ES modules use one unstamped canonical URL", stampedImports.length === 0, stampedImports.join("; "));

const html = fs.readFileSync(path.join(root, "web", "mobile.html"), "utf8");
const moduleUrls = [...html.matchAll(/<(?:script|link)\b[^>]*(?:src|href)="([^"]+\.js[^"']*)"[^>]*>/g)]
  .map((m) => m[1])
  .filter((url) => !url.includes("vendor/"));
ok("mobile HTML module entry and preload use canonical URLs", moduleUrls.every((url) => !url.includes("?")), moduleUrls.join(", "));

function source(rel) {
  return fs.readFileSync(path.join(root, rel), "utf8");
}

const activity = source("apps/cam-mobile/app/src/main/java/com/cam/app/MainActivity.java");
ok("release WebView disables debugging and unrestricted mixed content",
  activity.includes("setWebContentsDebuggingEnabled(false)")
    && !activity.includes("MIXED_CONTENT_ALWAYS_ALLOW")
    && activity.includes("MIXED_CONTENT_NEVER_ALLOW"));
const ssh = source("apps/cam-mobile/app/src/main/java/com/cam/app/MobileSshAuth.java");
ok("SSH uses strict persistent host-key verification",
  ssh.includes('StrictHostKeyChecking", "yes')
    && !ssh.includes('StrictHostKeyChecking", "no')
    && ssh.includes("MobileHostKeyRepository"));
const manifest = source("apps/cam-mobile/app/src/main/AndroidManifest.xml");
ok("dead APK installer bridge and permission are removed",
  !manifest.includes("REQUEST_INSTALL_PACKAGES")
    && !source("apps/cam-mobile/app/src/main/java/com/cam/app/CamJsBridge.java").includes("installApk"));
const dashboard = source("web/js/mobile/views/dashboard.js");
ok("dashboard escapes hub fields and constrains status classes",
  dashboard.includes("escapeAttr(agent.id)") && dashboard.includes("safeStatus(agentDisplayStatus(agent))"));
const detail = source("web/js/mobile/views/agent-detail.js");
ok("agent detail escapes metadata and constrains status classes",
  detail.includes("safeAgentBadge") && detail.includes("escapeHtml(renderMeta())"));
ok("agent detail stops terminal output polling and wires cancellation",
  detail.includes("isTerminalStatus(agent.status) || isTerminalMode()")
    && detail.includes("fn(controller.signal)")
    && detail.includes("document.removeEventListener('click', _documentMenuClickHandler)"));
const autoAttach = detail.slice(
  detail.indexOf("async function ensureAgentTerminalAttach"),
  detail.indexOf("function applyOutputMode"));
ok("terminal attach leaves the Android IME closed until the user taps",
  !autoAttach.includes("focusTerminalForAgent(")
    && detail.includes("host.addEventListener('click', () => focusTerminalForAgent(agentId))"));
const terminalMount = source("web/js/shared/terminal-mount.js");
const showTerminalEntry = terminalMount.slice(
  terminalMount.indexOf("function showTerminalEntry"),
  terminalMount.indexOf("function prepareThenShowTerminalEntry"));
ok("shared terminal show only auto-focuses outside Android",
  showTerminalEntry.includes("if (!isAndroidWebView())")
    && showTerminalEntry.indexOf("if (!isAndroidWebView())") < showTerminalEntry.indexOf("ent.term?.focus()"));
const nodes = source("web/js/shared/nodes-mode.js");
ok("Nodes viewport listeners are removed by route cleanup",
  nodes.includes("cleanupViewportListeners") && nodes.includes("vv.removeEventListener('resize', syncFooterInset)"));
const mobileApp = source("web/js/mobile/app.js");
ok("mobile refresh is visibility guarded and includes Direct",
  mobileApp.includes("if (document.hidden)") && mobileApp.includes("mode === 'direct'"));

console.log(`mobile-stability: ${pass} passed, ${fail} failed`);
process.exitCode = fail ? 1 : 0;
