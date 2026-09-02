'use strict';

/* cam-assist — CAM Desktop assistant child process (SDK edition).
 *
 * Spawned by the app's main process as
 *   ELECTRON_RUN_AS_NODE=1 <electron-binary> cam-assist.js
 * (the app binary IS a Node runtime — no standalone download, no
 * per-platform matrix; design: docs/desktop/assistant-design.md).
 *
 * 0.5.0: migrated from raw pi-agent-core to the pi-coding-agent SDK
 * (@mariozechner/pi-coding-agent@0.73.1 — the last Node-20-compatible
 * line; the @earendil-works/* rename requires Node ≥22). Inherited from
 * upstream: SessionManager (full JSONL history per thread), auto
 * compaction, Agent-Skills loading (progressive disclosure via a
 * skills-scoped `read` tool). Ours unchanged: the stdio contract below,
 * the `cam` hub-backoffice tool, and the durable `memory` tool
 * (memory.md injected as a context file).
 *
 * 0.6.0: local shell goes DIRECT (non-MAS flavor). The bash tool spawns
 * the platform shell right here in this child process — it is already a
 * full Node runtime, so the cam-pi bridge (the MAS-safe consent path) is
 * no longer needed on direct builds. MAS flavors build with
 * CAM_LOCAL_SHELL=false: the bash tool is never registered there.
 *
 * Protocol: JSON-lines over stdio. We own this contract no matter how
 * upstream pi evolves.
 *
 *   in:  {type:"configure", apiUrl, apiKey, model}  — a legacy bridge-pair
 *                                          field is accepted and IGNORED
 *                                          (direct flavor runs shell itself)
 *        {type:"send", text}             — while a turn is running the
 *                                          text is QUEUED (followUp) and
 *                                          answered right after
 *        {type:"stop"}                  — abort the running turn
 *        {type:"reset"}                 — start a fresh session
 *        {type:"load", messages, threadId?} — switch thread: with
 *                                          threadId the prior pi session
 *                                          file is reopened (FULL history);
 *                                          without it (old host) the
 *                                          messages are seeded as before
 *        {type:"hub", url, token}       — hub pair (re-)injection
 *        {type:"ping"}
 *   out: {type:"ready", version}
 *        {type:"configured"}
 *        {type:"user", text}            — echo of each accepted send, so
 *                                         the host event log stays a
 *                                         complete transcript on its own
 *        {type:"queued", text}          — send accepted into the queue
 *        {type:"turn"}                  — a new assistant turn started
 *        {type:"thinking", text}        — CUMULATIVE reasoning text
 *        {type:"delta", text}           — CUMULATIVE answer text
 *        {type:"tool", phase, name, isError?}
 *        {type:"compaction", phase, reason, willRetry?}
 *        {type:"done", text, stopReason, errorMessage}
 *        {type:"status", state}         — "idle" | "running"
 *        {type:"hub_status", ok}
 *        {type:"load_done", count}
 *        {type:"reset_done"}
 *        {type:"error", error, detail?}
 */

// Network-quiet before anything else: no version checks, no telemetry
// headers, no tool-binary downloads (we enable none of those tools).
process.env.PI_OFFLINE = process.env.PI_OFFLINE || '1';
process.env.PI_TELEMETRY = process.env.PI_TELEMETRY || '0';
process.env.PI_SKIP_VERSION_CHECK = process.env.PI_SKIP_VERSION_CHECK || '1';

import readline from 'node:readline';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';

import {
  createAgentSession,
  SessionManager,
  SettingsManager,
  ModelRegistry,
  AuthStorage,
  DefaultResourceLoader,
} from '@mariozechner/pi-coding-agent';
// typebox comes with pi-ai; importing it directly (NOT via the pi-ai
// index) keeps the lazy built-in provider registry — and with it every
// other vendor SDK — out of the bundle.
import { Type } from 'typebox';

const VERSION = '0.6.2';

/* Run the whole child in the user's home dir. The app launches us with an
 * inherited cwd (System32 for Start-Menu launches on Windows) — harmless
 * for our explicit absolute paths, but bash commands and any stray
 * relative access should land somewhere sane and predictable. */
