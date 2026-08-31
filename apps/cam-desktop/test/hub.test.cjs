/**
 * Deterministic embedded-Hub tests (CAM-DESK-RUN-011/014/015,
 * NODEUI-014/015, DIRECT-014/019).
 *
 * Runs entirely in-process — no Electron, no real SSH, no real camc.
 * The Hub is hand-written CommonJS that depends only on node stdlib +
 * two injected collaborators (credentialStore, sshTransport), so we
 * can require() it directly, inject mocks, start its HTTP server on an
 * OS-assigned loopback port, and drive the same /api/* surface the
 * renderer uses.
 *
 * Run:  node apps/cam-desktop/test/hub.test.cjs
 * Exit: 0 = pass, 1 = fail (prints the failing assertion).
 *
 * Coverage:
 *  1. /api/api-models shape for a local target — local sessions are
 *     unsupported, so source.error is local_unsupported with the
 *     SSH-node guidance (the response SHAPE stays
 *     { models, defaults, toolSupport, source }).
 *  2. /api/api-models remote SSH failure surfaces source.error + detail.
 *  3. /api/api-models error detail scrubs secrets (ghp_/glpat-/sk-/
 *     password=/bearer/private key).
 *  4. POST /api/agents rejects an unregistered node (node_not_registered)
 *     instead of falling through to agent auth.
 *  5. POST /api/agents accepts a bracketed IPv6 node key when a matching
 *     context exists, and rejects a bad IPv6-shaped key.
 *  6. POST /api/agents records auto_confirm/timeout/retry on the agent
 *     record and returns direct_limitations (Direct-mode parity note).
 *  7. /api/contexts/:name_or_id resolves by id AND by name (Sync/Delete
 *     Host path — CAM-DESK-NODEUI-014/015).
 *  8. Secret redaction helper is unit-tested directly.
 *  9. Local sessions retired 2026-07-17: POST /api/agents with
 *     node:'local' returns 400 local_unsupported with the SSH guidance.
 */

'use strict';

const assert = require('assert');
const http = require('http');
const path = require('path');
const crypto = require('crypto');

const HUB = require(path.join(__dirname, '..', 'electron', 'embedded-hub.cjs'));

// ── Test harness ───────────────────────────────────────────────────

let pass = 0, fail = 0;
const failures = [];
function ok(name, cond, extra) {
  if (cond) { pass++; /* console.log('  ok ' + name); */ }
  else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.error('FAIL ' + name + (extra ? ' — ' + extra : '')); }
}
function eq(name, a, b) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  ok(name, sa === sb, `got ${sa}, want ${sb}`);
}

// In-memory credential store mock.
function makeCredentialStore() {
  const map = new Map();
  return {
    available: () => true,
    put(ref, kind, secret) { map.set(ref, { kind, secret }); return { ok: true, ref, saved_at: 'now' }; },
    get(ref) { const e = map.get(ref); return e ? e.secret : null; },
    removeForContext(id) { for (const k of [...map.keys()]) { if (k.startsWith(id + ':')) map.delete(k); } },
    removeWithPrefix(p) { for (const k of [...map.keys()]) { if (k.startsWith(p)) map.delete(k); } },
    remove(ref) { map.delete(ref); },
  };
}

// Mock SSH transport. `execRemote` dispatches on the command string to
// per-test stubs; tests register handlers via setRemoteHandler().
let _remoteHandler = null;
function setRemoteHandler(fn) { _remoteHandler = fn; }
// writeRemoteFile is OPT-IN per test: _ensureRemoteCamc probes
// `typeof … === 'function'` and skips the upload path when absent —
// most tests rely on that skip. Tests that need the ensure path
// (python3 probe & friends) enable it via setWriteEnabled(true).
let _writeEnabled = false;
function setWriteEnabled(on) { _writeEnabled = !!on; }
function makeSshTransport() {
  const t = {
    execRemote(opts) {
      if (_remoteHandler) {
        try { return Promise.resolve(_remoteHandler(opts) || { ok: false, error: 'unhandled', detail: 'no handler' }); }
        catch (e) { return Promise.resolve({ ok: false, error: 'handler_threw', detail: e.message }); }
      }
      return Promise.resolve({ ok: false, error: 'no_handler', detail: 'no remote handler registered' });
    },
    closeAll() {},
  };
  Object.defineProperty(t, 'writeRemoteFile', {
    get() { return _writeEnabled ? (() => Promise.resolve({ ok: true, bytes: 0 })) : undefined; },
    configurable: true,
  });
  return t;
}

// ── HTTP helpers ──────────────────────────────────────────────────

let _base = '', _token = '';
function request(method, p, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(p, _base);
    const req = http.request({
      method,
      hostname: u.hostname,
      port: u.port,
      path: u.pathname + u.search,
      headers: {
        'Authorization': 'Bearer ' + _token,
        'Content-Type': 'application/json',
      },
    }, (res) => {
      let buf = '';
      res.on('data', (c) => { buf += c; });
      res.on('end', () => {
        let parsed = null;
        try { parsed = buf ? JSON.parse(buf) : null; } catch (_) { parsed = buf; }
        resolve({ status: res.statusCode, body: parsed });
      });
    });
    req.on('error', (e) => { e.message = `${method} ${p}: ${e.message}`; reject(e); });
    if (body != null) req.write(JSON.stringify(body));
    req.end();
  });
}

async function startHub(dataDir) {
  HUB.configure({ credentialStore: makeCredentialStore(), sshTransport: makeSshTransport() });
  const res = await HUB.start({ dataDir, apiToken: 'test-token' });
  assert(res.ok, 'hub failed to start: ' + JSON.stringify(res));
  _base = `http://127.0.0.1:${res.apiUrl.split(':').pop()}`;
  _token = res.apiToken;
  return res;
}

