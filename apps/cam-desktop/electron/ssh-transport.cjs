/**
 * CAM Desktop — main-process SSH transport (CAM-DESK-DIRECT-018/019).
 *
 * Thin async wrapper around the pure-Node `ssh2` client. Lives in
 * Electron main so plaintext passwords/passphrases never leave this
 * process: the renderer drives sync through CamApi, the embedded
 * Hub fetches the credential from `credential-store` (decrypted via
 * Electron `safeStorage`), and only this module ever sees the
 * cleartext for the brief moment it takes ssh2 to authenticate.
 *
 * No host shell required. No system `ssh` binary. No WSL. ssh2 is
 * a pure-JS protocol implementation; the optional `cpu-features`
 * native module accelerates one cipher but is not load-bearing.
 *
 * ## Long-lived connection pool (CAM-DESK-DIRECT-019)
 *
 * Each operation (exec, sftp upload) used to open + auth + close
 * its own ssh2.Client, paying a fresh TCP/auth handshake every
 * time. CAM's Python side avoids this with OpenSSH's
 * ControlMaster/ControlPersist=600 (see src/camc_pkg/remote.py,
 * src/cam/transport/ssh.py). We replicate that self-contained:
 * an in-process `Map<key, entry>` pool keyed by
 *
 *     host | user | port | auth_method | key_file | secret_digest
 *
 * where `secret_digest` is a SHA-256 truncated digest of any
 * password/passphrase present, so the raw secret never appears in
 * the pool key, in `poolStats()`, or in any log line. A pooled
 * `ssh2.Client` is reused for both `execRemote()` and
 * `writeRemoteFile()`; each operation opens its own channel
 * (`conn.exec` / `conn.sftp`) but does NOT open a new TCP/auth
 * handshake. Idle entries close after ~600s. An entry is dropped
 * (and its client destroyed) on any of: ssh2 `close`/`end`/`error`,
 * connect failure, auth failure, operation timeout that forces
 * destroy, or explicit `closeAll()`.
 *
 * Public surface:
 *   execRemote({
 *     host, user, port,
 *     auth_method,        // 'key' | 'password' | 'agent'
 *     key_file,           // path on disk (read with fs.readFileSync)
 *     passphrase,         // for encrypted private keys
 *     password,           // for password auth
 *     command,            // remote command to run
 *     timeout_ms,         // per-op budget; default 15000
 *   }) → { ok, stdout, stderr, code, signal, error, detail,
 *          timings:{pooled,connect_ms,op_ms,total_ms} }
 *
 *   writeRemoteFile({...same auth fields..., remotePath, content,
 *     timeout_ms}) → { ok, bytes, remotePath, error, detail, timings }
 *
 *   setOverride(stub | null)   // smoke-test injection point —
 *                              // bypasses the pool entirely.
 *
 *   closeAll()                 // tear down all pooled clients (tests, shutdown)
 *   poolStats()                // { size, keys: [secret-free strings] }
 *   _setSsh2ForTests(mod)      // inject a mock ssh2 module (tests only)
 *
 * Errors map to a small dictionary for the renderer to switch on:
 *   'invalid_args' | 'key_file_missing' | 'key_read_failed' |
 *   'connect_timeout' | 'auth_failed' | 'connect_refused' |
 *   'dns_failure' | 'connect_lost' | 'exec_failed' |
 *   'remote_nonzero' | 'sftp_failed' | 'sftp_write_failed'
 */

'use strict';

const fs     = require('node:fs');
const crypto = require('node:crypto');

let _override = null;
let _ssh2 = null;

const IDLE_CLOSE_MS = 600 * 1000;   // Match CAM ControlPersist=600
const DEFAULT_TIMEOUT_MS = 15000;

/** Pool of long-lived ssh2.Client entries.
 *  Key  : string built by _poolKey (no secrets in plaintext).
 *  Value: { client, state, readyPromise, connectMs, idleTimer,
 *           inflight, justCreated, connectError } */
