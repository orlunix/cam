"use strict";

/**
 * CAM Client forwarding / tunnel layer.
 *
 * This is a direct port of the reference Client in
 * components/cam-router/crates/cam-server/src/relay.rs (the same code used by
 * cam-clientd): hello/auth, routes.snapshot, raw message forwarding,
 * forward.error, resolve.query, and per-endpoint reconnect/backoff.
 *
 * A Client is transport state only: it never keeps a business reply table and
 * never turns a message into a CLI command. Desktop currently runs this in
 * Electron main; it can later be replaced by the shared Rust client daemon
 * without changing the wire contract.
 */

const fs = require("fs");
const net = require("net");
const path = require("path");
const { spawn } = require("child_process");

const FRAME_LIMIT = 4 * 1024 * 1024;
const QUEUE_BUDGET = 16 * 1024 * 1024;
const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 8000;
const HELLO_TIMEOUT_MS = 5000;
const AGENT_OPERATIONS = new Set([
  "message.send",
  "agent.send",
  "agent.capture",
  "agent.run",
  "agent.stop",
  "agent.remove",
  "agent.kill",
  "agent.reboot",
  "agent.key",
  "agent.status",
]);

function validId(value) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 128
    && /^[A-Za-z0-9_.-]+$/.test(value);
}

function validAgentId(value) {
  return validId(value) && !value.includes(".");
}

function encodeFrame(value) {
  const raw = Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
  if (raw.length > FRAME_LIMIT) throw new Error("frame limit exceeded");
  return raw;
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function validateEnvelope(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  if (value.version !== 1 || value.type !== "message" || !validId(value.message_id)) {
    return false;
  }
  for (const field of ["reply_to", "thread_id"]) {
    if (value[field] != null && !validId(value[field])) return false;
  }
  for (const side of ["source", "target"]) {
    const address = value[side];
    if (!address || typeof address !== "object") return false;
    const node = address.node_id;
    if (typeof node !== "string" || !node || node.length > 128
        || /\s/.test(node) || /[\x00-\x1f]/.test(node)) {
      return false;
    }
    if (address.agent_id != null && !validAgentId(address.agent_id)) return false;
  }
  if (!value.payload || typeof value.payload !== "object" || Array.isArray(value.payload)) {
    return false;
  }
  const operation = value.operation;
  if (AGENT_OPERATIONS.has(operation)) {
    if (!validAgentId(value.target.agent_id)) return false;
  } else if (operation === "test.echo") {
    if (value.target.receiver_id !== "test.echo") return false;
  } else if (operation === "operation.result" || operation === "operation.delivery") {
    const receiver = operation === "operation.result" ? "operation.results" : "delivery.status";
    if (value.target.receiver_id !== receiver || typeof value.reply_to !== "string") return false;
  } else {
    return false;
  }
  const expires = Date.parse(String(value.expires_at || ""));
  if (!Number.isFinite(expires)) return false;
  const end = Math.floor(expires / 1000);
  const now = nowSeconds();
  return end > now && end <= now + 90000;
}

/** Line-delimited JSON frames, preserving the original bytes for forwarding. */
class FrameQueue {
  constructor(stream) {
    this.stream = stream;
    this.buffer = Buffer.alloc(0);
    this.frames = [];
    this.waiters = [];
    this.error = null;
    this.onData = (chunk) => this.push(chunk);
    this.onError = (err) => this.fail(err);
    this.onClose = () => this.fail(new Error("connection closed"));
    stream.on("data", this.onData);
    stream.on("error", this.onError);
    stream.on("close", this.onClose);
    stream.on("end", this.onClose);
  }

  push(chunk) {
    if (this.error) return;
    this.buffer = Buffer.concat([this.buffer, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)]);
    let newline;
    while ((newline = this.buffer.indexOf(0x0a)) >= 0) {
      const raw = this.buffer.subarray(0, newline + 1);
      this.buffer = this.buffer.subarray(newline + 1);
      if (raw.length > FRAME_LIMIT) {
        this.fail(new Error("frame limit exceeded"));
        return;
      }
      let value;
      try {
        value = JSON.parse(raw.toString("utf8"));
      } catch (err) {
        this.fail(new Error(`invalid frame: ${err.message}`));
        return;
      }
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        this.fail(new Error("object frame required"));
        return;
      }
      this.deliver({ value, raw });
    }
    if (this.buffer.length > FRAME_LIMIT) this.fail(new Error("frame limit exceeded"));
  }

  deliver(item) {
    const waiter = this.waiters.shift();
    if (waiter) {
      clearTimeout(waiter.timer);
      waiter.resolve(item);
    } else {
      this.frames.push(item);
    }
  }

  fail(err) {
    if (this.error) return;
    this.error = err;
    for (const waiter of this.waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(err);
    }
  }

  next(timeoutMs = 0) {
    if (this.frames.length) return Promise.resolve(this.frames.shift());
    if (this.error) return Promise.reject(this.error);
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject, timer: null };
      if (timeoutMs > 0) {
        waiter.timer = setTimeout(() => {
          const index = this.waiters.indexOf(waiter);
          if (index >= 0) this.waiters.splice(index, 1);
          reject(new Error("frame timeout"));
        }, timeoutMs);
      }
      this.waiters.push(waiter);
    });
  }

  close() {
    try { this.stream.removeListener("data", this.onData); } catch (_) {}
    try { this.stream.removeListener("error", this.onError); } catch (_) {}
    try { this.stream.removeListener("close", this.onClose); } catch (_) {}
    try { this.stream.removeListener("end", this.onClose); } catch (_) {}
    this.fail(new Error("connection closed"));
  }
}