async function stopHub() {
  try { await HUB.stop(); } catch (_) {}
}

// ── Tests ─────────────────────────────────────────────────────────

async function main() {
  const tmpDir = path.join(process.env.TMPDIR || '/tmp', 'cam-hub-test-' + crypto.randomBytes(4).toString('hex'));
  await startHub(tmpDir);

  // Seed a registered SSH context (so donor lookup can find it).
  const sshCtx = {
    name: 'ren01',
    path: '/home/ren/src',
    host: 'ren.example',
    user: 'ren',
    port: 3422,
    auth_method: 'agent',
  };
  let r = await request('POST', '/api/contexts', sshCtx);
  eq('seed ssh context', r.status, 201);
  const created = r.body;
  const ctxId = created && created.id;

  // 1. /api/api-models for a local target — local sessions were retired
  //    2026-07-17, so the hub refuses with source.error
  //    local_unsupported + the SSH-node guidance. The response SHAPE
  //    must still be { models, defaults, toolSupport, source }.
  setRemoteHandler(null);
  r = await request('GET', '/api/api-models?node=local');
  ok('api-models shape', r.status === 200 && r.body && Array.isArray(r.body.models) && Array.isArray(r.body.defaults) && r.body.toolSupport && r.body.source, 'status=' + r.status);
  eq('api-models toolSupport claude', r.body.toolSupport.claude, true);
  eq('api-models toolSupport cursor', r.body.toolSupport.cursor, false);
  eq('api-models local refused', r.body.source.error, 'local_unsupported');
  ok('api-models local detail has SSH guidance', /SSH server/.test(r.body.source.detail || '') && /SSH node/.test(r.body.source.detail || ''), r.body.source.detail);

  // 2. /api/api-models remote SSH failure surfaces source.error + detail.
  //    Target the registered context so _resolveApiModelsTarget builds
  //    baseOpts, then have the mock SSH transport fail.
  setRemoteHandler(() => ({ ok: false, error: 'exec_failed', detail: 'camc: command not found', stderr: 'camc: command not found' }));
  r = await request('GET', '/api/api-models?context=ren01');
  ok('api-models remote failure has source', r.status === 200 && r.body && r.body.source, 'no source');
  eq('api-models remote failure error', r.body.source.error, 'remote_camc_failed');
  ok('api-models remote failure detail non-empty', typeof r.body.source.detail === 'string' && r.body.source.detail.length > 0, r.body.source.detail);

  // 3. Secret redaction in api-models error detail. Stub a remote camc
  //    that echoes a GitHub-PAT-shaped + GitLab-PAT-shaped + password=
  //    string in its stderr; the surfaced detail must NOT contain any
  //    of them. Uses SYNTHETIC placeholder values that still match the
  //    redaction regexes (ghp_ + 20+ alnum; glpat- + 10+ alnum;
  //    password= + 8+ chars) — no real credentials.
  setRemoteHandler(() => ({ ok: false, error: 'exec_failed', detail: 'boom ghp_TESTFAKE0000000000000000000000 and glpat-TESTFAKETOKEN123 password=testpass1234', stderr: 'same' }));
  r = await request('GET', '/api/api-models?context=ren01');
  const det = r.body.source.detail || '';
  ok('redact ghp_ in detail', !/ghp_[A-Za-z0-9]{20,}/.test(det), det);
  ok('redact glpat- in detail', !/glpat-[A-Za-z0-9_-]{10,}/.test(det), det);
  ok('redact password= in detail', !/password=[A-Za-z0-9._~+/=-]{8,}/i.test(det), det);
  ok('redact preserves context', /camc on .* failed/.test(det), det);

  // 4. POST /api/agents rejects an unregistered node. The renderer's
  //    node select only lists registered hosts, so a key like
  //    "stranger@rogue.example:22" must be refused with
  //    node_not_registered (CAM-DESK-DIRECT-014), NOT silently fabricate
  //    agent-auth creds.
  setRemoteHandler(null);
  r = await request('POST', '/api/agents', {
    tool: 'claude', prompt: 'hi', node: 'stranger@rogue.example:22', path: '/tmp/x',
  });
  eq('unregistered node rejected', r.status, 400);
  eq('unregistered node error code', r.body.error, 'node_not_registered');
  ok('unregistered node detail names the endpoint', /stranger@rogue\.example:22/.test(r.body.detail || ''), r.body.detail);

  // 5a. Bracketed IPv6 node key: register an IPv6 context, then start
  //     with the bracketed key. The hub must strip the brackets and
  //     match the donor.
  const ipv6Ctx = { name: 'v6box', path: '/home/u/src', host: '2001:db8::1', user: 'u', port: 22, auth_method: 'agent' };
  r = await request('POST', '/api/contexts', ipv6Ctx);
  eq('seed ipv6 context', r.status, 201);
  setRemoteHandler((opts) => {
    // The hub runs `camc run` then `camc --json status`. Both succeed;
    // the status call returns a minimal record. The command is
    // shell-quoted ('--json' 'run' ...), so match loosely on the
    // subcommand tokens.
    if (/'run'/.test(opts.command)) return { ok: true, stdout: '  ID: deadbeef\n  Tool: claude\n', stderr: '' };
    if (/'status'/.test(opts.command)) return { ok: true, stdout: JSON.stringify({ id: 'deadbeef', status: 'running', state: 'initializing', task: { tool: 'claude', name: '', prompt: 'hi' }, context_path: '/home/u/src', transport_type: 'ssh', hostname: 'v6' }), stderr: '' };
    return { ok: false, error: 'exec_failed', detail: 'unhandled cmd: ' + opts.command };
  });
  r = await request('POST', '/api/agents', {
    tool: 'claude', prompt: 'hi', node: 'u@[2001:db8::1]:22', path: '/home/u/src',
  });
  eq('bracketed ipv6 start accepted', r.status, 201);
  ok('bracketed ipv6 returns agent', r.body && r.body.agent, JSON.stringify(r.body));
  eq('bracketed ipv6 agent id', r.body.agentId, 'deadbeef');

  // 5b. A malformed IPv6-shaped key (colons inside an unbracketed host)
  //     is rejected as bad_node, not silently parsed.
  setRemoteHandler(null);
  r = await request('POST', '/api/agents', {
    tool: 'claude', prompt: 'hi', node: 'u@2001:db8::1:22', path: '/home/u/src',
  });
  eq('unbracketed ipv6 rejected', r.status, 400);
  ok('unbracketed ipv6 is bad_node', r.body.error === 'bad_node' || r.body.error === 'node_not_registered', r.body.error);

  // 6. Direct-mode parity: the renderer always sends auto_confirm +
  //    timeout + retry. The hub must record them on the agent record
  //    (requested_timeout / requested_retry) and return a
  //    direct_limitations note, even though camc has no such flags.
  setRemoteHandler((opts) => {
    if (process.env.HUB_TEST_DEBUG) console.error('PARITY CMD:', JSON.stringify(opts.command));
    if (/'run'/.test(opts.command)) return { ok: true, stdout: '  ID: abcdef12\n  Tool: claude\n', stderr: '' };
    if (/'status'/.test(opts.command)) return { ok: true, stdout: JSON.stringify({ id: 'abcdef12', status: 'running', state: 'initializing', task: { tool: 'claude', name: 't', prompt: 'p' }, context_path: '/home/ren/src', transport_type: 'ssh' }), stderr: '' };
    return { ok: false, error: 'exec_failed', detail: 'unhandled cmd' };
  });
  r = await request('POST', '/api/agents', {
    tool: 'claude', prompt: 'p', context: 'ren01', name: 't',
    auto_confirm: false, timeout: '30m', retry: 2, auto_exit: true,
  });
  eq('parity start accepted', r.status, 201);
  ok('parity returns direct_limitations', r.body && r.body.direct_limitations, JSON.stringify(r.body));
  eq('parity direct mode label', r.body.direct_limitations.mode, 'direct');
  eq('parity timeout sent', r.body.direct_limitations.timeout.sent, true);
  eq('parity timeout not enforced', r.body.direct_limitations.timeout.enforced, false);
  eq('parity retry sent', r.body.direct_limitations.retry.sent, true);
  eq('parity retry not enforced', r.body.direct_limitations.retry.enforced, false);
  eq('parity auto_confirm sent', r.body.direct_limitations.auto_confirm.sent, true);
  eq('parity auto_confirm not enforced', r.body.direct_limitations.auto_confirm.enforced, false);
  ok('parity agent records requested_timeout', r.body.agent && r.body.agent.requested_timeout === '30m', JSON.stringify(r.body.agent));
  eq('parity agent records requested_retry', r.body.agent && r.body.agent.requested_retry, 2);

  // An empty Start prompt is an interactive launch, not a validation
  // failure. CAMC accepts an empty positional prompt and leaves the selected
  // CLI ready for the user to type in its terminal.
  setRemoteHandler((opts) => {
    if (/'run'/.test(opts.command)) return { ok: true, stdout: '  ID: feedbeef\n', stderr: '' };
    if (/'status'/.test(opts.command)) return { ok: true, stdout: JSON.stringify({ id: 'feedbeef', status: 'running', state: 'initializing', task: { tool: 'claude', name: '', prompt: '' }, context_path: '/home/ren/src', transport_type: 'ssh' }), stderr: '' };
    return { ok: false, error: 'exec_failed', detail: 'unhandled cmd' };
  });
  r = await request('POST', '/api/agents', {
    tool: 'claude', prompt: '', context: 'ren01', path: '/home/ren/src',
  });
  eq('empty prompt start accepted', r.status, 201);
  eq('empty prompt start agent id', r.body && r.body.agentId, 'feedbeef');

  // 7. /api/contexts/:name_or_id resolves by id AND by name.
  r = await request('GET', '/api/contexts/' + encodeURIComponent(ctxId));
  eq('context GET by id', r.status, 200);
  eq('context GET by id matches', r.body && r.body.id, ctxId);
  r = await request('GET', '/api/contexts/ren01');
  eq('context GET by name', r.status, 200);
  eq('context GET by name matches', r.body && r.body.name, 'ren01');

  r = await request('PATCH', '/api/contexts/' + encodeURIComponent(ctxId), { name: 'v6box' });
  eq('context rename rejects duplicate name status', r.status, 400);
  eq('context rename rejects duplicate name error', r.body && r.body.error, 'duplicate_name');
  r = await request('PATCH', '/api/contexts/' + encodeURIComponent(ctxId), { name: 'ren02' });
  eq('context rename by id status', r.status, 200);
  eq('context rename by id changes name', r.body && r.body.name, 'ren02');
  r = await request('GET', '/api/contexts/ren01');
  eq('context old name no longer resolves after rename', r.status, 404);
  r = await request('GET', '/api/contexts/ren02');
  eq('context new name resolves after rename', r.status, 200);
  eq('context new name matches', r.body && r.body.name, 'ren02');
  r = await request('GET', '/api/agents');
  ok('context rename updates local agent context names',
    Array.isArray(r.body && r.body.agents)
      && r.body.agents.some(a => a && a.id === 'feedbeef' && a.context_name === 'ren02'),
    JSON.stringify(r.body));
  // Sync Host by id (CAM-DESK-NODEUI-014): the /sync sub-route shares
  // the same id-or-name resolution.
  setRemoteHandler((opts) => {
    if (/test -x/.test(opts.command)) return { ok: true, stdout: '', stderr: '' };
    if (/--json list/.test(opts.command)) return { ok: true, stdout: '[]', stderr: '' };
    return { ok: true, stdout: '', stderr: '' };
  });
  r = await request('POST', '/api/contexts/' + encodeURIComponent(ctxId) + '/sync', {});
  ok('context sync by id resolves (no 404)', r.status === 200 || r.status === 202, 'status=' + r.status + ' body=' + JSON.stringify(r.body));

  // 8. Delete Host by id (CAM-DESK-NODEUI-015): the DELETE route shares
  //    the same resolution; must NOT 404 when the caller leads with id.
  r = await request('DELETE', '/api/contexts/' + encodeURIComponent(ctxId));
  ok('context delete by id resolves (no 404)', r.status === 200, 'status=' + r.status + ' body=' + JSON.stringify(r.body));
  r = await request('GET', '/api/contexts/' + encodeURIComponent(ctxId));
  eq('context deleted', r.status, 404);

  // ── 9. Local sessions retired (2026-07-17, product decision) ──
  // POST /api/agents with node:'local' is refused with a structured
  // local_unsupported error and the SSH-node guidance: to use the local
  // machine as a node, run an SSH server on it and add it as an SSH
  // node (tmux + agent CLI + auth required).
  setRemoteHandler(null);
  r = await request('POST', '/api/agents', {
    tool: 'claude', prompt: 'hi', node: 'local', path: '/home/u/x',
  });
  eq('local start refused status', r.status, 400);
  eq('local start refused error', r.body && r.body.error, 'local_unsupported');
  ok('local start detail has SSH guidance', /SSH server/.test((r.body && r.body.detail) || '') && /SSH node/.test((r.body && r.body.detail) || ''), r.body && r.body.detail);

  // sync-status endpoint: 404 for unknown, shape for known context
  r = await request('GET', '/api/contexts/v6box/sync-status');
  eq('sync-status ok shape', r.status === 200 && r.body && r.body.ok === true && 'progress' in r.body, true);
  r = await request('GET', '/api/contexts/nonexistent-ctx/sync-status');
  eq('sync-status unknown context 404', r.status, 404);

  // ── python3 probe misclassification guards (sync error reporting) ──
  // The pre-upload `command -v python3` probe (routed via /bin/sh) must
  // only report python3_missing when it actually RAN and found nothing.
  // Connection-level failures used to be misreported as "install python3".
  {
    let r0 = await request('POST', '/api/contexts', {
      name: 'probebox', host: '10.9.3.1', user: 'u', port: 22,
      auth_method: 'agent', path: '/home/u',
    });
    eq('probe: seed context', r0.status, 201);
    setWriteEnabled(true); // let _ensureRemoteCamc reach the python3 probe

    // auth failure → passes through as auth_failed.
    setRemoteHandler(() => ({ ok: false, error: 'auth_failed', detail: 'All configured authentication methods failed' }));
    r0 = await request('POST', '/api/contexts/probebox/sync', {});
    eq('probe: auth failure passes through', r0.body && r0.body.error, 'auth_failed', JSON.stringify(r0.body));

    // Probe command must be routed through /bin/sh explicitly — the exec
    // channel is interpreted by the login shell; /bin/sh + `command -v`
    // are Linux-common and must not gate on the user's login shell.
    let probeCmd = null;
    setRemoteHandler((opts) => {
      if (/command -v python3/.test(opts.command)) {
        probeCmd = opts.command;
        return { ok: true, stdout: '/usr/bin/python3\n' };
      }
      return { ok: false, error: 'remote_nonzero', detail: '', stderr: '' };
    });
    r0 = await request('POST', '/api/contexts/probebox/sync', {});
    eq('probe: wrapped in /bin/sh', /^\/bin\/sh -c /.test(probeCmd || ''), true, String(probeCmd));

    // Genuine absence: probe ran under /bin/sh, exit 1, no output.
    setRemoteHandler((opts) => {
      if (/command -v python3/.test(opts.command)) {
        return { ok: false, error: 'remote_nonzero', detail: '', stderr: '' };
      }
      return { ok: false, error: 'remote_nonzero', detail: '', stderr: '' };
    });
    r0 = await request('POST', '/api/contexts/probebox/sync', {});
    eq('probe: genuine absence stays python3_missing', r0.body && r0.body.error, 'python3_missing', JSON.stringify(r0.body));

    // Duplicate concurrent sync for the same context: one runs, the
    // other is refused — no double probes / twin SFTP writers on the
    // same camc.tmp. (Frontend idle-abort cancels only the HTTP
    // request, never the hub-side sync, so this race was reachable by
    // reload/retry while a slow sync was still grinding.)
    setWriteEnabled(false);
    setRemoteHandler((opts) => new Promise(res => setTimeout(
      () => res({ ok: false, error: 'remote_nonzero', detail: '', stderr: '' }), 50)));
    const [s1, s2] = await Promise.all([
      request('POST', '/api/contexts/probebox/sync', {}),
      request('POST', '/api/contexts/probebox/sync', {}),
    ]);
    ok('sync: concurrent duplicate refused',
      [s1, s2].filter(s => s.body && s.body.error === 'sync_in_flight').length === 1,
      JSON.stringify([s1.body && s1.body.error, s2.body && s2.body.error]));

    // Wedged-sync reaper: if a sync's hub-side flow never settles (a step
    // promise that hangs), the in-flight lock must not lock the context out
    // until app restart — after syncStaleMs without step activity the next
    // sync reaps the stale lease. Uses the start() reused-path runtime
    // tunable: a hub restart here trips a pre-existing socket hang-up quirk
    // (restart + pending request), unrelated to this change.
    await HUB.start({ syncStaleMs: 250 });
    setRemoteHandler(() => new Promise(() => {})); // never settles = wedged
    const w1 = request('POST', '/api/contexts/probebox/sync', {});
    w1.catch(() => {}); // wedged hub-side flow; HTTP socket dies at stopHub
    await new Promise(res => setTimeout(res, 50)); // let w1 get in flight
    let rw = await request('POST', '/api/contexts/probebox/sync', {});
    eq('sync: fresh in-flight lock still refuses', rw.body && rw.body.error, 'sync_in_flight', JSON.stringify(rw.body));
    await new Promise(res => setTimeout(res, 300)); // past the stale window
    setRemoteHandler(() => ({ ok: false, error: 'remote_nonzero', detail: '', stderr: '' }));
    rw = await request('POST', '/api/contexts/probebox/sync', {});
    ok('sync: stale wedged lock reaped, sync allowed',
      rw.body && rw.body.error !== 'sync_in_flight', JSON.stringify(rw.body));
    await HUB.start({ syncStaleMs: 180000 });
    setRemoteHandler(null);
    setWriteEnabled(false);
  }

  // ── Heal endpoint: runs selected camc ops sequentially, whitelist ──
  {
    let r0 = await request('POST', '/api/contexts', {
      name: 'healbox', host: '10.0.0.9', user: 'demo', port: 22,
      auth_method: 'agent', path: '/home/demo',
    });
    eq('heal: seed context', r0.status, 201);
    const calls = [];
    setRemoteHandler((opts) => {
      calls.push(opts.command);
      if (/camc heal --tmux/.test(opts.command)) return { ok: false, error: 'remote_nonzero', detail: 'usage: camc ...', stdout: '', stderr: 'usage: camc' };
      return { ok: true, stdout: 'Heal: 2 healthy, 0 restarted', stderr: '' };
    });
    let hr = await request('POST', '/api/contexts/healbox/heal', { ops: ['monitor', 'tmux', 'bogus'] });
    eq('heal status', hr.status, 200);
    eq('heal ran whitelisted ops only', hr.body && hr.body.results && hr.body.results.length, 2);
    eq('heal op1 ok', hr.body.results[0].ok, true);
    eq('heal op2 fails through (old camc)', hr.body.results[1].ok, false);
    ok('heal passes camc error tail through', /usage/.test(hr.body.results[1].tail || ''), JSON.stringify(hr.body.results[1]));
    ok('heal executed sequentially via remote camc', calls.some(c => /camc heal --monitor/.test(c)) && calls.some(c => /camc heal --tmux/.test(c)), JSON.stringify(calls));
    hr = await request('POST', '/api/contexts/healbox/heal', { ops: ['bogus'] });
    eq('heal invalid ops rejected', hr.status, 400);
    eq('heal invalid ops error', hr.body && hr.body.error, 'invalid_ops');
    setRemoteHandler(null);
  }

  // ── ProxyJump: machine.jump validation + resolution ──
  {
    // Baseline jump node (ordinary SSH context).
    let r0 = await request('POST', '/api/contexts', {
      name: 'jumphost', host: '10.9.0.1', user: 'ju', port: 2200,
      auth_method: 'agent', path: '/home/ju',
    });
    eq('jump: seed jump node', r0.status, 201);

    // Valid jump reference is persisted.
    r0 = await request('POST', '/api/contexts', {
      name: 'viabox', host: '10.9.9.9', user: 'u', port: 22,
      auth_method: 'agent', path: '/home/u', jump: 'ju@10.9.0.1:2200',
    });
    eq('jump: create via jump accepted', r0.status, 201, JSON.stringify(r0.body));
    eq('jump: machine.jump persisted', r0.body && r0.body.machine && r0.body.machine.jump, 'ju@10.9.0.1:2200');

    // Unknown jump node → invalid_jump.
    r0 = await request('POST', '/api/contexts', {
      name: 'badbox', host: '10.9.9.8', user: 'u', port: 22,
      auth_method: 'agent', path: '/home/u', jump: 'ghost@10.0.0.66:22',
    });
    eq('jump: unknown jump node rejected', r0.status, 400);
    eq('jump: unknown jump node error', r0.body && r0.body.error, 'invalid_jump');

    // Self jump → invalid_jump.
    r0 = await request('POST', '/api/contexts', {
      name: 'selfbox', host: '10.9.0.1', user: 'ju', port: 2200,
      auth_method: 'agent', path: '/home/ju', jump: 'ju@10.9.0.1:2200',
    });
    eq('jump: self jump rejected', r0.status, 400);
    eq('jump: self jump error', r0.body && r0.body.error, 'invalid_jump');

    // Update: set then clear.
    r0 = await request('POST', '/api/contexts', {
      name: 'directbox', host: '10.9.9.7', user: 'u', port: 22,
      auth_method: 'agent', path: '/home/u',
    });
    eq('jump: seed direct node', r0.status, 201);
    r0 = await request('PUT', '/api/contexts/directbox', { jump: 'ju@10.9.0.1:2200' });
    eq('jump: update sets jump', r0.status === 200 && r0.body && r0.body.machine && r0.body.machine.jump, 'ju@10.9.0.1:2200');
    r0 = await request('PUT', '/api/contexts/directbox', { jump: '' });
    eq('jump: update clears jump', r0.status === 200 && !(r0.body && r0.body.machine && r0.body.machine.jump), true);

    // Loop: loopa.jump=loopb, then loopb.jump=loopa must fail.
    await request('POST', '/api/contexts', { name: 'loopb', host: '10.9.1.2', user: 'u', port: 22, auth_method: 'agent', path: '/home/u' });
    await request('POST', '/api/contexts', { name: 'loopa', host: '10.9.1.1', user: 'u', port: 22, auth_method: 'agent', path: '/home/u', jump: 'u@10.9.1.2:22' });
    r0 = await request('PUT', '/api/contexts/loopb', { jump: 'u@10.9.1.1:22' });
    eq('jump: two-node loop rejected', r0.status, 400);
    eq('jump: loop error', r0.body && r0.body.error, 'invalid_jump', JSON.stringify(r0.body));

    // Depth: chain 4 deep (d4→d3→d2→d1→jumphost) exceeds the 3-hop cap.
    await request('POST', '/api/contexts', { name: 'd1', host: '10.9.2.1', user: 'u', port: 22, auth_method: 'agent', path: '/home/u', jump: 'ju@10.9.0.1:2200' });
    await request('POST', '/api/contexts', { name: 'd2', host: '10.9.2.2', user: 'u', port: 22, auth_method: 'agent', path: '/home/u', jump: 'u@10.9.2.1:22' });
    await request('POST', '/api/contexts', { name: 'd3', host: '10.9.2.3', user: 'u', port: 22, auth_method: 'agent', path: '/home/u', jump: 'u@10.9.2.2:22' });
    r0 = await request('POST', '/api/contexts', { name: 'd4', host: '10.9.2.4', user: 'u', port: 22, auth_method: 'agent', path: '/home/u', jump: 'u@10.9.2.3:22' });
    eq('jump: chain deeper than 3 rejected', r0.status, 400);
    eq('jump: depth error', r0.body && r0.body.error, 'invalid_jump');
  }

  // ── Extensions API: install/list/toggle/remove + remote call ──
  {
    const fs = require('fs');
    const repoRoot = path.join(__dirname, '..', '..', '..');
    const sampleDir = path.join(repoRoot, 'extensions', 'packages', 'assistant');
    let r0 = await request('POST', '/api/extensions/install', { path: sampleDir });
    eq('ext: install assistant', r0.status, 201, JSON.stringify(r0.body));
    eq('ext: install returns manifest', r0.body && r0.body.manifest && r0.body.manifest.name, 'assistant');

    r0 = await request('GET', '/api/extensions');
    const exts = (r0.body && r0.body.extensions) || [];
    // The installed copy is the SAME version as the built-in assistant —
    // a tie goes to the built-in (app reinstall repairs stale shadows);
    // the row is the built-in, annotated with the ignored user version.
    ok('ext: list includes builtin skills/todos + builtin assistant (tie → built-in wins)',
      exts.some(e => e.name === 'skills' && e.source === 'builtin' && e.hasView === true
          && (e.capabilities || []).includes('hub:api'))
        && exts.some(e => e.name === 'assistant' && e.source === 'builtin' && e.hasView && e.hasTool
          && typeof e.shadowed_user === 'string'));

    // Toggle disable/enable persists via the store flags.
    r0 = await request('POST', '/api/extensions/assistant/disable');
    eq('ext: disable', r0.status, 200);
    r0 = await request('GET', '/api/extensions');
    ok('ext: disabled reflected', (r0.body.extensions.find(e => e.name === 'assistant') || {}).enabled === false);
    await request('POST', '/api/extensions/assistant/enable');

    // Remove is uniform for built-in and user extensions (product
    // decision 2026-08-17 v2): a built-in DELETE hides it via the store
    // `removed` flag and cascades its config (attributes + ext-data dir
    // + credential secrets); a same-name install or an app version
    // change restores it. Deleting a SHADOWING user copy merely reverts
    // to the built-in — config is kept.
    r0 = await request('PUT', '/api/extensions/todos/config', { config: { cascade_probe: 1 } });
    eq('ext: seed todos attributes', r0.status, 200, JSON.stringify(r0.body));
    const todosData = path.join(tmpDir, 'ext-data', 'todos');
    fs.mkdirSync(todosData, { recursive: true });
    fs.writeFileSync(path.join(todosData, 'probe.txt'), 'x');
    r0 = await request('DELETE', '/api/extensions/todos');
    eq('ext: remove builtin ok', r0.status, 200, JSON.stringify(r0.body));
    r0 = await request('GET', '/api/extensions');
    ok('ext: builtin hidden after remove', !r0.body.extensions.some(e => e.name === 'todos'));
    const cfgAfterRm = JSON.parse(fs.readFileSync(path.join(tmpDir, 'extension-config.json'), 'utf8'));
    ok('ext: remove cascades attributes', cfgAfterRm.todos === undefined, JSON.stringify(cfgAfterRm));
    ok('ext: remove cascades ext-data dir', !fs.existsSync(todosData));
    const tmpT = fs.mkdtempSync(path.join(require('os').tmpdir(), 'todos-restore-'));
    fs.writeFileSync(path.join(tmpT, 'manifest.yaml'), 'name: todos\nversion: 9.9.9\ntitle: Todos\ndescription: restored copy\n');
    fs.writeFileSync(path.join(tmpT, 'index.html'), '<html></html>');
    r0 = await request('POST', '/api/extensions/install', { path: tmpT });
    eq('ext: same-name install lands as user copy', r0.status, 201, JSON.stringify(r0.body));
    r0 = await request('GET', '/api/extensions');
    const todosRow = (r0.body.extensions || []).find(e => e.name === 'todos');
    ok('ext: same-name row is the shadowing user copy', todosRow && todosRow.source === 'user' && todosRow.shadowing === true);
    // Config on the shadow copy, then delete the copy → reverts to the
    // built-in; config KEPT (uninstall-an-update ≠ remove-the-ext).
    r0 = await request('PUT', '/api/extensions/todos/config', { config: { shadow_probe: 2 } });
    eq('ext: seed shadow attributes', r0.status, 200, JSON.stringify(r0.body));
    r0 = await request('DELETE', '/api/extensions/todos');
    eq('ext: delete the user copy', r0.status, 200);
    ok('ext: shadow delete reports the revert', r0.body && r0.body.reverted_to_builtin === true, JSON.stringify(r0.body));
    r0 = await request('GET', '/api/extensions');
    const todosBack = (r0.body.extensions || []).find(e => e.name === 'todos');
    ok('ext: builtin reappears after user-copy delete', todosBack && todosBack.source === 'builtin');
    r0 = await request('GET', '/api/extensions/todos/config');
    ok('ext: shadow delete keeps attributes', r0.status === 200 && r0.body && r0.body.config && r0.body.config.shadow_probe === 2, JSON.stringify(r0.body));
    r0 = await request('PUT', '/api/extensions/todos/config', { config: {} });

    // Per-extension storage (view-written local state for sandboxed views):
    // roundtrip under ext-data/<name>/storage.json, cap + unknown ext.
    r0 = await request('PUT', '/api/extensions/todos/storage', { storage: { cam_desktop_worklog: '{"items":[]}', theme: 'dark' } });
    eq('ext: storage PUT', r0.status, 200, JSON.stringify(r0.body));
    r0 = await request('GET', '/api/extensions/todos/storage');
    ok('ext: storage GET roundtrip', r0.status === 200 && r0.body && r0.body.storage
      && r0.body.storage.theme === 'dark' && typeof r0.body.storage.cam_desktop_worklog === 'string', JSON.stringify(r0.body));
    ok('ext: storage file lives in ext-data', fs.existsSync(path.join(tmpDir, 'ext-data', 'todos', 'storage.json')));
    r0 = await request('PUT', '/api/extensions/todos/storage', { storage: { big: 'x'.repeat(600000) } });
    eq('ext: storage cap 512KB', r0.status, 400);
    r0 = await request('GET', '/api/extensions/no-such-ext/storage');
    eq('ext: storage unknown ext 404', r0.status, 404);
    r0 = await request('PUT', '/api/extensions/todos/storage', { storage: {} });
    eq('ext: storage empty object ok', r0.status, 200);

    // Per-extension attributes (Extensions → Settings): stored outside the
    // package dir, so reinstalling the package must not wipe them.
    r0 = await request('PUT', '/api/extensions/assistant/config', { config: { prompt_warn_kb: 12, note: 'x' } });
    eq('ext cfg: put', r0.status, 200, JSON.stringify(r0.body));
    r0 = await request('GET', '/api/extensions/assistant/config');
    ok('ext cfg: get roundtrip', r0.status === 200 && r0.body && r0.body.config
      && r0.body.config.prompt_warn_kb === 12 && r0.body.config.note === 'x', JSON.stringify(r0.body));
    r0 = await request('PUT', '/api/extensions/assistant/config', { config: [1, 2] });
    eq('ext cfg: array rejected', r0.status, 400);
    r0 = await request('GET', '/api/extensions/no-such-ext/config');
    eq('ext cfg: unknown extension 404', r0.status, 404);
    // Reinstall the package (same flow as an update) → config survives.
    r0 = await request('POST', '/api/extensions/install', { path: sampleDir });
    eq('ext cfg: reinstall ok', r0.status, 201);
    r0 = await request('GET', '/api/extensions/assistant/config');
    ok('ext cfg: survives reinstall', r0.body && r0.body.config && r0.body.config.prompt_warn_kb === 12);
    r0 = await request('PUT', '/api/extensions/assistant/config', { config: {} });
    r0 = await request('GET', '/api/extensions/assistant/config');
    ok('ext cfg: empty object resets', r0.body && r0.body.config && Object.keys(r0.body.config).length === 0);

    // Remote call: stub the transport so the .hash probe matches the
    // local main.py (deploy skipped), then answer the method call.
    const toolContent = fs.readFileSync(path.join(sampleDir, 'main.py'));
    const localHash = crypto.createHash('sha256').update(toolContent).digest('hex').slice(0, 16);
    const calls = [];
    setRemoteHandler((opts) => {
      calls.push(opts.command);
      if (/cat \$HOME\/.cam\/extensions\/assistant\/.hash/.test(opts.command)) {
        return { ok: true, stdout: localHash + '\n', stderr: '' };
      }
      if (/extensions\/assistant\/main\.py/.test(opts.command) && /sysinfo/.test(opts.command)) {
        return { ok: true, stdout: JSON.stringify({ hostname: 'fake-host', python: '3.8.0' }) + '\n', stderr: '' };
      }
      return { ok: true, stdout: '', stderr: '' };
    });
    r0 = await request('POST', '/api/extensions/assistant/call', { context: 'jumphost', method: 'sysinfo', args: {} });
    ok('ext: call sysinfo ok', r0.status === 200 && r0.body && r0.body.ok === true
      && r0.body.result && r0.body.result.hostname === 'fake-host', JSON.stringify(r0.body));
    ok('ext: deploy skipped when hash matches', !calls.some(c => /main\.py\.tmp/.test(c)));

    // Capability gate: remove 'exec' from the manifest → 403.
    // (assistant declares exec; use a bogus method for a 200-with-error path instead.)
    r0 = await request('POST', '/api/extensions/assistant/call', { context: 'jumphost', method: 'no such method!', args: {} });
    ok('ext: invalid method rejected', r0.body && r0.body.ok === false && r0.body.error === 'invalid_method');

    setRemoteHandler(null);

    // /ext/ view serving: query token is the VIEW token (separate from
    // the API token so a hostile view cannot escalate to /api/*).
    const rawGet = (p) => new Promise((resolve, reject) => {
      const u = new URL(p, _base);
      http.get({ hostname: u.hostname, port: u.port, path: u.pathname + u.search }, (res) => {
        let buf = '';
        res.on('data', (c) => { buf += c; });
        res.on('end', () => resolve({ status: res.statusCode, text: buf }));
      }).on('error', reject);
    });
    let vt = await request('GET', '/api/extensions/view-token');
    const viewToken = vt.body && vt.body.token;
    ok('ext: view-token endpoint returns a token', !!viewToken && viewToken !== _token);
    let ev = await rawGet('/ext/assistant/index.html?token=' + encodeURIComponent(viewToken));
    ok('ext: view served over hub', ev.status === 200 && /Assistant/.test(ev.text));
    ev = await rawGet('/ext/assistant/index.html?token=' + encodeURIComponent(_token));
    eq('ext: API token refused for views', ev.status, 401);
    ev = await rawGet('/ext/assistant/index.html');
    eq('ext: view requires token', ev.status, 401);
    ev = await rawGet('/ext/assistant/..%2F..%2Fmanifest.yaml?token=' + encodeURIComponent(viewToken));
    ok('ext: path traversal refused', ev.status === 400 || ev.status === 404);
    ev = await rawGet('/ext/client.js');
    ok('ext: bridge client is public', ev.status === 200 && /camExt/.test(ev.text));

    r0 = await request('DELETE', '/api/extensions/assistant');
    eq('ext: remove', r0.status, 200);
    r0 = await request('GET', '/api/extensions');
    // assistant is a built-in: deleting the user copy re-exposes the
    // built-in row rather than removing the name altogether.
    ok('ext: user copy removed, builtin row remains',
      (r0.body.extensions || []).some(e => e.name === 'assistant' && e.source === 'builtin' && !e.shadowing));
  }

  await stopHub();

  // ── Hub restart (resetApp path): stop must not hang on open
  //    keep-alive connections — the renderer polls constantly, and
  //    server.close() alone waits for them to drain forever. The first
  //    client request after a restart may reuse a dead pooled socket
  //    (ECONNRESET once); a retry must succeed.
  {
    const res = await HUB.restart({ dataDir: tmpDir });
    assert(res && res.ok, 'hub restart failed: ' + JSON.stringify(res));
    _base = `http://127.0.0.1:${res.apiUrl.split(':').pop()}`;
    _token = res.apiToken;
    let restartOk = false, restartErr = '';
    for (let i = 0; i < 3 && !restartOk; i++) {
      try {
        r = await request('GET', '/api/contexts');
        restartOk = r.status === 200 && Array.isArray(r.body && r.body.contexts);
        if (!restartOk) restartErr = 'status=' + r.status;
      } catch (e) { restartErr = e.message; }
    }
    ok('hub serves requests after in-process restart (retry allowed)', restartOk, restartErr);

    // App reinstall/upgrade (version change) restores removed built-ins:
    // the store `removed` hide-flag (set by built-in Remove since
    // 0.2.24) is honored on a same-version start but must not survive a
    // version change. This process's hub keeps its store in memory
    // across restarts — so plant the flag in a fresh dataDir and drive a
    // throwaway hub CHILD process.
    const fs2 = require('fs');
    const legacyDir = fs2.mkdtempSync(path.join(require('os').tmpdir(), 'hub-legacy-ext-'));
    fs2.writeFileSync(path.join(legacyDir, 'embedded-hub.json'), JSON.stringify({
      version: 1,
      app_version: '0.0.1-old',
      contexts: [], agents: [],
      extensions: [{ name: 'skills', enabled: true, removed: true }],
    }));
    const childScript = `
      const HUB = require(${JSON.stringify(path.join(__dirname, '..', 'electron', 'embedded-hub.cjs'))});
      const listExts = async (r) => {
        for (let i = 0; i < 3; i++) {
          try {
            const res = await fetch(r.apiUrl + '/api/extensions', { headers: { Authorization: 'Bearer ' + r.apiToken } });
            return (await res.json()).extensions || [];
          } catch (e) { if (i === 2) throw e; }
        }
      };
      (async () => {
        const r1 = await HUB.start({ dataDir: process.env.LEGACY_DIR, appVersion: '0.0.1-old' });
        const sameVersion = (await listExts(r1)).some(e => e.name === 'skills');
        const r2 = await HUB.restart({ dataDir: process.env.LEGACY_DIR, appVersion: '9.9.9-test' });
        const back = (await listExts(r2)).find(e => e.name === 'skills');
        console.log('LEGACY_RESULT ' + JSON.stringify({ sameVersion, restored: !!(back && back.source === 'builtin') }));
        await HUB.stop();
      })().catch((e) => { console.error(e); process.exit(1); });
    `;
    const childOut = require('node:child_process').execFileSync(
      process.execPath, ['-e', childScript],
      { encoding: 'utf8', timeout: 30000, env: { ...process.env, LEGACY_DIR: legacyDir } });
    const legacy = JSON.parse((childOut.trim().split('\n').pop() || '').replace(/^LEGACY_RESULT /, '') || '{}');
    ok('restore: legacy removed flag honored on same version', legacy.sameVersion === false, childOut);
    ok('restore: removed builtin returns on app version change', legacy.restored === true, childOut);
    await stopHub();
  }

  // ── Summary ─────────────────────────────────────────────────────
  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail) { for (const f of failures) console.error('  ' + f); process.exit(1); }
  process.exit(0);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