try { process.chdir(os.homedir()); } catch (_) {}

/* Data root: owned by the child, home-relative so the host needs no new
 * field. Sessions (pi JSONL, full history), skills (discovered by the
 * ResourceLoader), memory.md, and the thread→session map live here. */
const DATA_ROOT = path.join(os.homedir(), '.cam', 'assistant');
const SESSIONS_DIR = path.join(DATA_ROOT, 'sessions');
const SKILLS_DIR = path.join(DATA_ROOT, 'skills');
const MEMORY_PATH = path.join(DATA_ROOT, 'memory.md');
const THREADS_MAP_PATH = path.join(DATA_ROOT, 'threads-map.json');
const MEMORY_MAX_BYTES = 32 * 1024;

/* Debug rail: key lifecycle markers on stderr, each stamped with
 * seconds since boot. The host forwards child stderr into cam-desktop.log
 * as "[assistant] child stderr: …", so the emit-side timeline can be
 * compared against the host-side event log to pin down where a turn
 * stalls (child vs delivery vs view). */
const T0 = Date.now();
const dlog = (m) => {
  try { process.stderr.write(`[cam-assist +${((Date.now() - T0) / 1000).toFixed(1)}s] ${m}\n`); } catch (_) { /* pipe gone */ }
};

const SYSTEM_PROMPT = [
  'You are the CAM Desktop assistant — a local helper embedded in the',
  'CAM Desktop app. CAM Desktop manages AI coding agents (codex, claude,',
  'cursor, kimi, …) running in tmux sessions on remote Linux nodes over',
  'SSH: nodes (SSH hosts), contexts (a node + a remote workdir), agents',
  '(a camc-run process in a context), extensions, sync, and attach.',
  'Answer concisely and concretely. When you do not know an app-specific',
  'fact, say so instead of inventing commands or endpoints.',
  '',
  'You can OPERATE the app through the `cam` tool: one authenticated HTTP',
  'call against the app\'s local hub API — { method, path, body? }, path',
  'must start with /api/. Directory:',
  '',
  'Nodes (contexts):',
  '  GET    /api/contexts                     list nodes (id, name, machine{host,user,port,auth_method}, path)',
  '  POST   /api/contexts                     add node {name, machine:{type:"ssh",host,user,port,auth_method:"key"|"password",key_file?}, path, password?, remember_password?}',
  '  GET    /api/contexts/<name>              one node',
  '  PATCH  /api/contexts/<name>              edit node settings {name?, path?, machine?}',
  '  DELETE /api/contexts/<name>              remove node',
  '  POST   /api/contexts/<name>/sync         import/sync the node\'s agents (sync_in_flight → wait, then GET sync-status)',
  '  GET    /api/contexts/<name>/sync-status  sync progress',
  '  POST   /api/contexts/<name>/heal         repair the node\'s remote camc',
  'Agents:',
  '  GET    /api/agents                       list agents (?refresh=1 re-syncs first)',
  '  POST   /api/agents                       start an agent {context, tool:"claude"|"codex"|"kimi"|…, name, prompt, path?}',
  '  PATCH  /api/agents/<id>                  edit agent {name?, auto_confirm?, tags?}',
  '  DELETE /api/agents/<id>                  stop the agent (?force=1 to kill)',
  '  DELETE /api/agents/<id>/history          remove the agent record',
  '  GET    /api/agents/<id>/output?lines=N   recent terminal output',
  '  POST   /api/agents/<id>/input {text}     send text input to the agent',
  '  POST   /api/agents/<id>/key {key}        send a named key (Enter, Escape, C-c, …)',
  'Extensions:',
  '  GET    /api/extensions                   list extensions (built-in + user)',
  '  POST   /api/extensions/install {path}    install from a folder/.tar.gz path on THIS machine',
  '  POST   /api/extensions/<name>/enable     enable (or …/disable)',
  '  DELETE /api/extensions/<name>            remove a USER extension (built-ins are disable-only)',
  '  GET    /api/extensions/<name>/config     per-extension attributes',
  '  PUT    /api/extensions/<name>/config     set attributes {config:{…}} ({} resets)',
  'System:',
  '  GET    /api/system/health                hub liveness',
  '',
  'Rules: GET before you mutate; after a mutation GET again to confirm',
  'and report what actually changed. An error JSON body means the call',
  'failed — explain it, don\'t retry blindly. If the tool reports',
  'hub_unavailable, the desktop backend is not running.',
  '',
  'You have a `memory` tool for durable facts the user asks you to',
  'remember (preferences, environment notes). The current memory content',
  'is appended below as project context when present.',
].join('\n');

