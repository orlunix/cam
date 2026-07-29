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
 * handshake. The app NEVER closes pooled connections on its own
 * while it is running (desktop semantics 2026-07-18: an open desktop
 * terminal holds its sessions — unlike mobile there is no suspend
 * lifecycle to justify idle reaping). An entry is dropped
 * (and its client destroyed) on any of: ssh2 `close`/`end`/`error`,
 * connect failure, auth failure, operation timeout that forces
 * destroy, or explicit `closeAll()` (app quit).
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
const _systemSsh = require('./system-ssh.cjs');

let _override = null;
let _ssh2 = null;

const DEFAULT_TIMEOUT_MS = 15000;

/** Pool of long-lived ssh2.Client entries for EXEC traffic
 *  (execRemote / writeRemoteFile / SFTP).
 *  Key  : string built by _poolKey (no secrets in plaintext).
 *  Value: { client, state, readyPromise, connectMs, idleTimer,
 *           inflight, justCreated, connectError, poolRef } */
const _pool = new Map();

/** Separate pool for INTERACTIVE terminal channels. A terminal attach
 *  must never die because an unrelated exec timed out or the exec
 *  connection was reset — tab semantics: switching tabs cannot kill a
 *  session. Terminal channels share one dedicated connection per
 *  endpoint (multiplexed), isolated from exec traffic. */
const _termPool = new Map();

/** Optional diagnostic logger — main injects _diagLog so every connect/
 *  channel-open step lands in userData/cam-desktop.log alongside the
 *  renderer evidence. */
let _logFn = null;
function setLogger(fn) { _logFn = (typeof fn === 'function') ? fn : null; }
function _log(msg) { if (_logFn) { try { _logFn(msg); } catch { /* noop */ } } }

/** Compose a one-line negotiation summary from the captured ssh2 debug
 *  stream: server banner, kex/cipher/hostkey actually negotiated, and
 *  the auth method we used. Purely presentational — appended to the
 *  connect-ready log line. */
