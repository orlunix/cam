/* Terminal channel for cam-container — a WS port of the desktop's term:*
 * IPC stack (apps/cam-desktop/electron/main.cjs:283-1032). Copied, not
 * shared: main.cjs must stay byte-stable for the desktop app. The three
 * adaptations:
 *   - session ownership: event.sender.id (Electron webContents) → the WS
 *     connection object; a disconnect closes everything it owned
 *   - push: sender.send('term:data'/'term:status') → ws frames
 *     { push: 'term:data'|'term:status', ... }
 *   - logging: _diagLog → the server log rail
 * All tmux/SSH semantics (discovery gates, ready-gate, size repair,
 * fallback attach) are verbatim from main.cjs — the hard-won comments
 * travel with the code. Keep in sync manually until a shared-module
 * refactor is worth the desktop regression risk.
 */

import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const _require = createRequire(import.meta.url);
const ELECTRON_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'cam-desktop', 'electron');
const {
  tmuxMetadataForAgent, selectOnlyClient, selectNewClient,
  parseClientState, parseWindowRows, tmuxCommand,
} = _require(path.join(ELECTRON_DIR, 'tmux-controls.cjs'));

const TERM_MIN_COLS = 40;
const TERM_MIN_ROWS = 4;
const TMUX_CLIENT_RECOVERY_MAX_ATTEMPTS = 10;
const TMUX_CLIENT_RECOVERY_RETRY_DELAY_MS = 30000;
const TERM_READY_FALLBACK_MS = 2000;
const TERM_READY_BUFFER_CAP = 256 * 1024;
const ATTACH_FALLBACK_ERRORS = new Set(['connect_timeout', 'connect_refused', 'dns_failure', 'connect_lost', 'auth_failed']);

function _terminalOpenSize(payload = {}) {
  const rawCols = Number(payload.cols);
  const rawRows = Number(payload.rows);
  return {
    cols: Math.max(TERM_MIN_COLS, Math.min(500, Number.isFinite(rawCols) && rawCols >= TERM_MIN_COLS ? rawCols : 80)),
    rows: Math.max(TERM_MIN_ROWS, Math.min(500, Number.isFinite(rawRows) && rawRows >= TERM_MIN_ROWS ? rawRows : 24)),
  };
}

function _terminalResizeSize(payload = {}) {
  const rawCols = Number(payload.cols);
  const rawRows = Number(payload.rows);
  if (!Number.isFinite(rawCols) || !Number.isFinite(rawRows)) return null;
  if (rawCols < TERM_MIN_COLS || rawRows < TERM_MIN_ROWS) return null;
  return { cols: Math.min(500, rawCols), rows: Math.min(500, rawRows) };
}

function _shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

/* The remote terminal-size repair (tmux 2.7-safe: control-mode client +
 * refresh-client -C, since resize-window does not exist there). Also
 * detaches tiny (<40x4) clients that clamp the shared tmux window —
 * the "window shrank to ~10 cols" mystery. Verbatim from main.cjs. */