/* The seeded hub-api skill: proves the skills pipeline out of the box
 * and gives the model a stable reference even as the prompt above
 * evolves. Written only when missing — user edits win. */
const HUB_API_SKILL = `---
name: hub-api
description: CAM Desktop hub API reference — endpoints for nodes (contexts), agents, extensions, and system health. Use when operating the app via the cam tool.
---

# CAM hub API

All calls go through the \`cam\` tool: { method, path, body? }, path must
start with /api/. GET before mutating; GET again after to confirm.

## Nodes (contexts)
- GET /api/contexts — list nodes
- POST /api/contexts — add node {name, machine:{type:"ssh",host,user,port,auth_method}, path}
- GET|PATCH|DELETE /api/contexts/<name>
- POST /api/contexts/<name>/sync then GET /api/contexts/<name>/sync-status
- POST /api/contexts/<name>/heal — repair remote camc

## Agents
- GET /api/agents (?refresh=1 re-syncs first)
- POST /api/agents — start {context, tool, name, prompt, path?}
- PATCH /api/agents/<id> — {name?, auto_confirm?, tags?}
- DELETE /api/agents/<id> (?force=1) — stop; DELETE /api/agents/<id>/history — remove record
- GET /api/agents/<id>/output?lines=N — recent output
- POST /api/agents/<id>/input {text} / /key {key} / /upload {filename, data(base64)}

## Extensions
- GET /api/extensions
- POST /api/extensions/install {path}
- POST /api/extensions/<name>/enable (or /disable)
- GET|PUT /api/extensions/<name>/config

## System
- GET /api/system/health
`;

const emit = (obj) => {
  try { process.stdout.write(JSON.stringify(obj) + '\n'); } catch (_) { /* pipe gone */ }
};

let session = null;      // AgentSession (SDK)
let cfg = null;          // { apiUrl, apiKey, model }
let hub = null;          // { url, token } — injected by the host, rotates on hub restart
let unsubscribe = null;  // session event subscription
let lastText = '';       // cumulative answer text of the in-flight assistant message
let lastThinking = '';   // cumulative reasoning text of the in-flight turn

/* The single generic CAM backoffice tool: one authenticated HTTP call to
 * the app's loopback hub. Generic by design — every present and future
 * hub endpoint is covered without a bundle rebuild (only the prompt
 * directory above is text). Every call is emitted as a hub_call event so
 * the host can write the [assistant] audit line. */
const camTool = {
  name: 'cam',
  label: 'CAM Desktop',
  description: 'Operate the CAM Desktop app: one authenticated HTTP call to its local hub API. See the system prompt for the endpoint directory.',
  parameters: Type.Object({
    method: Type.Union([Type.Literal('GET'), Type.Literal('POST'), Type.Literal('PUT'), Type.Literal('PATCH'), Type.Literal('DELETE')]),
    path: Type.String({ description: 'hub API path starting with /api/, e.g. /api/agents' }),
    body: Type.Optional(Type.Any({ description: 'JSON body for POST/PUT/PATCH' })),
  }),
  execute: async (_toolCallId, params) => {
    if (!hub || !hub.url || !hub.token) throw new Error('hub_unavailable');
    const p = String(params.path || '');
    if (!p.startsWith('/api/')) throw new Error('path_must_start_with_/api/');
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 30000);
    try {
      const res = await fetch(hub.url + p, {
        method: params.method,
        headers: { authorization: `Bearer ${hub.token}`, 'content-type': 'application/json' },
        body: params.body != null && params.method !== 'GET' && params.method !== 'DELETE'
          ? JSON.stringify(params.body) : undefined,
        signal: ctrl.signal,
      });
      const raw = await res.text();
      let out = raw;
      try { out = JSON.stringify(JSON.parse(raw)); } catch (_) { /* plain text */ }
      if (out.length > 12000) out = out.slice(0, 12000) + '…(truncated)';
      emit({ type: 'hub_call', method: params.method, path: p, status: res.status });
      return {
        content: [{ type: 'text', text: `HTTP ${res.status}\n${out}` }],
        details: { status: res.status },
      };
    } finally {
      clearTimeout(timer);
    }
  },
};

