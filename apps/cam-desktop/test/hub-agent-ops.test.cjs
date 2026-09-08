/**
 * Orphan / unreachable-node agent ops tests for the embedded Hub.
 *
 * Bug (2026-09-08): deleting a context left its agents in the store
 * (DELETE /api/contexts does not cascade), and those orphan rows could
 * not be stopped/removed/attached — every op bailed on
 * _contextForAgent's context_not_found ("Terminal attach failed:
 * agent <id>: context_not_found").
 *
 * Design rule (user, 2026-09-08): a context is ONLY a start-agent
 * template (node+path shortcut). Once an agent exists, ops on it must
 * not depend on the context record at all.
 *
 * Fix under test:
 *  - agents stamp the full connection profile at sync (endpoint +
 *    auth_method + credential_ref + jump + local_context_id)
 *  - _contextForAgent falls back to an ephemeral connection profile
 *    built from those stamps when no context claims the endpoint →
 *    ops on orphans of a LIVE node keep working remotely
 *  - context deletion keeps the remembered credential while agents
 *    still reference the endpoint (password nodes stay operable)
 *  - ops against an unreachable node fail honestly; rm flags
 *    can_forget and the explicit ?local=1 escape hatch drops the row
 *    without any remote traffic
 *  - non-connectivity rm failures do NOT offer can_forget
 *  - normal stop/rm against a healthy, context-backed node are
 *    unchanged (no local_only)
 *
 * Runs entirely in-process — no Electron, no real SSH, no real camc.
 * Same harness pattern as hub-bridge.test.cjs.
 *
 * Run:  node apps/cam-desktop/test/hub-agent-ops.test.cjs
 * Exit: 0 = pass, 1 = fail.
 */

'use strict';

const assert = require('assert');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const HUB = require(path.join(__dirname, '..', 'electron', 'embedded-hub.cjs'));

// ── Test harness ───────────────────────────────────────────────────

let pass = 0, fail = 0;
const failures = [];
function ok(name, cond, extra) {
  if (cond) { pass++; }
  else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.error('FAIL ' + name + (extra ? ' — ' + extra : '')); }
}
function eq(name, a, b) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  ok(name, sa === sb, `got ${sa}, want ${sb}`);
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ── In-memory credential store (records put/removeForContext) ──────

const credPuts = [];      // refs passed to put()
const credRemovals = [];  // contextIds passed to removeForContext()
function makeCredentialStore() {
  const map = new Map();
  return {
    available: () => true,
    put(ref, kind, secret) { credPuts.push(ref); map.set(ref, { kind, secret }); return { ok: true, ref, saved_at: 'now' }; },
    get(ref) { const e = map.get(ref); return e ? e.secret : null; },
    removeForContext(id) { credRemovals.push(id); for (const k of [...map.keys()]) { if (k.startsWith(id + ':')) map.delete(k); } },
    removeWithPrefix(p) { for (const k of [...map.keys()]) { if (k.startsWith(p)) map.delete(k); } },
    remove(ref) { map.delete(ref); },
  };
}

// ── Mock SSH transport ─────────────────────────────────────────────

const HOSTS = { nodeA: '10.20.0.1', nodeB: '10.20.0.2', nodeP: '10.20.0.3' };

const AGENTS_BY_HOST = {
  [HOSTS.nodeA]: [
    { id: 'aa11aaaa-1111-4000-8000-aaaaaaaaaaaa', status: 'running', state: 'idle', task: { name: 'rm-me', tool: 'claude' }, context_path: '/home/a', transport_type: 'ssh' },
    { id: 'aa22aaaa-2222-4000-8000-aaaaaaaaaaaa', status: 'running', state: 'idle', task: { name: 'stop-me', tool: 'claude' }, context_path: '/home/a', transport_type: 'ssh' },
    { id: 'aa33aaaa-3333-4000-8000-aaaaaaaaaaaa', status: 'running', state: 'idle', task: { name: 'orphan-stop', tool: 'claude' }, context_path: '/home/a', transport_type: 'ssh' },
    { id: 'aa44aaaa-4444-4000-8000-aaaaaaaaaaaa', status: 'running', state: 'idle', task: { name: 'orphan-rm', tool: 'claude' }, context_path: '/home/a', transport_type: 'ssh' },
  ],
  [HOSTS.nodeB]: [
    { id: 'bb11bbbb-1111-4000-8000-bbbbbbbbbbbb', status: 'running', state: 'idle', task: { name: 'forget-me', tool: 'codex' }, context_path: '/home/b', transport_type: 'ssh' },
    { id: 'bb22bbbb-2222-4000-8000-bbbbbbbbbbbb', status: 'running', state: 'idle', task: { name: 'rm-fails', tool: 'codex' }, context_path: '/home/b', transport_type: 'ssh' },
  ],
  [HOSTS.nodeP]: [
    { id: 'cc11cc11-1111-4000-8000-cccccccccccc', status: 'running', state: 'idle', task: { name: 'pw-node', tool: 'claude' }, context_path: '/home/p', transport_type: 'ssh' },
  ],
};