/** Bounded outbound queue; frames keep their original bytes and order. */
class Channel {
  constructor(stream) {
    this.stream = stream;
    this.queue = [];
    this.bytes = 0;
    this.flushing = false;
    this.closed = false;
  }

  send(raw) {
    if (this.closed || !Buffer.isBuffer(raw)) return false;
    if (this.bytes + raw.length > QUEUE_BUDGET) return false;
    this.queue.push(raw);
    this.bytes += raw.length;
    this.flush();
    return true;
  }

  frame(value) {
    try {
      return this.send(encodeFrame(value));
    } catch (_) {
      return false;
    }
  }

  flush() {
    if (this.flushing || this.closed) return;
    this.flushing = true;
    while (this.queue.length) {
      const raw = this.queue.shift();
      this.bytes -= raw.length;
      let ok = false;
      try {
        ok = this.stream.write(raw);
      } catch (_) {
        this.close();
        return;
      }
      if (!ok) {
        this.stream.once("drain", () => {
          this.flushing = false;
          this.flush();
        });
        return;
      }
    }
    this.flushing = false;
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.queue.length = 0;
    this.bytes = 0;
    try { this.stream.destroy(); } catch (_) {}
  }
}

function loadConfig(configPath) {
  const absolute = path.resolve(configPath);
  const config = JSON.parse(fs.readFileSync(absolute, "utf8"));
  if (!validId(config.client_id)) throw new Error("invalid client_id");
  if (!Array.isArray(config.endpoints) || config.endpoints.length > 128) {
    throw new Error("invalid endpoints");
  }
  const ttl = config.agent_ttl_seconds == null ? 60 : Number(config.agent_ttl_seconds);
  if (!Number.isInteger(ttl) || ttl < 1 || ttl > 3600) {
    throw new Error("invalid agent_ttl_seconds");
  }
  const base = path.dirname(absolute);
  const nodes = new Set();
  const endpoints = config.endpoints.map((endpoint) => {
    if (!endpoint || !validId(endpoint.node_id) || nodes.has(endpoint.node_id)) {
      throw new Error("each endpoint needs a unique Node");
    }
    nodes.add(endpoint.node_id);
    const hasSocket = typeof endpoint.socket === "string" && endpoint.socket.length > 0;
    const hasCommand = Array.isArray(endpoint.command) && endpoint.command.length > 0
      && endpoint.command.every((part) => typeof part === "string");
    if (hasSocket === hasCommand) {
      throw new Error("each endpoint needs exactly one socket/command");
    }
    if (!endpoint.token_file) throw new Error("endpoint token_file is required");
    const tokenFile = path.resolve(base, endpoint.token_file);
    const token = fs.readFileSync(tokenFile, "utf8").trim();
    if (token.length < 32) throw new Error("short endpoint credential");
    if (process.platform !== "win32") {
      const mode = fs.statSync(tokenFile).mode & 0o777;
      if ((mode & 0o077) !== 0) {
        throw new Error(`token file ${tokenFile} must not be group/world readable`);
      }
    }
    return {
      node_id: endpoint.node_id,
      socket: hasSocket ? path.resolve(base, endpoint.socket) : null,
      command: hasCommand ? endpoint.command.slice() : null,
      token,
    };
  });
  let agentsFile = null;
  if (config.agents_file) agentsFile = path.resolve(base, config.agents_file);
  const agents = Array.isArray(config.agents) ? config.agents : [];
  return {
    client_id: config.client_id,
    endpoints,
    agents,
    agents_file: agentsFile,
    agent_ttl_seconds: ttl,
  };
}

