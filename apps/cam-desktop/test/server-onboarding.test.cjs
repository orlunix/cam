/**
 * Desktop CAM Server onboarding tests (no SSH, no Electron).
 *
 * The provisioning script itself is cam-tui's server_setup.py; these tests
 * only cover Desktop's transport adapter and manager wiring.
 *
 * Run:  node apps/cam-desktop/test/server-onboarding.test.cjs
 */

"use strict";

const assert = require("assert");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const {
  ForwardingManager,
  desktopClientId,
  ensureNode,
  loadBundle,
  parseLastJson,
  setupCommand,
} = require("../electron/server-onboarding.cjs");

let pass = 0;
let fail = 0;
const failures = [];
function ok(name, condition, detail) {
  if (condition) pass++;
  else {
    fail++;
    failures.push(name + (detail ? ` — ${detail}` : ""));
    console.error(`FAIL ${name}${detail ? " — " + detail : ""}`);
  }
}

function info(overrides = {}) {
  return {
    node_id: "node-b",
    token: "T".repeat(64),
    binary: "/home/u/.local/bin/cam-server",
    socket: "/tmp/cam-server-1000-abc/runtime/server.sock",
    deployment: "current",
    ...overrides,
  };
}

async function main() {
  ok("parse: last JSON line wins", JSON.stringify(parseLastJson("x\n{\"a\":1}\n{\"b\":2}\n")) === "{\"b\":2}");
  ok("client id: sanitized", desktopClientId("DESKTOP-DNAV2B7") === "desktop-desktop-dnav2b7");

  const quoted = setupCommand("print(1)", { x: "a'b" });
  ok("command: safe quote", quoted.includes("'\\''"), quoted);
  ok("command: base64 script", quoted.includes(Buffer.from("print(1)").toString("base64")));

  const probe = await ensureNode({
    node: { context_name: "pdx046", opts: { host: "h", user: "u" } },
    clientId: "desktop-test",
    script: "print(1)",
    execRemote: async (opts) => {
      ok("probe: carries client id", opts.command.includes("desktop-test"));
      ok("probe: no stdin", opts.stdin == null);
      return { ok: true, stdout: `noise\n${JSON.stringify(info())}\n` };
    },
  });
  ok("probe: succeeds", probe.ok === true, probe.detail);
  ok("probe: node id", probe.node_id === "node-b");
  ok("probe: token", probe.token === "T".repeat(64));

  const noBundle = await ensureNode({
    node: { context_name: "old", opts: { host: "h", user: "u" } },
    clientId: "desktop-test",
    script: "print(1)",
    execRemote: async () => ({ ok: true, stdout: JSON.stringify({ need_bundle: true, target: "x86_64-unknown-linux-musl" }) }),
  });
  ok("probe: missing bundle is explicit", noBundle.error === "server_update_required", noBundle.error);

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cam-onboard-"));
  try {
    const data = Buffer.from("fake-release");
    const bundlePath = path.join(dir, "cam-server.bundle");
    fs.writeFileSync(bundlePath, data);
    fs.writeFileSync(`${bundlePath}.json`, JSON.stringify({
      version: "0.1.3",
      target: "x86_64-unknown-linux-musl",
      size: data.length,
      sha256: crypto.createHash("sha256").update(data).digest("hex"),
    }));
    const bundle = loadBundle(bundlePath);
    ok("bundle: checksum verified", !!bundle && bundle.data.equals(data));

    let calls = 0;
    const installed = await ensureNode({
      node: { context_name: "new", opts: { host: "h", user: "u" } },
      clientId: "desktop-test",
      script: "print(1)",
      bundle,
      execRemote: async (opts) => {
        calls++;
        if (calls === 1) {
          return { ok: true, stdout: JSON.stringify({ need_bundle: true, target: bundle.manifest.target }) };
        }
        ok("install: bundle on stdin", Buffer.isBuffer(opts.stdin) && opts.stdin.equals(data));
        ok("install: manifest in request", opts.command.includes(bundle.manifest.sha256));
        return { ok: true, stdout: JSON.stringify(info({ deployment: "started" })) };
      },
    });
    ok("install: succeeds", installed.ok === true, installed.detail);
    ok("install: deployment", installed.deployment === "started");

    const bad = await ensureNode({
      node: { context_name: "bad", opts: { host: "h", user: "u" } },
      clientId: "desktop-test",
      script: "print(1)",
      execRemote: async () => ({ ok: true, stdout: JSON.stringify(info({ node_id: "bad id" })) }),
    });
    ok("probe: rejects invalid node id", bad.ok === false && bad.error === "setup_failed");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }

  const added = [];
  const removed = [];
  const stored = [];
  const manager = new ForwardingManager({
    forwarder: {
      upsertEndpoint(endpoint) { added.push(endpoint); },
      removeEndpoint(node) { removed.push(node); },
      startWithEndpoints() { return { dynamic: true, enabled: true }; },
    },
    getNodes: () => [{
      context_id: "ctx-1",
      context_name: "pdx046",
      context_key: "u@h:22",
      opts: { host: "h", user: "u" },
    }],
    credentialStore: {
      put(ref, kind, secret) { stored.push({ ref, kind, secret }); return { ok: true }; },
    },
    clientId: "desktop-test",
    script: "print(1)",
    execRemote: async () => ({ ok: true, stdout: JSON.stringify(info()) }),
    openExecChannel: async () => ({ ok: false, error: "test" }),
    log: () => {},
  });
  manager.stopped = false;
  await manager.sync();
  ok("manager: endpoint added", added.length === 1);
  ok("manager: endpoint id", added[0] && added[0].node_id === "node-b");
  ok("manager: token stored", stored.length === 1 && stored[0].ref === "forward:node-b"
    && stored[0].kind === "forward-token" && stored[0].secret === "T".repeat(64));
  ok("manager: status ready", manager.status().nodes[0].state === "ready");

  manager.getNodes = () => [];
  await manager.sync();
  ok("manager: removed when node disappears", removed.includes("node-b"));

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail) {
    for (const failure of failures) console.error(`  ${failure}`);
    process.exit(1);
  }
  process.exit(0);
}

main().catch((err) => {
  console.error("FATAL", err);
  process.exit(1);
});