/* Local shell — DIRECT execution (non-MAS flavor, 0.6.0). This child is
 * already a full Node process, so the bash tool spawns the platform shell
 * itself: no cam-pi bridge, no setup. Every invocation is audit-logged
 * host-side via the shell_call event. execLocal is kept in sync with the
 * (now legacy) cam-pi.js bridge command runner. */
const LOCAL_SHELL = (typeof CAM_LOCAL_SHELL === 'undefined') ? true : CAM_LOCAL_SHELL;
const LOCAL_SHELL_CWD = os.homedir(); // predictable landing dir — never System32/app-dir

function execLocal(command, timeoutS, cwd = LOCAL_SHELL_CWD) {
  return new Promise((resolve) => {
    const isWin = process.platform === 'win32';
    const shell = isWin ? (process.env.COMSPEC || 'cmd.exe') : '/bin/sh';
    const args = isWin ? ['/d', '/s', '/c', command] : ['-c', command];
    let child;
    try {
      child = spawn(shell, args, {
        stdio: ['ignore', 'pipe', 'pipe'],
        cwd,
        env: { ...process.env, NO_COLOR: '1', TERM: 'dumb' },
      });
    } catch (e) {
      resolve({ code: -1, out: String((e && e.message) || e), killed: false });
      return;
    }
    let buf = '';
    let killed = false;
    const timer = setTimeout(() => {
      killed = true;
      try { child.kill(); } catch (_) {}
    }, Math.max(1, Math.min(300, timeoutS || 60)) * 1000);
    const cap = (c) => { buf += c; if (buf.length > 20000) buf = buf.slice(-20000); };
    child.stdout.on('data', cap);
    child.stderr.on('data', cap);
    child.on('error', (e) => { clearTimeout(timer); resolve({ code: -1, out: String((e && e.message) || e), killed }); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code, out: buf, killed }); });
  });
}

const bashTool = LOCAL_SHELL ? {
  name: 'bash',
  label: 'Local shell',
  description: 'Run a shell command directly on the user\'s machine: cmd.exe on Windows (dir, not ls; no POSIX tools), /bin/sh on macOS/Linux. Commands run in the user\'s home directory unless you pass the cwd parameter (absolute path — use it when the user names a project directory); every result is prefixed with [platform · cwd]. To use PowerShell on Windows, call it as a program and do NOT wrap the command in double quotes: powershell.exe -NoProfile -Command Get-Date (quoted "-Command" args echo back literally; pwsh.exe works the same). Calls return when the command exits (timeout default 60s, max 300s); for long-running tasks, background them OS-natively and poll a log file — Windows: start /b cmd /c "build > C:\\path\\build.log 2>&1" then poll with type; POSIX: nohup build > /tmp/build.log 2>&1 & echo $!. Use for local inspection or operations the cam tool cannot do. Prefer read-only commands unless the user explicitly asked for a change.',
  parameters: Type.Object({
    command: Type.String({ description: 'the shell command line to run (cmd.exe syntax on Windows, sh syntax elsewhere)' }),
    cwd: Type.Optional(Type.String({ description: 'absolute working directory for this command — default: the user\'s home. Use it when the user names a project directory instead of cd-chaining.' })),
    timeout: Type.Optional(Type.Number({ description: 'seconds (default 60, max 300)' })),
  }),
  execute: async (_toolCallId, params) => {
    const command = String(params.command || '');
    if (!command.trim()) throw new Error('empty_command');
    const cwd = params.cwd ? String(params.cwd) : LOCAL_SHELL_CWD;
    if (!/^([a-zA-Z]:[\\/]|\\\\|\/)/.test(cwd)) throw new Error('invalid_cwd: must be an absolute path');
    emit({ type: 'shell_call', command });
    dlog(`shell $ ${command.slice(0, 120)}`);
    const data = await execLocal(command, Number(params.timeout) || 60, cwd);
    let text = String(data.out || '');
    if (text.length > 12000) text = text.slice(-12000) + '\n…(truncated)';
    const where = `[${process.platform} · ${cwd}] `;
    return {
      content: [{ type: 'text', text: `${data.killed ? '(killed: timeout)\n' : ''}${where}exit ${data.code}\n${text}` }],
      details: { exitCode: data.code, timeout: !!data.killed, platform: process.platform, cwd },
    };
  },
} : null;

