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
const tunnelHandlers = {}; // host → onConnection registered via ensureForwardIn
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
      const text = Buffer.isBuffer(rec.content) ? rec.content.toString('utf8') : String(rec.content || '');
      if (rec.remotePath.startsWith('.cam/camb/inbox/')) {
        let parsed = null;
        try { parsed = JSON.parse(text); } catch (_) { /* captured raw below */ }
        inboxWrites.push({ host: rec.host, remotePath: rec.remotePath, response: parsed });
      } else if (rec.remotePath.startsWith('.cam/camb/outbox/')) {
        // Released events: the hub re-publishes to the node's outbox —
        // emulate the node filesystem by feeding them back to the drain.
        try { drainQueues[rec.host].push(JSON.parse(text)); } catch (_) { /* malformed release */ }
        uploads.push(rec);
      } else {
        uploads.push(rec);
      }
      return Promise.resolve({ ok: true, bytes: 0 });
    },
    closeAll() {},
    // Tunnel (§10): record the connection handler per host and report a
    // fixed armed port; tests drive the handler with fake streams.
    ensureForwardIn(opts, { onConnection } = {}) {
      tunnelHandlers[opts.host] = onConnection;
      return Promise.resolve({ ok: true, port: 41777 });
    },
    unforwardIn() { return Promise.resolve({ ok: true }); },
  };
}

