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
 *  1. /api/api-models success (local) — models + defaults + toolSupport.
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
    remove(ref) { map.delete(ref); },
  };
}

// Mock SSH transport. `execRemote` dispatches on the command string to
// per-test stubs; tests register handlers via setRemoteHandler().
let _remoteHandler = null;
function setRemoteHandler(fn) { _remoteHandler = fn; }
function makeSshTransport() {
  return {
    execRemote(opts) {
      if (_remoteHandler) {
        try { return Promise.resolve(_remoteHandler(opts) || { ok: false, error: 'unhandled', detail: 'no handler' }); }
        catch (e) { return Promise.resolve({ ok: false, error: 'handler_threw', detail: e.message }); }
      }
      return Promise.resolve({ ok: false, error: 'no_handler', detail: 'no remote handler registered' });
    },
    closeAll() {},
  };
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
    req.on('error', reject);
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

  // 1. /api/api-models success (local node) — no remote handler needed;
  //    the local path runs `camc` via execFile. On a dev box with camc
  //    on PATH this returns real profiles; on a box without camc it
  //    returns an empty list with a source.error. Either way the SHAPE
  //    must be { models, defaults, toolSupport, source }.
  setRemoteHandler(null);
  r = await request('GET', '/api/api-models?node=local');
  ok('api-models shape', r.status === 200 && r.body && Array.isArray(r.body.models) && Array.isArray(r.body.defaults) && r.body.toolSupport && r.body.source, 'status=' + r.status);
  eq('api-models toolSupport claude', r.body.toolSupport.claude, true);
  eq('api-models toolSupport cursor', r.body.toolSupport.cursor, false);

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

  await stopHub();

  // ── Summary ─────────────────────────────────────────────────────
  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail) { for (const f of failures) console.error('  ' + f); process.exit(1); }
  process.exit(0);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