/* Skills-scoped `read`: the SDK only advertises the skills section in
 * the system prompt when a tool named "read" is active, and skills are
 * loaded on demand BY READING their SKILL.md. The built-in read tool
 * would give the model unrestricted local file read — unacceptable for
 * an assistant that ingests untrusted hub output. This replacement
 * serves only files under the skills directories. */
const skillReadTool = {
  name: 'read',
  label: 'Read skill file',
  description: 'Read a file inside the assistant skills directory (used to load skills on demand). Paths outside the skills directories are refused.',
  parameters: Type.Object({
    path: Type.String({ description: 'file path inside the skills directory' }),
  }),
  execute: async (_toolCallId, params) => {
    const p = String(params.path || '');
    const resolved = path.resolve(p);
    const root = path.resolve(SKILLS_DIR) + path.sep;
    if (!resolved.startsWith(root)) throw new Error('path_outside_skills');
    let text;
    try { text = fs.readFileSync(resolved, 'utf8'); }
    catch (e) { throw new Error(`read_failed: ${(e && e.code) || e}`); }
    if (text.length > 100 * 1024) text = text.slice(0, 100 * 1024) + '\n…(truncated)';
    return { content: [{ type: 'text', text }], details: { path: resolved } };
  },
};

/* Durable memory: memory.md is injected into the system prompt as a
 * context file (see agentsFilesOverride below); this tool is the only
 * write path. Atomic, size-capped, audit-logged host-side via the
 * memory_write event. */
const memoryTool = {
  name: 'memory',
  label: 'Memory',
  description: 'Update your durable memory file (survives restarts and threads). op "append" adds text; op "replace" rewrites the whole file — use replace to prune when it grows. To read it, look at the Project Context section of your system prompt.',
  parameters: Type.Object({
    op: Type.Union([Type.Literal('append'), Type.Literal('replace')]),
    text: Type.String({ description: 'content to append or replacement full content' }),
  }),
  execute: async (_toolCallId, params) => {
    const text = String(params.text || '');
    const current = params.op === 'append' ? _readMemory() : '';
    const next = params.op === 'append' ? (current ? current.replace(/\n?$/, '\n') + text : text) : text;
    if (Buffer.byteLength(next) > MEMORY_MAX_BYTES) {
      throw new Error(`memory_too_large (>${MEMORY_MAX_BYTES} bytes) — replace with a pruned version instead of appending`);
    }
    const tmp = MEMORY_PATH + '.tmp';
    fs.mkdirSync(DATA_ROOT, { recursive: true });
    fs.writeFileSync(tmp, next, { mode: 0o600 });
    fs.renameSync(tmp, MEMORY_PATH);
    emit({ type: 'memory_write', bytes: Buffer.byteLength(next) });
    dlog(`memory ${params.op} → ${Buffer.byteLength(next)}B`);
    // Best effort: make the fresh content visible in THIS session too.
    try { if (session) await session.reload(); } catch (_) { /* next session picks it up */ }
    return {
      content: [{ type: 'text', text: `memory updated (${Buffer.byteLength(next)} bytes)` }],
      details: { bytes: Buffer.byteLength(next) },
    };
  },
};

