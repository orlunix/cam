/**
 * Deterministic fleet-bridge (camb) tests for the embedded Hub.
 *
 * Runs entirely in-process — no Electron, no real SSH, no real camc/camb.
 * The Hub is hand-written CommonJS with two injected collaborators
 * (credentialStore, sshTransport); we require() it, inject mocks, start
 * the HTTP server on a loopback port, and drive /api/* plus the bridge
 * worker loop end to end: worker drain (stubbed exec) → dispatch →
 * inbox write (captured from stubbed writeRemoteFile).
 *
 * Run:  node apps/cam-desktop/test/hub-bridge.test.cjs
 * Exit: 0 = pass, 1 = fail (prints the failing assertions).
 *
 * Coverage:
 *  1. machine.bridge create/update round trip (incl. untouched by
 *     unrelated updates) via the contexts HTTP surface.
 *  2. Worker lifecycle: starts on create-with-bridge and on PUT
 *     toggle-on (drain called within ~3s, camb deployed on first tick),
 *     stops on toggle-off, and re-boots from the store on hub restart.
 *  3. Fleet `list` aggregation across seeded contexts (+ --status /
 *     --last / --json and the plain-text table shape).
 *  4. Target resolution: source-node-first, node: prefix (stripped
 *     before forwarding), fleet ambiguity → candidates, not-found.
 *  5. Consent: targeting a bridge-off node is refused without any exec.
 *  6. Passthrough fidelity: the exact `camc send af12 --text hi` argv
 *     reaches the target node; remote_nonzero relays camc's own stderr;
 *     `msg send` round-trips its reply through exec stdout.
 *  7. Unsupported verbs get the pinned v1 error string.
 *  8. GET /api/contexts/<name>/bridge-status shape (worker or not).
 *  9. bus-audit.jsonl gets exactly one line per dispatch.
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
async function waitFor(name, fn, timeoutMs) {
  const t0 = Date.now();
  for (;;) {
    let v = null;
    try { v = fn(); } catch (_) { /* keep polling */ }
    if (v) { ok(name, true); return v; }
    if (Date.now() - t0 > timeoutMs) { ok(name, false, `timeout after ${timeoutMs}ms`); return null; }
    await sleep(100);
  }
}

// ── In-memory credential store (same shape as hub.test.cjs) ────────

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

// ── Mock SSH transport ─────────────────────────────────────────────
// Three fake nodes. execRemote dispatches on the command string;
// writeRemoteFile captures camb deploys (.cam/camb.tmp) and bridge
// inbox responses (.cam/camb/inbox/<id>.json).

const HOSTS = { nodeA: '10.10.0.1', nodeB: '10.10.0.2', nodeC: '10.10.0.3' };

// Agents each node's `camc --json list` reports (sync seeds the hub
// store from these). Deliberate collisions:
//  - id prefix 'c0ffee00' exists on nodeA AND nodeC (source-node-first)
//  - name 'dup' exists on nodeB AND nodeC (fleet ambiguity)
const AGENTS_BY_HOST = {
  [HOSTS.nodeA]: [
    { id: 'aa11aaaa-1111-4000-8000-aaaaaaaaaaaa', status: 'running', state: 'idle', task: { name: 'alpha', tool: 'claude' }, context_path: '/home/a', transport_type: 'ssh' },
    { id: 'c0ffee00-aaaa-4000-8000-aaaaaaaaaaaa', status: 'idle', state: 'idle', task: { name: 'localdup', tool: 'claude' }, context_path: '/home/a', transport_type: 'ssh' },
  ],
  [HOSTS.nodeB]: [
    { id: 'af12bc34-5678-4000-8000-bbbbbbbbbbbb', status: 'running', state: 'idle', task: { name: 'pdx-034', tool: 'claude' }, context_path: '/home/b', transport_type: 'ssh' },
    { id: 'bb22bbbb-2222-4000-8000-bbbbbbbbbbbb', status: 'exited', state: 'done', task: { name: 'dup', tool: 'codex' }, context_path: '/home/b', transport_type: 'ssh' },
  ],
  [HOSTS.nodeC]: [
    { id: 'c0ffee00-bbbb-4000-8000-cccccccccccc', status: 'running', state: 'idle', task: { name: 'dup', tool: 'claude' }, context_path: '/home/c', transport_type: 'ssh' },
  ],
};

