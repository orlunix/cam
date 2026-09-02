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
const hub = fs.readFileSync(path.join(root, "apps", "cam-mobile", "app", "src", "main", "java", "com", "cam", "app", "MobileEmbeddedHub.java"), "utf8");
const ssh = fs.readFileSync(path.join(root, "apps", "cam-mobile", "app", "src", "main", "java", "com", "cam", "app", "MobileSshExec.java"), "utf8");
const build = fs.readFileSync(path.join(root, "apps", "cam-mobile", "build.sh"), "utf8");
const nodes = fs.readFileSync(path.join(root, "web", "js", "shared", "nodes-mode.js"), "utf8");

ok("Sync Host returns timing metadata", hub.includes('put("sync", syncTiming('));
ok("Sync Host timing includes lock/connect/check/list/import/total", ["lockWaitMs", "connectMs", "checkMs", "listMs", "importMs", "totalMs"].every(k => hub.includes(k)));
ok("Sync Host skips upload when remote camc exists", hub.includes('uploadDecision = "skipped_present"'));
ok("Sync Host uploads bundled camc only after missing check", hub.includes("isCamcMissing(check)") && hub.includes("deployBundledCamcForSync(sshAuth, ctxId)"));
ok("Sync Host does not use long-held withSession flow", !hub.includes("withSession(sshAuth") && !ssh.includes("SessionWork<T>"));

ok("SSH sequence records lock/connect/command timings", ssh.includes("public long lockWaitMs") && ssh.includes("public long connectMs") && ssh.includes("public long[] commandMs"));
ok("Android build bundles camc asset for missing-host deploy", build.includes("assets/camc") && build.includes("../dist/camc"));
ok("Nodes UI renders Sync Host timing summary", nodes.includes("formatSyncTiming") && nodes.includes("sync.totalMs"));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