function _negotiatedSummary(dbgLines, authMethod) {
  const grab = (re) => {
    for (let i = dbgLines.length - 1; i >= 0; i--) {
      const m = re.exec(dbgLines[i]);
      if (m) return m[1];
    }
    return '';
  };
  const banner  = grab(/Remote version: (SSH-2.0-\S+)/) || grab(/(SSH-2.0-[^\s']+)/) || grab(/(SSH-2.0-\S+)/);
  const kex     = grab(/Handshake: KEX algorithm: (\S+)/);
  const cipher  = grab(/Handshake: S->C cipher: (\S+)/) || grab(/Handshake: C->S cipher: (\S+)/);
  const hostkey = grab(/Handshake: Host key format: (\S+)/);
  const parts = [];
  if (banner)  parts.push(banner.replace(/^SSH-2.0-/, ''));
  if (kex)     parts.push(`kex=${kex}`);
  if (cipher)  parts.push(`cipher=${cipher}`);
  if (hostkey) parts.push(`hostkey=${hostkey}`);
  if (authMethod) parts.push(`auth=${authMethod}`);
  return parts.length ? ` [${parts.join(', ')}]` : '';
}

function _loadSsh2() {
  if (_ssh2) return _ssh2;  try { _ssh2 = require('ssh2'); }
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
      if (e.code === 'ENOENT') return { error: 'key_file_missing', detail: `${opts.key_file}: not found — edit the host to select a valid key file or use password auth` };
      return { error: 'key_read_failed', detail: e && e.message };
    }
    const fields = { privateKey: keyBuf };
    if (opts.passphrase) {
      fields.passphrase = opts.passphrase;
    } else {
      // A key that cannot be parsed without a passphrase (encrypted
      // key, no passphrase provided) can never authenticate directly —
      // fall back to the SSH agent, which is exactly how the system
      // ssh (and Cursor/VSCode Remote) still connects via Keychain.
      const sock = process.env.SSH_AUTH_SOCK || '';
      if (sock) {
        let parseable = true;
        try {
          const ssh2 = _loadSsh2();
          const parsed = ssh2 && ssh2.utils && ssh2.utils.parseKey(keyBuf);
          parseable = !!(parsed && !(parsed instanceof Error) && (!Array.isArray(parsed) || parsed.length > 0));
        } catch (_) { parseable = false; }
        if (!parseable) {
          console.warn('[ssh-auth] key file not parseable without passphrase; falling back to agent');
          return { auth: 'agent', fields: { agent: sock } };
        }
      }
    }
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
  // Desktop semantics (2026-07-18, product decision): NEVER idle-close
  // pooled SSH connections while the app is running — unlike mobile,
  // an open desktop terminal holds its sessions. Connections come down
  // on transport errors or closeAll() (app quit) only. Keep clearing
  // any timer armed by an older code path.
  _clearIdleTimer(entry);
}

function _dropEntryForOpts(opts, reason) {
  try { _dropEntry(_poolKey(opts), reason); } catch { /* noop */ }
}

function _isRetryableChannelError(res) {
  if (!res || res.ok) return false;
  return ['sftp_failed', 'sftp_write_failed', 'exec_failed', 'connect_lost', 'connect_timeout'].includes(res.error);
}

async function _retryOnceAfterPoolDrop(opts, reason, fn) {
  _dropEntryForOpts(opts, reason);
  return fn();
}

function _dropEntry(key, _reason) {
  const entry = _pool.get(key) || _termPool.get(key);
  if (!entry) return;
  entry.poolRef.delete(key);
  _clearIdleTimer(entry);
  entry.state = 'closed';
  // Best-effort close. end() asks for clean shutdown; destroy() if needed.
  try { entry.client && entry.client.end && entry.client.end(); }    catch { /* noop */ }
  try { entry.client && entry.client.destroy && entry.client.destroy(); } catch { /* noop */ }
}

function _getOrCreate(key, opts, ssh2, poolRef) {
  const pool = poolRef || _pool;
  const existing = pool.get(key);
  if (existing && existing.state !== 'closed') {
    existing.justCreated = false;
    _log(`connect reuse ${opts.host}:${opts.port || 22} state=${existing.state} pool=${pool === _termPool ? 'term' : 'exec'}`);
    return existing;
  }

  const authBuilt = _buildAuth(opts);
  if (authBuilt.error) {
    // Return a sentinel "closed-on-arrival" entry whose readyPromise
    // rejects immediately. We do NOT insert it into the pool — there's
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
    poolRef: pool,
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
      _log(`connect fail ${opts.host}:${port} after ${Date.now() - t0}ms: ${c.error} ${c.detail || ''}`);
      if (dbgLines.length) {
        _log(`connect fail ${opts.host}:${port} handshake debug (last ${dbgLines.length}):\n  ` + dbgLines.slice(-40).join('\n  '));
      }
      _dropEntry(key, 'connect_error');
      reject(c);
    };
    const onError = (err) => {
      if (entry.state === 'connecting') failConnect(err);
      else {
        _log(`connect lost ${opts.host}:${port} (pool=${pool === _termPool ? 'term' : 'exec'}): ${(err && err.message) || 'error'}`);
        _dropEntry(key, 'error');
      }
    };
    _log(`connect start ${opts.user}@${opts.host}:${port} (pool=${pool === _termPool ? 'term' : 'exec'})`);
    const onClose = () => {
      // If we never reached ready, treat close as connect failure.
      if (entry.state === 'connecting') failConnect(new Error('Connection lost before handshake'));
      else {
        _log(`connect closed ${opts.host}:${port} (pool=${pool === _termPool ? 'term' : 'exec'})`);
        _dropEntry(key, 'close');
      }
    };
    const onEnd = () => {
      if (entry.state === 'connecting') failConnect(new Error('Connection ended before handshake'));
      else {
        _log(`connect ended ${opts.host}:${port} (pool=${pool === _termPool ? 'term' : 'exec'})`);
        _dropEntry(key, 'end');
      }
    };

    // Keep a permanent error listener for the full client lifetime.
    // ssh2 can emit a late "Connection lost before handshake" from the
    // socket after close/destroy; without a listener that becomes an
    // uncaught exception in Electron's main process.
    client.on('error', onError);
    client.on('close', onClose);
    client.on('end', onEnd);
    if (authBuilt.auth === 'password') {
      // PAM-backed servers (UsePAM yes) verify passwords over the
      // keyboard-interactive method, not 'password'; k-i-only servers
      // reject password-only clients outright. Answer every prompt
      // with the configured password — servers asking for anything
      // else (OTP etc.) simply fail, exactly as with tryKeyboard off.
      client.on('keyboard-interactive', (_name, _instructions, _lang, prompts, finish) => {
        try { finish((Array.isArray(prompts) ? prompts : []).map(() => opts.password)); }
        catch { /* noop */ }
      });
    }
    client.once('ready', () => {
      if (settled) return;
      settled = true;
      entry.state = 'ready';
      entry.connectMs = Date.now() - t0;
      _log(`connect ready ${opts.host}:${port} in ${entry.connectMs}ms${_negotiatedSummary(dbgLines, authBuilt.auth)}`);
      _startIdleTimer(entry);
      resolve();
    });

    // Handshake debug: capture ssh2's debug lines per connection and
    // dump them into the diag log on connect FAILURE only (success
    // stays quiet). This is how a bare "15s timeout" gets a cause —
    // algorithm negotiation failure, auth-method exhaustion, or a
    // silent TCP stall are all visible in the ssh2 debug stream.
    const dbgLines = [];
    const dbg = (line) => {
      dbgLines.push(String(line));
      if (dbgLines.length > 80) dbgLines.shift();
    };

    // Algorithm list: ssh2's pure-JS defaults are modern-only and fail
    // against legacy servers (the "OpenSSH connects but the app does
    // not" class). Extend the runtime-verified DEFAULTS with legacy
    // pure-JS algorithms via append — never enumerate feature-detected
    // ciphers (chacha20/aes-gcm): their support varies between OpenSSL
    // (dev boxes) and Electron's BoringSSL (packaged app), and an
    // unsupported name makes connect() throw for EVERY host.
    const algorithms = {
      kex: {
        append: [
          'diffie-hellman-group-exchange-sha256',
          'diffie-hellman-group16-sha512', 'diffie-hellman-group18-sha512',
          'diffie-hellman-group14-sha256', 'diffie-hellman-group14-sha1',
          'diffie-hellman-group-exchange-sha1', 'diffie-hellman-group1-sha1',
        ],
      },
      serverHostKey: { append: ['ssh-rsa', 'ssh-dss'] },
      cipher: { append: ['aes256-cbc', 'aes192-cbc', 'aes128-cbc', '3des-cbc'] },
      hmac: { append: ['hmac-sha1'] },
    };

    const connectOpts = {
      host:               String(opts.host),
      port,
      username:           String(opts.user),
      readyTimeout:       Math.min(20000, timeoutMs),
      algorithms,
      debug:              dbg,
      // 15s application-level keepalive (was 0 = disabled). A pooled
      // connection with no keepalive dies silently at NAT/firewall idle
      // timeouts — the next op then discovers a half-open socket only
      // via its own (multi-second) timeout, and any attached terminal
      // channel drops without warning. 15s keeps the mapping warm and
      // surfaces dead sockets early.
      keepaliveInterval:  15000,
      keepaliveCountMax:  3,
      // PAM/k-i-only servers need the keyboard-interactive fallback
      // (handler registered above; password auth only).
      tryKeyboard:        authBuilt.auth === 'password',
      ...authBuilt.fields,
    };
    try { client.connect(connectOpts); }
    catch (e) { failConnect(e); }
  });

  pool.set(key, entry);
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

  // Reserve the connection for the op's lifetime (in-flight accounting).
  entry.inflight++;
  _clearIdleTimer(entry);
  const opT0 = Date.now();
  const timeoutMs = opts.timeout_ms ? Number(opts.timeout_ms) : DEFAULT_TIMEOUT_MS;

  return new Promise((resolve) => {
    let settled = false;
    let abortOperation = null;
    const setAbortOperation = (fn) => {
      abortOperation = typeof fn === 'function' ? fn : null;
    };
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
      // preserve_connection_on_timeout exists for ops that SHARE a
      // connection with a long-lived PTY they must not kill. Since the
      // terminal/exec pool split, tmux controls live on the exec pool
      // (no PTY at stake) and deliberately do NOT pass the flag: a
      // timeout drops the whole (possibly zombie) client so the next
      // op reconnects — one sacrificed op instead of a connection that
      // never recovers.
      const preserveConnection = !!opts.preserve_connection_on_timeout && !!abortOperation;
      if (preserveConnection) {
        try { abortOperation(); } catch { /* noop */ }
      }
      finish({ ok: false, error: 'connect_timeout', detail: `op timeout after ${timeoutMs}ms` });
      if (!preserveConnection) _dropEntry(key, 'op_timeout');
    }, timeoutMs);
    if (tm && typeof tm.unref === 'function') tm.unref();

    try {
      op(entry.client, (partial) => { clearTimeout(tm); finish(partial); }, setAbortOperation);
    } catch (e) {
      clearTimeout(tm);
      finish({ ok: false, error: 'exec_failed', detail: e && e.message });
    }
  });
}