const _pool = new Map();

function _loadSsh2() {
  if (_ssh2) return _ssh2;
  try { _ssh2 = require('ssh2'); }
  catch (e) {
    _ssh2 = { _loadError: e && e.message };
  }
  return _ssh2;
}

function _setSsh2ForTests(mod) {
  // Test-only injection. Pass null to reset to default require('ssh2').
  _ssh2 = mod || null;
}

function setOverride(fn) {
  _override = (typeof fn === 'function') ? fn : null;
}

function _secretDigest(opts) {
  // Sha256(`p:<password>` || `h:<passphrase>`) → 16-char hex.
  // Stable across calls with same secrets; cryptographically
  // useless for guessing the secret back; safe to put in pool key,
  // poolStats(), or logs. Empty string when no secret is present.
  const h = crypto.createHash('sha256');
  let any = false;
  if (typeof opts.password === 'string' && opts.password) {
    h.update('p:'); h.update(opts.password); any = true;
  }
  if (typeof opts.passphrase === 'string' && opts.passphrase) {
    h.update('h:'); h.update(opts.passphrase); any = true;
  }
  return any ? h.digest('hex').slice(0, 16) : '';
}

function _poolKey(opts) {
  const port = opts.port ? Number(opts.port) : 22;
  const auth = (opts.auth_method || (opts.key_file ? 'key' : 'agent')).toLowerCase();
  const keyFile = opts.key_file || '';
  return [
    String(opts.host || ''),
    String(opts.user || ''),
    String(port),
    auth,
    keyFile,
    _secretDigest(opts),
  ].join('|');
}

function _buildAuth(opts) {
  const auth = (opts.auth_method || (opts.key_file ? 'key' : 'agent')).toLowerCase();
  if (auth === 'password') {
    if (typeof opts.password !== 'string' || !opts.password) {
      return { error: 'invalid_args', detail: 'password auth requires a non-empty password' };
    }
    return { auth, fields: { password: opts.password } };
  }
  if (auth === 'key') {
    if (!opts.key_file) {
      return { error: 'invalid_args', detail: 'key auth requires key_file path' };
    }
    let keyBuf;
    try { keyBuf = fs.readFileSync(opts.key_file); }
    catch (e) {
      if (e.code === 'ENOENT') return { error: 'key_file_missing', detail: `${opts.key_file}: not found` };
      return { error: 'key_read_failed', detail: e && e.message };
    }
    const fields = { privateKey: keyBuf };
    if (opts.passphrase) fields.passphrase = opts.passphrase;
    return { auth, fields };
  }
  if (auth === 'agent') {
    const sock = process.env.SSH_AUTH_SOCK || '';
    if (!sock) {
      return { error: 'invalid_args', detail: 'agent auth requires SSH_AUTH_SOCK in the environment' };
    }
    return { auth, fields: { agent: sock } };
  }
  return { error: 'invalid_args', detail: `unknown auth_method: ${opts.auth_method}` };
}

function _classifyError(err) {
  if (!err) return { error: 'exec_failed', detail: 'unknown' };
  const msg = String(err.message || err);
  if (/authentication/i.test(msg) || /All configured authentication methods failed/i.test(msg)) {
    return { error: 'auth_failed', detail: msg };
  }
  if (err.code === 'ECONNREFUSED' || /ECONNREFUSED/i.test(msg)) {
    return { error: 'connect_refused', detail: msg };
  }
  if (err.code === 'ENOTFOUND' || /ENOTFOUND/i.test(msg) || /getaddrinfo/i.test(msg)) {
    return { error: 'dns_failure', detail: msg };
  }
  if (err.code === 'ETIMEDOUT' || /timed out/i.test(msg)) {
    return { error: 'connect_timeout', detail: msg };
  }
  if (/Connection lost before handshake|closed before ready|ended before ready|before handshake/i.test(msg)) {
    return { error: 'connect_lost', detail: msg };
  }
  return { error: 'exec_failed', detail: msg };
}

