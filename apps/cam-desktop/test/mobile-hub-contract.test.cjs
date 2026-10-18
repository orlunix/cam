"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");
const crypto = require("crypto");
const HUB = require(path.join(__dirname, "..", "electron", "embedded-hub.cjs"));

let pass = 0;
let fail = 0;
function ok(name, condition, detail = "") {
  if (condition) pass++;
  else {
    fail++;
    console.error("FAIL " + name + (detail ? " — " + detail : ""));
  }
}

let base = "";
let token = "";
function request(method, routePath, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(routePath, base);
    const req = http.request({
      method,
      hostname: u.hostname,
      port: u.port,
      path: u.pathname + u.search,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    }, (res) => {
      let raw = "";
      res.on("data", (chunk) => { raw += chunk; });
      res.on("end", () => {
        let data;
        try { data = raw ? JSON.parse(raw) : null; } catch { data = raw; }
        resolve({ status: res.statusCode, data });
      });
    });
    req.on("error", reject);
    if (body != null) req.write(JSON.stringify(body));
    req.end();
  });
}

function makeCredentialStore() {
  const map = new Map();
  return {
    available: () => true,
    put(ref, kind, secret) { map.set(ref, { kind, secret }); return { ok: true, ref }; },
    get(ref) { const ent = map.get(ref); return ent ? ent.secret : null; },
    removeForContext(id) { for (const key of [...map.keys()]) if (key.startsWith(id + ":")) map.delete(key); },
    removeWithPrefix(prefix) { for (const key of [...map.keys()]) if (key.startsWith(prefix)) map.delete(key); },
    remove(ref) { map.delete(ref); },
  };
}

async function main() {
  const root = path.join(__dirname, "..", "..", "..");
  const contract = JSON.parse(fs.readFileSync(path.join(
    root, "apps", "cam-mobile", "test", "hub-contract", "contract.json"), "utf8"));
  const dataDir = path.join(process.env.TMPDIR || "/tmp", "cam-desktop-contract-" + crypto.randomBytes(4).toString("hex"));
  HUB.configure({
    credentialStore: makeCredentialStore(),
    sshTransport: {
      execRemote: async () => ({ ok: false, error: "not_expected", detail: "contract must not reach SSH" }),
      closeAll() {},
    },
  });
  const started = await HUB.start({ dataDir, apiToken: "contract-token" });
  ok("desktop hub starts for shared contract", !!started.ok, JSON.stringify(started));
  base = started.apiUrl;
  token = started.apiToken;

  try {
    for (const route of contract.routes) {
      const body = route.body === "$context" ? contract.context : route.body;
      const res = await request(route.method, route.path, body);
      ok(`${route.name} status`, res.status === route.status, `got ${res.status}, want ${route.status}: ${JSON.stringify(res.data)}`);
      const data = res.data;
      ok(`${route.name} JSON object`, data && typeof data === "object" && !Array.isArray(data), JSON.stringify(data));
      for (const key of route.required || []) ok(`${route.name} has ${key}`, Object.prototype.hasOwnProperty.call(data || {}, key), JSON.stringify(data));
      for (const key of route.arrayKeys || []) ok(`${route.name} ${key} is array`, Array.isArray(data && data[key]));
      for (const [key, expected] of Object.entries(route.fields || {})) {
        const actual = key.split(".").reduce((value, part) => value && value[part], data);
        ok(`${route.name} ${key} matches`, actual === expected, `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);
      }
      if (route.error) ok(`${route.name} error matches`, data && data.error === route.error, JSON.stringify(data));
      pass++;
      console.log("  ok   — " + route.name);
    }
  } finally {
    try { await HUB.stop(); } catch {}
    fs.rmSync(dataDir, { recursive: true, force: true });
  }

  console.log(`mobile-hub-contract: ${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
}

main().catch((e) => {
  console.error(e);
  try { HUB.stop().catch(() => {}); } catch {}
  process.exitCode = 1;
});