function validateDynamicEndpoint(endpoint) {
  if (!endpoint || !validId(endpoint.node_id)) {
    throw new Error("dynamic endpoint needs a valid node_id");
  }
  if (typeof endpoint.token !== "string" || endpoint.token.trim().length < 32
      || endpoint.token.length > 1024) {
    throw new Error(`dynamic endpoint ${endpoint.node_id} needs a pairing token`);
  }
  if (typeof endpoint.open !== "function") {
    throw new Error(`dynamic endpoint ${endpoint.node_id} needs an open() transport`);
  }
  return endpoint;
}

class ClientForwarder {
  constructor({ configPath = null, log = null } = {}) {
    this.configPath = configPath;
    this.logFn = log;
    this.config = null;
    this.running = false;
    this.stopped = true;
    this.ready = new Map();        // node_id -> connection
    this.loops = new Map();        // node_id -> promise
    this.state = new Map();        // node_id -> connected|disconnected
    this.endpointGeneration = new Map(); // node_id -> loop generation
    this.dynamic = false;
    this.lastError = null;
    this.startedAt = Date.now();
  }

  log(message) {
    try { if (this.logFn) this.logFn(message); } catch (_) {}
  }

  status() {
    const nodes = {};
    for (const [node, value] of this.state) nodes[node] = value;
    return {
      enabled: this.running,
      dynamic: this.dynamic,
      client_id: this.config ? this.config.client_id : null,
      nodes,
      endpoints: this.config && Array.isArray(this.config.endpoints)
        ? this.config.endpoints.map((e) => e.node_id)
        : [],
      last_error: this.lastError,
      config: this.configPath || null,
    };
  }

  async start() {
    if (this.running) return this.status();
    if (!this.configPath || !fs.existsSync(this.configPath)) {
      this.lastError = "client forwarding config not found";
      this.log(`disabled: ${this.lastError} (${this.configPath || "no path"})`);
      return this.status();
    }
    try {
      this.config = loadConfig(this.configPath);
    } catch (err) {
      this.lastError = `invalid config: ${err.message}`;
      this.log(this.lastError);
      return this.status();
    }
    this.running = true;
    this.stopped = false;
    this.dynamic = false;
    this.lastError = null;
    this.state = new Map(this.config.endpoints.map((e) => [e.node_id, "disconnected"]));
    for (const endpoint of this.config.endpoints) this.spawnLoop(endpoint);
    this.log(`started client=${this.config.client_id} nodes=${[...this.state.keys()].join(",")}`);
    return this.status();
  }