function _clearIdleTimer(entry) {
  if (entry && entry.idleTimer) {
    clearTimeout(entry.idleTimer);
    entry.idleTimer = null;
  }
}

function _startIdleTimer(entry) {
  _clearIdleTimer(entry);
  entry.idleTimer = setTimeout(() => {
    // Only drop if still no in-flight ops; a late-arriving op
    // would have already cleared this timer.
    if (entry.inflight === 0 && _pool.get(entry.key) === entry) {
      _dropEntry(entry.key, 'idle');
    }
  }, IDLE_CLOSE_MS);
  if (entry.idleTimer && typeof entry.idleTimer.unref === 'function') {
    entry.idleTimer.unref();   // don't keep Node event loop alive
  }
}

function _dropEntry(key, _reason) {
  const entry = _pool.get(key);
  if (!entry) return;
  _pool.delete(key);
  _clearIdleTimer(entry);
  entry.state = 'closed';
  // Best-effort close. end() asks for clean shutdown; destroy() if needed.
  try { entry.client && entry.client.end && entry.client.end(); }    catch { /* noop */ }
  try { entry.client && entry.client.destroy && entry.client.destroy(); } catch { /* noop */ }
}

function _getOrCreate(key, opts, ssh2) {
  const existing = _pool.get(key);
  if (existing && existing.state !== 'closed') {
    existing.justCreated = false;
    return existing;
  }

  const authBuilt = _buildAuth(opts);
  if (authBuilt.error) {
    // Return a sentinel "closed-on-arrival" entry whose readyPromise
    // rejects immediately. We do NOT insert it into _pool — there's
    // nothing to reuse.
    return {
      key,
      justCreated: true,
      state: 'closed',
      readyPromise: Promise.reject({ error: authBuilt.error, detail: authBuilt.detail }),
      client: null,
      connectMs: null,
    };
  }

  const port = opts.port ? Number(opts.port) : 22;
  const timeoutMs = opts.timeout_ms ? Number(opts.timeout_ms) : DEFAULT_TIMEOUT_MS;
  const client = new ssh2.Client();
  const entry = {
    key,
    client,
    state: 'connecting',
    connectMs: null,
    idleTimer: null,
    inflight: 0,
    justCreated: true,
    connectError: null,
  };

  entry.readyPromise = new Promise((resolve, reject) => {
    const t0 = Date.now();
    let settled = false;
    const failConnect = (err) => {
      if (settled) return;
      settled = true;
      const c = (err && err.error && err.detail) ? err : _classifyError(err);
      entry.connectError = c;
      _dropEntry(key, 'connect_error');
      reject(c);
    };
    const onError = (err) => {
      if (entry.state === 'connecting') failConnect(err);
      else _dropEntry(key, 'error');
    };
    const onClose = () => {
      // If we never reached ready, treat close as connect failure.
      if (entry.state === 'connecting') failConnect(new Error('Connection lost before handshake'));
      else _dropEntry(key, 'close');
    };
    const onEnd = () => {
      if (entry.state === 'connecting') failConnect(new Error('Connection ended before handshake'));
      else _dropEntry(key, 'end');
    };

    // Keep a permanent error listener for the full client lifetime.
    // ssh2 can emit a late "Connection lost before handshake" from the
    // socket after close/destroy; without a listener that becomes an
    // uncaught exception in Electron's main process.
    client.on('error', onError);
    client.on('close', onClose);
    client.on('end', onEnd);
    client.once('ready', () => {
      if (settled) return;
      settled = true;
      entry.state = 'ready';
      entry.connectMs = Date.now() - t0;
      _startIdleTimer(entry);
      resolve();
    });

    const connectOpts = {
      host:               String(opts.host),
      port,
      username:           String(opts.user),
      readyTimeout:       Math.min(10000, timeoutMs),
      keepaliveInterval:  0,
      tryKeyboard:        false,
      ...authBuilt.fields,
    };
    try { client.connect(connectOpts); }
    catch (e) { failConnect(e); }
  });

  _pool.set(key, entry);
  return entry;
}

