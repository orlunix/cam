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

/** PTY attach via the OS ssh client (spawn `ssh -tt`).
 *
 *  Size model: the remote pty starts at 80x24 because our child has no
 *  local tty, so the command is prefixed with `stty rows R cols C` to
 *  set the correct winsize before the attach starts. Live resize is
 *  not possible through a child process (no window-change API), so
 *  resize() kills and respawns the child with the new size — debounced
 *  and silent: the tmux session survives, only the attach view
 *  reconnects.
 */
async function openViaSystemSsh(opts, hooks = {}) {
  const probe = await probeSsh();
  if (!probe.available) {
    return { ok: false, error: 'system_ssh_unavailable', detail: _missingDetail(), via: 'system-ssh' };
  }
  if (opts.auth_method === 'password') {
    return { ok: false, error: 'system_ssh_password_unsupported', detail: 'System OpenSSH driver cannot do password auth non-interactively (BatchMode). Use key auth, or switch this node back to the built-in driver.', via: 'system-ssh' };
  }
  const onData  = typeof hooks.onData === 'function' ? hooks.onData : () => {};
  const onClose = typeof hooks.onClose === 'function' ? hooks.onClose : () => {};
  let cols = Math.max(2, Math.min(500, Number(hooks.cols) || 80));
  let rows = Math.max(2, Math.min(500, Number(hooks.rows) || 24));

  let child = null;
  let disposed = false;
  let opened = false;
  let resizeTimer = null;

  const args = (c, r) => {
    const a = [
      '-tt',
      '-o', 'BatchMode=yes',
      '-o', `ConnectTimeout=${Math.ceil(Math.min(Number(opts.timeout_ms) || 15000, 20000) / 1000)}`,
      '-p', String(opts.port || 22),
    ];
    if ((opts.auth_method === 'key' || (!opts.auth_method && opts.key_file)) && opts.key_file) {
      a.push('-i', String(opts.key_file));
    }
    a.push(`${opts.user}@${opts.host}`, `stty rows ${r} cols ${c}; exec ${opts.command}`);
    return a;
  };

  const spawnAttach = (c, r) => new Promise((resolve) => {
    const ch = spawn(probe.path, ['-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', ...args(c, r)], { windowsHide: true });
    // Per-child close suppression: a shared boolean races with the async
    // close event (kill() returns before 'close' fires), which made
    // renderer auto-reconnect fire alongside our own respawn — two
    // children on one tmux session, seen as instability. Tag the child.
    ch._camSuppress = false;
    child = ch;
    let openedHere = false;
    const openTimer = setTimeout(() => {
      if (!openedHere) {
        try { ch.kill('SIGKILL'); } catch { /* noop */ }
        resolve({ ok: false, error: 'connect_timeout', detail: 'system ssh attach open timed out', via: 'system-ssh' });
      }
    }, Math.max(5000, Math.min(60000, Number(opts.timeout_ms) || 15000)));
    if (openTimer.unref) openTimer.unref();
    ch.stdout.on('data', (d) => {
      if (disposed) return;
      if (!openedHere) { openedHere = true; opened = true; clearTimeout(openTimer); resolve({ ok: true }); }
      onData(d);
    });
    ch.stderr.on('data', (d) => { if (!disposed) onData(d); });
    ch.on('error', (e) => {
      if (!openedHere) { clearTimeout(openTimer); resolve({ ok: false, error: 'spawn_failed', detail: e && e.message, via: 'system-ssh' }); }
      else if (!disposed) onClose({ code: null, signal: null, error: e && e.message });
    });
    ch.on('close', (code, signal) => {
      if (!openedHere) { clearTimeout(openTimer); resolve({ ok: false, error: 'exec_failed', detail: `ssh exited before attach opened (code ${code})`, via: 'system-ssh' }); return; }
      if (ch._camSuppress) return; // internal respawn — not a real drop
      if (!disposed) onClose({ code: typeof code === 'number' ? code : null, signal: signal || null });
    });
  });

  const first = await spawnAttach(cols, rows);
  if (!first.ok) return first;

  return {
    ok: true,
    via: 'system-ssh',
    dispose() {
      disposed = true;
      if (resizeTimer) { clearTimeout(resizeTimer); resizeTimer = null; }
      try { child && child.kill('SIGKILL'); } catch { /* noop */ }
    },
    write(buf) {
      try { return child && child.stdin && child.stdin.write(buf); } catch { return false; }
    },
    resize(c, r) {
      const nc = Math.max(2, Math.min(500, c | 0));
      const nr = Math.max(2, Math.min(500, r | 0));
      if (disposed || !opened) return false;
      // The renderer refits generously (tab switch, focus, pane layout)
      // and often reports the SAME or ±1 size — respawning on every
      // jitter made system-attach look like it was always reconnecting
      // (repro: Settings → Diagnostics → back to agent). tmux absorbs
      // ±1 gracefully, so only respawn on a real change.
      if (nc === cols && nr === rows) return true;
      if (Math.abs(nc - cols) < 2 && Math.abs(nr - rows) < 2) return true;
      cols = nc;
      rows = nr;
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(async () => {
        resizeTimer = null;
        if (disposed) return;
        const old = child;
        if (old) old._camSuppress = true;
        try { old && old.kill('SIGKILL'); } catch { /* noop */ }
        await spawnAttach(cols, rows);
      }, 600);
      return true;
    },
  };
}

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

module.exports = { execViaSystemSsh, openViaSystemSsh, probeSsh };
