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
  nodeName < host && host < user && user < port && port < auth && auth < contextName && contextName < remotePath && remotePath < envSetup,
  `order=${[nodeName, host, user, port, auth, contextName, remotePath, envSetup].join(",")}`);
ok("mobile Nodes context name remains required", /id="nodes-add-name"[^>]*\srequired\b/.test(shell));
ok("mobile Nodes remote path remains required", /id="nodes-add-path"[^>]*\srequired\b/.test(shell));
ok("mobile Nodes context name placeholder explains default", /id="nodes-add-name"[^>]*placeholder="defaults to node name"/.test(shell));
ok("mobile Nodes remote path placeholder explains home-dir default", /id="nodes-add-path"[^>]*placeholder="\/home\/&lt;user&gt;"/.test(shell));

ok("mobile nodes has helper for default context name", mode.includes("function mobileDefaultContextName") && mode.includes("function ensureMobileContextNameDefault"));
ok("mobile nodes has helper for default remote path", mode.includes("function mobileDefaultRemotePath"));
ok("mobile Add Host defaults context name from node name", mode.includes("ensureMobileContextNameDefault(host)") && mode.includes("mobileDefaultContextName({ explicitName: fName.value.trim(), nodeName: fNodeName ? fNodeName.value.trim() : '', host })"));
ok("mobile Add Host defaults path from username", mode.includes("ensureMobileRemotePathDefault(user)") && mode.includes("mobileDefaultRemotePath(fPath.value, user)"));
ok("desktop Add Host still requires explicit context name/path", mode.includes("if (!mobileForm && (!name || !ctxPath))"));
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
  fs.readFileSync(path.join(root, "android", "app", "src", "main", "java", "com", "cam", "app", "MobileEmbeddedHub.java"), "utf8")
    .includes("if (body.has(\"name\"))"));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
