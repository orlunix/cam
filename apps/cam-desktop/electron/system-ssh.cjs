/* system-ssh.cjs — per-node "System OpenSSH" exec driver.
 *
 * Isolation contract:
 *  - Used ONLY for nodes whose `ssh_driver === 'system'`. Deleting
 *    this file and its single branch in ssh-transport.execRemote
 *    removes the feature wholesale.
 *  - No connection pooling, no shared state beyond one cached probe.
 *  - BatchMode: key/agent/config auth only (ProxyJump, certificates,
 *    IdentityAgent come free from ~/.ssh/config). Password-auth nodes
 *    cannot be served (no non-interactive password without sshpass).
 *  - Scope: exec only (sync/heal/camc ops). Terminal attach stays on
 *    the built-in ssh2 datapath regardless of this setting.
 */

const { spawn } = require('child_process');

const CANDIDATES = process.platform === 'win32'
  ? ['ssh.exe', 'C:\\Windows\\System32\\OpenSSH\\ssh.exe']
  : ['ssh', '/usr/bin/ssh'];

let _probe = null; // cached { available, path, version } | { available:false }

function _run(cmd, args, timeoutMs) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { windowsHide: true });
    let out = '', err = '', settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { child.kill('SIGKILL'); } catch { /* noop */ }
      resolve({ ok: false, error: 'timeout', stdout: out, stderr: err, code: null });
    }, timeoutMs);
    if (timer.unref) timer.unref();
    child.stdout.on('data', (d) => { out += d.toString('utf8'); });
    child.stderr.on('data', (d) => { err += d.toString('utf8'); });
    child.on('error', (e) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok: false, error: 'spawn_failed', detail: e && e.message, stdout: out, stderr: err, code: null });
    });
    child.on('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok: code === 0, stdout: out, stderr: err, code });
    });
  });
}

async function probeSsh() {
  if (_probe) return _probe;
  for (const cmd of CANDIDATES) {
    const r = await _run(cmd, ['-V'], 5000);
    if (r.code === 0 || (r.stderr && /OpenSSH/i.test(r.stderr))) {
      _probe = { available: true, path: cmd, version: String(r.stderr || r.stdout).trim().split('\n')[0] };
      return _probe;
    }
  }
  _probe = { available: false };
  return _probe;
}

/** Execute opts.command on opts.host via the OS ssh client.
 *  Returns the same shape as ssh-transport's execRemote plus `via`. */
async function execViaSystemSsh(opts) {
  const probe = await probeSsh();
  if (!probe.available) {
    return { ok: false, error: 'system_ssh_unavailable', detail: 'no usable system ssh client found', via: 'system-ssh' };
  }
  const port = opts.port || 22;
  const timeoutMs = Math.max(5000, Math.min(120000, Number(opts.timeout_ms) || 30000));
  const args = [
    '-o', 'BatchMode=yes',
    '-o', `ConnectTimeout=${Math.ceil(Math.min(timeoutMs, 20000) / 1000)}`,
    '-o', 'ServerAliveInterval=0',
    '-p', String(port),
  ];
  // Key-auth nodes: pass the configured identity file through (Windows
  // paths are fine for the Windows OpenSSH client). Password auth is
  // impossible in BatchMode — caller surfaces a clear error.
  if ((opts.auth_method === 'key' || (!opts.auth_method && opts.key_file)) && opts.key_file) {
    args.push('-i', String(opts.key_file));
  }
  if (opts.auth_method === 'password') {
    return { ok: false, error: 'system_ssh_password_unsupported', detail: 'System OpenSSH driver cannot do password auth non-interactively (BatchMode). Use key auth, or set this node back to the built-in driver.', via: 'system-ssh' };
  }
  args.push(`${opts.user}@${opts.host}`, opts.command);
  const r = await _run(probe.path, args, timeoutMs + 5000);
  const res = {
    via: 'system-ssh',
    stdout: r.stdout || '',
    stderr: r.stderr || '',
    code: r.code,
  };
  if (r.ok) return { ok: true, ...res };
  return {
    ok: false,
    error: r.error === 'timeout' ? 'connect_timeout' : (r.code != null ? 'remote_nonzero' : (r.error || 'exec_failed')),
    detail: String(r.stderr || r.stdout || r.detail || '').trim().slice(0, 400),
    ...res,
  };
}

module.exports = { execViaSystemSsh, probeSsh };