  /** Start with endpoints discovered from Desktop Nodes (no config file).
   *  Each endpoint carries its own token and `open()` transport. */
  startWithEndpoints({ clientId, endpoints = [] } = {}) {
    if (this.running) {
      if (this.dynamic) {
        this.setEndpoints(endpoints);
        return this.status();
      }
      this.log("static client config is active; automatic endpoints skipped");
      return this.status();
    }
    if (!validId(clientId)) {
      this.lastError = "invalid client_id";
      this.log(this.lastError);
      return this.status();
    }
    this.config = {
      client_id: clientId,
      endpoints: [],
      agents: [],
      agents_file: null,
      agent_ttl_seconds: 60,
    };
    this.running = true;
    this.stopped = false;
    this.dynamic = true;
    this.lastError = null;
    this.state = new Map();
    this.endpointGeneration.clear();
    this.setEndpoints(endpoints);
    this.log(`started auto client=${clientId} nodes=${[...this.state.keys()].join(",")}`);
    return this.status();
  }

  setEndpoints(endpoints) {
    const next = new Map();
    for (const endpoint of Array.isArray(endpoints) ? endpoints : []) {
      const checked = validateDynamicEndpoint(endpoint);
      next.set(checked.node_id, checked);
    }
    for (const node of [...this.state.keys()]) {
      if (!next.has(node)) this.removeEndpoint(node);
    }
    for (const endpoint of next.values()) this.upsertEndpoint(endpoint);
  }

  upsertEndpoint(endpoint) {
    const checked = validateDynamicEndpoint(endpoint);
    const node = checked.node_id;
    const existing = this.config && this.config.endpoints.find((e) => e.node_id === node);
    if (existing && existing.token === checked.token && existing.open === checked.open) {
      this.state.set(node, this.state.get(node) || "disconnected");
      return;
    }
    if (existing) this.removeEndpoint(node);
    if (!this.dynamic || this.stopped) return;
    this.config.endpoints = this.config.endpoints.filter((e) => e.node_id !== node);
    this.config.endpoints.push(checked);
    this.endpointGeneration.set(node, (this.endpointGeneration.get(node) || 0) + 1);
    this.state.set(node, "disconnected");
    this.spawnLoop(checked);
    this.broadcastRoutes();
  }

  removeEndpoint(node) {
    if (!node) return;
    this.endpointGeneration.set(node, (this.endpointGeneration.get(node) || 0) + 1);
    const connection = this.ready.get(node);
    if (connection) {
      try { clearInterval(connection.routeTimer); } catch (_) {}
      try { connection.channel.close(); } catch (_) {}
      try { connection.queue.close(); } catch (_) {}
      try { connection.close(); } catch (_) {}
      this.ready.delete(node);
    }
    this.state.delete(node);
    this.loops.delete(node);
    if (this.config && Array.isArray(this.config.endpoints)) {
      this.config.endpoints = this.config.endpoints.filter((e) => e.node_id !== node);
    }
    this.broadcastRoutes();
  }

  stop() {
    this.stopped = true;
    this.running = false;
    for (const connection of [...this.ready.values()]) {
      try { clearInterval(connection.routeTimer); } catch (_) {}
      try { connection.channel.close(); } catch (_) {}
      try { connection.queue.close(); } catch (_) {}
      try { connection.close(); } catch (_) {}
    }
    this.ready.clear();
    for (const node of this.state.keys()) this.state.set(node, "disconnected");
    this.loops.clear();
    this.endpointGeneration.clear();
    this.log("stopped");
  }