function _readMemory() {
  try { return fs.readFileSync(MEMORY_PATH, 'utf8'); } catch (_) { return ''; }
}

function _makeModel(id, baseUrl) {
  return {
    id,
    name: id,
    api: 'openai-completions',
    provider: 'cam-assistant',
    baseUrl,
    reasoning: false,
    input: ['text'],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    maxTokens: 8192,
  };
}

function _textOf(msg) {
  if (!msg || !Array.isArray(msg.content)) return '';
  return msg.content
    .filter((c) => c && c.type === 'text')
    .map((c) => c.text || '')
    .join('');
}

function _thinkingOf(msg) {
  if (!msg || !Array.isArray(msg.content)) return '';
  return msg.content
    .filter((c) => c && c.type === 'thinking')
    .map((c) => c.thinking || '')
    .join('');
}

function _wireSession(s) {
  if (unsubscribe) { try { unsubscribe(); } catch (_) { /* stale */ } }
  unsubscribe = s.subscribe((ev) => {
    try {
      if (ev.type === 'turn_start') {
        // One LLM round per turn (tool loops and queued follow-ups each
        // open a new turn) — the view starts a fresh bubble per turn.
        lastText = '';
        lastThinking = '';
        emit({ type: 'turn' });
      } else if (ev.type === 'message_update') {
        const sub = ev.assistantMessageEvent || {};
        const partial = sub.partial;
        if (sub.type === 'thinking_delta' || sub.type === 'thinking_start') {
          const th = _thinkingOf(partial);
          if (th && th !== lastThinking) { lastThinking = th; emit({ type: 'thinking', text: th }); }
        } else if (sub.type === 'text_delta' || sub.type === 'text_start') {
          const t = _textOf(partial);
          if (t && t !== lastText) { lastText = t; emit({ type: 'delta', text: t }); }
        }
      } else if (ev.type === 'tool_execution_start') {
        const a = ev.args || {};
        const summary = ev.toolName === 'cam'
          ? `${a.method || ''} ${a.path || ''}`.trim()
          : ev.toolName === 'bash'
            ? String(a.command || '').replace(/\s+/g, ' ').slice(0, 80)
            : '';
        dlog(`tool start ${ev.toolName} ${summary}`);
        emit({ type: 'tool', phase: 'start', id: ev.toolCallId, name: ev.toolName, summary });
      } else if (ev.type === 'tool_execution_end') {
        dlog(`tool end ${ev.toolName}${ev.isError ? ' (error)' : ''}`);
        emit({ type: 'tool', phase: 'end', id: ev.toolCallId, name: ev.toolName, isError: !!ev.isError });
      } else if (ev.type === 'agent_end') {
        const msgs = ev.messages || [];
        const last = msgs[msgs.length - 1];
        const t = _textOf(last) || lastText;
        dlog(`done stop=${(last && last.stopReason) || 'stop'} len=${t.length}`);
        emit({
          type: 'done',
          text: t,
          stopReason: (last && last.stopReason) || 'stop',
          errorMessage: (s.agent && s.agent.state && s.agent.state.errorMessage) || '',
        });
      } else if (ev.type === 'compaction_start') {
        emit({ type: 'compaction', phase: 'start', reason: ev.reason });
      } else if (ev.type === 'compaction_end') {
        dlog(`compaction end reason=${ev.reason} willRetry=${ev.willRetry}`);
        emit({ type: 'compaction', phase: 'end', reason: ev.reason, willRetry: !!ev.willRetry });
      } else if (ev.type === 'auto_retry_start') {
        dlog(`auto-retry ${ev.attempt}/${ev.maxAttempts}: ${ev.errorMessage}`);
      }
    } catch (e) {
      emit({ type: 'error', error: 'event_pump', detail: String((e && e.message) || e) });
    }
  });
}

function _ensureDirs() {
  fs.mkdirSync(SESSIONS_DIR, { recursive: true });
  fs.mkdirSync(SKILLS_DIR, { recursive: true });
  const hubApi = path.join(SKILLS_DIR, 'hub-api', 'SKILL.md');
  if (!fs.existsSync(hubApi)) {
    fs.mkdirSync(path.dirname(hubApi), { recursive: true });
    fs.writeFileSync(hubApi, HUB_API_SKILL, 'utf8');
  }
}

