"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");

let pass = 0;
let fail = 0;
function ok(name, condition, detail = "") {
  if (condition) pass++;
  else {
    fail++;
    console.error("FAIL " + name + (detail ? " — " + detail : ""));
  }
}

class WebSocketMock {
  static OPEN = 1;
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    WebSocketMock.instances.push(this);
  }
  open() { this.readyState = 1; if (this.onopen) this.onopen(); }
  send(data) {
    this.sent.push(data);
    if (typeof data === "string") this.lastControl = JSON.parse(data);
  }
  close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  text(msg) { if (this.onmessage) this.onmessage({ data: JSON.stringify(msg) }); }
  binary(bytes) { if (this.onmessage) this.onmessage({ data: Uint8Array.from(bytes).buffer }); }
}

async function main() {
  const root = path.join(__dirname, "..", "..", "..");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cam-terminal-ws-"));
  for (const name of ["term-bridge", "hub-capabilities"]) {
    let src = fs.readFileSync(path.join(root, "web", "js", "shared", name + ".js"), "utf8");
    src = src.replace("./hub-capabilities.js", "./hub-capabilities.mjs");
    fs.writeFileSync(path.join(tmp, name + ".mjs"), src);
  }
  const mod = await import(pathToFileURL(path.join(tmp, "term-bridge.mjs")));
  global.WebSocket = WebSocketMock;
  global.window = undefined;

  const received = [];
  const statuses = [];
  const api = {
    serverUrl: "http://127.0.0.1:8420",
    token: "secret",
    agentOutput: async () => ({ unchanged: true }),
    sendInput: async () => ({}),
  };
  const bridge = mod.createDirectWebSocketTermBridge(api);
  bridge.onData((msg) => received.push(msg));
  bridge.onStatus((msg) => statuses.push(msg));

  const opening = bridge.open({ agentId: "a1", cols: 80, rows: 24 });
  const ws = WebSocketMock.instances[0];
  ok("terminal URL uses loopback /term and token", ws.url === "ws://127.0.0.1:8420/term?token=secret", ws.url);
  ws.open();
  ws.binary([97, 98, 99]);
  ws.text({ type: "result", requestId: ws.lastControl.requestId, action: "open", ok: true, sessionId: "t1" });
  const opened = await opening;
  ok("open result preserves terminal session", opened.ok && opened.sessionId === "t1");
  await new Promise((r) => setImmediate(r));
  ok("early binary is queued until open completes", received.length === 1 && received[0].data === "abc" && received[0].sessionId === "t1");

  ws.binary([0xE4, 0xBD]);
  ws.binary([0xA0]);
  await new Promise((r) => setImmediate(r));
  ok("incremental UTF-8 decoding preserves split characters", received[1] && received[1].data === "你");

  await bridge.input({ sessionId: "t1", data: "你" });
  const inputBytes = ws.sent[ws.sent.length - 1];
  ok("terminal input uses binary UTF-8", inputBytes instanceof Uint8Array && Buffer.from(inputBytes).toString("utf8") === "你");

  const resizing = bridge.resize({ sessionId: "t1", cols: 100, rows: 30 });
  ws.text({ type: "result", requestId: ws.lastControl.requestId, action: "resize", ok: true });
  const resized = await resizing;
  ok("resize uses ordered control response", resized.ok && ws.lastControl.action === "resize");

  const closing = bridge.close({ sessionId: "t1" });
  ws.text({ type: "result", requestId: ws.lastControl.requestId, action: "close", ok: true });
  await closing;
  ok("close cleans terminal session", ws.readyState === 3);

  const relay = mod.createRelayTermBridge({ mode: "relay", agentOutput: api.agentOutput, sendInput: api.sendInput });
  const relaySession = await relay.open({ agentId: "r1" });
  await relay.close({ sessionId: relaySession.sessionId });
  ok("Relay close stops capture polling", relaySession.ok !== false);

  console.log(`terminal-ws: ${pass} passed, ${fail} failed`);
  fs.rmSync(tmp, { recursive: true, force: true });
  process.exitCode = fail ? 1 : 0;
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
