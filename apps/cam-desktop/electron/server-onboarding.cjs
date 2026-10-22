"use strict";

/**
 * Desktop-side adapter for CAM Server onboarding.
 *
 * The provisioning logic is not reimplemented here: it is the exact
 * `components/cam-router/scripts/server_setup.py` script embedded by cam-tui
 * (`cam_server::SETUP_SCRIPT`). Desktop only transports the same
 * request JSON and release bytes over its existing `ssh2` connection pool,
 * then speaks the same `cam-server connect` stdio protocol as cam-clientd.
 *
 * Windows Nodes are intentionally unsupported: cam-server currently requires
 * Unix sockets, UID/mode checks and Unix signals. See
 * docs/cam-server-lifecycle-spec.md.
 */

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_MIN_VERSION = process.env.CAM_SERVER_MIN_VERSION || "0.1.3";
const PROBE_TIMEOUT_MS = 30000;
const INSTALL_TIMEOUT_MS = 150000;
const REFRESH_MS = 5 * 60 * 1000;
const RETRY_BASE_MS = 30000;
const RETRY_MAX_MS = 10 * 60 * 1000;

function validNodeId(value) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 128
    && /^[A-Za-z0-9_.-]+$/.test(value);
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function setupCommand(script, request) {
  const code = `import base64;exec(base64.b64decode(${JSON.stringify(
    Buffer.from(String(script), "utf8").toString("base64"),
  )}))`;
  return `python3 -c ${shellQuote(code)} ${shellQuote(JSON.stringify(request))}`;
}

function parseLastJson(stdout) {
  const lines = String(stdout || "").trim().split(/\r?\n/).reverse();
  for (const line of lines) {
    try {
      const value = JSON.parse(line);
      if (value && typeof value === "object") return value;
    } catch (_) {}
  }
  return null;
}

function setupError(result, info) {
  if (info && typeof info.setup_error === "string") {
    const safe = info.setup_error.length < 80
      && /^[a-z0-9_]+$/.test(info.setup_error);
    return { ok: false, error: "setup_failed", detail: safe ? info.setup_error : "setup_failed" };
  }
  if (!result || result.ok) return null;
  return {
    ok: false,
    error: result.error || "setup_failed",
    detail: String(result.detail || result.stderr || "CAM Server setup failed").split("\n")[0].slice(0, 300),
  };
}

function validateInfo(info) {
  if (!info || typeof info !== "object") return { ok: false, error: "setup_failed", detail: "empty setup response" };
  if (!validNodeId(info.node_id)) return { ok: false, error: "setup_failed", detail: "invalid Server identity" };
  if (typeof info.token !== "string" || info.token.length < 32 || info.token.length > 1024) {
    return { ok: false, error: "setup_failed", detail: "invalid pairing credential" };
  }
  for (const key of ["binary", "socket"]) {
    const value = info[key];
    if (typeof value !== "string" || !value.startsWith("/") || /[\x00-\x1f]/.test(value)) {
      return { ok: false, error: "setup_failed", detail: `invalid Server ${key}` };
    }
  }
  return { ok: true };
}

function versionAtLeast(version, minimum) {
  const a = String(version || "").split(".").map((n) => Number.parseInt(n, 10) || 0);
  const b = String(minimum || "").split(".").map((n) => Number.parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const left = a[i] || 0;
    const right = b[i] || 0;
    if (left !== right) return left > right;
  }
  return true;
}

function loadBundle(bundlePath) {
  if (!bundlePath) return null;
  try {
    const manifest = JSON.parse(fs.readFileSync(`${bundlePath}.json`, "utf8"));
    const data = fs.readFileSync(bundlePath);
    if (manifest.size !== data.length
        || manifest.sha256 !== crypto.createHash("sha256").update(data).digest("hex")) {
      return null;
    }
    return { data, manifest };
  } catch (_) {
    return null;
  }
}