async function execRemote(opts) {
  if (_override) return _override(opts);
  // Per-node exec driver: 'system' routes every exec through the OS
  // OpenSSH client (ProxyJump/certificates/GSSAPI come free from
  // ~/.ssh/config). Absent or 'ssh2' = the built-in datapath, byte for
  // byte identical to before. Terminal attach is unaffected either way.
  if (opts && opts.ssh_driver === 'system') {
    try {
      _log(`exec via system-ssh ${opts.user}@${opts.host}:${opts.port || 22}: ${String(opts.command || '').slice(0, 80)}`);
      const r = await _systemSsh.execViaSystemSsh(opts);
      _log(`exec via system-ssh ${opts.host}:${opts.port || 22} ${r.ok ? 'ok' : `failed: ${r.error}`}`);
      return r;
    } catch (e) {
      return { ok: false, error: 'system_ssh_failed', detail: e && e.message || String(e), via: 'system-ssh' };
    }
  }
  const _execT0 = Date.now();
  const _execCmd = String(opts.command || '').slice(0, 80);
  const _execDone = (r) => {
    _log(`exec ${opts.host}:${opts.port || 22} ${r && r.ok ? 'ok' : `failed: ${(r && r.error) || '?'}`} ${Date.now() - _execT0}ms: ${_execCmd}`);
    return r;
  };
  if (!opts || typeof opts.command !== 'string' || !opts.command) {
    return { ok: false, error: 'invalid_args', detail: 'command is required' };
  }
  const run = () => _withPooledClient(opts, (client, finish, setAbortOperation) => {
    client.exec(opts.command, { pty: false }, (err, stream) => {
      if (err) return finish({ ok: false, error: 'exec_failed', detail: err.message });
      setAbortOperation(() => {
        try { stream.close && stream.close(); } catch { /* noop */ }
        try { stream.destroy && stream.destroy(); } catch { /* noop */ }
      });
      let stdout = '';
      let stderr = '';
      stream.on('data', (d) => { stdout += d.toString('utf8'); });
      stream.on('error', (streamErr) => {
        finish({ ok: false, error: 'exec_failed', detail: streamErr && streamErr.message || 'stream error' });
      });
      if (stream.stderr) stream.stderr.on('data', (d) => { stderr += d.toString('utf8'); });
      const input = opts.stdin != null ? opts.stdin : opts.input;
      if (input != null) {
        try {
          stream.write(Buffer.isBuffer(input) ? input : Buffer.from(String(input), 'utf8'));
          stream.end();
        } catch (writeErr) {
          return finish({ ok: false, error: 'exec_failed', detail: writeErr && writeErr.message || 'stdin write failed' });
        }
      }
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
  const first = await run();
  // A tmux control request can time out while the shared terminal PTY is
  // healthy.  Its timeout handler has already closed only that exec channel;
  // retrying through the generic path would drop the pooled client and detach
  // the live terminal that this request was meant to protect.
  const preserveConnectionTimeout = !!opts.preserve_connection_on_timeout
    && first && first.error === 'connect_timeout';
  if (!preserveConnectionTimeout && _isRetryableChannelError(first) && first.timings && first.timings.pooled) {
    const second = await _retryOnceAfterPoolDrop(opts, 'exec_retry_after_channel_error', run);
    if (second && second.timings) second.timings.retried = true;
    return _execDone(second);
  }
  return _execDone(first);
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
  const run = () => _withPooledClient(opts, (client, finish) => {
    client.sftp((err, sftp) => {
      if (err) return finish({ ok: false, error: 'sftp_failed', detail: err.message });
      sftp.writeFile(opts.remotePath, content, (writeErr) => {
        try { sftp.end(); } catch { /* noop */ }
        if (writeErr) return finish({ ok: false, error: 'sftp_write_failed', detail: writeErr.message });
        finish({ ok: true, bytes: content.length, remotePath: opts.remotePath });
      });
    });
  });
  const first = await run();
  if (_isRetryableChannelError(first) && first.timings && first.timings.pooled) {
    const second = await _retryOnceAfterPoolDrop(opts, 'sftp_retry_after_channel_error', run);
    if (second && second.timings) second.timings.retried = true;
    return second;
  }
  if (_isRetryableChannelError(first)) _dropEntryForOpts(opts, 'sftp_channel_error');
  return first;
}

/* List directory entries via SFTP, returning a sorted (dirs-first
 * then name) array of `{name, type:'dir'|'file', size, mtime}`.
 * Used by the Desktop Workspace Browser (CAM-DESK-FILE-010..017).
 * Read-only: never opens a writable handle. */
async function listRemoteFiles(opts) {
  if (_override) return _override({ ...opts, operation: 'listRemoteFiles' });
  if (!opts || typeof opts.remotePath !== 'string' || !opts.remotePath) {
    return { ok: false, error: 'invalid_args', detail: 'remotePath is required' };
  }
  return _withPooledClient(opts, (client, finish) => {
    client.sftp((err, sftp) => {
      if (err) return finish({ ok: false, error: 'sftp_failed', detail: err.message });
      sftp.readdir(opts.remotePath, (readErr, list) => {
        try { sftp.end(); } catch { /* noop */ }
        if (readErr) {
          return finish({
            ok: false,
            error:  readErr.code === 2 ? 'not_found' : 'sftp_readdir_failed',
            detail: readErr.message,
          });
        }
        const entries = (list || []).map(item => {
          const longname = String(item.longname || '');
          const isDir = longname.startsWith('d') ||
            (item.attrs && typeof item.attrs.isDirectory === 'function' && item.attrs.isDirectory());
          const size = (item.attrs && typeof item.attrs.size === 'number') ? item.attrs.size : 0;
          const mtime = (item.attrs && typeof item.attrs.mtime === 'number') ? item.attrs.mtime : null;
          return {
            name: String(item.filename || ''),
            type: isDir ? 'dir' : 'file',
            size: isDir ? 0 : size,
            mtime,
          };
        }).filter(e => e.name && e.name !== '.' && e.name !== '..');
        // Mirror the mobile File Browser sort: directories first,
        // then case-insensitive name order. The frontend sorts again
        // defensively but doing it here keeps a remote call → render
        // pipeline deterministic.
        entries.sort((a, b) => {
          if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
          return a.name.localeCompare(b.name);
        });
        finish({ ok: true, entries });
      });
    });
  });
}

/* Read a remote file via SFTP. Size capped by `maxBytes`
 * (default 5 MiB) to keep the renderer responsive — Workspace
 * Browser is for previewing, not transferring. The returned
 * `Buffer` is small enough to base64-encode safely for binary
 * detection by the caller. */
async function readRemoteFile(opts) {
  if (_override) return _override({ ...opts, operation: 'readRemoteFile' });
  if (!opts || typeof opts.remotePath !== 'string' || !opts.remotePath) {
    return { ok: false, error: 'invalid_args', detail: 'remotePath is required' };
  }
  const maxBytes = Number.isFinite(opts.maxBytes) && opts.maxBytes > 0
    ? Math.min(opts.maxBytes, 50 * 1024 * 1024) // hard ceiling 50 MiB
    : 5 * 1024 * 1024;
  return _withPooledClient(opts, (client, finish) => {
    client.sftp((err, sftp) => {
      if (err) return finish({ ok: false, error: 'sftp_failed', detail: err.message });
      sftp.stat(opts.remotePath, (statErr, stats) => {
        if (statErr) {
          try { sftp.end(); } catch { /* noop */ }
          return finish({
            ok: false,
            error:  statErr.code === 2 ? 'not_found' : 'sftp_stat_failed',
            detail: statErr.message,
          });
        }
        if (stats && stats.isDirectory && stats.isDirectory()) {
          try { sftp.end(); } catch { /* noop */ }
          return finish({ ok: false, error: 'is_directory', detail: 'path is a directory' });
        }
        const size = (stats && typeof stats.size === 'number') ? stats.size : 0;
        if (size > maxBytes) {
          try { sftp.end(); } catch { /* noop */ }
          return finish({
            ok: false,
            error:  'too_large',
            detail: `file is ${size} bytes (max ${maxBytes})`,
            size,
          });
        }
        sftp.readFile(opts.remotePath, (readErr, buf) => {
          try { sftp.end(); } catch { /* noop */ }
          if (readErr) {
            return finish({
              ok:     false,
              error:  readErr.code === 2 ? 'not_found' : 'sftp_read_failed',
              detail: readErr.message,
            });
          }
          finish({ ok: true, content: buf || Buffer.alloc(0), size });
        });
      });
    });
  });
}

function closeAll() {
  for (const k of [..._pool.keys()]) _dropEntry(k, 'closeAll');
  for (const k of [..._termPool.keys()]) _dropEntry(k, 'closeAll');
}

/** Drop only entries with no in-flight work (both pools). Called on OS
 * resume: idle sockets that silently died while the machine slept get
 * discarded so the next op reconnects fresh, while busy connections
 * (live terminals, in-flight execs) are left to detect via their own
 * traffic/keepalive. This is NOT an idle close — nothing is reaped
 * while the machine stays awake (desktop semantics 2026-07-18). */
function dropIdleEntries() {
  for (const [k, e] of [..._pool.entries()]) {
    if (e && !e.inflight) _dropEntry(k, 'resume_idle');
  }
  for (const [k, e] of [..._termPool.entries()]) {
    if (e && !e.inflight) _dropEntry(k, 'resume_idle');
  }
}

function poolStats() {
  // Keys are safe to expose: they contain only host/user/port/auth/key_file
  // plus a SHA-256 digest (never the raw secret). `inflight` and the
  // per-entry connect_ms are useful diagnostics.
  const out = [];
  for (const [k, e] of _pool.entries()) {
    out.push({
      key:        k,
      pool:       'exec',
      state:      e.state,
      inflight:   e.inflight,
      connect_ms: e.connectMs,
    });
  }
  for (const [k, e] of _termPool.entries()) {
    out.push({
      key:        k,
      pool:       'term',
      state:      e.state,
      inflight:   e.inflight,
      connect_ms: e.connectMs,
    });
  }
  return { size: _pool.size + _termPool.size, entries: out, keys: out.map(e => e.key) };
}

/**
 * Open a long-lived PTY exec channel for an interactive attach
 * (CAM-DESK-TERM-001). Unlike execRemote/writeRemoteFile which return
 * after a single roundtrip, this opens a streaming channel:
 *
 *   - A dedicated terminal pool (`_termPool`) is used — one shared
 *     connection per endpoint for ALL terminal channels, isolated from
 *     the exec pool. An exec timeout or exec-connection reset can no
 *     longer kill an attached terminal (tab semantics: switching tabs
 *     must never detach a session). The handshake is still paid only
 *     once per endpoint.
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
  if (_override) return _override({ ...opts, operation: 'openTerminalChannel' }, hooks);
  // Per-node driver: 'system' attaches via the OS ssh client with the
  // same contract ({ok, dispose, write, resize}) as the built-in path.
  if (opts && opts.ssh_driver === 'system') {
    try {
      _log(`attach via system-ssh ${opts.user}@${opts.host}:${opts.port || 22}: ${String(opts.command || '').slice(0, 60)}`);
      const r = await _systemSsh.openViaSystemSsh(opts, hooks);
      _log(`attach via system-ssh ${opts.host}:${opts.port || 22} ${r.ok ? 'ok' : `failed: ${r.error}`}`);
      return r;
    } catch (e) {
      return { ok: false, error: 'system_ssh_failed', detail: e && e.message || String(e), via: 'system-ssh' };
    }
  }
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
  const openTimeoutMs = Math.max(5000, Math.min(120000, Number(opts.timeout_ms) || 15000));

  const ssh2 = _loadSsh2();
  if (!ssh2 || !ssh2.Client) {
    return { ok: false, error: 'exec_failed', detail: `ssh2 module not loadable: ${ssh2 && ssh2._loadError || 'unknown'}` };
  }

  const key = _poolKey(opts);
  // Terminal channels live in the dedicated terminal pool, isolated
  // from exec traffic: an exec timeout/reset on the exec pool can no
  // longer kill an attached terminal (the tab-switch detach).
  let entry = _getOrCreate(key, opts, ssh2, _termPool);

  try {
    await entry.readyPromise;
  } catch (e) {
    // Retry once on handshake/connect failure: drop the (possibly stale)
    // pool entry and reconnect from scratch. On VPN/NAT links the first
    // handshake regularly dies to a transient drop ("Timed out while
    // waiting for handshake") while a fresh TCP connect recovers.
    _dropEntry(key, 'terminal_open_retry');
    entry = _getOrCreate(key, opts, ssh2, _termPool);
    try {
      await entry.readyPromise;
    } catch (e2) {
      return { ok: false, error: e2.error || 'exec_failed', detail: e2.detail || 'connect failed' };
    }
  }

  // Reserve the client for the attach's lifetime (in-flight accounting).
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
      if (entry.inflight === 0 && entry.poolRef.get(key) === entry) {
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
      // A channel open that times out on a supposedly-ready connection
      // means the pooled socket is suspect (half-dead). Drop it so the
      // next attach reconnects instead of reusing the corpse.
      _dropEntry(key, 'open_timeout');
      _log(`channel open TIMEOUT ${opts.host} after ${openTimeoutMs}ms — pool entry dropped`);
      if (wasActive) {
        finishOpen({ ok: false, error: 'exec_timeout', detail: 'terminal channel open timed out' });
      }
    }, openTimeoutMs);
    if (openTimer && typeof openTimer.unref === 'function') openTimer.unref();

    try {
      _log(`channel open ${opts.host}: ${opts.command} (timeout ${openTimeoutMs}ms)`);
      entry.client.exec(opts.command, {
        pty: {
          term: 'xterm-256color',
          cols, rows,
          width: 0, height: 0,
        },
      }, (err, s) => {
        if (err) {
          release();
          // Same rationale as the open timeout: a channel-open error on
          // a ready-pooled connection marks the socket as suspect.
          _dropEntry(key, 'open_failed');
          _log(`channel open FAILED ${opts.host}: ${err && err.message}`);
          return finishOpen({ ok: false, error: 'exec_failed', detail: err.message });
        }
        if (!active) {
          try { if (s && typeof s.destroy === 'function') s.destroy(); } catch { /* noop */ }
          return;
        }
        _log(`channel open ok ${opts.host}`);
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
  listRemoteFiles,
  readRemoteFile,
  openTerminalChannel,
  setOverride,
  setLogger,
  closeAll,
  dropIdleEntries,
  poolStats,
  _setSsh2ForTests,
};