  spawnLoop(endpoint) {
    if (this.loops.has(endpoint.node_id)) return;
    const generation = this.endpointGeneration.get(endpoint.node_id) || 0;
    const loop = (async () => {
      let failures = 0;
      while (!this.stopped && (this.endpointGeneration.get(endpoint.node_id) || 0) === generation) {
        const started = Date.now();
        try {
          await this.runEndpoint(endpoint);
        } catch (err) {
          if (!this.stopped) {
            this.lastError = `${endpoint.node_id}: ${err.message}`;
            this.log(`node=${endpoint.node_id} disconnected: ${err.message}`);
          }
        }
        if (this.stopped) break;
        if (Date.now() - started >= 10000) failures = 0;
        failures += 1;
        const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * (2 ** Math.max(0, failures - 1)));
        await sleep(delay);
      }
    })();
    this.loops.set(endpoint.node_id, loop);
  }

  async connect(endpoint) {
    if (typeof endpoint.open === "function") {
      const stream = await endpoint.open();
      if (!stream || typeof stream.on !== "function" || typeof stream.write !== "function") {
        throw new Error("invalid exec channel");
      }
      return {
        read: stream,
        write: stream,
        close: () => {
          try { stream.destroy && stream.destroy(); } catch (_) {}
          try { stream.close && stream.close(); } catch (_) {}
        },
      };
    }
    if (endpoint.socket) {
      const socket = await new Promise((resolve, reject) => {
        const client = net.connect(endpoint.socket);
        const timer = setTimeout(() => {
          try { client.destroy(); } catch (_) {}
          reject(new Error("connect timeout"));
        }, HELLO_TIMEOUT_MS);
        client.once("connect", () => { clearTimeout(timer); resolve(client); });
        client.once("error", (err) => { clearTimeout(timer); reject(err); });
      });
      return {
        read: socket,
        write: socket,
        close: () => { try { socket.destroy(); } catch (_) {} },
      };
    }
    const child = spawn(endpoint.command[0], endpoint.command.slice(1), {
      stdio: ["pipe", "pipe", "ignore"],
    });
    child.on("error", (err) => this.log(`node=${endpoint.node_id} spawn failed: ${err.message}`));
    return {
      read: child.stdout,
      write: child.stdin,
      close: () => { try { child.kill(); } catch (_) {} },
    };
  }

  async runEndpoint(endpoint) {
    let transport = null;
    let queue = null;
    let channel = null;
    let connection = null;
    try {
      transport = await this.connect(endpoint);
      queue = new FrameQueue(transport.read);
      channel = new Channel(transport.write);
      if (!channel.frame({
        type: "hello",
        version: 1,
        role: "client",
        client_id: this.config.client_id,
        token: endpoint.token,
      })) {
        throw new Error("hello write failed");
      }
      const first = await queue.next(HELLO_TIMEOUT_MS);
      const hello = first.value;
      if (hello.type !== "hello" || hello.version !== 1
          || hello.node_id !== endpoint.node_id || !validId(hello.session_id)) {
        throw new Error("Server identity or version mismatch");
      }
      const ttl = Number(hello.route_ttl_seconds);
      if (!Number.isInteger(ttl) || ttl < 3 || ttl > 120) {
        throw new Error("invalid route TTL");
      }
      if (!Array.isArray(hello.allowed_targets)
          || !hello.allowed_targets.every((node) => typeof node === "string")) {
        throw new Error("missing Server route grant");
      }
      const allowed = new Set(hello.allowed_targets.filter(validId));
      connection = {
        endpoint,
        node_id: endpoint.node_id,
        generation: hello.session_id,
        allowed,
        channel,
        queue,
        ttl,
        seq: 0,
        routeTimer: null,
        close: transport.close,
      };
      this.ready.set(endpoint.node_id, connection);
      this.state.set(endpoint.node_id, "connected");
      this.lastError = null;
      connection.sendRoutes = () => this.sendRoutes(connection);
      connection.sendRoutes();
      connection.routeTimer = setInterval(
        connection.sendRoutes,
        Math.max(1, Math.floor(ttl / 3)) * 1000,
      );
      this.broadcastRoutes();
      this.log(`connected node=${endpoint.node_id}`);

      while (!this.stopped) {
        const { value: frame, raw } = await queue.next(0);
        if (frame.type === "message") {
          if (!validateEnvelope(frame)) throw new Error("invalid message envelope");
          if (frame.source.node_id !== endpoint.node_id) {
            throw new Error("message source does not match authenticated ingress Server");
          }
          const target = frame.target.node_id;
          if (target === endpoint.node_id || !allowed.has(target)) {
            throw new Error("recursive or unauthorized destination");
          }
          const destination = this.ready.get(target);
          const delivered = !!(destination && destination.channel.send(raw));
          if (!delivered) {
            // Transport status only; never fabricate a business result.
            channel.frame({
              type: "forward.error",
              message_id: frame.message_id,
              target_node: target,
              status: "not_forwarded",
            });
            this.broadcastRoutes();
          }
        } else if (frame.type === "resolve.query") {
          channel.frame({
            type: "resolve.answer",
            id: frame.id,
            matches: this.agentMatches(frame.node, frame.agent),
          });
        } else if (frame.type === "pong") {
          // Keep-alive acknowledgement; nothing to do.
        } else {
          throw new Error("unsupported Server frame");
        }
      }
    } finally {
      if (connection) {
        try { clearInterval(connection.routeTimer); } catch (_) {}
        if (this.ready.get(endpoint.node_id) === connection) {
          this.ready.delete(endpoint.node_id);
        }
        this.state.set(endpoint.node_id, "disconnected");
      }
      try { if (channel) channel.close(); } catch (_) {}
      try { if (queue) queue.close(); } catch (_) {}
      try { if (transport) transport.close(); } catch (_) {}
      if (connection) this.broadcastRoutes();
    }
  }

  sendRoutes(connection) {
    if (this.ready.get(connection.node_id) !== connection) return;
    const reachable = [...this.ready.keys()]
      .filter((node) => node !== connection.node_id && connection.allowed.has(node));
    connection.seq += 1;
    connection.channel.frame({
      type: "routes.snapshot",
      session_id: connection.generation,
      seq: connection.seq,
      ttl_seconds: connection.ttl,
      reachable,
    });
  }

  broadcastRoutes() {
    for (const connection of this.ready.values()) {
      try { connection.sendRoutes(); } catch (_) {}
    }
  }

  agentMatches(node, reference) {
    if (!this.config || typeof reference !== "string") return [];
    const now = nowSeconds();
    let agents = this.config.agents;
    let defaultTime = Math.floor(this.startedAt / 1000);
    if (this.config.agents_file) {
      try {
        agents = JSON.parse(fs.readFileSync(this.config.agents_file, "utf8"));
        defaultTime = Math.floor(fs.statSync(this.config.agents_file).mtimeMs / 1000);
      } catch (_) {
        return [];
      }
    }
    if (!Array.isArray(agents)) return [];
    const ready = new Set(this.ready.keys());
    const valid = agents.filter((agent) => agent
      && ready.has(agent.node_id)
      && (!node || agent.node_id === node)
      && now - Number(agent.updated_at == null ? defaultTime : agent.updated_at)
        <= this.config.agent_ttl_seconds);
    const exact = valid.some((agent) => agent.id === reference
      || String(agent.name || "").toLowerCase() === reference.toLowerCase());
    return valid
      .filter((agent) => (exact
        ? agent.id === reference || String(agent.name || "").toLowerCase() === reference.toLowerCase()
        : String(agent.id || "").startsWith(reference)
          || String(agent.name || "").toLowerCase().startsWith(reference.toLowerCase())))
      .map((agent) => ({ node_id: agent.node_id, agent_id: agent.id }));
  }
}

module.exports = {
  ClientForwarder,
  loadConfig,
  validateEnvelope,
  validateDynamicEndpoint,
  encodeFrame,
  FRAME_LIMIT,
};