function _readThreadsMap() {
  try { return JSON.parse(fs.readFileSync(THREADS_MAP_PATH, 'utf8')); } catch (_) { return {}; }
}

function _writeThreadsMap(map) {
  const tmp = THREADS_MAP_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(map, null, 1), { mode: 0o600 });
  fs.renameSync(tmp, THREADS_MAP_PATH);
}

/* Build a fresh AgentSession around a SessionManager. Used by configure
 * (new session), reset (new session), and load (reopened session). */
async function _startSession(sessionManager) {
  if (session) { try { session.dispose(); } catch (_) { /* best effort */ } session = null; }
  const authStorage = AuthStorage.inMemory();
  authStorage.setRuntimeApiKey('cam-assistant', cfg.apiKey);
  const modelRegistry = ModelRegistry.inMemory(authStorage);
  const settingsManager = SettingsManager.inMemory({
    enableInstallTelemetry: false,
    compaction: { enabled: true },
  });
  const resourceLoader = new DefaultResourceLoader({
    cwd: DATA_ROOT,
    agentDir: DATA_ROOT,
    settingsManager,
    noExtensions: true,       // pi extensions run arbitrary TS — not here
    noThemes: true,
    noPromptTemplates: true,  // no slash-template magic in user messages
    noContextFiles: true,     // memory.md is injected explicitly below
    systemPrompt: SYSTEM_PROMPT,
    agentsFilesOverride: (base) => ({
      agentsFiles: [
        ...base.agentsFiles,
        // Lazy read: the loader re-reads memory.md on every (re)build.
        { path: MEMORY_PATH, content: _readMemory() },
      ],
    }),
  });
  await resourceLoader.reload();
  // Explicit allowlist: built-in bash/edit/write never register; the
  // built-in "read" IS shadowed by our skills-scoped read below (custom
  // tools overwrite same-name registry entries — agent-session.js
  // _refreshToolRegistry), which also satisfies the system-prompt
  // skills gate (skills are only advertised when a "read" tool exists).
  const toolNames = LOCAL_SHELL
    ? ['cam', 'read', 'bash', 'memory']
    : ['cam', 'read', 'memory'];
  const tools = LOCAL_SHELL
    ? [camTool, bashTool, skillReadTool, memoryTool]
    : [camTool, skillReadTool, memoryTool];
  const result = await createAgentSession({
    model: _makeModel(cfg.model, cfg.apiUrl),
    authStorage,
    modelRegistry,
    settingsManager,
    resourceLoader,
    sessionManager,
    tools: toolNames,
    customTools: tools,
  });
  session = result.session;
  _wireSession(session);
  lastText = '';
  lastThinking = '';
  return session;
}

async function onConfigure(msg) {
  const apiUrl = String(msg.apiUrl || '').trim().replace(/\/+$/, '');
  const apiKey = String(msg.apiKey || '');
  const model = String(msg.model || '').trim();
  if (!apiUrl || !model) { emit({ type: 'error', error: 'missing_config', detail: 'apiUrl and model are required' }); return; }
  cfg = { apiUrl, apiKey, model };
  try {
    _ensureDirs();
    await _startSession(SessionManager.create(DATA_ROOT, SESSIONS_DIR));
  } catch (e) {
    emit({ type: 'error', error: 'configure_failed', detail: String((e && e.message) || e) });
    return;
  }
  dlog(`configured model=${model} shell=${LOCAL_SHELL ? 'direct' : 'off'} root=${DATA_ROOT}`);
  emit({ type: 'configured' });
}

