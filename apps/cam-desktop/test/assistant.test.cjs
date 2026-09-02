'use strict';

/* assistant.test.cjs — AssistantHost + the real cam-assist bundle
 * (pi-agent-core + pi-ai) against a mock OpenAI-compatible endpoint.
 *
 * Covers the seams that actually break:
 *  - save = validate (setConfig probes GET /models before persisting)
 *  - token lands in the credential store, never in assistant.json
 *  - child boot → configure → send → streamed done through the event log
 *  - the committed bundle is the one under test (skipped only when a
 *    fresh checkout hasn't built it yet — build with
 *    `cd extensions/vendor/cam-assist && npm install && npm run build`)
 *
 * Packaged-runtime verification (ELECTRON_RUN_AS_NODE on the real
 * Electron binary) is the MSI smoke test's job, per the
 * connection-layer rule — this suite is the fast reference.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const assert = require('assert');

const host = require(path.join(__dirname, '..', 'electron', 'assistant-host.cjs'));

let n = 0;
function ok(cond, msg) { assert(cond, msg); n++; }

const BUNDLE = path.join(__dirname, '..', '..', '..', 'extensions', 'vendor', 'cam-assist', 'dist', 'cam-assist.js');
if (!fs.existsSync(BUNDLE)) {
  console.log('SKIP assistant.test: cam-assist bundle not built (extensions/vendor/cam-assist/dist/cam-assist.js)');
  process.exit(0);
}

// In-memory credential-store stub (same shape as credential-store.cjs).
function makeCredStub() {
  const items = new Map();
  return {
    items,
    put(ref, kind, secret) { items.set(ref, { kind, secret, saved_at: new Date().toISOString() }); return { ok: true, ref, kind }; },
    get(ref) { return items.has(ref) ? items.get(ref).secret : null; },
    metadata(ref) { return items.has(ref) ? { ref, kind: items.get(ref).kind } : null; },
    remove(ref) { items.delete(ref); },
  };
}

function makeMockLlm() {
  const state = { completionRequests: [] };
  const server = http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/v1/models') {
      if (req.headers.authorization !== 'Bearer good-token') {
        res.writeHead(401, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: { message: 'bad token' } }));
        return;
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ object: 'list', data: [{ id: 'mock-2', object: 'model' }, { id: 'mock-1', object: 'model' }] }));
      return;
    }
    if (req.method === 'POST' && req.url === '/v1/chat/completions') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        const parsed = JSON.parse(body);
        state.completionRequests.push(parsed);
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        const chunk = (delta, finish) =>
          `data: ${JSON.stringify({ id: 'x', object: 'chat.completion.chunk', created: 0, model: parsed.model, choices: [{ index: 0, delta, finish_reason: finish || null }] })}\n\n`;
        const streamWords = (words, delayMs) => {
          let i = 0;
          const tick = () => {
            if (i < words.length) {
              res.write(chunk(i === 0 ? { role: 'assistant', content: words[i] } : { content: words[i] }));
              i++;
              setTimeout(tick, delayMs || 15);
            } else {
              res.write(chunk({}, 'stop'));
              res.write('data: [DONE]\n\n');
              res.end();
            }
          };
          tick();
        };
        // Tool-call script: only the "health" prompt triggers it — a
        // request carrying tools and no tool result yet asks for the cam
        // tool; the follow-up (with the tool result) answers in text.
        // A follow-up request after a tool call has the tool RESULT as
        // the last message (real OpenAI protocol shape) — checking "any
        // tool message in history" would poison every later round.
        const lastMsg = parsed.messages[parsed.messages.length - 1];
        const hasToolResult = !!(lastMsg && lastMsg.role === 'tool');
        const offeredTools = Array.isArray(parsed.tools) && parsed.tools.length > 0;
        const lastUser = [...parsed.messages].reverse().find((m) => m.role === 'user');
        const userText = lastUser ? (typeof lastUser.content === 'string' ? lastUser.content : JSON.stringify(lastUser.content)) : '';
        // Bash script: "runshell" prompts call the local bash tool when
        // it was offered (shell access on), otherwise answer plainly.
        const wantsBash = /runshell/i.test(userText);
        const offeredBash = offeredTools && parsed.tools.some((t) => t && t.function && t.function.name === 'bash');
        if (wantsBash && !hasToolResult) {
          if (!offeredBash) { streamWords(['No', ' shell', ' available.']); return; }
          const args = JSON.stringify({ command: 'echo hi-from-shell' });
          res.write(chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'call_b1', type: 'function', function: { name: 'bash', arguments: args.slice(0, 15) } }] }));
          res.write(chunk({ tool_calls: [{ index: 0, function: { arguments: args.slice(15) } }] }));
          res.write(chunk({}, 'tool_calls'));
          res.write('data: [DONE]\n\n');
          res.end();
          return;
        }
        // Tool-call script: only the "health" prompt triggers it — a
        // request carrying tools and no tool result yet asks for the cam
        // tool; the follow-up (with the tool result) answers in text.
        const wantsTool = /health/i.test(userText);
        if (offeredTools && wantsTool && !hasToolResult) {
          const args = JSON.stringify({ method: 'GET', path: '/api/system/health' });
          res.write(chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'call_1', type: 'function', function: { name: 'cam', arguments: args.slice(0, 20) } }] }));
          res.write(chunk({ tool_calls: [{ index: 0, function: { arguments: args.slice(20) } }] }));
          res.write(chunk({}, 'tool_calls'));
          res.write('data: [DONE]\n\n');
          res.end();
          return;
        }
        if (hasToolResult) {
          const toolMsg = lastMsg;
          state.toolResults = state.toolResults || [];
          state.toolResults.push(String(toolMsg.content || ''));
          if (/hi-from-shell/.test(String(toolMsg.content || ''))) { streamWords(['Shell', ' said', ' hi-from-shell']); return; }
          streamWords(['Hub', ' is', ' healthy.']);
          return;
        }
        // Reasoning script: "think" prompts stream reasoning_content
        // (kimi/deepseek style) before the answer text.
        if (/think/i.test(userText)) {
          res.write(chunk({ role: 'assistant', reasoning_content: 'reasoning ' }));
          res.write(chunk({ reasoning_content: 'step.' }));
          res.write(chunk({ content: 'Thought' }));
          res.write(chunk({ content: ' out.' }));
          res.write(chunk({}, 'stop'));
          res.write('data: [DONE]\n\n');
          res.end();
          return;
        }
        // Slow script: keeps the turn streaming long enough for a
        // deterministic mid-run follow-up send.
        if (/slow/i.test(userText)) {
          streamWords(Array.from({ length: 30 }, (_, i) => ` w${i}`), 50);
          return;
        }
        streamWords(['Hello', ' from', ' mock', ' LLM', '!']);
      });
      return;
    }
    res.writeHead(404); res.end('nope');
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, state, url: `http://127.0.0.1:${server.address().port}/v1` }));
  });
}

async function waitFor(fn, what, timeoutMs = 15000) {
  const t0 = Date.now();
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() - t0 > timeoutMs) throw new Error('timeout waiting for ' + what);
    await new Promise((r) => setTimeout(r, 100));
  }
}

(async () => {
  const mock = await makeMockLlm();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'assistant-test-'));
  const creds = makeCredStub();

  const logLines = [];
  host._resetForTests();
  host.configure({ dataDir: tmp, credentialStore: creds, logger: (m) => logLines.push(String(m)) });

  // ── status before any config ──
  let st = host.status();
  ok(st.ok && st.status === 'stopped' && !st.configured && !st.hasToken, 'fresh status: stopped + unconfigured');

  // ── endpoint validation (no child needed) ──
  let r = await host.fetchModels('not-a-url', 'x');
  ok(!r.ok && r.error === 'invalid_url', 'fetchModels rejects a non-URL');
  r = await host.fetchModels(mock.url, 'bad-token');
  ok(!r.ok && r.error === 'auth_failed', 'fetchModels maps 401 to auth_failed');
  r = await host.fetchModels(mock.url, 'good-token');
  ok(r.ok && r.models.length === 2 && r.models[0] === 'mock-1', 'fetchModels lists + sorts model ids');

  // ── setConfig = save with validation ──
  r = await host.setConfig({ apiUrl: mock.url, model: 'mock-1', token: 'bad-token' });
  ok(!r.ok && r.error === 'auth_failed', 'setConfig refuses to persist an invalid token');
  ok(!fs.existsSync(path.join(tmp, 'ext-data', 'assistant', 'config.json')), 'nothing persisted on failed validation');
  r = await host.setConfig({ apiUrl: mock.url, model: 'no-such-model', token: 'good-token' });
  ok(!r.ok && r.error === 'model_not_listed' && Array.isArray(r.models), 'setConfig rejects a model the endpoint does not list');
  r = await host.setConfig({ apiUrl: mock.url, model: 'mock-1', token: 'good-token' });
  ok(r.ok && r.models.length === 2, 'setConfig ok returns the model list');
  st = host.status();
  ok(st.configured && st.hasToken && st.apiUrl === mock.url && st.model === 'mock-1', 'status reflects the saved config');
  ok(creds.get('assistant:llm-token') === 'good-token', 'token stored in the credential store');
  const cfgOnDisk = JSON.parse(fs.readFileSync(path.join(tmp, 'ext-data', 'assistant', 'config.json'), 'utf8'));
  ok(cfgOnDisk.apiUrl === mock.url && cfgOnDisk.model === 'mock-1' && !JSON.stringify(cfgOnDisk).includes('good-token'),
    'ext-data config.json holds url+model only, never the token');

  // ── child lifecycle + Q&A round-trip through the real bundle ──
  r = host.start();
  ok(r.ok && r.status === 'starting', 'start spawns the child');
  await waitFor(() => host.status().status === 'idle', 'child configured + idle');
  r = host.send({ text: 'say hi' });
  ok(r.ok, 'send accepted');
  const done = await waitFor(() => {
    const p = host.poll({ since: 0 });
    return p.events.find((e) => e.type === 'done');
  }, 'assistant done event');
  ok(done.text === 'Hello from mock LLM!', 'assistant answered through the mock LLM', JSON.stringify(done));
  const deltas = host.poll({ since: 0 }).events.filter((e) => e.type === 'delta');
  ok(deltas.length >= 2 && deltas[deltas.length - 1].text === 'Hello from mock LLM!',
    'cumulative deltas streamed before done');
  ok(mock.state.completionRequests.length === 1
    && mock.state.completionRequests[0].model === 'mock-1'
    && /CAM Desktop assistant/.test(mock.state.completionRequests[0].messages[0].content),
    'child sent system prompt + configured model');
  await waitFor(() => host.status().status === 'idle', 'back to idle');

  // ── poll cursor: only newer events come back ──
  const p1 = host.poll({ since: 0 });
  const p2 = host.poll({ since: p1.seq });
  ok(p2.events.length === 0 && p2.seq === p1.seq, 'poll cursor drains without re-delivery');

  // ── the generic `cam` tool: model → tool call → mock hub → answer ──
  const hubCalls = [];
  const hubServer = http.createServer((req, res) => {
    hubCalls.push({ method: req.method, url: req.url, auth: req.headers.authorization });
    if (req.url === '/api/system/health' && req.headers.authorization === 'Bearer hub-token') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true, status: 'up' }));
      return;
    }
    res.writeHead(401, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'unauthorized' }));
  });
  await new Promise((r) => hubServer.listen(0, '127.0.0.1', r));
  const hubUrl = `http://127.0.0.1:${hubServer.address().port}`;
  host.setHub({ url: hubUrl, token: 'hub-token' });
  r = host.send({ text: 'check the hub health please' });
  ok(r.ok, 'send for tool-call flow accepted');
  const toolDone = await waitFor(() => {
    const p = host.poll({ since: 0 });
    const ds = p.events.filter((e) => e.type === 'done');
    return ds.length > 1 ? ds[ds.length - 1] : null;
  }, 'tool-call done event');
  ok(toolDone.text === 'Hub is healthy.', 'assistant answered via the cam tool', JSON.stringify(toolDone));
  ok(hubCalls.length === 1 && hubCalls[0].url === '/api/system/health' && hubCalls[0].auth === 'Bearer hub-token',
    'child called the mock hub with the injected token', JSON.stringify(hubCalls));
  ok((mock.state.toolResults || []).some((t) => /HTTP 200/.test(t) && /"up"/.test(t)),
    'tool result flowed back to the model', JSON.stringify(mock.state.toolResults));
  const toolEvents = host.poll({ since: 0 }).events.filter((e) => e.type === 'tool' && e.name === 'cam');
  ok(toolEvents.some((e) => e.phase === 'start') && toolEvents.some((e) => e.phase === 'end' && e.isError === false),
    'tool start/end events in the view event log');
  ok(logLines.some((l) => /GET \/api\/system\/health → 200/.test(l)),
    'hub_call audit line logged (the [assistant] prefix is main.cjs\'s)', JSON.stringify(logLines.slice(-4)));
  await waitFor(() => host.status().status === 'idle', 'idle after tool run');
  hubServer.close();

  // ── local shell, DIRECT flavor (0.6.0): the bash tool is ALWAYS
  // registered and executes in the child process itself — no bridge ──
  r = host.send({ text: 'runshell please' });
  ok(r.ok, 'send for direct-shell round accepted');
  const directDone = await waitFor(() => {
    const ds = host.poll({ since: 0 }).events.filter((e) => e.type === 'done');
    return ds.length && /hi-from-shell/.test(ds[ds.length - 1].text) ? ds[ds.length - 1] : null;
  }, 'direct-shell done event');
  ok(/Shell said hi-from-shell/.test(directDone.text), 'bash tool runs locally with NO bridge configured', JSON.stringify(directDone));
  await waitFor(() => host.status().status === 'idle', 'idle after direct-shell round');

  // ── saved config carries no bridge pair (0.2.36 cleanup) ──
  const cfgNoBridge = JSON.parse(fs.readFileSync(path.join(tmp, 'ext-data', 'assistant', 'config.json'), 'utf8'));
  ok(!('shellUrl' in cfgNoBridge), 'saved config has no shellUrl field', JSON.stringify(cfgNoBridge));

  // ── concurrent saves: the superseded configure wait must SETTLE
  // (rejected), not hang the first IPC invoke forever ──
  const loadBefore = host.poll({ since: 0 }).events.filter((e) => e.type === 'load_done').length;
  const [sv1, sv2] = await Promise.all([
    host.setConfig({ apiUrl: mock.url, model: 'mock-1' }),
    host.setConfig({ apiUrl: mock.url, model: 'mock-1' }),
  ]);
  ok(sv1.ok || /superseded/.test(sv1.detail || ''), 'save1 settled (ok or superseded)', JSON.stringify(sv1));
  ok(sv2.ok || /superseded/.test(sv2.detail || ''), 'save2 settled (ok or superseded)', JSON.stringify(sv2));
  // Both saves wrote 'configure' to the child → two reconfigure+reload
  // cycles. Drain them BOTH before any further send, or a late load_done
  // lands mid-send and clobbers the next section's run.
  await waitFor(() => host.poll({ since: 0 }).events.filter((e) => e.type === 'load_done').length >= loadBefore + 2,
    'both reconfigures drained');

  // ── defaults: empty url/model resolve to the NVIDIA inference defaults ──
  const rsv0 = host._resolveConfig({});
  ok(rsv0.apiUrl === host.DEFAULT_API_URL && rsv0.model === host.DEFAULT_MODEL,
    'empty form resolves to the built-in defaults');
  ok(host.DEFAULT_API_URL === 'https://inference-api.nvidia.com/v1' && host.DEFAULT_MODEL === 'nvidia/moonshotai/kimi-k3',
    'the defaults are the NVIDIA inference endpoint + kimi-k3');
  const rsv1 = host._resolveConfig({ apiUrl: ' http://x/v1/ ', model: ' m ' });
  ok(rsv1.apiUrl === 'http://x/v1' && rsv1.model === 'm', 'explicit values win (trimmed, trailing slash dropped)');
  ok(host.status().defaults && host.status().defaults.apiUrl === host.DEFAULT_API_URL,
    'status exposes the defaults for the Settings form');

  // ── reasoning stream: thinking events before the answer ──
  host.reset();
  r = host.send({ text: 'think about it' });
  ok(r.ok, 'thinking-flow send accepted');
  const thinkDone = await waitFor(() => host.poll({ since: 0 }).events.find((e) => e.type === 'done'),
    'thinking-flow done event');
  const thinks = host.poll({ since: 0 }).events.filter((e) => e.type === 'thinking');
  ok(thinks.length >= 1 && thinks[thinks.length - 1].text === 'reasoning step.',
    'cumulative thinking events streamed before the answer', JSON.stringify(thinks));
  ok(thinkDone.text === 'Thought out.', 'answer text after the reasoning', JSON.stringify(thinkDone));
  ok(host.poll({ since: 0 }).events.some((e) => e.type === 'turn'), 'turn boundary event present');
  await waitFor(() => host.status().status === 'idle', 'idle after thinking flow');

  // ── mid-run send queues (followUp) instead of erroring "busy" ──
  host.reset();
  const reqsBefore = mock.state.completionRequests.length;
  host.send({ text: 'slow first message' });
  await waitFor(() => host.poll({ since: 0 }).events.some((e) => e.type === 'turn'), 'first turn streaming');
  r = host.send({ text: 'second please' });
  ok(r.ok, 'mid-run send accepted (queued, not rejected)');
  await waitFor(() => {
    const p = host.poll({ since: 0 });
    return p.events.some((e) => e.type === 'done') && mock.state.completionRequests.length >= reqsBefore + 2 ? p : null;
  }, 'queued follow-up answered');
  ok(host.poll({ since: 0 }).events.some((e) => e.type === 'queued'), 'queued event emitted for the mid-run send');
  ok(host.poll({ since: 0 }).events.filter((e) => e.type === 'turn').length >= 2, 'one turn per message');
  ok(mock.state.completionRequests.slice(reqsBefore).some((q) => /second please/.test(JSON.stringify(q.messages))),
    'the follow-up reached the model');
  await waitFor(() => host.status().status === 'idle', 'idle after queued flow');

  // ── transcript persistence: the active thread's JSONL rehydrates ──
  const th0 = host.threads();
  ok(th0.ok && th0.active && th0.threads.length === 1, 'one active thread after the first chats');
  const evFile = path.join(tmp, 'ext-data', 'assistant', 'threads', th0.active + '.jsonl');
  ok(fs.existsSync(evFile), 'active thread JSONL persisted under ext-data/assistant/threads');
  const preRestart = host.poll({ since: 0 }).events;
  ok(preRestart.some((e) => e.type === 'user') && preRestart.some((e) => e.type === 'done'),
    'user + done events in the live log (complete transcript)');
  // Streaming intermediates are live-only: they must never hit disk
  // (they flooded thread files ~97% and truncated visible history).
  const onDisk = fs.readFileSync(evFile, 'utf8');
  ok(!/"type":"delta"/.test(onDisk) && !/"type":"thinking"/.test(onDisk),
    'delta/thinking events are NOT persisted to the thread file');
  ok(/"type":"user"/.test(onDisk) && /"type":"done"/.test(onDisk),
    'durable user/done events persisted');
  // Legacy files (written before the delta-free rule) carry streaming
  // lines: rehydration must skip them yet keep the full durable history
  // and continue the seq past them.
  fs.appendFileSync(evFile,
    JSON.stringify({ seq: 9001, type: 'delta', text: 'STREAM' }) + '\n'
    + JSON.stringify({ seq: 9002, type: 'thinking', text: 'THINK' }) + '\n');
  host._resetForTests();
  ok(host.poll({ since: 0 }).events.length === 0, 'in-memory log dropped on test reset');
  host.configure({ dataDir: tmp, credentialStore: creds, logger: () => {} });
  const replayed = host.poll({ since: 0 }).events;
  ok(replayed.some((e) => e.type === 'user') && replayed.some((e) => e.type === 'done'),
    'transcript rehydrated from the thread file after a host restart');
  ok(!replayed.some((e) => e.type === 'delta' || e.type === 'thinking'),
    'legacy streaming lines skipped on rehydrate');
  const afterReplay = host.poll({ since: 0 });
  ok(afterReplay.seq >= 9002, 'seq continues past skipped legacy lines');

  // ── threads: newChat archives, openThread restores, deleteThread removes ──
  const firstId = th0.active;
  r = host.newChat();
  ok(r.ok && r.active && r.active !== firstId, 'newChat mints a fresh thread');
  ok(host.poll({ since: 0 }).events.every((e) => e.type !== 'user'), 'fresh thread has no user events');
  ok(host.threads().threads.length === 2 && host.threads().active === r.active,
    'threads index lists both, active switched');
  const newId = r.active;
  r = host.openThread({ id: firstId });
  ok(r.ok && r.active === firstId
    && host.poll({ since: 0 }).events.some((e) => e.type === 'user'),
    'openThread restores the archived transcript');
  ok((host.threads().threads.find((t) => t.id === firstId) || {}).title,
    'thread title derived from the first user message');
  r = host.openThread({ id: 'nope' });
  ok(!r.ok && r.error === 'unknown_thread', 'openThread rejects an unknown id');
  r = host.deleteThread({ id: firstId });
  ok(r.ok && host.threads().threads.length === 1 && host.threads().active === newId,
    'deleteThread removes it and falls back to the remaining thread');
  ok(!fs.existsSync(evFile), 'deleted thread file is gone');
  host.reset();
  ok(host.poll({ since: 0 }).events.every((e) => e.type !== 'done'),
    'reset wipes the active thread transcript');

  // ── stop ends the child; config survives a host "restart" ──
  host.stop();
  ok(host.status().status === 'stopped', 'stop leaves status stopped');
  host._resetForTests();
  host.configure({ dataDir: tmp, credentialStore: creds, logger: () => {} });
  st = host.status();
  ok(st.configured && st.apiUrl === mock.url && st.model === 'mock-1', 'config + token survive a host restart');

  // ── legacy config migration: <dataDir>/assistant.json → ext-data ──
  host._resetForTests();
  fs.rmSync(path.join(tmp, 'ext-data'), { recursive: true, force: true });
  fs.writeFileSync(path.join(tmp, 'assistant.json'), JSON.stringify({ apiUrl: 'http://legacy/v1', model: 'legacy-m' }));
  host.configure({ dataDir: tmp, credentialStore: creds, logger: () => {} });
  st = host.status();
  ok(st.apiUrl === 'http://legacy/v1' && st.model === 'legacy-m', 'legacy assistant.json migrated');
  ok(!fs.existsSync(path.join(tmp, 'assistant.json'))
    && fs.existsSync(path.join(tmp, 'ext-data', 'assistant', 'config.json')),
    'legacy file moved into ext-data');

  // ── legacy shell-bridge pair is stripped on first boot (0.2.36) ──
  host._resetForTests();
  fs.mkdirSync(path.join(tmp, 'ext-data', 'assistant'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'ext-data', 'assistant', 'config.json'),
    JSON.stringify({ apiUrl: 'http://legacy/v1', model: 'legacy-m', shellUrl: 'http://127.0.0.1:54822' }));
  creds.put('assistant:shell-token', 'shell-token', 'stale-shell');
  host.configure({ dataDir: tmp, credentialStore: creds, logger: () => {} });
  st = host.status();
  ok(st.apiUrl === 'http://legacy/v1' && st.model === 'legacy-m', 'apiUrl/model kept through the bridge strip');
  const strippedCfg = JSON.parse(fs.readFileSync(path.join(tmp, 'ext-data', 'assistant', 'config.json'), 'utf8'));
  ok(!('shellUrl' in strippedCfg), 'legacy shellUrl stripped from config.json');
  ok(!creds.get('assistant:shell-token'), 'legacy shell token removed from the credential store');

  // ── version-gated bundle shadowing: a NEWER user package's cam-assist.js wins ──
  const shadowDir = path.join(tmp, 'extensions', 'assistant');
  fs.mkdirSync(shadowDir, { recursive: true });
  fs.writeFileSync(path.join(shadowDir, 'manifest.yaml'), 'name: assistant\nversion: 99.0.0\n');
  fs.writeFileSync(path.join(shadowDir, 'cam-assist.js'),
    'process.stdout.write(JSON.stringify({type:"ready",version:"shadow"})+"\\n");\n'
    + 'require("node:readline").createInterface({input:process.stdin,terminal:false}).on("line",()=>{});\n');
  const shadowLogs = [];
  host.configure({ logger: (m) => shadowLogs.push(String(m)) });
  r = host.start();
  ok(r.ok, 'start with a shadow bundle accepted');
  await waitFor(() => shadowLogs.some((l) => /cam-assist vshadow/.test(l)), 'shadow bundle booted');
  ok(shadowLogs.some((l) => /bundle: .*extensions.assistant.cam-assist\.js/.test(l.replace(/\\/g, '/'))),
    'host logged the shadow bundle path', JSON.stringify(shadowLogs));
  host.stop();

  // A tie or an OLDER user copy loses to the built-in — an app
  // reinstall/upgrade must repair stale shadows.
  const builtinVer = /^version:\s*(\S+)$/m.exec(
    fs.readFileSync(path.join(__dirname, '..', '..', '..', 'extensions', 'packages', 'assistant', 'manifest.yaml'), 'utf8'))[1];
  const entryVer = /const VERSION = '([^']+)'/.exec(
    fs.readFileSync(path.join(__dirname, '..', '..', '..', 'extensions', 'vendor', 'cam-assist', 'entry.js'), 'utf8'))[1];
  for (const v of [builtinVer, '0.0.1']) {
    fs.writeFileSync(path.join(shadowDir, 'manifest.yaml'), `name: assistant\nversion: ${v}\n`);
    shadowLogs.length = 0;
    r = host.start();
    ok(r.ok, `start accepted with a ${v} shadow present`);
    await waitFor(() => shadowLogs.some((l) => l.includes(`child ready (cam-assist v${entryVer})`)),
      `bundled child boots over a ${v} shadow (tie/older → built-in wins)`);
    host.stop();
  }
  // Restore the newer shadow for the MAS gate below.
  fs.writeFileSync(path.join(shadowDir, 'manifest.yaml'), 'name: assistant\nversion: 99.0.0\n');

  // ── MAS gate (test hook): user-copy bundle ignored (Apple 2.5.2) ──
  host._setMasForTests(true);
  shadowLogs.length = 0;
  r = host.start();
  ok(r.ok, 'start accepted on MAS');
  await waitFor(() => shadowLogs.some((l) => l.includes(`child ready (cam-assist v${entryVer})`)),
    'bundled child booted on MAS');
  ok(shadowLogs.some((l) => /bundle: .*vendor.cam-assist.dist/.test(l.replace(/\\/g, '/'))),
    'MAS ignores the user-copy bundle (Apple 2.5.2)', JSON.stringify(shadowLogs));
  host.stop();
  host._setMasForTests(false);

  host.stop();
  mock.server.close();
  console.log(`${n} passed, 0 failed`);
  process.exit(0);
})().catch((e) => {
  console.error('FATAL', e);
  try {
    const evs = host.poll({ since: 0 }).events.map((x) => `${x.type}:${String(x.text || x.state || x.error || '').slice(0, 60)}`);
    console.error('EVENTS', JSON.stringify(evs));
  } catch (_) {}
  try { host.stop(); } catch (_) {}
  process.exit(1);
});