// failHosts: every exec reports connect_timeout (node dead).
// rmFailHosts: node reachable, but `camc rm` exits nonzero.
const failHosts = new Set();
const rmFailHosts = new Set();
const execCalls = []; // {host, command, hasPassword}

function remoteHandler(opts) {
  const cmd = String(opts.command || '');
  const host = opts.host;
  execCalls.push({ host, command: cmd, hasPassword: !!opts.password });
  if (failHosts.has(host)) {
    return { ok: false, error: 'connect_timeout', detail: 'Timed out while waiting for handshake' };
  }
  if (cmd.includes('camc version')) return { ok: true, stdout: 'camc v99.0.0 (test)\n', stderr: '' };
  if (cmd.includes('--json list')) {
    return { ok: true, stdout: JSON.stringify(AGENTS_BY_HOST[host] || []), stderr: '' };
  }
  if (cmd.startsWith('~/.cam/camc ')) {
    if (/'rm'/.test(cmd) && rmFailHosts.has(host)) {
      return { ok: false, error: 'remote_nonzero', detail: 'exit status 1', stdout: '', stderr: 'camc: rm failed on purpose\n' };
    }
    return { ok: true, stdout: 'CAMC-OK\n', stderr: '' };
  }
  return { ok: true, stdout: '', stderr: '' };
}

function makeSshTransport() {
  return {
    execRemote(opts) {
      try { return Promise.resolve(remoteHandler(opts)); }
      catch (e) { return Promise.resolve({ ok: false, error: 'handler_threw', detail: e.message }); }
    },
    writeRemoteFile() { return Promise.resolve({ ok: true, bytes: 0 }); },
    closeAll() {},
  };
}

function camcCallsOn(host, verb) {
  return execCalls.filter(c => c.host === host
    && c.command.startsWith('~/.cam/camc ')
    && c.command.includes(`'${verb}'`));
}

// ── HTTP helpers ───────────────────────────────────────────────────

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

async function agentRows() {
  const r = await request('GET', '/api/agents');
  return (r.body && r.body.agents) || [];
}

// ── Tests ──────────────────────────────────────────────────────────