function fakeTunnelConn() {
  const listeners = {};
  const out = [];
  let ended = false;
  const stream = {
    on(ev, fn) { listeners[ev] = fn; return stream; },
    write(s) { out.push(String(s)); return true; },
    end() { ended = true; },
  };
  return {
    stream,
    send(obj) { if (listeners.data) listeners.data(Buffer.from(JSON.stringify(obj) + '\n')); },
    async response(timeoutMs = 6000) {
      const t0 = Date.now();
      while (!ended) {
        if (Date.now() - t0 > timeoutMs) throw new Error('tunnel response timeout');
        // eslint-disable-next-line no-await-in-loop
        await sleep(20);
      }
      return JSON.parse(out.join('').split('\n')[0]);
    },
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
  }, 15000); // r-notfound now survives two release re-publishes (~3 ticks) before its terminal error
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

  // ── 10. v1.1 router artifacts: drain claims under --bridge-id, and
  //    this hub publishes a coverage card per bridge-enabled node ──
  {
    const drains = callsOn(HOSTS.nodeA, /--hub-drain --bridge-id [0-9a-f]{32}\b/);
    ok('drain carries --bridge-id', drains.length >= 1,
      JSON.stringify(callsOn(HOSTS.nodeA, /--hub-drain/).slice(0, 1)));
    const bidMatch = /--bridge-id ([0-9a-f]{32})/.exec(drains[0] || '');
    const cardUp = uploads.find(u => u.host === HOSTS.nodeA && /^\.cam\/camb\/hubs\/[0-9a-f]{32}\.json$/.test(u.remotePath));
    ok('coverage card published to nodeA', !!cardUp,
      JSON.stringify(uploads.map(u => u.remotePath).filter(p => p.includes('hubs'))));
    let card = null;
    try { card = JSON.parse(Buffer.isBuffer(cardUp.content) ? cardUp.content.toString('utf8') : String(cardUp.content || '')); } catch (_) {}
    ok('card shape (hub_id/ts/covers/proto)',
      !!card && typeof card.hub_id === 'string' && typeof card.ts === 'number'
        && Array.isArray(card.covers) && card.proto === 1,
      JSON.stringify(card));
    ok('card covers all three nodes by name AND canonical key',
      !!card && ['nodeA', 'nodeB', 'nodeC'].every(n => card.covers.some(e => e && e.name === n))
        && ['u@10.10.0.1:22', 'u@10.10.0.2:22', 'u@10.10.0.3:22'].every(k => card.covers.some(e => e && e.key === k)),
      JSON.stringify(card && card.covers));
    ok('drain bridge-id == card hub_id', !!bidMatch && !!card && bidMatch[1] === card.hub_id);
    ok('card advertises the armed tunnel port', !!card && !!card.tunnel && card.tunnel.port === 41777,
      JSON.stringify(card && card.tunnel));
  }

  // ── 11. release: unroutable bare ref re-published with attempts ──
  //    (r-notfound's terminal 'no agent matches' error was asserted
  //    above; here we pin the two release re-publishes that preceded it)
  {
    const rels = uploads
      .filter(u => u.host === HOSTS.nodeA && u.remotePath === '.cam/camb/outbox/r-notfound.json')
      .map(u => { try { return JSON.parse(Buffer.isBuffer(u.content) ? u.content.toString('utf8') : String(u.content || '')); } catch (_) { return null; } });
    ok('r-notfound released twice (attempts 1 then 2)',
      rels.length === 2 && rels[0] && rels[0].attempts === 1 && rels[1] && rels[1].attempts === 2,
      JSON.stringify(rels.map(x => x && x.attempts)));
  }

  // ── 12. seen-id idempotency: duplicate event ids dispatch once ──
  {
    const statusBefore = callsOn(HOSTS.nodeA, /'status' 'aa11aaaa'/).length;
    drainQueues[HOSTS.nodeA].push(
      { id: 'r-dup', argv: ['status', 'aa11aaaa'], from: FROM, ts: 20 },
      { id: 'r-dup', argv: ['status', 'aa11aaaa'], from: FROM, ts: 21 },
    );
    await waitFor('same-batch dup pair answered', () =>
      inboxWrites.filter(w => w.host === HOSTS.nodeA && w.response && w.response.id === 'r-dup').length >= 2, 6000);
    eq('same-batch duplicate dispatched once',
      callsOn(HOSTS.nodeA, /'status' 'aa11aaaa'/).length - statusBefore, 1);
    // A third occurrence in a LATER drain still gets the cached response.
    drainQueues[HOSTS.nodeA].push({ id: 'r-dup', argv: ['status', 'aa11aaaa'], from: FROM, ts: 22 });
    await waitFor('cross-drain dup answered', () =>
      inboxWrites.filter(w => w.host === HOSTS.nodeA && w.response && w.response.id === 'r-dup').length >= 3, 6000);
    eq('cross-drain duplicate not re-dispatched',
      callsOn(HOSTS.nodeA, /'status' 'aa11aaaa'/).length - statusBefore, 1);
    const dups = inboxWrites.filter(w => w.host === HOSTS.nodeA && w.response && w.response.id === 'r-dup').map(w => w.response);
    ok('all dup responses identical and ok',
      dups.length === 3 && dups.every(d => d.ok === true && d.text === dups[0].text),
      JSON.stringify(dups));
  }

  // ── 13. canonical endpoint-key target: <user@host:port>:<agent> ──
  {
    const before = callsOn(HOSTS.nodeB, /'status' 'af12'/).length;
    drainQueues[HOSTS.nodeA].push({ id: 'r-keyaddr', argv: ['status', 'u@10.10.0.2:22:af12'], from: FROM, ts: 23 });
    const wr = await waitFor('key-addressed response', () =>
      inboxWrites.find(x => x.host === HOSTS.nodeA && x.response && x.response.id === 'r-keyaddr') || null, 6000);
    eq('key-addressed response ok', wr && wr.response && wr.response.ok, true);
    eq('key-addressed landed on nodeB',
      callsOn(HOSTS.nodeB, /'status' 'af12'/).length - before, 1);
  }

  // ── 14. tunnel (§10): token gate + shared dispatch funnel ──
  {
    ok('tunnel forward armed on nodeA', typeof tunnelHandlers[HOSTS.nodeA] === 'function');
    ok('tunnel forward armed on nodeB', typeof tunnelHandlers[HOSTS.nodeB] === 'function');
    const tokUp = uploads.find(u => u.host === HOSTS.nodeA && /^\.cam\/camb\/hubs\/[0-9a-f]{32}\.token$/.test(u.remotePath));
    ok('tunnel token deployed next to the card', !!tokUp,
      JSON.stringify(uploads.map(u => u.remotePath).filter(p => p.includes('.token'))));
    const token = tokUp ? (Buffer.isBuffer(tokUp.content) ? tokUp.content.toString('utf8') : String(tokUp.content)).trim() : '';
    ok('token is the derived 64-hex value', /^[0-9a-f]{64}$/.test(token), token);

    // Wrong token → unauthorized, nothing dispatched.
    const c1 = fakeTunnelConn();
    tunnelHandlers[HOSTS.nodeA]({ stream: c1.stream, info: {} });
    c1.send({ token: 'wrong', request: { id: 't-unauth', argv: ['list'], from: FROM, ts: 30 } });
    const r1 = await c1.response();
    eq('tunnel bad token rejected', r1.ok, false);
    eq('tunnel bad token error', r1.error, 'unauthorized');

    // Right token → dispatched through the shared funnel to nodeB.
    const c2 = fakeTunnelConn();
    tunnelHandlers[HOSTS.nodeA]({ stream: c2.stream, info: {} });
    c2.send({ token, request: { id: 't-msg', argv: ['msg', 'send', 'af12', '-t', 'via-tunnel'], from: FROM, ts: 31 } });
    const r2 = await c2.response(8000);
    eq('tunnel msg ok', r2 && r2.ok, true);
    eq('tunnel msg id echoes', r2 && r2.id, 't-msg');
    ok('tunnel msg reply relayed', /bridge-pong/.test((r2 && r2.text) || ''), r2 && r2.text);
    eq('tunnel msg dispatched exactly once',
      callsOn(HOSTS.nodeB, /'msg' 'send' 'af12' '-t' 'via-tunnel'/).length, 1);

    // Same event id again over the tunnel → cached response, no 2nd exec.
    const c3 = fakeTunnelConn();
    tunnelHandlers[HOSTS.nodeA]({ stream: c3.stream, info: {} });
    c3.send({ token, request: { id: 't-msg', argv: ['msg', 'send', 'af12', '-t', 'via-tunnel'], from: FROM, ts: 32 } });
    const r3 = await c3.response(8000);
    eq('tunnel duplicate gets cached response', r3 && r3.ok, true);
    eq('tunnel duplicate not re-dispatched',
      callsOn(HOSTS.nodeB, /'msg' 'send' 'af12' '-t' 'via-tunnel'/).length, 1);

    // Unroutable bare ref over the tunnel → not_my_fleet (the ferry
    // cascades to the next tunnel / mailbox instead of failing).
    const c4 = fakeTunnelConn();
    tunnelHandlers[HOSTS.nodeA]({ stream: c4.stream, info: {} });
    c4.send({ token, request: { id: 't-ghost', argv: ['status', 'zzzz'], from: FROM, ts: 33 } });
    const r4 = await c4.response();
    eq('tunnel unroutable ok false', r4 && r4.ok, false);
    ok('tunnel unroutable is not_my_fleet', /^not_my_fleet:/.test((r4 && r4.error) || ''), r4 && r4.error);
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