async function _withPooledClient(opts, op /* (client, finishWithTimings) */) {
  if (!opts || !opts.host || !opts.user) {
    return { ok: false, error: 'invalid_args', detail: 'host and user are required' };
  }

  const ssh2 = _loadSsh2();
  if (!ssh2 || !ssh2.Client) {
    return { ok: false, error: 'exec_failed', detail: `ssh2 module not loadable: ${ssh2 && ssh2._loadError || 'unknown'}` };
  }

  const totalT0 = Date.now();
  const key = _poolKey(opts);
  const entry = _getOrCreate(key, opts, ssh2);
  const pooled = !entry.justCreated;

  try {
    await entry.readyPromise;
  } catch (e) {
    return {
      ok: false,
      error:   e.error  || 'exec_failed',
      detail:  e.detail || 'connect failed',
      timings: { pooled, connect_ms: null, op_ms: 0, total_ms: Date.now() - totalT0 },
    };
  }

  // Reserve the connection so the idle timer cannot drop us mid-op.
  entry.inflight++;
  _clearIdleTimer(entry);
  const opT0 = Date.now();
  const timeoutMs = opts.timeout_ms ? Number(opts.timeout_ms) : DEFAULT_TIMEOUT_MS;

  return new Promise((resolve) => {
    let settled = false;
    const finish = (partial) => {
      if (settled) return;
      settled = true;
      const op_ms = Date.now() - opT0;
      const total_ms = Date.now() - totalT0;
      entry.inflight = Math.max(0, entry.inflight - 1);
      if (entry.inflight === 0 && _pool.get(key) === entry) {
        _startIdleTimer(entry);
      }
      resolve({
        ...partial,
        timings: { pooled, connect_ms: entry.connectMs, op_ms, total_ms },
      });
    };

    const tm = setTimeout(() => {
      // Op-level timeout: destroy this client so a hung channel can't
      // block future ops. The pool entry is dropped as part of cleanup.
      finish({ ok: false, error: 'connect_timeout', detail: `op timeout after ${timeoutMs}ms` });
      _dropEntry(key, 'op_timeout');
    }, timeoutMs);
    if (tm && typeof tm.unref === 'function') tm.unref();

    try {
      op(entry.client, (partial) => { clearTimeout(tm); finish(partial); });
    } catch (e) {
      clearTimeout(tm);
      finish({ ok: false, error: 'exec_failed', detail: e && e.message });
    }
  });
}

async function execRemote(opts) {
  if (_override) return _override(opts);
  if (!opts || typeof opts.command !== 'string' || !opts.command) {
    return { ok: false, error: 'invalid_args', detail: 'command is required' };
  }
  return _withPooledClient(opts, (client, finish) => {
    client.exec(opts.command, { pty: false }, (err, stream) => {
      if (err) return finish({ ok: false, error: 'exec_failed', detail: err.message });
      let stdout = '';
      let stderr = '';
      stream.on('data', (d) => { stdout += d.toString('utf8'); });
      if (stream.stderr) stream.stderr.on('data', (d) => { stderr += d.toString('utf8'); });
      stream.on('close', (exitCode, exitSignal) => {
        const code   = (typeof exitCode === 'number') ? exitCode : null;
        const signal = exitSignal || null;
        if (code === 0) {
          finish({ ok: true, stdout, stderr, code, signal });
        } else {
          finish({
            ok:     false,
            error:  'remote_nonzero',
            detail: stderr.trim() || stdout.trim() || `remote exit code ${code}`,
            stdout, stderr, code, signal,
          });
        }
      });
    });
  });
}