async function onSend(msg) {
  if (!session) { emit({ type: 'error', error: 'not_configured' }); return; }
  const text = String(msg.text || '');
  if (!text.trim()) { emit({ type: 'error', error: 'empty_message' }); return; }
  dlog(`send recv len=${text.length}${session.isStreaming ? ' (streaming→queue)' : ''}`);
  // Echo the user message into the event log: the host-side transcript
  // stays complete on its own, and a remounted or restarted view
  // rebuilds the full conversation by replaying it.
  emit({ type: 'user', text });
  if (session.isStreaming) {
    // Chat-style multi-send: queue behind the running turn instead of
    // rejecting — the agent answers it as soon as the turn ends.
    try { await session.followUp(text); } catch (e) {
      emit({ type: 'error', error: 'queue_failed', detail: String((e && e.message) || e) });
      return;
    }
    emit({ type: 'queued', text });
    return;
  }
  lastText = '';
  lastThinking = '';
  emit({ type: 'status', state: 'running' });
  try {
    await session.prompt(text);
  } catch (e) {
    emit({ type: 'error', error: 'prompt_failed', detail: String((e && e.message) || e) });
  } finally {
    emit({ type: 'status', state: 'idle' });
  }
}

async function onLoad(msg) {
  if (!cfg) { emit({ type: 'error', error: 'not_configured' }); return; }
  const threadId = String(msg.threadId || '');
  const map = threadId ? _readThreadsMap() : null;
  const knownFile = map && map[threadId];
  try {
    if (knownFile && fs.existsSync(knownFile)) {
      // Full-history path: reopen the thread's pi session file — the
      // model gets EVERYTHING back (compaction notes included), not the
      // host log's tail.
      await _startSession(SessionManager.open(knownFile, SESSIONS_DIR, DATA_ROOT));
      dlog(`load thread ${threadId} from file (${session.messages.length} msgs)`);
      emit({ type: 'load_done', count: session.messages.length });
      return;
    }
    // New thread (or old host / pre-SDK thread): fresh session, seeded
    // from the host log's user/assistant messages as before.
    await _startSession(SessionManager.create(DATA_ROOT, SESSIONS_DIR));
    const msgs = Array.isArray(msg.messages) ? msg.messages : [];
    session.agent.state.messages = msgs
      .filter((m) => m && (m.role === 'user' || m.role === 'assistant') && typeof m.text === 'string' && m.text)
      .map((m) => ({ role: m.role, content: [{ type: 'text', text: m.text }], timestamp: Date.now() }));
    if (threadId && session.sessionFile) {
      map[threadId] = session.sessionFile;
      _writeThreadsMap(map);
    }
    dlog(`load thread ${threadId || '(no id)'} seeded msgs=${session.agent.state.messages.length}`);
    emit({ type: 'load_done', count: session.agent.state.messages.length });
  } catch (e) {
    emit({ type: 'error', error: 'load_failed', detail: String((e && e.message) || e) });
  }
}

async function onMessage(msg) {
  switch (msg && msg.type) {
    case 'configure': return onConfigure(msg);
    case 'send': return onSend(msg);
    case 'hub':
      // Hub endpoint + token (re-)injection from the host; the token
      // rotates on every hub restart (app reload), so this can arrive
      // at any time and simply replaces the old pair.
      hub = (msg.url && msg.token) ? { url: String(msg.url), token: String(msg.token) } : null;
      emit({ type: 'hub_status', ok: !!hub });
      return;
    case 'stop':
      if (session) {
        try { await session.abort(); } catch (_) { /* already idle */ }
      }
      return;
    case 'reset':
      if (cfg) {
        try { await _startSession(SessionManager.create(DATA_ROOT, SESSIONS_DIR)); } catch (_) { /* reported via error event if any */ }
      }
      lastText = '';
      lastThinking = '';
      dlog('reset');
      emit({ type: 'reset_done' });
      return;
    case 'load': return onLoad(msg);
    case 'ping':
      emit({ type: 'pong' });
      return;
    default:
      emit({ type: 'error', error: 'unknown_message', detail: String((msg && msg.type) || '') });
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
  const s = String(line || '').trim();
  if (!s) return;
  let msg;
  try { msg = JSON.parse(s); } catch (e) {
    emit({ type: 'error', error: 'bad_json', detail: String((e && e.message) || e) });
    return;
  }
  onMessage(msg).catch((e) => emit({ type: 'error', error: 'internal', detail: String((e && e.message) || e) }));
});

emit({ type: 'ready', version: VERSION });
