"use strict";

const fs = require("fs");
const path = require("path");

function importSource(file) {
  const src = fs.readFileSync(file, "utf8");
  return import("data:text/javascript;base64," + Buffer.from(src).toString("base64"));
}

let pass = 0;
let fail = 0;
function ok(name, condition, detail = "") {
  if (condition) pass++;
  else {
    fail++;
    console.error("FAIL " + name + (detail ? " — " + detail : ""));
  }
}

class StorageMock {
  constructor() { this.map = new Map(); }
  get length() { return this.map.size; }
  key(i) { return [...this.map.keys()][i] || null; }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setItem(k, v) { this.map.set(k, String(v)); }
  removeItem(k) { this.map.delete(k); }
}

class WebSocketMock {
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closeCalls = 0;
    this.sent = [];
  }
  open() { this.readyState = 1; if (this.onopen) this.onopen(); }
  send(data) { this.sent.push(data); }
  close() { this.closeCalls++; this.readyState = 3; if (this.onclose) this.onclose(); }
}

async function main() {
  const root = path.join(__dirname, "..", "..", "..");
  global.localStorage = new StorageMock();
  global.WebSocket = WebSocketMock;
  global.window = {
    dispatchEvent() {},
    CamBridge: {
      directHub_start() {},
      directHub_request(...args) { window.lastNativeRequest = args; },
    },
  };
  global.CustomEvent = class { constructor(type) { this.type = type; } };

  const { CamApi } = await importSource(path.join(root, "web", "js", "api.js"));

  localStorage.setItem("cam_cache:/api/agents", JSON.stringify({ data: { agents: [] }, ts: Date.now() }));
  localStorage.setItem("cam_cache:/api/contexts", JSON.stringify({ data: { contexts: [] }, ts: Date.now() }));
  const cachedApi = new CamApi();
  cachedApi.mode = "direct";
  cachedApi._directRequest = async () => ({ ok: true });
  await cachedApi.request("POST", "/api/agents", {});
  ok("successful mutations invalidate mobile cache", localStorage.length === 0);

  const relay = new CamApi();
  relay.configure({ relayUrl: "ws://relay", relayToken: "t" });
  relay.mode = "relay";
  const firstOpen = relay._connectRelay();
  const first = relay._relayConnecting;
  first.open();
  await firstOpen;
  const secondOpen = relay._connectRelay();
  const second = relay._relayConnecting;
  second.open();
  await secondOpen;
  first.onclose();
  ok("stale Relay close cannot clear the healthy replacement socket", relay.ws === second);

  const mobile = await importSource(path.join(root, "web", "js", "mobile", "mobile-bridge.js"));
  mobile.installMobileCamBridgeShim();
  const controller = new AbortController();
  const pending = window.__camDirectHub.request("GET", "/api/agents", null, "t", controller.signal);
  controller.abort();
  let abortName = "";
  try { await pending; } catch (e) { abortName = e.name; }
  ok("native Hub request abort rejects with AbortError", abortName === "AbortError", abortName);
  ok("native Hub abort removes pending callback", Object.keys(window.__camBridgeShared.dhPending).length === 0);
  const [id] = window.lastNativeRequest;
  window.__camDirectHubCb(id, true, JSON.stringify({ ok: true, data: { agents: [] } }));
  ok("late native callback after abort is ignored", Object.keys(window.__camBridgeShared.dhPending).length === 0);
  relay.disconnect();

  console.log(`api-stability: ${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