async function ensureNode({
  node,
  clientId,
  minVersion = DEFAULT_MIN_VERSION,
  script,
  execRemote,
  bundle = null,
  log = () => {},
}) {
  if (!script) return { ok: false, error: "setup_script_missing", detail: "server_setup.py is not bundled" };
  if (!execRemote) return { ok: false, error: "ssh_transport_unavailable", detail: "no execRemote" };
  if (!node || !node.opts) return { ok: false, error: "invalid_node", detail: "missing SSH connection options" };

  const request = {
    mode: "probe",
    version: minVersion,
    client_id: clientId,
    expected_node: node.expected_node || "",
  };
  let result = await execRemote({
    ...node.opts,
    command: setupCommand(script, request),
    timeout_ms: PROBE_TIMEOUT_MS,
  });
  let info = parseLastJson(result && result.stdout);
  let failure = setupError(result, info);
  if (failure) return failure;
  if (!info) return { ok: false, error: "setup_failed", detail: "empty setup response" };

  if (info.need_bundle === true) {
    if (!bundle) {
      return {
        ok: false,
        error: "server_update_required",
        detail: `Node ${node.context_name || node.context_id || ""} needs cam-server ${minVersion}; no release bundle is installed with Desktop`,
      };
    }
    if (bundle.manifest.target !== info.target) {
      return {
        ok: false,
        error: "server_bundle_target_mismatch",
        detail: `Node target ${info.target}, Desktop bundle ${bundle.manifest.target}`,
      };
    }
    if (!versionAtLeast(bundle.manifest.version, minVersion)) {
      return {
        ok: false,
        error: "server_bundle_version_mismatch",
        detail: `Bundle ${bundle.manifest.version}, required ${minVersion}`,
      };
    }
    request.mode = "install";
    request.version = bundle.manifest.version;
    request.target = bundle.manifest.target;
    request.size = bundle.manifest.size;
    request.sha256 = bundle.manifest.sha256;
    log(`node=${node.context_name || node.context_id} installing cam-server ${minVersion}`);
    result = await execRemote({
      ...node.opts,
      command: setupCommand(script, request),
      stdin: bundle.data,
      timeout_ms: INSTALL_TIMEOUT_MS,
    });
    info = parseLastJson(result && result.stdout);
    failure = setupError(result, info);
    if (failure) return failure;
    if (!info) return { ok: false, error: "setup_failed", detail: "empty install response" };
  }

  const checked = validateInfo(info);
  if (!checked.ok) return checked;
  return {
    ok: true,
    node_id: info.node_id,
    token: info.token,
    binary: info.binary,
    socket: info.socket,
    deployment: info.deployment || "current",
  };
}

function desktopClientId(hostname) {
  const base = String(hostname || "desktop")
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "")
    .slice(0, 96);
  return `desktop-${base || "local"}`;
}

function contextSignature(node) {
  const opts = node.opts || {};
  const secret = crypto.createHash("sha256")
    .update(String(opts.password || ""))
    .update("\0")
    .update(String(opts.passphrase || ""))
    .digest("hex")
    .slice(0, 16);
  return JSON.stringify([
    node.context_id || "",
    node.context_name || "",
    node.context_key || "",
    opts.host || "",
    opts.user || "",
    opts.port || 22,
    opts.auth_method || "",
    opts.key_file || "",
    opts.jump ? [opts.jump.host, opts.jump.user, opts.jump.port] : null,
    secret,
  ]);
}

class ForwardingManager {
  constructor({
    forwarder,
    getNodes,
    credentialStore,
    clientId,
    script,
    minVersion = DEFAULT_MIN_VERSION,
    bundlePath = null,
    execRemote,
    openExecChannel,
    log = () => {},
    syncIntervalMs = 60000,
    concurrency = 1,
    refreshMs = REFRESH_MS,
  }) {
    this.forwarder = forwarder;
    this.getNodes = getNodes;
    this.credentialStore = credentialStore;
    this.clientId = clientId;
    this.script = script;
    this.minVersion = minVersion;
    this.bundle = loadBundle(bundlePath);
    this.execRemote = execRemote;
    this.openExecChannel = openExecChannel;
    this.log = log;
    this.syncIntervalMs = syncIntervalMs;
    this.concurrency = Math.max(1, Math.min(8, Number(concurrency) || 1));
    this.refreshMs = Math.max(30000, Number(refreshMs) || REFRESH_MS);
    this.entries = new Map();   // context key -> { sig, ok, node_id, node, ensuredAt, attempts, retryAt }
    this.timer = null;
    this.stopped = true;
    this.syncing = false;
    this.lastSyncAt = null;
    this.lastError = null;
  }

