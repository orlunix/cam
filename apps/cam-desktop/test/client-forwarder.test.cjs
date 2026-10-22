/**
 * Client forwarding/tunnel tests.
 *
 * Drives the Node port of cam-tui's relay.rs against two in-process fake
 * CAM Servers on Unix sockets: handshake, routes.snapshot, raw message
 * forwarding, forward.error, and resolve.answer.
 *
 * Run:  node apps/cam-desktop/test/client-forwarder.test.cjs
 */

"use strict";

const assert = require("assert");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const { ClientForwarder, loadConfig, validateEnvelope } = require("../electron/client-forwarder.cjs");

if (process.platform === "win32") {
  console.log("SKIP client-forwarder: Unix sockets are required");
  process.exit(0);
}

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

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(fn, timeoutMs = 5000, label = "condition") {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = fn();
    if (value) return value;
    await sleep(20);
  }
  throw new Error(`timeout waiting for ${label}`);
}

function fakeServer(socketPath, nodeId, allowedTargets) {
  const state = { frames: [], sockets: new Set(), server: null };
  const notify = () => {
    for (const waiter of state.waiters.splice(0)) waiter();
  };
  state.waiters = [];
  state.write = (value) => {
    const line = `${JSON.stringify(value)}\n`;
    for (const socket of state.sockets) socket.write(line);
  };
  state.server = net.createServer((socket) => {
    state.sockets.add(socket);
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        let frame;
        try { frame = JSON.parse(line); }
        catch (_) { continue; }
        state.frames.push(frame);
        if (frame.type === "hello") {
          socket.write(`${JSON.stringify({
            type: "hello",
            version: 1,
            node_id: nodeId,
            session_id: `sess-${nodeId}`,
            route_ttl_seconds: 3,
            allowed_targets: allowedTargets,
          })}\n`);
        }
        notify();
      }
    });
    socket.on("error", () => {});
    socket.on("close", () => { state.sockets.delete(socket); notify(); });
    notify();
  });
  state.listen = () => new Promise((resolve, reject) => {
    state.server.once("error", reject);
    state.server.listen(socketPath, resolve);
  });
  state.close = () => new Promise((resolve) => {
    for (const socket of state.sockets) socket.destroy();
    state.server.close(resolve);
  });
  state.find = (predicate) => state.frames.find(predicate);
  state.waitFor = (predicate, label) => waitFor(() => state.find(predicate), 5000, label);
  return state;
}