async function writeRemoteFile(opts) {
  if (_override) return _override({ ...opts, operation: 'writeRemoteFile' });
  if (!opts || typeof opts.remotePath !== 'string' || !opts.remotePath) {
    return { ok: false, error: 'invalid_args', detail: 'remotePath is required' };
  }
  if (opts.content == null) {
    return { ok: false, error: 'invalid_args', detail: 'content is required' };
  }
  const content = Buffer.isBuffer(opts.content) ? opts.content : Buffer.from(String(opts.content), 'utf8');
  return _withPooledClient(opts, (client, finish) => {
    client.sftp((err, sftp) => {
      if (err) return finish({ ok: false, error: 'sftp_failed', detail: err.message });
      sftp.writeFile(opts.remotePath, content, (writeErr) => {
        try { sftp.end(); } catch { /* noop */ }
        if (writeErr) return finish({ ok: false, error: 'sftp_write_failed', detail: writeErr.message });
        finish({ ok: true, bytes: content.length, remotePath: opts.remotePath });
      });
    });
  });
}

function closeAll() {
  for (const k of [..._pool.keys()]) _dropEntry(k, 'closeAll');
}

function poolStats() {
  // Keys are safe to expose: they contain only host/user/port/auth/key_file
  // plus a SHA-256 digest (never the raw secret). `inflight` and the
  // per-entry connect_ms are useful diagnostics.
  const out = [];
  for (const [k, e] of _pool.entries()) {
    out.push({
      key:        k,
      state:      e.state,
      inflight:   e.inflight,
      connect_ms: e.connectMs,
    });
  }
  return { size: _pool.size, entries: out, keys: out.map(e => e.key) };
}

/**
 * Open a long-lived PTY exec channel for an interactive attach
 * (CAM-DESK-TERM-001). Unlike execRemote/writeRemoteFile which return
 * after a single roundtrip, this opens a streaming channel:
 *
 *   - The same connection pool is reused (no extra TCP/auth handshake
 *     when an output / sync session is already open to the same
 *     endpoint+auth identity).
 *   - A fresh ssh2 channel (`conn.exec(cmd, {pty:{cols,rows,...}}, cb)`)
 *     is opened — concurrent with any other channel on the same
 *     client. Closing this channel does NOT affect other channels and
 *     does NOT close the pooled client.
 *   - `onData(buf)` fires for every chunk of stdout / stderr; we
 *     merge both onto the same callback because xterm.js treats them
 *     as one byte stream.
 *   - `onClose({code, signal})` fires once when the remote command
 *     ends, the channel is destroyed, or the underlying connection
 *     drops.
 *
 * Returns:
 *   { ok: true, dispose, write(buf), resize(cols, rows) }
 *   { ok: false, error, detail }
 *
 * `dispose()` closes the channel (best-effort) and stops invoking
 * onData/onClose. It is idempotent. The agent process keeps running —
 * `camc attach` is a read+input attach to the agent's tmux, not the
 * agent itself.
 */
