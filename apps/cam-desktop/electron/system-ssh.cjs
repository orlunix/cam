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

function _missingDetail() {
  return process.platform === 'win32'
    ? 'No OpenSSH client found on this machine. Install it via Settings → Apps → Optional features → OpenSSH Client, or switch this node back to the built-in driver.'
    : 'No ssh client found in PATH on this machine. Install OpenSSH, or switch this node back to the built-in driver.';
}

/** Map OS-ssh stderr to a clear, actionable error. Distinguishes:
 *  auth failure / dns / refused / timeout / unreachable / host-key /
 *  generic remote failure (with the stderr tail attached). */
function _classifySystemSshError(r) {
  const s = String(r.stderr || r.stdout || '');
  const tail = s.trim().split('\n').slice(-3).join(' ').slice(0, 300);
  const map = [
    [/Permission denied/i,                 'auth_failed',       'authentication failed — key/agent rejected by the server'],
    [/Could not resolve hostname|Name or service not known|Temporary failure in name resolution/i, 'dns_failure', 'DNS resolution failed — check VPN/network'],
    [/Connection refused/i,                'connect_refused',   'connection refused — sshd not listening on this port'],
    [/Connection timed out|Operation timed out|connect to host .* timed out/i, 'connect_timeout', 'connection timed out — host unreachable (VPN/network?)'],
    [/No route to host/i,                  'connect_unreachable','no route to host — check VPN/network'],
    [/Host key verification failed/i,      'host_key_failed',   'host key verification failed — accept the key in ~/.ssh/known_hosts or check for MITM'],
    [/Operation not permitted|Permission denied \(publickey/i, 'auth_failed', 'authentication failed'],
  ];
  for (const [re, error, label] of map) {
    if (re.test(s)) return { error, detail: `${label}. ssh: ${tail}` };
  }
  if (r.error === 'timeout') {
    return { error: 'connect_timeout', detail: `system ssh timed out after the configured budget. ${tail}` };
  }
  if (r.code != null && r.code !== 0) {
    return { error: 'remote_nonzero', detail: `remote command exited with code ${r.code}${tail ? `. ssh: ${tail}` : ''}` };
  }
  return { error: r.error || 'exec_failed', detail: tail || (r.detail || 'system ssh failed') };
}

/** Execute opts.command on opts.host via the OS ssh client.
 *  Returns the same shape as ssh-transport's execRemote plus `via`. */
async function execViaSystemSsh(opts) {
  const probe = await probeSsh();
  if (!probe.available) {
    return { ok: false, error: 'system_ssh_unavailable', detail: _missingDetail(), via: 'system-ssh' };
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
  const classified = _classifySystemSshError(r);
  return { ok: false, error: classified.error, detail: classified.detail, ...res };
}

module.exports = { execViaSystemSsh, probeSsh };
