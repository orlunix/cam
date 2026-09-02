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
const shell = fs.readFileSync(path.join(root, "web", "js", "mobile", "nodes-shell.js"), "utf8");
const mode = fs.readFileSync(path.join(root, "web", "js", "shared", "nodes-mode.js"), "utf8");
const css = fs.readFileSync(path.join(root, "web", "css", "nodes-mode.css"), "utf8");

function idx(label) {
  const i = shell.indexOf(label);
  ok(`mobile Nodes shell contains ${label}`, i >= 0);
  return i;
}

const nodeName = idx("Node name");
const host = idx(">Host");
const user = idx(">User");
const port = idx(">Port");
const auth = idx("Auth method");
const contextName = idx("Context name");
const remotePath = idx("Remote path");
const envSetup = idx("Env setup (optional)");

ok("mobile Nodes keeps connection fields before defaulted workspace fields",
  host < nodeName && nodeName < user && user < port && port < auth && auth < contextName && contextName < remotePath && remotePath < envSetup,
  `order=${[nodeName, host, user, port, auth, contextName, remotePath, envSetup].join(",")}`);
ok("mobile Nodes context name remains required", /id="nodes-add-name"[^>]*\srequired\b/.test(shell));
ok("mobile Nodes remote path remains required", /id="nodes-add-path"[^>]*\srequired\b/.test(shell));
ok("mobile Nodes context name placeholder explains default", /id="nodes-add-name"[^>]*placeholder="defaults to node name"/.test(shell));
ok("mobile Nodes remote path placeholder explains home-dir default", /id="nodes-add-path"[^>]*placeholder="\/home\/&lt;user&gt;"/.test(shell));

ok("mobile nodes has helper for default context name", mode.includes("function mobileDefaultContextName") && mode.includes("function ensureMobileContextNameDefault"));
ok("mobile nodes has helper for default remote path", mode.includes("function mobileDefaultRemotePath"));
ok("mobile Add Host defaults context name from node name", mode.includes("ensureMobileContextNameDefault(host)") && mode.includes("mobileDefaultContextName({ explicitName: fName.value.trim(), nodeName: fNodeName ? fNodeName.value.trim() : '', host })"));
ok("mobile Add Host defaults path from username", mode.includes("ensureMobileRemotePathDefault(user)") && mode.includes("mobileDefaultRemotePath(fPath.value, user)"));
ok("desktop Add Host defaults context name/path too (no explicit-only gate)",
  !mode.includes("!mobileForm && (!name || !ctxPath)")
    && mode.includes("mobileDefaultContextName({ explicitName: fName.value.trim(), nodeName: fNodeName ? fNodeName.value.trim() : '', host })")
    && mode.includes("mobileDefaultRemotePath(fPath.value, user)"));
ok("mobile Add Context also defaults context name/path", mode.includes("mobileDefaultContextName({ explicitName: fName ? fName.value.trim() : '', nodeName: node.displayName || node.nodeName || '', host: m.host })") && mode.includes("mobileDefaultRemotePath(fPath ? fPath.value : '', m.user)"));
ok("mobile context cards expose duplicate action",
  !mode.includes("mobileForm ? '' : `<button type=\"button\" class=\"btn-xs ctx-duplicate-context-btn\"")
    && mode.includes("ctx-duplicate-context-btn"));
ok("mobile context row has a stacked-only layout branch",
  mode.includes("ctx-row-main ctx-row-main-stacked")
    && mode.includes("ctx-row-path-line")
    && mode.includes("${mobileForm ? renderMobileContextMain(ctx, actionButtons)"));
ok("desktop context row keeps the existing inline action branch",
  mode.includes("renderDesktopContextMain(ctx, lastOneLine)")
    && mode.includes("<div class=\"ctx-row-actions\">${actionButtons}</div>"));
ok("mobile context actions use short compact labels",
  mode.includes("mobileForm ? 'Dup' : 'duplicate'")
    && mode.includes("mobileForm ? 'Del' : 'delete'"));
ok("mobile context row CSS stacks name path and actions",
  css.includes(".ctx-row-main-stacked")
    && css.includes(".ctx-row-path-line")
    && css.includes("justify-content: flex-start"));
ok("duplicate context names use numeric index",
  mode.includes("function nextIndexedContextName")
    && mode.includes("candidate = `${base}-${i}`")
    && !mode.includes("-copy"));

// Desktop Nodes form: same layout + defaults contract as mobile.
// (Scope label searches to the add-form — ">User"/">Host" appear
// elsewhere in desktop.html.)
const desktopHtml = fs.readFileSync(path.join(root, "web", "desktop.html"), "utf8");
const desktopFormHtml = desktopHtml.slice(desktopHtml.indexOf('id="nodes-add-form"'));
const desktopOrder = [">Host", "Node name", ">User", ">Port", "Auth method", "Context name", "Remote path", "Env setup (optional)"]
  .map((label) => {
    const i = desktopFormHtml.indexOf(label);
    ok(`desktop Nodes form contains ${label}`, i >= 0);
    return i;
  });