async function openTerminalChannel(opts, hooks = {}) {
  if (_override) return _override({ ...opts, operation: 'openTerminalChannel' });
  if (!opts || !opts.host || !opts.user) {
    return { ok: false, error: 'invalid_args', detail: 'host and user are required' };
  }
  if (typeof opts.command !== 'string' || !opts.command) {
    return { ok: false, error: 'invalid_args', detail: 'command is required' };
  }
  const onData  = typeof hooks.onData  === 'function' ? hooks.onData  : () => {};
  const onClose = typeof hooks.onClose === 'function' ? hooks.onClose : () => {};
  const cols = Math.max(2, Math.min(500, Number(hooks.cols) || 80));
  const rows = Math.max(2, Math.min(500, Number(hooks.rows) || 24));
  const openTimeoutMs = Math.max(5000, Math.min(120000, Number(opts.timeout_ms) || 60000));

  const ssh2 = _loadSsh2();
  if (!ssh2 || !ssh2.Client) {
    return { ok: false, error: 'exec_failed', detail: `ssh2 module not loadable: ${ssh2 && ssh2._loadError || 'unknown'}` };
  }

  const key = _poolKey(opts);
  const entry = _getOrCreate(key, opts, ssh2);

  try {
    await entry.readyPromise;
  } catch (e) {
    return { ok: false, error: e.error || 'exec_failed', detail: e.detail || 'connect failed' };
  }

  // Reserve so the idle timer cannot drop the client mid-attach.
  entry.inflight++;
  _clearIdleTimer(entry);

  return await new Promise((resolve) => {
    let stream = null;
    let active = true;
    let settledOpen = false;
    let openTimer = null;

    const finishOpen = (result) => {
      if (settledOpen) return;
      settledOpen = true;
      if (openTimer) { clearTimeout(openTimer); openTimer = null; }
      resolve(result);
    };
    const release = () => {
      if (!active) return;
      active = false;
      entry.inflight = Math.max(0, entry.inflight - 1);
      if (entry.inflight === 0 && _pool.get(key) === entry) {
        _startIdleTimer(entry);
      }
    };
    const dispose = () => {
      if (!active) return;
      // Stop emitting; close the channel; release the pool reservation.
      try { if (stream && typeof stream.end === 'function') stream.end(); } catch { /* noop */ }
      try { if (stream && typeof stream.destroy === 'function') stream.destroy(); } catch { /* noop */ }
      release();
    };

    openTimer = setTimeout(() => {
      const wasActive = active;
      release();
      try { if (stream && typeof stream.destroy === 'function') stream.destroy(); } catch { /* noop */ }
      if (wasActive) {
        finishOpen({ ok: false, error: 'exec_timeout', detail: 'terminal channel open timed out' });
      }
    }, openTimeoutMs);
    if (openTimer && typeof openTimer.unref === 'function') openTimer.unref();

    try {
      entry.client.exec(opts.command, {
        pty: {
          term: 'xterm-256color',
          cols, rows,
          width: 0, height: 0,
        },
      }, (err, s) => {
        if (err) {
          release();
          return finishOpen({ ok: false, error: 'exec_failed', detail: err.message });
        }
        if (!active) {
          try { if (s && typeof s.destroy === 'function') s.destroy(); } catch { /* noop */ }
          return;
        }
        stream = s;
        s.on('data', (d) => { if (active) onData(d); });
        if (s.stderr) s.stderr.on('data', (d) => { if (active) onData(d); });
        s.on('close', (code, signal) => {
          const exitCode = (typeof code === 'number') ? code : null;
          const exitSig  = signal || null;
          const wasActive = active;
          release();
          if (wasActive) {
            try { onClose({ code: exitCode, signal: exitSig }); } catch { /* noop */ }
          }
        });
        s.on('error', (err) => {
          const wasActive = active;
          release();
          if (wasActive) {
            try { onClose({ code: null, signal: null, error: err && err.message || String(err) }); } catch { /* noop */ }
          }
        });
        finishOpen({
          ok: true,
          dispose,
          write(buf) {
            if (!active || !stream || stream.destroyed) return false;
            try { return stream.write(buf); } catch { return false; }
          },
          resize(c, r) {
            if (!active || !stream || typeof stream.setWindow !== 'function') return false;
            try { stream.setWindow(Math.max(2, r|0), Math.max(2, c|0), 0, 0); return true; }
            catch { return false; }
          },
        });
      });
    } catch (e) {
      release();
      finishOpen({ ok: false, error: 'exec_failed', detail: e && e.message });
    }
  });
}

module.exports = {
  execRemote,
  writeRemoteFile,
  openTerminalChannel,
  setOverride,
  closeAll,
  poolStats,
  _setSsh2ForTests,
};