function _terminalRepairCommand(agentId, cols, rows) {
  const safeCols = Math.max(TERM_MIN_COLS, Math.min(500, Math.floor(Number(cols) || 80)));
  const safeRows = Math.max(TERM_MIN_ROWS, Math.min(500, Math.floor(Number(rows) || 24)));
  const py = String.raw`import json
import os
import subprocess
import sys

agent_id = sys.argv[1]
cols = max(40, min(500, int(sys.argv[2])))
rows = max(4, min(500, int(sys.argv[3])))
camc = os.path.expanduser("~/.cam/camc")

try:
    raw = subprocess.check_output(
        [camc, "--json", "status", agent_id],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
    data = json.loads(raw or "{}")
except Exception:
    sys.exit(0)

if isinstance(data, list):
    data = data[0] if data else {}
if not isinstance(data, dict):
    sys.exit(0)

socket = data.get("tmux_socket") or data.get("socket")
session = data.get("tmux_session") or data.get("session") or data.get("tmux_name")
if not socket or not session:
    sys.exit(0)

# Resolve the tmux binary the same way camc does: the agent record's
# tmux_bin (the binary that started the server) first, then the
# environment's tmux via PATH — never a blind /bin/tmux assumption.
tmux_bin = (
    data.get("tmux_bin")
    or (data.get("runtime") or {}).get("tmux", {}).get("bin")
    or ""
)
if not tmux_bin:
    try:
        import shutil
        tmux_bin = shutil.which("tmux") or "tmux"
    except Exception:
        tmux_bin = "tmux"

try:
    out = subprocess.check_output(
        [tmux_bin, "-S", str(socket), "list-clients", "-F", "#{client_name}\t#{client_width}\t#{client_height}"],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=3,
    )
except Exception:
    out = ""

for line in out.splitlines():
    parts = line.split("\t")
    if len(parts) != 3:
        continue
    name, width, height = parts
    try:
        if int(width) < 40 or int(height) < 4:
            subprocess.run(
                [tmux_bin, "-S", str(socket), "detach-client", "-t", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            print(f"REPAIR_DETACHED tiny-client name={name} size={width}x{height}")
    except Exception:
        pass

# tmux 2.7 (main PDX environment) has no resize-window command. Use a
# short-lived control-mode client and set its size; tmux then propagates that
# size to the session/window. This also works on newer tmux versions.
try:
    proc = subprocess.Popen(
        [tmux_bin, "-S", str(socket), "-C", "attach-session", "-t", str(session)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        proc.communicate(f"refresh-client -C {cols},{rows}\n", timeout=1)
    except subprocess.TimeoutExpired:
        try:
            proc.stdin.write("detach-client\n")
            proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.communicate(timeout=2)
        except Exception:
            proc.kill()
except Exception:
    pass
`;
  // No heredoc: the exec channel is interpreted by the remote LOGIN
  // shell, which may be csh/tcsh where `<<` is a syntax error.
  return {
    command: `python3 - ${_shellQuote(agentId)} ${_shellQuote(String(safeCols))} ${_shellQuote(String(safeRows))}`,
    stdin: py,
  };
}

/** Per-channel streaming UTF-8 decoder (Tabby UTF8SplitterMiddleware
 *  equivalent): multibyte chars split across transport chunks must not
 *  decode to ''. */
function _utf8Stream() {
  const dec = new TextDecoder('utf-8');
  return {
    decode: (buf) => dec.decode(buf, { stream: true }),
    flush: () => dec.decode(),
  };
}