async function main() {
  const tmpDir = path.join(os.tmpdir(), 'cam-hub-agent-ops-test-' + crypto.randomBytes(4).toString('hex'));
  fs.mkdirSync(tmpDir, { recursive: true });
  await startHub(tmpDir);

  let r = await request('POST', '/api/contexts', {
    name: 'nodeA', path: '/home/u/src', host: HOSTS.nodeA, user: 'u', port: 22, auth_method: 'agent',
  });
  eq('create nodeA', r.status, 201);
  r = await request('POST', '/api/contexts', {
    name: 'nodeB', path: '/home/u/src', host: HOSTS.nodeB, user: 'u', port: 22, auth_method: 'agent',
  });
  eq('create nodeB', r.status, 201);
  r = await request('POST', '/api/contexts', {
    name: 'nodeP', path: '/home/u/src', host: HOSTS.nodeP, user: 'u', port: 22,
    auth_method: 'password', password: 's3cret', remember_password: true,
  });
  eq('create nodeP (password)', r.status, 201);
  const nodePId = r.body && r.body.id;
  ok('nodeP id returned', !!nodePId);
  ok('nodeP password remembered via credential store',
    credPuts.some(ref => ref.startsWith(`${nodePId}:`)), JSON.stringify(credPuts));

  // Auto-sync on create seeds all three nodes' agents from the stub.
  {
    const t0 = Date.now();
    let agents = [];
    for (;;) {
      // eslint-disable-next-line no-await-in-loop
      agents = await agentRows();
      if (agents.length === 7) break;
      if (Date.now() - t0 > 6000) break;
      // eslint-disable-next-line no-await-in-loop
      await sleep(100);
    }
    eq('seeded agent count', agents.length, 7);
  }

  const A1 = 'aa11aaaa-1111-4000-8000-aaaaaaaaaaaa';
  const A2 = 'aa22aaaa-2222-4000-8000-aaaaaaaaaaaa';
  const A3 = 'aa33aaaa-3333-4000-8000-aaaaaaaaaaaa';
  const A4 = 'aa44aaaa-4444-4000-8000-aaaaaaaaaaaa';
  const B1 = 'bb11bbbb-1111-4000-8000-bbbbbbbbbbbb';
  const B2 = 'bb22bbbb-2222-4000-8000-bbbbbbbbbbbb';
  const P1 = 'cc11cc11-1111-4000-8000-cccccccccccc';

  // ── 1. Normal rm against a healthy, context-backed node ──
  r = await request('DELETE', `/api/agents/${A1}/history`);
  eq('normal rm status', r.status, 200);
  ok('normal rm has no local_only', !(r.body && r.body.local_only), JSON.stringify(r.body));
  ok('normal rm exec reached nodeA with rm --kill',
    camcCallsOn(HOSTS.nodeA, 'rm').some(c => c.command.includes(A1) && c.command.includes('--kill')),
    JSON.stringify(camcCallsOn(HOSTS.nodeA, 'rm')));
  ok('normal rm dropped the row', (await agentRows()).every(a => a.id !== A1));

  // ── 2. Normal stop against a healthy, context-backed node ──
  r = await request('DELETE', `/api/agents/${A2}`);
  eq('normal stop status', r.status, 200);
  ok('normal stop has no local_only', !(r.body && r.body.local_only), JSON.stringify(r.body));
  ok('normal stop exec reached nodeA', camcCallsOn(HOSTS.nodeA, 'stop').some(c => c.command.includes(A2)));
  ok('normal stop mirrored status', (await agentRows()).some(a => a.id === A2 && a.status === 'stopped'));

  // ── 3. Delete the context → its agents stay behind (orphans) ──
  const rmCallsBeforeDelete = camcCallsOn(HOSTS.nodeA, 'rm').length;
  const stopCallsBeforeDelete = camcCallsOn(HOSTS.nodeA, 'stop').length;
  r = await request('DELETE', '/api/contexts/nodeA');
  eq('delete nodeA', r.status, 200);
  ok('orphan rows survive context delete',
    (await agentRows()).some(a => a.id === A3) && (await agentRows()).some(a => a.id === A4));

  // ── 4. Orphan on a LIVE node: stop still execs remotely (ephemeral
  //       connection profile from the agent's stamps) ──
  r = await request('DELETE', `/api/agents/${A3}`);
  eq('orphan stop status', r.status, 200);
  ok('orphan stop is remote (no local_only)', !(r.body && r.body.local_only), JSON.stringify(r.body));
  eq('orphan stop exec reached nodeA', camcCallsOn(HOSTS.nodeA, 'stop').length, stopCallsBeforeDelete + 1);
  ok('orphan marked stopped', (await agentRows()).some(a => a.id === A3 && a.status === 'stopped'));

  // ── 5. Orphan on a LIVE node: rm still execs remotely ──
  r = await request('DELETE', `/api/agents/${A4}/history`);
  eq('orphan rm status', r.status, 200);
  ok('orphan rm is remote (no local_only)', !(r.body && r.body.local_only), JSON.stringify(r.body));
  eq('orphan rm exec reached nodeA', camcCallsOn(HOSTS.nodeA, 'rm').length, rmCallsBeforeDelete + 1);
  ok('orphan rm exec used rm --kill',
    camcCallsOn(HOSTS.nodeA, 'rm').some(c => c.command.includes(A4) && c.command.includes('--kill')));
  ok('orphan rm dropped the row', (await agentRows()).every(a => a.id !== A4));

  // ── 6. Orphan + node dies → honest failure + can_forget ──
  failHosts.add(HOSTS.nodeA);
  r = await request('DELETE', `/api/agents/${A3}/history`);
  eq('dead-orphan rm status', r.status, 502);
  eq('dead-orphan rm can_forget', r.body && r.body.can_forget, true);
  ok('dead-orphan rm kept the row', (await agentRows()).some(a => a.id === A3));

  // ── 7. Explicit escape hatch: ?local=1 drops the row, zero execs ──
  {
    const before = execCalls.length;
    r = await request('DELETE', `/api/agents/${A3}/history?local=1`);
    eq('local rm status', r.status, 200);
    eq('local rm local_only', r.body && r.body.local_only, true);
    eq('local rm made no remote call', execCalls.length, before);
    ok('local rm dropped the row', (await agentRows()).every(a => a.id !== A3));
  }

  // ── 8. rm for a truly-unknown agent still 404s ──
  r = await request('DELETE', `/api/agents/${A3}/history`);
  eq('rm unknown agent 404', r.status, 404);
  eq('rm unknown agent error', r.body && r.body.error, 'agent_not_found');

  // ── 9. Context-backed agent, node dies → can_forget + escape hatch ──
  failHosts.add(HOSTS.nodeB);
  r = await request('DELETE', `/api/agents/${B1}/history`);
  eq('dead-node rm status', r.status, 502);
  eq('dead-node rm can_forget', r.body && r.body.can_forget, true);
  ok('dead-node rm kept the row', (await agentRows()).some(a => a.id === B1));
  r = await request('DELETE', `/api/agents/${B1}/history?local=1`);
  eq('local rm (B1) status', r.status, 200);
  eq('local rm (B1) local_only', r.body && r.body.local_only, true);

  // ── 10. Non-connectivity rm failure does NOT offer can_forget ──
  failHosts.delete(HOSTS.nodeB);
  rmFailHosts.add(HOSTS.nodeB);
  r = await request('DELETE', `/api/agents/${B2}/history`);
  eq('remote_nonzero rm status', r.status, 502);
  eq('remote_nonzero rm error', r.body && r.body.error, 'remote_nonzero');
  ok('remote_nonzero rm has no can_forget', !(r.body && r.body.can_forget), JSON.stringify(r.body));
  ok('remote_nonzero rm kept the row', (await agentRows()).some(a => a.id === B2));

  // ── 11. Password node: context delete keeps the credential while
  //        agents reference the endpoint, and orphan ops still auth ──
  credRemovals.length = 0;
  r = await request('DELETE', '/api/contexts/nodeP');
  eq('delete nodeP', r.status, 200);
  eq('nodeP delete reports credentials_kept', r.body && r.body.credentials_kept, true);
  ok('nodeP credential NOT cascade-deleted',
    !credRemovals.includes(nodePId), JSON.stringify(credRemovals));
  const pwCallsBefore = camcCallsOn(HOSTS.nodeP, 'stop').length;
  r = await request('DELETE', `/api/agents/${P1}`);
  eq('password-orphan stop status', r.status, 200);
  ok('password-orphan stop is remote', !(r.body && r.body.local_only), JSON.stringify(r.body));
  eq('password-orphan stop exec reached nodeP', camcCallsOn(HOSTS.nodeP, 'stop').length, pwCallsBefore + 1);
  ok('password-orphan stop exec carried the remembered password',
    camcCallsOn(HOSTS.nodeP, 'stop').some(c => c.command.includes(P1) && c.hasPassword));

  // ── 12. Endpoint with no agents left → cred cascade still fires ──
  //        (nodeB has B2 left; nodeP now has none after P1 was stopped?
  //        P1 is stopped but still references the endpoint — remove it
  //        first, then re-check the cascade on a fresh password node.)
  r = await request('DELETE', `/api/agents/${P1}/history`);
  eq('rm P1 (orphan, live node)', r.status, 200);
  // nodeP context is already deleted, so there is nothing to cascade
  // against anymore — this assertion pins the endpoint-match rule via
  // nodeB instead: deleting nodeB's context with B2 still referencing
  // keeps any (hypothetical) creds, and B2 stays operable.
  r = await request('DELETE', '/api/contexts/nodeB');
  eq('delete nodeB', r.status, 200);
  rmFailHosts.delete(HOSTS.nodeB);
  r = await request('DELETE', `/api/agents/${B2}/history`);
  eq('orphan rm (B2) after context delete, live node', r.status, 200);
  ok('B2 row dropped', (await agentRows()).every(a => a.id !== B2));

  await stopHub();
  try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (_) {}

  console.log(`\n${pass} passed, ${fail} failed`);
  if (failures.length) { console.error('failures:\n - ' + failures.join('\n - ')); }
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