  start() {
    if (this.timer) return;
    const status = this.forwarder.startWithEndpoints({
      clientId: this.clientId,
      endpoints: [],
    });
    if (!status || status.dynamic !== true) {
      this.log("static client config is active; automatic forwarding skipped");
      return;
    }
    this.stopped = false;
    void this.sync();
    this.timer = setInterval(() => {
      void this.sync().catch((err) => this.log(`sync failed: ${err.message}`));
    }, this.syncIntervalMs);
    if (this.timer && typeof this.timer.unref === "function") this.timer.unref();
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  status() {
    const live = (this.forwarder && typeof this.forwarder.status === "function")
      ? this.forwarder.status().nodes
      : {};
    const nodes = [];
    for (const [key, entry] of this.entries) {
      nodes.push({
        context: key,
        node_id: entry.node_id || null,
        state: entry.ok ? (live[entry.node_id] || "ready") : "error",
        error: entry.error || null,
        detail: entry.detail || null,
        ensured_at: entry.ensuredAt || null,
      });
    }
    return {
      client_id: this.clientId,
      last_sync_at: this.lastSyncAt,
      last_error: this.lastError,
      nodes,
    };
  }

  async sync() {
    if (this.stopped || this.syncing) return this.status();
    this.syncing = true;
    try {
      const list = this.getNodes();
      const nodes = Array.isArray(list) ? list : [];
      const present = new Map();
      const jobs = [];
      const now = Date.now();
      for (const node of nodes) {
        const key = String(node.context_id || node.context_name || "");
        if (!key) continue;
        present.set(key, node);
        if (node.error) {
          const entry = this.entries.get(key);
          if (entry && !entry.ok && entry.retryAt > now) continue;
          if (entry && entry.node_id) this.forwarder.removeEndpoint(entry.node_id);
          this.entries.set(key, {
            sig: contextSignature(node),
            ok: false,
            error: node.error,
            detail: node.detail || "",
            attempts: (entry && entry.attempts || 0) + 1,
            retryAt: now + this.retryDelay(entry),
          });
          continue;
        }
        const sig = contextSignature(node);
        const entry = this.entries.get(key);
        const fresh = entry && entry.sig === sig && entry.ok
          && Date.now() - (entry.ensuredAt || 0) < this.refreshMs;
        const waiting = entry && !entry.ok && entry.retryAt > now;
        if (fresh || waiting) continue;
        if (entry && entry.node_id) this.forwarder.removeEndpoint(entry.node_id);
        jobs.push({
          key,
          sig,
          node: {
            ...node,
            expected_node: (entry && entry.ok && entry.node_id) || node.expected_node || "",
          },
        });
      }
      for (const [key, entry] of [...this.entries]) {
        if (!present.has(key)) {
          if (entry.node_id) this.forwarder.removeEndpoint(entry.node_id);
          this.entries.delete(key);
        }
      }
      await this.runJobs(jobs);
      this.lastSyncAt = Date.now();
      this.lastError = null;
    } catch (err) {
      this.lastError = err && err.message || String(err);
      this.log(`sync failed: ${this.lastError}`);
    } finally {
      this.syncing = false;
    }
    return this.status();
  }

  retryDelay(entry) {
    const attempts = Math.max(0, (entry && entry.attempts) || 0);
    return Math.min(RETRY_MAX_MS, RETRY_BASE_MS * (2 ** Math.min(5, attempts)));
  }

  async runJobs(jobs) {
    const queue = jobs.slice();
    const workers = Array.from(
      { length: Math.min(this.concurrency, queue.length) },
      async () => {
        while (queue.length && !this.stopped) {
          const job = queue.shift();
          await this.ensureOne(job);
        }
      },
    );
    await Promise.all(workers);
  }

  async ensureOne({ key, node, sig }) {
    const previous = this.entries.get(key);
    try {
      const result = await ensureNode({
        node,
        clientId: this.clientId,
        minVersion: this.minVersion,
        script: this.script,
        execRemote: this.execRemote,
        bundle: this.bundle,
        log: this.log,
      });
      if (!result.ok) {
        this.entries.set(key, {
          sig,
          ok: false,
          error: result.error,
          detail: result.detail,
          attempts: (previous && previous.attempts || 0) + 1,
          retryAt: Date.now() + this.retryDelay(previous),
        });
        this.log(`node=${node.context_name || key} ensure failed: ${result.error}${result.detail ? ` (${result.detail})` : ""}`);
        return;
      }
      if (this.credentialStore) {
        const stored = this.credentialStore.put(`forward:${result.node_id}`, "forward-token", result.token);
        if (stored && stored.ok === false) {
          this.log(`node=${result.node_id} token store failed: ${stored.error || "unknown"}`);
        }
      }
      const opts = node.opts;
      this.forwarder.upsertEndpoint({
        node_id: result.node_id,
        token: result.token,
        open: async () => {
          const opened = await this.openExecChannel(
            opts,
            `${shellQuote(result.binary)} connect --socket ${shellQuote(result.socket)}`,
          );
          if (!opened || !opened.ok) {
            throw new Error(`${(opened && opened.error) || "exec_failed"}: ${(opened && opened.detail) || ""}`);
          }
          return opened.stream;
        },
      });
      this.entries.set(key, {
        sig,
        ok: true,
        node_id: result.node_id,
        ensuredAt: Date.now(),
        attempts: 0,
        retryAt: 0,
        deployment: result.deployment,
      });
      this.log(`node=${node.context_name || key} forwarding ready (${result.node_id}, ${result.deployment})`);
    } catch (err) {
      this.entries.set(key, {
        sig,
        ok: false,
        error: "ensure_failed",
        detail: err && err.message || String(err),
        attempts: (previous && previous.attempts || 0) + 1,
        retryAt: Date.now() + this.retryDelay(previous),
      });
      this.log(`node=${node.context_name || key} ensure failed: ${err && err.message || err}`);
    }
  }
}

function loadSetupScript({ isPackaged = false, resourcesPath = "", electronDir = __dirname } = {}) {
  const candidates = [];
  if (isPackaged && resourcesPath) {
    candidates.push(path.join(resourcesPath, "cam-server", "server_setup.py"));
  }
  candidates.push(path.resolve(electronDir, "..", "..", "..", "components", "cam-router", "scripts", "server_setup.py"));
  for (const candidate of candidates) {
    try {
      if (fs.statSync(candidate).isFile()) return fs.readFileSync(candidate, "utf8");
    } catch (_) {}
  }
  return null;
}

module.exports = {
  DEFAULT_MIN_VERSION,
  ForwardingManager,
  desktopClientId,
  ensureNode,
  loadBundle,
  loadSetupScript,
  parseLastJson,
  setupCommand,
};