export function createTermChannel({ embeddedHub, sshTransport, log }) {
  const _terminals = new Map();       // sessionId → entry
  const _termReady = new Map();       // sessionId → ready gate
  const _tmuxDiscoveryTails = new Map();
  const _tmuxDiscoveryReleases = new Set();
  const _tmuxBinProbes = new Map();   // endpointKey → Promise<string>
  let _termSeq = 0;

  /* ── ws plumbing ── */
  const _wsAlive = (ws) => ws && ws.readyState === 1; // OPEN
  const _send = (ws, msg) => { if (_wsAlive(ws)) { try { ws.send(JSON.stringify(msg)); } catch (_) {} } };

  function _rawTermSend(sessionId, ws, data) {
    _send(ws, { push: 'term:data', sessionId, data });
  }

  /* ── ready gate (verbatim semantics from main.cjs) ── */
  function _termGateOpen(sessionId, ws) {
    const gate = { ready: false, chunks: [], bytes: 0, ws, timer: null };
    gate.timer = setTimeout(() => {
      gate.ready = true; // renderer never signalled — flush anyway
      _termGateFlush(sessionId, gate);
    }, TERM_READY_FALLBACK_MS);
    if (gate.timer.unref) gate.timer.unref();
    _termReady.set(sessionId, gate);
  }
  function _termGateSend(sessionId, ws, data) {
    const gate = _termReady.get(sessionId);
    if (!gate || gate.ready) return _rawTermSend(sessionId, ws, data);
    gate.ws = ws;
    gate.chunks.push(data);
    gate.bytes += data.length;
    while (gate.bytes > TERM_READY_BUFFER_CAP && gate.chunks.length > 1) {
      gate.bytes -= gate.chunks.shift().length;
    }
  }
  function _termGateFlush(sessionId, gate) {
    if (gate.timer) { clearTimeout(gate.timer); gate.timer = null; }
    for (const chunk of gate.chunks) _rawTermSend(sessionId, gate.ws, chunk);
    gate.chunks = [];
    gate.bytes = 0;
  }
  function _termGateClose(sessionId) {
    const gate = _termReady.get(sessionId);
    if (!gate) return;
    gate.ready = true;
    _termGateFlush(sessionId, gate);
    _termReady.delete(sessionId);
  }

  /* ── session bookkeeping (ownership = the WS connection) ── */
  const _owned = (ws, payload) => {
    const ent = _terminals.get(String(payload && payload.sessionId || ''));
    return (ent && ent.owner === ws) ? ent : null;
  };
  function _sessionForAgent(ws, agentId) {
    for (const [sid, ent] of _terminals) {
      if (ent && ent.owner === ws && ent.agentId === agentId) return [sid, ent];
    }
    return [null, null];
  }
  function _dropSession(sessionId) {
    const ent = _terminals.get(sessionId);
    if (!ent) return;
    _terminals.delete(sessionId);
    try { ent.dispose && ent.dispose(); } catch (_) {}
  }

  /* ── tmux helpers (verbatim from main.cjs) ── */
  function _probeRemoteTmuxBin(opts) {
    const key = `${opts.host}|${opts.user}|${opts.port || 22}`;
    if (_tmuxBinProbes.has(key)) return _tmuxBinProbes.get(key);
    const p = (async () => {
      try {
        const res = await sshTransport.execRemote({
          ...opts,
          command: "/bin/sh -c 'command -v tmux || command -v /bin/tmux'",
          timeout_ms: 15000,
        });
        const first = res && res.ok ? String(res.stdout || '').split('\n')[0].trim() : '';
        return /^\/[^\s]+$/.test(first) ? first : '';
      } catch (_) { return ''; }
    })();
    _tmuxBinProbes.set(key, p);
    return p;
  }

  function _beginTmuxDiscovery(sessionKey) {
    const previous = _tmuxDiscoveryTails.get(sessionKey) || Promise.resolve();
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const tail = previous.then(() => gate);
    _tmuxDiscoveryTails.set(sessionKey, tail);
    _tmuxDiscoveryReleases.add(release);
    return previous.then(() => () => {
      _tmuxDiscoveryReleases.delete(release);
      release();
      if (_tmuxDiscoveryTails.get(sessionKey) === tail) _tmuxDiscoveryTails.delete(sessionKey);
    });
  }

  function _resetTmuxDiscovery() {
    for (const release of [..._tmuxDiscoveryReleases]) {
      try { release(); } catch (_) {}
    }
    _tmuxDiscoveryReleases.clear();
    _tmuxDiscoveryTails.clear();
  }

  async function _tmuxExec(ent, args) {
    if (!ent?.tmux || !ent?.opts) return { ok: false, error: 'tmux_unavailable', detail: 'tmux metadata unavailable' };
    let command;
    try { command = tmuxCommand(ent.tmux, args); }
    catch (e) { return { ok: false, error: 'tmux_unavailable', detail: e.message }; }
    const res = await sshTransport.execRemote({ ...ent.opts, command, timeout_ms: 15000 });
    if (!res?.ok) {
      const t = res && res.timings ? res.timings : {};
      console.warn(`[tmux-exec] ${ent.agentId} ${args[0]} failed: ${res && res.error} | pooled=${t.pooled} connect=${t.connect_ms}ms op=${t.op_ms}ms total=${t.total_ms}ms | ${String(res && res.detail || '').slice(0, 160)}`);
    }
    return res;
  }

  function _tmuxFailure(stage, result, ent) {
    const baseDetail = String(result?.detail || result?.error || 'tmux control operation failed');
    const lastProbeError = String(ent?.tmuxLastProbeError || '');
    return {
      ok: false,
      stage,
      error: result?.error || 'tmux_unavailable',
      detail: lastProbeError && !baseDetail.includes(lastProbeError)
        ? `${baseDetail}; last client probe: ${lastProbeError}`
        : baseDetail,
      diagnostics: {
        session: String(ent?.tmux?.session || ''),
        socket: String(ent?.tmux?.socket || ''),
        clientTty: String(ent?.tmuxClientTty || ''),
        initialPending: !!ent?.tmuxInitialDiscoveryPending,
        recoveryAttempts: Number(ent?.tmuxClientRecoveryAttempts || 0),
        beforeClientCount: ent?.tmuxBeforeClients instanceof Set ? ent.tmuxBeforeClients.size : null,
        lastProbeError,
        lastProbeMs: Number(ent?.tmuxLastProbeMs || 0),
      },
    };
  }

  async function _tmuxClientSet(opts, tmux, diagnosticsTarget = null) {
    const started = Date.now();
    const record = (error = '') => {
      if (!diagnosticsTarget || typeof diagnosticsTarget !== 'object') return;
      diagnosticsTarget.tmuxLastProbeError = String(error || '');
      diagnosticsTarget.tmuxLastProbeMs = Date.now() - started;
    };
    let command;
    try { command = tmuxCommand(tmux, ['list-clients', '-t', tmux.session, '-F', '#{client_tty}']); }
    catch (e) { record(e?.message || e); return null; }
    const result = await sshTransport.execRemote({ ...opts, command, timeout_ms: 15000 });
    if (!result?.ok) {
      const t = result && result.timings ? result.timings : {};
      console.warn(`[tmux-probe] list-clients failed: ${result && result.error} | pooled=${t.pooled} connect=${t.connect_ms}ms op=${t.op_ms}ms total=${t.total_ms}ms`);
      record(`${result?.error || 'probe_failed'}: ${result?.detail || 'no detail'}`);
      return null;
    }
    record('');
    return new Set(String(result.stdout || '').split(/\r?\n/).filter((tty) => /^\/dev\/pts\/\d+$/.test(tty)));
  }

  async function _discoverTmuxClient(sessionId, beforeClients) {
    const ent = _terminals.get(sessionId);
    if (!ent?.tmux) return;
    try {
      for (let attempt = 0; attempt < 15; attempt++) {
        const afterClients = await _tmuxClientSet(ent.opts, ent.tmux, ent);
        const tty = afterClients && (beforeClients
          ? selectNewClient(beforeClients, afterClients)
          : selectOnlyClient(afterClients));
        if (tty) {
          ent.tmuxClientTty = tty;
          ent.tmuxControlError = '';
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      ent.tmuxControlError = 'Could not identify this tmux client';
    } finally {
      ent.tmuxInitialDiscoveryPending = false;
    }
  }

  async function _retryTmuxClientDiscovery(ent) {
    if (!ent?.tmux || ent.tmuxClientTty || ent.tmuxInitialDiscoveryPending
        || ent.tmuxClientRecoveryInFlight || Date.now() < (ent.tmuxClientRecoveryNextAt || 0)) {
      return false;
    }
    ent.tmuxClientRecoveryInFlight = true;
    ent.tmuxClientRecoveryAttempts += 1;
    try {
      const afterClients = await _tmuxClientSet(ent.opts, ent.tmux, ent);
      const tty = afterClients && (ent.tmuxBeforeClients
        ? selectNewClient(ent.tmuxBeforeClients, afterClients)
        : selectOnlyClient(afterClients));
      if (tty) {
        ent.tmuxClientTty = tty;
        ent.tmuxControlError = '';
        return true;
      }
      if (ent.tmuxClientRecoveryAttempts >= TMUX_CLIENT_RECOVERY_MAX_ATTEMPTS) {
        ent.tmuxClientRecoveryAttempts = 0;
        ent.tmuxClientRecoveryNextAt = Date.now() + TMUX_CLIENT_RECOVERY_RETRY_DELAY_MS;
        ent.tmuxControlError = 'Waiting to retry tmux client discovery';
        return false;
      }
      ent.tmuxControlError = 'Waiting for tmux client discovery';
      return false;
    } finally {
      ent.tmuxClientRecoveryInFlight = false;
    }
  }

  // Window listing and pane state depend only on the session, not the
  // attached client's tty: the display-message probe targets the session
  // (the `-p -t <session>` form — `-p -c <client>` is rejected by tmux
  // < 3.3). A missing tty must not fail the tab strip.
  async function _tmuxClientState(ent) {
    if (!ent?.tmux) return _tmuxFailure('client_discovery', {
      error: 'tmux_unavailable',
      detail: 'tmux metadata unavailable',
    }, ent);
    if (!ent.tmux.bin) return _tmuxFailure('client_discovery', {
      error: 'tmux_unavailable',
      detail: 'no tmux binary recorded or discoverable on this endpoint',
    }, ent);
    if (!ent.tmuxClientTty) void _retryTmuxClientDiscovery(ent).catch(() => {});
    const result = await _tmuxExec(ent, ['display-message', '-p', '-t', ent.tmux.session, '#{window_index}:#{pane_id}:#{pane_in_mode}']);
    if (!result?.ok) return _tmuxFailure('client_state', result, ent);
    const state = parseClientState(result.stdout);
    return state ? { ok: true, ...state } : _tmuxFailure('client_state', { error: 'tmux_parse_failed', detail: 'invalid client state' }, ent);
  }

  // switch-client genuinely needs the attached client's tty.
  async function _ensureTmuxClientTty(ent) {
    if (!ent?.tmuxClientTty) await _retryTmuxClientDiscovery(ent);
    if (!ent?.tmuxClientTty) return _tmuxFailure('client_discovery', {
      error: 'tmux_unavailable',
      detail: ent?.tmuxControlError || 'tmux client discovery pending',
    }, ent);
    return { ok: true };
  }

  async function _repairRemoteTerminalSize(opts, agentId, cols, rows) {
    if (!opts || !agentId) return;
    try {
      const repair = _terminalRepairCommand(agentId, cols, rows);
      const res = await sshTransport.execRemote({
        ...opts,
        command: repair.command,
        stdin: repair.stdin,
        timeout_ms: 8000,
      });
      const out = (res && res.ok && res.stdout) || '';
      for (const line of String(out).split('\n')) {
        if (line.startsWith('REPAIR_DETACHED')) {
          console.warn(`[terminal-repair] ${agentId}: ${line}`);
          log(`[terminal-repair] ${agentId}: ${line}`);
        }
      }
    } catch (_) { /* best-effort: attach proceeds even if repair no-ops */ }
  }

  /* ── ops (frame handlers) ── */

  async function open(ws, payload = {}) {
    const agentId = String(payload && payload.agentId || '');
    const { cols, rows } = _terminalOpenSize(payload);
    if (!agentId) return { ok: false, error: 'invalid_args', detail: 'agentId is required' };

    // Warm-session reuse (renderer fast switch shows its cached xterm).
    const [existingSid, existingEnt] = _sessionForAgent(ws, agentId);
    if (existingSid) {
      const sizeChanged = !!(existingEnt && (existingEnt._appliedCols !== cols || existingEnt._appliedRows !== rows));
      if (sizeChanged) {
        try {
          existingEnt.resize && existingEnt.resize(cols, rows);
          existingEnt._appliedCols = cols;
          existingEnt._appliedRows = rows;
        } catch (_) {}
        if (existingEnt.opts) void _repairRemoteTerminalSize(existingEnt.opts, agentId, cols, rows);
      }
      return { ok: true, sessionId: existingSid, reused: true };
    }

    const tAttach0 = Date.now();
    log(`[attach] ${agentId}: getAttachConnectOpts begin`);
    const resolved = await embeddedHub.getAttachConnectOpts(agentId);
    if (!resolved.ok) {
      log(`[attach] ${agentId}: getAttachConnectOpts failed after ${Date.now() - tAttach0}ms: ${resolved.error} ${resolved.detail || ''}`);
      return { ok: false, error: resolved.error, detail: resolved.detail };
    }
    log(`[attach] ${agentId}: getAttachConnectOpts ok after ${Date.now() - tAttach0}ms host=${resolved.opts && resolved.opts.host}`);

    // `~/.cam/camc attach <agent>` is the attach contract.
    const command = resolved.command || `~/.cam/camc attach ${agentId}`;
    const sessionId = `t${++_termSeq}-${Date.now().toString(36)}`;
    const tmux = tmuxMetadataForAgent(resolved.agent);
    if (tmux && !tmux.bin) {
      // Older agent records carry no tmux_bin: probe once per endpoint.
      tmux.bin = await _probeRemoteTmuxBin(resolved.opts);
    }
    if (tmux && !tmux.bin) {
      console.warn(`[tmux-probe] no tmux binary found on ${resolved.opts.host}; window controls disabled`);
    }
    // Bounded discovery-queue wait (15s), then proceed without ordering.
    const discoveryKey = `${resolved.opts.host}|${tmux ? tmux.socket : ''}`;
    const discoveryBeginP = tmux && tmux.bin ? _beginTmuxDiscovery(discoveryKey) : null;
    const releaseTmuxDiscovery = discoveryBeginP
      ? await Promise.race([discoveryBeginP, new Promise((r) => setTimeout(() => r(null), 15000))])
      : null;
    if (discoveryBeginP && !releaseTmuxDiscovery) {
      log(`[attach] ${agentId}: tmux discovery queue wait exceeded 15s — proceeding without ordering`);
      void discoveryBeginP.then((releaseFn) => { try { if (releaseFn) releaseFn(); } catch (_) {} });
    }
    const initialTmuxProbe = {};

    // Baseline client probe runs in PARALLEL with the channel open.
    const beforeClientsP = tmux && tmux.bin
      ? _tmuxClientSet(resolved.opts, tmux, initialTmuxProbe)
      : Promise.resolve(null);

    const tOpen0 = Date.now();
    const utf8 = _utf8Stream();
    const hooks = {
      cols, rows,
      onData: (buf) => {
        if (!_wsAlive(ws)) { _dropSession(sessionId); return; }
        _termGateSend(sessionId, ws, utf8.decode(buf));
      },
      onClose: ({ code, signal }) => {
        const tail = utf8.flush();
        if (tail) _termGateSend(sessionId, ws, tail);
        _termGateClose(sessionId);
        _send(ws, { push: 'term:status', sessionId, kind: 'closed', code, signal });
        _terminals.delete(sessionId);
      },
    };
    log(`[attach] ${agentId}: openTerminalChannel begin host=${resolved.opts.host}`);
    let ch = await sshTransport.openTerminalChannel({ ...resolved.opts, command }, hooks);
    log(`[attach] ${agentId}: openTerminalChannel primary ${ch.ok ? 'ok' : 'failed ' + ch.error + ' ' + (ch.detail || '')} after ${Date.now() - tOpen0}ms`);
    if (!ch.ok && resolved.fallbackOpts && ATTACH_FALLBACK_ERRORS.has(ch.error)) {
      console.warn(`[cam-container] attach via context host ${resolved.opts.host} failed (${ch.error}); retrying via agent machine fields (${resolved.fallbackOpts.host})`);
      log(`[attach] context-host ${resolved.opts.host} failed (${ch.error}); retry via machine fields ${resolved.fallbackOpts.host}`);
      ch = await sshTransport.openTerminalChannel({ ...resolved.fallbackOpts, command }, hooks);
      log(`[attach] ${agentId}: openTerminalChannel fallback ${ch.ok ? 'ok' : 'failed ' + ch.error}`);
    }
    const openMs = Date.now() - tOpen0;

    if (!ch.ok) {
      if (releaseTmuxDiscovery) releaseTmuxDiscovery();
      return { ok: false, error: ch.error, detail: ch.detail };
    }
    const beforeClients = await beforeClientsP;
    _terminals.set(sessionId, {
      dispose: ch.dispose,
      write:   ch.write,
      resize:  ch.resize,
      owner:   ws,
      agentId,
      _appliedCols: cols,
      _appliedRows: rows,
      opts: resolved.opts,
      tmux,
      tmuxClientTty: '',
      tmuxControlError: tmux ? '' : 'tmux metadata unavailable',
      tmuxBeforeClients: beforeClients,
      tmuxInitialDiscoveryPending: !!tmux,
      tmuxClientRecoveryAttempts: 0,
      tmuxClientRecoveryNextAt: 0,
      tmuxClientRecoveryInFlight: false,
      tmuxLastProbeError: initialTmuxProbe.tmuxLastProbeError || '',
      tmuxLastProbeMs: initialTmuxProbe.tmuxLastProbeMs || 0,
    });
    // Post-open housekeeping never adds to the attach latency.
    void _repairRemoteTerminalSize(resolved.opts, agentId, cols, rows);
    if (releaseTmuxDiscovery) void _discoverTmuxClient(sessionId, beforeClients).finally(releaseTmuxDiscovery);
    _termGateOpen(sessionId, ws);
    return { ok: true, sessionId, timings: { open_ms: openMs } };
  }

  function ready(ws, payload = {}) {
    const gate = _termReady.get(String(payload && payload.sessionId || ''));
    if (!gate) return { ok: true, ignored: true };
    gate.ready = true;
    _termGateFlush(String(payload.sessionId), gate);
    return { ok: true };
  }

  function input(ws, payload = {}) {
    const ent = _owned(ws, payload);
    if (!ent) return { ok: false, error: 'not_found' };
    ent.write(String(payload.data == null ? '' : payload.data));
    return { ok: true };
  }

  async function resize(ws, payload = {}) {
    const ent = _owned(ws, payload);
    if (!ent) return { ok: false, error: 'not_found' };
    const size = _terminalResizeSize(payload);
    if (!size) return { ok: true, ignored: true, reason: 'invalid_terminal_size' };
    // Skip redundant setWindow round trips (each one redraws the pane).
    if (ent._appliedCols !== size.cols || ent._appliedRows !== size.rows) {
      ent.resize(size.cols, size.rows);
      ent._appliedCols = size.cols;
      ent._appliedRows = size.rows;
    }
    return { ok: true };
  }

  function close(ws, payload = {}) {
    const sid = String(payload && payload.sessionId || '');
    const ent = _owned(ws, payload);
    if (!ent) return { ok: false, error: 'not_found' };
    _dropSession(sid);
    return { ok: true };
  }

  async function listWindows(ws, payload = {}) {
    const ent = _owned(ws, payload);
    if (!ent) return { ok: false, error: 'not_found' };
    const state = await _tmuxClientState(ent);
    if (!state.ok) return state;
    const result = await _tmuxExec(ent, ['list-windows', '-t', ent.tmux.session, '-F', '#{window_index}:#{window_name}']);
    if (!result?.ok) return _tmuxFailure('list_windows', result, ent);
    const windows = parseWindowRows(result.stdout).map((window) => ({ ...window, active: window.index === state.activeIndex }));
    return { ok: true, windows, activeIndex: state.activeIndex, paneId: state.paneId, copyMode: state.copyMode };
  }

  async function enterCopyMode(ws, payload = {}) {
    const ent = _owned(ws, payload);
    if (!ent) return { ok: false, error: 'not_found' };
    const state = await _tmuxClientState(ent);
    if (!state.ok) return state;
    if (state.copyMode) return { ok: true, copyMode: true, paneId: state.paneId };
    const result = await _tmuxExec(ent, ['copy-mode', '-u', '-t', state.paneId]);
    return result?.ok
      ? { ok: true, copyMode: true, paneId: state.paneId }
      : _tmuxFailure('copy_mode_enter', result, ent);
  }

  async function cancelCopyMode(ws, payload = {}) {
    const ent = _owned(ws, payload);
    if (!ent) return { ok: false, error: 'not_found' };
    const state = await _tmuxClientState(ent);
    if (!state.ok) return state;
    if (!state.copyMode) return { ok: true, copyMode: false, paneId: state.paneId };
    const result = await _tmuxExec(ent, ['send-keys', '-X', '-t', state.paneId, 'cancel']);
    return result?.ok
      ? { ok: true, copyMode: false, paneId: state.paneId }
      : _tmuxFailure('copy_mode_cancel', result, ent);
  }

  /** PTY fast path for window switches (C-b <digit> / C-b :) — tmux-
   *  internal on the same stream, no exec round trips. */
  function _ptySwitchWindow(ent, index) {
    try {
      if (index <= 9) ent.write(`\x02${index}`);
      else ent.write(`\x02:select-window -t :${index}\r`);
      return true;
    } catch (_) { return false; }
  }

  async function selectWindow(ws, payload = {}) {
    const ent = _owned(ws, payload);
    const index = Number(payload && payload.index);
    if (!ent) return { ok: false, error: 'not_found' };
    if (!Number.isInteger(index) || index < 0 || index > 9999) return { ok: false, error: 'invalid_args', detail: 'window index is invalid' };
    if (typeof ent.write === 'function' && _ptySwitchWindow(ent, index)) {
      return { ok: true, via: 'pty' };
    }
    const client = await _ensureTmuxClientTty(ent);
    if (!client.ok) return client;
    const result = await _tmuxExec(ent, ['switch-client', '-c', ent.tmuxClientTty, '-t', `${ent.tmux.session}:${index}`]);
    return result?.ok ? listWindows(ws, payload) : result;
  }

  async function createWindow(ws, payload = {}) {
    const ent = _owned(ws, payload);
    if (!ent) return { ok: false, error: 'not_found' };
    if (typeof ent.write === 'function') {
      try { ent.write('\x02c'); return { ok: true, via: 'pty' }; } catch (_) {}
    }
    const client = await _ensureTmuxClientTty(ent);
    if (!client.ok) return client;
    const created = await _tmuxExec(ent, ['new-window', '-t', ent.tmux.session, '-P', '-F', '#{window_index}']);
    const index = Number(String(created?.stdout || '').trim());
    if (!created?.ok || !Number.isInteger(index) || index < 0 || index > 9999) return created?.ok ? { ok: false, error: 'tmux_parse_failed', detail: 'new window index missing' } : created;
    const selected = await _tmuxExec(ent, ['switch-client', '-c', ent.tmuxClientTty, '-t', `${ent.tmux.session}:${index}`]);
    return selected?.ok ? listWindows(ws, payload) : selected;
  }

  /* ── public surface ── */
  const OPS = {
    open, ready, input, resize, close,
    listWindows, selectWindow, createWindow,
    copyMode: enterCopyMode, cancelCopyMode,
  };

  return {
    /** Handle one shim frame: { ch:'term', id, op, ...payload }. */
    async handle(ws, frame) {
      const op = OPS[String(frame.op || '')];
      if (!op) return { ok: false, error: 'unknown_op', detail: String(frame.op || '') };
      return op(ws, frame);
    },
    /** Wire a fresh WS connection; drops its sessions on close. */
    attach(ws) {
      ws.on('close', () => {
        for (const [sid, ent] of [..._terminals]) {
          if (ent.owner === ws) _dropSession(sid);
        }
      });
    },
    /** app:reset semantics: dispose every session + release wedged gates. */
    closeAll() {
      for (const sid of [..._terminals.keys()]) _dropSession(sid);
      for (const [sid] of [..._termReady]) _termGateClose(sid);
      _resetTmuxDiscovery();
    },
    sessionCount() { return _terminals.size; },
  };
}