ok("desktop Nodes keeps connection fields before defaulted workspace fields",
  desktopOrder.every((v, i) => i === 0 || v > desktopOrder[i - 1]),
  `order=${desktopOrder.join(",")}`);
ok("desktop context name placeholder explains default",
  /id="nodes-add-name"[^>]*placeholder="defaults to node name"/.test(desktopHtml));
ok("desktop context name/path rely on JS defaults (no native required)",
  !/id="nodes-add-name"[^>]*\srequired\b/.test(desktopHtml)
    && !/id="nodes-add-path"[^>]*\srequired\b/.test(desktopHtml));
ok("system user default comes from Electron preload, used verbatim",
  fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "preload.cjs"), "utf8").includes("getSystemUser")
    && mode.includes("function systemUsername")
    && mode.includes("return raw.trim();")
    && !mode.includes("split('\\\\').pop().split('@')[0]"));
// Sandboxed preloads can require only 'electron' and a tiny built-in
// allowlist (events/timers/url) — anything else (e.g. 'os') throws
// "module not found" and kills the whole CamBridge surface.
ok("preload only requires sandbox-allowed modules",
  (fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "preload.cjs"), "utf8")
    .match(/require\(\s*['"][^'"]+['"]\s*\)/g) || [])
    .every((r) => /require\(\s*['"](electron|events|timers|url)['"]\s*\)/.test(r)));

const editContextStart = mode.indexOf("panel._openEditContext = function");
const editContextEnd = mode.indexOf("panel._openAddContext", editContextStart);
const editContextBlock = editContextStart >= 0 && editContextEnd > editContextStart
  ? mode.slice(editContextStart, editContextEnd)
  : "";
ok("mobile edit context keeps context name editable",
  editContextBlock.includes("mobileForm")
    && editContextBlock.includes("fName.readOnly = false")
    && !editContextBlock.includes("fName.readOnly = true"));
ok("save context sends edited context name",
  mode.includes("const nextName =")
    && mode.includes("const body = { name: nextName, path: ctxPath")
    && mode.includes("await persistUpdate(editContextTarget, body)"));

ok("desktop hub accepts context rename updates",
  fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8")
    .includes("if (body.name != null)"));
ok("android hub accepts context rename updates",
  fs.readFileSync(path.join(root, "apps", "cam-mobile", "app", "src", "main", "java", "com", "cam", "app", "MobileEmbeddedHub.java"), "utf8")
    .includes("if (body.has(\"name\"))"));

ok("ssh-config import flags missing IdentityFile and guides manual key/password fix",
  mode.includes("(not found)") && mode.includes("Edit the host to pick a valid key file or use password auth"));
ok("hub reports IdentityFile existence for ssh-config import",
  fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8").includes("key_exists:"));
ok("ssh-config parser strips quotes and resolves relative IdentityFile against ~/.ssh",
  fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8").includes("function _normalizeIdentityFile")
    && fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8").includes("path.join(os.homedir(), '.ssh', p)")
    && fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8").includes("path.isAbsolute"));
{
  const nodesSrc = fs.readFileSync(path.join(root, "web", "js", "shared", "nodes-mode.js"), "utf8");
  const hubSrc = fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8");
  const apiSrc = fs.readFileSync(path.join(root, "web", "js", "api.js"), "utf8");
  const deskHtml = fs.readFileSync(path.join(root, "web", "desktop.html"), "utf8");
  const mobSrc = fs.readFileSync(path.join(root, "web", "js", "mobile", "nodes-shell.js"), "utf8");
  ok("per-host Heal panel: hub endpoint + panel + sequential ops",
    hubSrc.includes("sub === '/heal'") && hubSrc.includes("tmux: 'heal --tmux'")
      && hubSrc.includes("monitor: 'heal --monitor'") && hubSrc.includes("restart: 'heal --restart'")
      && apiSrc.includes("healContext")
      && nodesSrc.includes("heal-host-btn") && nodesSrc.includes("renderHealPanel")
      && nodesSrc.includes("api.healContext"));
  ok("secrets are always remembered — no remember checkboxes, remember flag set when secret provided",
    !deskHtml.includes("nodes-add-remember-password")
      && !deskHtml.includes("nodes-add-remember-passphrase")
      && !mobSrc.includes("nodes-add-remember-password")
      && !mobSrc.includes("nodes-add-remember-passphrase")
      && !nodesSrc.includes("requires Remember password")
      && nodesSrc.includes("body.remember_password = true")
      && nodesSrc.includes("hostBody.remember_password = true"));
}

ok("direct API client gives sync/heal/upload a slow-op timeout (was hardcoded 15s)",
  fs.readFileSync(path.join(root, "web", "js", "api.js"), "utf8").includes("const reqTimeoutMs = slowOp ? 120000 : 15000;")
    && fs.readFileSync(path.join(root, "web", "js", "api.js"), "utf8").includes("request timed out after ${Math.round(reqTimeoutMs / 1000)}s")
    // context sync: 45s IDLE timeout driven by backend progress, not an absolute cap
    && fs.readFileSync(path.join(root, "web", "js", "api.js"), "utf8").includes("const isSync = /\\/contexts\\/[^/]+\\/sync(\\/|$|\\?)/.test(path)")
    && fs.readFileSync(path.join(root, "web", "js", "api.js"), "utf8").includes("const SYNC_IDLE_MS = 45000;")
    && fs.readFileSync(path.join(root, "web", "js", "api.js"), "utf8").includes("_syncIdleWatchers")
    && fs.readFileSync(path.join(root, "web", "js", "api.js"), "utf8").includes("syncHeartbeat()")
    // the poller feeds the heartbeat only on real backend progress
    && fs.readFileSync(path.join(root, "web", "js", "shared", "nodes-mode.js"), "utf8").includes("api.syncHeartbeat()"));

// Auth select vs auth section mismatch: reopening Add Host after a
// cancelled Edit left the select on the previous host's method while
// the visible section was stale (mode-off toggles un-hide ALL auth
// sections). openManage must re-apply sections on every open, and the
// add toggle must reset the select to the HTML default (draft restore
// on mobile still wins afterwards).
ok("openManage always re-syncs auth sections; Add Host resets select to key (draft still wins)",
  fs.readFileSync(path.join(root, "web", "js", "shared", "nodes-mode.js"), "utf8").includes("if (isAddHostMode()) restoreDraft();\n    // Always re-sync auth section visibility")
    && fs.readFileSync(path.join(root, "web", "js", "shared", "nodes-mode.js"), "utf8").includes("if (fAuth) fAuth.value = 'key';\n    openManage('manual');"));

// Sync Host is one SSH connection to the host: the live progress
// (connecting/uploading %) lives on the host header (hostSync map),
// while final results are mirrored to per-context rows.
ok("sync live status is node-level (hostSync on host header), results mirrored per context",
  mode.includes("const hostSync = new Map()")
    && mode.includes("syncOneContext(primary, { hostKey: node.key })")
    && mode.includes("hostSync.set(hostKey, entry)")
    && mode.includes("hostSync.get(node.key)"));

// The dedicated extension page's Back header must never match the
// ext-viewing hiding selector (a shared .settings-header class once hid
// the Back button itself — the "page hang" bug).
ok("extensions dedicated page keeps its Back header (no CSS self-hiding)",
  fs.readFileSync(path.join(root, "web", "css", "desktop.css"), "utf8").includes('#mode-extensions.ext-viewing .ext-page-header')
    && !fs.readFileSync(path.join(root, "web", "css", "desktop.css"), "utf8").includes('#mode-extensions.ext-viewing .settings-header')
    && fs.readFileSync(path.join(root, "web", "desktop.html"), "utf8").includes('class="settings-header ext-view-header"'));

// ProxyJump: the form exposes a Jump host dropdown on both form
// variants, submits machine.jump ('' = direct), inherits it into new
// contexts, and badges tunneled hosts.
ok("ProxyJump: Jump host dropdown on both forms, submit + inherit + via badge",
  fs.readFileSync(path.join(root, "web", "desktop.html"), "utf8").includes('id="nodes-add-jump"')
    && fs.readFileSync(path.join(root, "web", "js", "mobile", "nodes-shell.js"), "utf8").includes('id="nodes-add-jump"')
    && mode.includes("refreshJumpOptions")
    && mode.includes("hostBody.jump = fJump.value || ''")
    && mode.includes("if (m.jump) body.jump = m.jump")
    && mode.includes("ProxyJump via ${esc(primaryM.jump)}"));

// Nodes Export: ssh_config text built from contexts (alias from node
// name, ProxyJump carried), saved via the Electron dialog; Import can
// Browse an explicit config file (hub ?path= passthrough); imported
// ProxyJump maps to machine.jump only when it matches a known node.
ok("Nodes export (ssh_config) + import from a chosen file + ProxyJump import mapping",
  mode.includes("function buildNodesSshConfig")
    && mode.includes("if (m.jump) lines.push(`  ProxyJump ${m.jump}`);")
    && mode.includes("files.saveText({ title: 'Export nodes as ssh config'")
    && mode.includes("api.sshConfigHosts(importConfigPath || undefined)")
    && mode.includes("if (known) body.jump = jumpKey;")
    && fs.readFileSync(path.join(root, "web", "desktop.html"), "utf8").includes('id="nodes-export-btn"')
    && fs.readFileSync(path.join(root, "apps", "cam-desktop", "electron", "embedded-hub.cjs"), "utf8").includes("sshConfigHosts(explicit || null)"));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