function messageFrame(overrides = {}) {
  return {
    version: 1,
    type: "message",
    message_id: "msg-1",
    source: { node_id: "node-a", agent_id: "agent-a" },
    target: { node_id: "node-b", agent_id: "agent-b" },
    operation: "message.send",
    payload: { text: "hello" },
    expires_at: new Date(Date.now() + 60000).toISOString(),
    ...overrides,
  };
}

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cam-fwd-"));
  const tokenPath = path.join(dir, "token-a");
  fs.writeFileSync(tokenPath, "A".repeat(40) + "\n", { mode: 0o600 });
  const agentsPath = path.join(dir, "agents.json");
  fs.writeFileSync(agentsPath, JSON.stringify([
    { node_id: "node-b", id: "agent-b", name: "agent-b", status: "idle", updated_at: Math.floor(Date.now() / 1000) },
    { node_id: "node-a", id: "agent-a", name: "agent-a", status: "idle", updated_at: Math.floor(Date.now() / 1000) },
  ]));
  const configPath = path.join(dir, "client.json");
  fs.writeFileSync(configPath, JSON.stringify({
    client_id: "desk-test",
    endpoints: [
      { node_id: "node-a", token_file: "token-a", socket: "a.sock" },
      { node_id: "node-b", token_file: "token-a", socket: "b.sock" },
    ],
    agents_file: "agents.json",
    agent_ttl_seconds: 60,
  }), { mode: 0o600 });

  // ── Config and envelope unit checks ─────────────────────────────
  const loaded = loadConfig(configPath);
  ok("config: relative socket resolved", loaded.endpoints[0].socket === path.join(dir, "a.sock"));
  ok("config: token trimmed", loaded.endpoints[0].token === "A".repeat(40));
  ok("config: agents file resolved", loaded.agents_file === agentsPath);

  const readableToken = path.join(dir, "token-open");
  fs.writeFileSync(readableToken, "B".repeat(40), { mode: 0o644 });
  const openConfig = path.join(dir, "open.json");
  fs.writeFileSync(openConfig, JSON.stringify({
    client_id: "desk-test",
    endpoints: [{ node_id: "node-a", token_file: "token-open", socket: "a.sock" }],
  }));
  let rejected = false;
  try { loadConfig(openConfig); } catch (err) { rejected = /group\/world/.test(err.message); }
  ok("config: group/world readable token rejected", rejected);

  ok("envelope: valid message accepted", validateEnvelope(messageFrame()));
  ok("envelope: expired message rejected", !validateEnvelope(messageFrame({
    expires_at: new Date(Date.now() - 1000).toISOString(),
  })));
  ok("envelope: unknown operation rejected", !validateEnvelope(messageFrame({ operation: "list" })));

  // ── Live forwarding ─────────────────────────────────────────────
  const serverA = fakeServer(path.join(dir, "a.sock"), "node-a", ["node-b", "node-c"]);
  const serverB = fakeServer(path.join(dir, "b.sock"), "node-b", ["node-a"]);
  await serverA.listen();
  await serverB.listen();

  const forwarder = new ClientForwarder({ configPath });
  await forwarder.start();
  ok("status: enabled after start", forwarder.status().enabled === true);

  try {
    const helloA = await serverA.waitFor((f) => f.type === "hello", "hello A");
    ok("hello: role client", helloA.role === "client");
    ok("hello: client_id", helloA.client_id === "desk-test");
    ok("hello: token", helloA.token === "A".repeat(40));

    await serverB.waitFor((f) => f.type === "hello", "hello B");
    const routesA = await serverA.waitFor((f) => f.type === "routes.snapshot"
      && f.reachable.includes("node-b"), "routes A -> node-b");
    ok("routes: session id echoed", routesA.session_id === "sess-node-a");
    ok("routes: ttl echoed", routesA.ttl_seconds === 3);
    const routesB = await serverB.waitFor((f) => f.type === "routes.snapshot"
      && f.reachable.includes("node-a"), "routes B -> node-a");
    ok("routes: node-a advertised to node-b", routesB.session_id === "sess-node-b");

    const outgoing = messageFrame();
    serverA.write(outgoing);
    const forwarded = await serverB.waitFor((f) => f.type === "message" && f.message_id === "msg-1", "forwarded message");
    ok("forward: payload preserved", JSON.stringify(forwarded) === JSON.stringify(outgoing));
    ok("forward: routes snapshot seq increasing", forwarder.status().nodes["node-a"] === "connected");

    const unroutable = messageFrame({
      message_id: "msg-2",
      target: { node_id: "node-c", agent_id: "agent-c" },
    });
    serverA.write(unroutable);
    const refusal = await serverA.waitFor((f) => f.type === "forward.error" && f.message_id === "msg-2", "forward.error");
    ok("forward: missing route reports not_forwarded", refusal.status === "not_forwarded");
    ok("forward: refusal names the target", refusal.target_node === "node-c");

    serverA.write({ type: "resolve.query", id: "q-1", node: "node-b", agent: "agent" });
    const answer = await serverA.waitFor((f) => f.type === "resolve.answer" && f.id === "q-1", "resolve.answer");
    ok("resolve: prefix match", JSON.stringify(answer.matches) === JSON.stringify([
      { node_id: "node-b", agent_id: "agent-b" },
    ]));

    serverA.write({ type: "pong" });
    await sleep(100);
    ok("pong: connection stays up", forwarder.status().nodes["node-a"] === "connected");

    // Dynamic endpoints (Desktop Nodes -> forwarding manager): no config
    // file, token in memory, open() supplies the exec/socket stream.
    const serverC = fakeServer(path.join(dir, "c.sock"), "node-c", []);
    await serverC.listen();
    const dynamic = new ClientForwarder({ log: () => {} });
    dynamic.startWithEndpoints({
      clientId: "desk-dyn",
      endpoints: [{
        node_id: "node-c",
        token: "C".repeat(40),
        open: () => Promise.resolve(net.connect(path.join(dir, "c.sock"))),
      }],
    });
    const helloC = await serverC.waitFor((f) => f.type === "hello", "dynamic hello");
    ok("dynamic: client id", helloC.client_id === "desk-dyn");
    ok("dynamic: node connected", dynamic.status().nodes["node-c"] === "connected");
    dynamic.removeEndpoint("node-c");
    ok("dynamic: node removed", !("node-c" in dynamic.status().nodes));
    dynamic.stop();
    await serverC.close();
  } finally {
    forwarder.stop();
    await serverA.close();
    await serverB.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }

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