const drainQueues = { [HOSTS.nodeA]: [], [HOSTS.nodeB]: [], [HOSTS.nodeC]: [] };
const drainCount = { [HOSTS.nodeA]: 0, [HOSTS.nodeB]: 0, [HOSTS.nodeC]: 0 };
const execCalls = [];   // {host, command}
const uploads = [];     // {host, remotePath}
const inboxWrites = []; // {host, remotePath, response}
// First camb probe on nodeA reports "missing" so the worker's first
// tick exercises the full deploy path (upload + chmod/mv + verify).
const cambMissingOnce = new Set([HOSTS.nodeA]);

function remoteHandler(opts) {
  const cmd = String(opts.command || '');
  const host = opts.host;
  execCalls.push({ host, command: cmd });
  // Bridge channel (camb)
  if (cmd.includes('--hub-drain')) {
    drainCount[host] = (drainCount[host] || 0) + 1;
    const q = drainQueues[host] || [];
    const items = q.length ? q.splice(0, q.length) : [];
    return { ok: true, stdout: JSON.stringify(items), stderr: '' };
  }
  if (cmd.includes('camb version')) {
    if (cambMissingOnce.has(host)) {
      cambMissingOnce.delete(host);
      return { ok: false, error: 'remote_nonzero', detail: 'exit 1', stderr: '' };
    }
    return { ok: true, stdout: 'camb 0.1.0\n', stderr: '' };
  }
  if (cmd.includes('camb.tmp')) return { ok: true, stdout: '', stderr: '' };      // chmod + mv install
  if (cmd === 'test -x ~/.cam/camb') return { ok: true, stdout: '', stderr: '' }; // post-install verify
  if (cmd.includes('mkdir -p')) return { ok: true, stdout: '', stderr: '' };      // ~/.cam, camb/inbox
  // camc on the node
  if (cmd.includes('camc version')) return { ok: true, stdout: 'camc v99.0.0 (test)\n', stderr: '' };
  if (cmd.includes('--json list')) {
    return { ok: true, stdout: JSON.stringify(AGENTS_BY_HOST[host] || []), stderr: '' };
  }
  if (cmd.startsWith('~/.cam/camc ')) {
    // msg send blocks server-side waiting for the reply; the reply text
    // rides back on exec stdout (the bridge round trip).
    if (/'msg' 'send'/.test(cmd)) return { ok: true, stdout: 'REPLY: bridge-pong\n', stderr: '' };
    // capture against a dead agent: camc exits nonzero and its own
    // stderr is what the bridge user must see.
    if (/'capture'/.test(cmd)) {
      return { ok: false, error: 'remote_nonzero', detail: 'exit status 1', stdout: '', stderr: 'camc: agent not running\n' };
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
    writeRemoteFile(opts) {
      const rec = { host: opts.host, remotePath: String(opts.remotePath || ''), content: opts.content };
      if (rec.remotePath.startsWith('.cam/camb/inbox/')) {
        let parsed = null;
        const text = Buffer.isBuffer(rec.content) ? rec.content.toString('utf8') : String(rec.content || '');
        try { parsed = JSON.parse(text); } catch (_) { /* captured raw below */ }
        inboxWrites.push({ host: rec.host, remotePath: rec.remotePath, response: parsed });
      } else {
        uploads.push(rec);
      }
      return Promise.resolve({ ok: true, bytes: 0 });
    },
    closeAll() {},
  };
}

function callsOn(host, re) {
  return execCalls.filter(c => c.host === host && re.test(c.command)).map(c => c.command);
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

// ── Tests ──────────────────────────────────────────────────────────

async function main() {
  const tmpDir = path.join(os.tmpdir(), 'cam-hub-bridge-test-' + crypto.randomBytes(4).toString('hex'));
  await startHub(tmpDir);

  // ── Seed three nodes. nodeA starts bridge-off (toggled on later),
  //    nodeB is created with bridge on (create-path worker boot),
  //    nodeC stays bridge-off (consent target). Auto-sync on create
  //    seeds each node's agents from the stub's --json list.
  const mk = (name, host, bridge) => request('POST', '/api/contexts', {
    name, path: '/home/u/src', host, user: 'u', port: 22, auth_method: 'agent',
    ...(bridge == null ? {} : { bridge }),
  });
  let r = await mk('nodeA', HOSTS.nodeA);
  eq('create nodeA', r.status, 201);
  eq('nodeA bridge defaults false', r.body && r.body.machine && r.body.machine.bridge, false);
  r = await mk('nodeB', HOSTS.nodeB, true);
  eq('create nodeB', r.status, 201);
  eq('nodeB bridge persisted from create', r.body && r.body.machine && r.body.machine.bridge, true);
  r = await mk('nodeC', HOSTS.nodeC, false);
  eq('create nodeC', r.status, 201);
  eq('nodeC bridge false', r.body && r.body.machine && r.body.machine.bridge, false);

  // Wait for auto-sync (fired by POST /api/contexts) to seed all five
  // agents from the stubbed per-node `camc --json list`.
  {
    const t0 = Date.now();
    let agents = [];
    for (;;) {
      // eslint-disable-next-line no-await-in-loop
      const ar = await request('GET', '/api/agents');
      agents = (ar.body && ar.body.agents) || [];
      if (agents.length === 5) break;
      if (Date.now() - t0 > 6000) break;
      // eslint-disable-next-line no-await-in-loop
      await sleep(100);
    }
    eq('seeded agent count', agents.length, 5);
    ok('agents stamped with context_name',
      agents.some(a => a.id.startsWith('af12bc34') && a.context_name === 'nodeB')
        && agents.some(a => a.id.startsWith('aa11aaaa') && a.context_name === 'nodeA')
        && agents.some(a => a.id.startsWith('c0ffee00-bbbb') && a.context_name === 'nodeC'),
      JSON.stringify(agents.map(a => [a.id, a.context_name])));
  }

  // ── 1. bridge field round trip via PUT ──
  r = await request('PUT', '/api/contexts/nodeC', { bridge: true });
  eq('nodeC PUT bridge true', r.body && r.body.machine && r.body.machine.bridge, true);
  r = await request('GET', '/api/contexts/nodeC');
  eq('nodeC bridge persisted', r.body && r.body.machine && r.body.machine.bridge, true);
  r = await request('PUT', '/api/contexts/nodeC', { bridge: false });
  eq('nodeC PUT bridge false', r.body && r.body.machine && r.body.machine.bridge, false);
  // An unrelated update must not clear the flag.
  r = await request('PUT', '/api/contexts/nodeB', { env_setup: 'export CAM_TEST=1' });
  eq('nodeB unrelated update ok', r.status, 200);
  eq('nodeB bridge survives unrelated update', r.body && r.body.machine && r.body.machine.bridge, true);

  // ── 2a. Worker booted at create for nodeB (bridge:true) ──
  await waitFor('nodeB worker drains after create', () => drainCount[HOSTS.nodeB] >= 1, 3500);

  // ── 2b. Toggle nodeA on: worker starts, first tick deploys camb ──
  r = await request('PUT', '/api/contexts/nodeA', { bridge: true });
  eq('nodeA PUT bridge true', r.body && r.body.machine && r.body.machine.bridge, true);
  await waitFor('nodeA worker drains within ~3s of toggle-on', () => drainCount[HOSTS.nodeA] >= 1, 3500);
  ok('nodeA first tick deployed camb (upload .cam/camb.tmp)',
    uploads.some(u => u.host === HOSTS.nodeA && u.remotePath === '.cam/camb.tmp'),
    JSON.stringify(uploads.map(u => [u.host, u.remotePath])));
  ok('nodeA camb install chmod+mv ran',
    callsOn(HOSTS.nodeA, /chmod 700 "\$HOME\/\.cam\/camb\.tmp" && mv .* ~\/\.cam\/camb/).length === 1);

  // ── 2c. Toggle nodeA off: drains stop ──
  r = await request('PUT', '/api/contexts/nodeA', { bridge: false });
  eq('nodeA PUT bridge false', r.body && r.body.machine && r.body.machine.bridge, false);
  const drainedAtOff = drainCount[HOSTS.nodeA];
  await sleep(2600); // > one poll interval
  eq('nodeA drains stop after toggle-off', drainCount[HOSTS.nodeA], drainedAtOff);

  // ── 2d. Toggle back on: drains resume ──
  r = await request('PUT', '/api/contexts/nodeA', { bridge: true });
  eq('nodeA re-enabled', r.body && r.body.machine && r.body.machine.bridge, true);
  await waitFor('nodeA drains resume after re-enable', () => drainCount[HOSTS.nodeA] > drainedAtOff, 2000);

  // ── 3-8. One drained batch exercises list/resolution/passthrough/consent ──
  const FROM = { agent: 'aa11aaaa-1111-4000-8000-aaaaaaaaaaaa' };
  const reqs = [
    { id: 'r-list-plain',    argv: ['list'],                                     from: FROM, ts: 1 },
    { id: 'r-list-json',     argv: ['list', '--json'],                           from: FROM, ts: 2 },
    { id: 'r-list-status',   argv: ['list', '--status', 'running', '--json'],    from: FROM, ts: 3 },
    { id: 'r-list-last',     argv: ['list', '--last', '1', '--json'],            from: FROM, ts: 4 },
    { id: 'r-send',          argv: ['send', 'af12', '--text', 'hi'],             from: FROM, ts: 5 },
    { id: 'r-status-node',   argv: ['status', 'nodeB:af12'],                     from: FROM, ts: 6 },
    { id: 'r-status-local',  argv: ['status', 'c0ffee00'],                       from: FROM, ts: 7 },
    { id: 'r-status-ambig',  argv: ['status', 'dup'],                            from: FROM, ts: 8 },
    { id: 'r-status-consent', argv: ['status', 'nodeC:c0ffee00-bbbb'],           from: FROM, ts: 9 },
    { id: 'r-capture-dead',  argv: ['capture', 'af12', '--lines', '5'],          from: FROM, ts: 10 },
    { id: 'r-msg',           argv: ['msg', 'send', 'af12', '-t', 'hello-bridge'], from: FROM, ts: 11 },
    { id: 'r-unsupported',   argv: ['run', 'x'],                                 from: FROM, ts: 12 },
    { id: 'r-notfound',      argv: ['status', 'zzzz'],                           from: FROM, ts: 13 },
  ];
  drainQueues[HOSTS.nodeA].push(...reqs);
  const responses = await waitFor('all 13 responses written to nodeA inbox', () => {
    const m = new Map();
    for (const w of inboxWrites) {
      if (w.host === HOSTS.nodeA && w.response && w.response.id) m.set(w.response.id, w);
    }
    return m.size >= reqs.length ? m : null;
  }, 8000);
  const resp = (id) => (responses && responses.get(id) && responses.get(id).response) || {};

  // Fleet list — plain table
  let o = resp('r-list-plain');
  eq('list plain ok', o.ok, true);
  ok('list table header has CONTEXT', /^ID\s+NAME\s+TOOL\s+STATUS\s+CONTEXT/m.test(o.text || ''), o.text);
  ok('list table spans nodes',
    (o.text || '').includes('af12bc34') && (o.text || '').includes('aa11aaaa')
      && (o.text || '').includes('pdx-034') && (o.text || '').includes('nodeB')
      && (o.text || '').includes('nodeC'),
    o.text);

  // Fleet list — JSON + filters
  o = resp('r-list-json');
  eq('list json ok', o.ok, true);
  {
    let arr = null;
    try { arr = JSON.parse(o.text || ''); } catch (_) {}
    ok('list json parses to 5 agents', Array.isArray(arr) && arr.length === 5, o.text && o.text.slice(0, 200));
    ok('list json rows carry context_name', Array.isArray(arr) && arr.every(a => typeof a.context_name === 'string' && a.context_name));
  }
  o = resp('r-list-status');
  {
    let arr = null;
    try { arr = JSON.parse(o.text || ''); } catch (_) {}
    ok('list --status running filters', Array.isArray(arr) && arr.length === 3 && arr.every(a => a.status === 'running'),
      o.text && o.text.slice(0, 200));
  }
  o = resp('r-list-last');
  {
    let arr = null;
    try { arr = JSON.parse(o.text || ''); } catch (_) {}
    ok('list --last 1 caps', Array.isArray(arr) && arr.length === 1, o.text && o.text.slice(0, 200));
  }

  // Passthrough fidelity: exact argv reaches nodeB's camc.
  o = resp('r-send');
  eq('send ok', o.ok, true);
  eq('send relays stdout as text', o.text, 'CAMC-OK\n');
  ok("passthrough: nodeB got `camc send af12 --text hi`",
    callsOn(HOSTS.nodeB, /~\/\.cam\/camc 'send' 'af12' '--text' 'hi'/).length === 1,
    JSON.stringify(callsOn(HOSTS.nodeB, /'send'/)));

  // node: prefix resolves the node and is stripped before forwarding.
  o = resp('r-status-node');
  eq('status nodeB:af12 ok', o.ok, true);
  ok("node: prefix stripped → `camc status af12` on nodeB",
    callsOn(HOSTS.nodeB, /~\/\.cam\/camc 'status' 'af12'/).length === 1,
    JSON.stringify(callsOn(HOSTS.nodeB, /'status'/)));
  ok('no nodeB-prefixed token forwarded',
    !execCalls.some(c => c.command.includes('nodeB:') && c.command.includes('camc')));

  // Source-node-first: 'c0ffee00' exists on nodeA AND nodeC; the call
  // must land on nodeA (the requesting node).
  o = resp('r-status-local');
  eq('status c0ffee00 ok', o.ok, true);
  ok('source-node-first: exec landed on nodeA',
    callsOn(HOSTS.nodeA, /~\/\.cam\/camc 'status' 'c0ffee00'/).length === 1,
    JSON.stringify(callsOn(HOSTS.nodeA, /'status'/)));
  eq('source-node-first: nothing sent to nodeC',
    callsOn(HOSTS.nodeC, /'status' 'c0ffee00'/).length, 0);

  // Fleet ambiguity → candidates, no exec anywhere.
  o = resp('r-status-ambig');
  eq('ambiguous ok false', o.ok, false);
  eq('ambiguous error', o.error, 'ambiguous target');
  ok('ambiguous candidates are <ctx>:<id> <name>',
    Array.isArray(o.candidates) && o.candidates.length === 2
      && o.candidates.some(c => /^nodeB:bb22bbbb-2222-4000-8000-bbbbbbbbbbbb dup$/.test(c))
      && o.candidates.some(c => /^nodeC:c0ffee00-bbbb-4000-8000-cccccccccccc dup$/.test(c)),
    JSON.stringify(o.candidates));

  // Consent: nodeC has bridge off → refused without any exec.
  o = resp('r-status-consent');
  eq('consent ok false', o.ok, false);
  eq('consent error', o.error, 'target node has bridge disabled');
  eq('consent: no exec reached nodeC',
    callsOn(HOSTS.nodeC, /'status' 'c0ffee00-bbbb'/).length, 0);

  // remote_nonzero relays camc's own stderr text.
  o = resp('r-capture-dead');
  eq('capture nonzero ok false', o.ok, false);
  ok('capture nonzero relays camc stderr', /camc: agent not running/.test(o.error || ''), o.error);

  // msg send round trip: reply text comes back through exec stdout.
  o = resp('r-msg');
  eq('msg ok', o.ok, true);
  ok('msg reply text relayed', /bridge-pong/.test(o.text || ''), o.text);
  ok("msg forwarded as `camc msg send af12 -t hello-bridge`",
    callsOn(HOSTS.nodeB, /~\/\.cam\/camc 'msg' 'send' 'af12' '-t' 'hello-bridge'/).length === 1,
    JSON.stringify(callsOn(HOSTS.nodeB, /'msg'/)));

  // Unsupported verb gets the pinned v1 message.
  o = resp('r-unsupported');
  eq('unsupported ok false', o.ok, false);
  eq('unsupported error text', o.error,
    "unsupported verb 'run' via bridge (v1: list, status, capture, send, key, msg)");

  // Unknown agent → not found.
  o = resp('r-notfound');
  eq('notfound ok false', o.ok, false);
  eq('notfound error', o.error, "no agent matches 'zzzz'");

  // Inbox filenames follow <id>.json.
  ok('inbox path shape', !!(responses.get('r-send')
    && responses.get('r-send').remotePath === '.cam/camb/inbox/r-send.json'),
    responses.get('r-send') && responses.get('r-send').remotePath);

  // ── 8. bridge-status route ──
  r = await request('GET', '/api/contexts/nodeA/bridge-status');
  eq('bridge-status nodeA http', r.status, 200);
  ok('bridge-status nodeA running shape',
    r.body && r.body.ok === true && r.body.enabled === true && r.body.running === true
      && typeof r.body.lastPoll === 'string' && r.body.lastError === null
      && r.body.backoffMs === 2000,
    JSON.stringify(r.body));
  r = await request('GET', '/api/contexts/nodeC/bridge-status');
  ok('bridge-status nodeC disabled shape',
    r.body && r.body.ok === true && r.body.enabled === false && r.body.running === false
      && r.body.lastPoll === null && r.body.lastError === null && r.body.backoffMs === null,
    JSON.stringify(r.body));
  r = await request('GET', '/api/contexts/ghost/bridge-status');
  eq('bridge-status unknown context 404', r.status, 404);

  // ── 9. Audit: exactly one jsonl line per dispatched request ──
  {
    const auditFile = path.join(tmpDir, 'bus-audit.jsonl');
    ok('audit file exists', fs.existsSync(auditFile));
    const lines = fs.readFileSync(auditFile, 'utf8').trim().split('\n').filter(Boolean);
    eq('audit line count == dispatched requests', lines.length, reqs.length);
    let entries = null;
    try { entries = lines.map(l => JSON.parse(l)); } catch (_) {}
    ok('audit lines parse as JSON', Array.isArray(entries));
    if (Array.isArray(entries)) {
      const sendEntry = entries.find(e => Array.isArray(e.argv) && e.argv[0] === 'send');
      ok('audit send entry fields',
        !!sendEntry && sendEntry.from_ctx === 'nodeA'
          && sendEntry.from_agent === FROM.agent
          && JSON.stringify(sendEntry.argv) === JSON.stringify(['send', 'af12', '--text', 'hi'])
          && sendEntry.target_ctx === 'nodeB' && sendEntry.ok === true
          && typeof sendEntry.ts === 'string',
        JSON.stringify(sendEntry));
      const consentEntry = entries.find(e => e.error === 'target node has bridge disabled');
      ok('audit consent entry', !!consentEntry && consentEntry.target_ctx === 'nodeC' && consentEntry.ok === false,
        JSON.stringify(consentEntry));
      const listEntry = entries.find(e => Array.isArray(e.argv) && e.argv[0] === 'list');
      ok('audit list entry has null target', !!listEntry && listEntry.target_ctx === null && listEntry.ok === true,
        JSON.stringify(listEntry));
    }
  }

  // ── 2e. Hub restart re-boots workers from the persisted store ──
  {
    const res = await HUB.restart({ dataDir: tmpDir });
    assert(res && res.ok, 'hub restart failed: ' + JSON.stringify(res));
    _base = `http://127.0.0.1:${res.apiUrl.split(':').pop()}`;
    _token = res.apiToken;
    const drainedBeforeRestart = drainCount[HOSTS.nodeA];
    await waitFor('nodeA worker re-boots after hub restart', () => drainCount[HOSTS.nodeA] > drainedBeforeRestart, 3500);
    await waitFor('nodeB worker re-boots after hub restart', () => drainCount[HOSTS.nodeB] > 0, 100);
    r = await request('GET', '/api/contexts/nodeA/bridge-status');
    ok('bridge-status after restart still running',
      r.body && r.body.enabled === true && r.body.running === true, JSON.stringify(r.body));
  }

  await stopHub();

  // ── Summary ──────────────────────────────────────────────────────
  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail) { for (const f of failures) console.error('  ' + f); process.exit(1); }
  process.exit(0);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
