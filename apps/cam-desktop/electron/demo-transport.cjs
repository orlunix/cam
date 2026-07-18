/**
 * CAM Desktop — demo transport (DEMO MODE, self-contained and deletable).
 *
 * A fully offline simulator of a remote camc node, implementing the
 * same surface as ssh-transport.cjs so the embedded Hub + renderer can
 * exercise the complete datapath with ZERO network:
 *
 *   Nodes page shows "Demo (built-in)" → Start an agent → it runs a
 *   scripted session in the terminal → the user can type at it.
 *
 * Install with `sshTransport.setOverride(demo)` to enter demo mode and
 * `setOverride(null)` to leave. When not installed it does absolutely
 * nothing — the normal SSH datapath is untouched. Deleting this file
 * plus its two call sites removes the feature cleanly.
 *
 * Canned behaviors:
 *   camc list / status / capture / stop / rm / env check / version
 *   tmux list-windows / list-clients
 *   camc run → new scripted agent (plausible staged output)
 *   attach → a fake PTY stream that plays a staged agent session and
 *   answers one line of input.
 */

'use strict';

const DEMO_HOST = 'demo.local';
const DEMO_SOCKET_DIR = '/tmp/cam-demo-sockets';

let _seq = 0;
const _agents = new Map();   // id -> scripted agent

function _newId() {
  _seq += 1;
  // hex-only id (camc's `ID:` parser requires [0-9a-f]{6,})
  return `de${String(0x10000 + _seq).slice(1)}${String(_seq).padStart(2, '0')}`;
}

/* ─────────────── Scripted sessions ─────────────── */

const STAGES = [
  { wait: 600,  text: '\x1b[36m●\x1b[0m Reading workspace…\r\n' },
  { wait: 900,  text: '\x1b[36m●\x1b[0m Planning: 3 files to change\r\n' },
  { wait: 1200, text: '\x1b[32m+\x1b[0m src/api/routes.ts  (edited)\r\n' },
  { wait: 800,  text: '\x1b[32m+\x1b[0m src/api/auth.ts    (edited)\r\n' },
  { wait: 700,  text: '\x1b[32m+\x1b[0m test/api.test.ts   (added)\r\n' },
  { wait: 1400, text: '\x1b[36m●\x1b[0m Running tests…\r\n' },
  { wait: 1600, text: '\x1b[32m✓\x1b[0m 14 passed, 0 failed (2.1s)\r\n' },
  { wait: 600,  text: '\r\n\x1b[1mDone.\x1b[0m Edited 2 files, added 1 test. All tests green.\r\n' },
  { wait: 400,  text: '\x1b[2m(idle — type anything for a canned reply, Ctrl+B D to detach)\x1b[0m\r\n\x1b[36m❯\x1b[0m ' },
];

function _seed() {
  if (_agents.size) return;
  const running = _makeAgent({ name: 'demo-refine-auth', tool: 'claude', status: 'running', script: STAGES });
  running.startedAt = Date.now() - 42000;
  running.playhead = 0;
  _agents.set(running.id, running);
  const done = _makeAgent({ name: 'demo-fix-parser', tool: 'codex', status: 'completed', script: STAGES });
  done.startedAt = Date.now() - 3600000;
  done.completedAt = done.startedAt + 305000;
  done.transcript = STAGES.map(s => s.text).join('') + '\r\n(completed 5m ago)\r\n';
  done.playhead = done.script.length;
  _agents.set(done.id, done);
}

function _makeAgent({ name, tool, status, script }) {
  const id = _newId();
  return {
    id,
    session: `cam-${id}`,
    socket: `${DEMO_SOCKET_DIR}/cam-${id}.sock`,
    task: { name, tool, prompt: name, auto_confirm: true, auto_exit: false },
    status,
    state: status === 'running' ? 'editing' : 'idle',
    script: script || STAGES,
    transcript: '',
    playhead: 0,
    startedAt: Date.now(),
    completedAt: null,
  };
}

function _recordOf(a) {
  return {
    id: a.id,
    session_id: '',
    task: a.task,
    context_name: 'Demo (built-in)',
    context_path: '/home/demo/workspace',
    transport_type: 'ssh',
    status: a.status,
    state: a.state,
    tmux_session: a.session,
    tmux_socket: a.socket,
    hostname: DEMO_HOST,
    started_at: new Date(a.startedAt).toISOString(),
    completed_at: a.completedAt ? new Date(a.completedAt).toISOString() : null,
  };
}

function _listJson() { _seed(); return JSON.stringify([..._agents.values()].map(_recordOf)); }

function _statusJson(id) {
  _seed();
  const a = [..._agents.values()].find(x => x.id === id || x.id.startsWith(id) || id.startsWith(x.id));
  return a ? JSON.stringify(_recordOf(a)) : 'null';
}

function _capture(id, lines) {
  const a = [..._agents.values()].find(x => x.id === id || x.session === id);
  if (!a) return { ok: false, error: 'not_found', detail: `agent ${id} not found` };
  const full = a.transcript || a.script.map(s => s.text).join('');
  const rows = full.split(/\r?\n/);
  const tail = lines ? rows.slice(-lines) : rows;
  return { ok: true, stdout: tail.join('\n') + '\n', stderr: '' };
}

function _run(parsed) {
  _seed();
  const name = parsed.name || 'demo-task';
  const a = _makeAgent({ name, tool: parsed.tool || 'claude', status: 'running', script: STAGES });
  _agents.set(a.id, a);
  return { ok: true, stdout: `  ID: ${a.id}  Tool: ${a.task.tool}  Session: ${a.session}\n  Path: /home/demo/workspace\n`, stderr: '' };
}

/* ─────────────── Command routing ─────────────── */

function _parseRunArgs(cmd) {
  const tool = (cmd.match(/-t\s+(\S+)/) || [])[1] || 'claude';
  const name = (cmd.match(/-n\s+'([^']+)'/) || cmd.match(/-n\s+(\S+)/) || [])[1] || '';
  return { tool, name };
}

function execRemote(opts) {
  const cmd = String(opts && opts.command || '');
  // camc list
  if (/camc\b.*--json\s+list/.test(cmd) || /camc\b.*\blist\b/.test(cmd) && !/list-windows|list-clients/.test(cmd)) {
    return Promise.resolve({ ok: true, stdout: _listJson(), stderr: '' });
  }
  // camc status
  let m = cmd.match(/--json\s+status\s+'?([\w-]+)'?/);
  if (m) return Promise.resolve({ ok: true, stdout: _statusJson(m[1]), stderr: '' });
  // camc capture
  m = cmd.match(/\bcapture\s+'?([\w-]+)'?(?:\s+--lines\s+(\d+))?/);
  if (m) return Promise.resolve(_capture(m[1], m[2] ? Number(m[2]) : 0));
  // camc stop / kill
  m = cmd.match(/\b(stop|kill)\s+'?([\w-]+)'?/);
  if (m) {
    const a = [..._agents.values()].find(x => x.id === m[2] || x.session === m[2]);
    if (a) { a.status = 'completed'; a.completedAt = Date.now(); }
    return Promise.resolve({ ok: true, stdout: 'Stopped.\n', stderr: '' });
  }
  // camc rm
  m = cmd.match(/\brm\s+'?([\w-]+)'?/);
  if (m) { _agents.delete(m[1]) || [..._agents.values()].find(x => x.session === m[1] && _agents.delete(x.id)); return Promise.resolve({ ok: true, stdout: '', stderr: '' }); }
  // camc env check
  if (/\benv\s+check\b/.test(cmd)) {
    return Promise.resolve({ ok: true, stdout: JSON.stringify({ issues: [], resolved: { tmux: '/bin/tmux', tool: '/usr/local/bin/demo' }, warnings: [] }), stderr: '' });
  }
  // camc run
  if (/\brun\b/.test(cmd) && /camc/.test(cmd)) return Promise.resolve(_run(_parseRunArgs(cmd)));
  // tmux list-windows
  if (/list-windows/.test(cmd)) return Promise.resolve({ ok: true, stdout: '0:demo-agent\n1:notes\n', stderr: '' });
  // tmux list-clients
  if (/list-clients/.test(cmd)) return Promise.resolve({ ok: true, stdout: '/dev/pts/42\n', stderr: '' });
  // probes used by ensure flows (version / md5 / test -x / mkdir / chmod / mv)
  if (/camc\b.*\bversion\b/.test(cmd)) return Promise.resolve({ ok: true, stdout: 'camc v9.9.9 (demo)\n', stderr: '' });
  if (/md5sum/.test(cmd)) return Promise.resolve({ ok: true, stdout: 'demo0000000\n', stderr: '' });
  if (/test -x/.test(cmd) || /mkdir -p/.test(cmd) || /chmod 700/.test(cmd) || /\bmv /.test(cmd)) {
    return Promise.resolve({ ok: true, stdout: '', stderr: '' });
  }
  // default: benign success
  return Promise.resolve({ ok: true, stdout: '', stderr: '' });
}

function writeRemoteFile() {
  return Promise.resolve({ ok: true });
}

/* ─────────────── Fake attach channel ─────────────── */

function openAttachChannel(opts, hooks = {}) {
  const onData = typeof hooks.onData === 'function' ? hooks.onData : () => {};
  const onClose = typeof hooks.onClose === 'function' ? hooks.onClose : () => {};
  const cmd = String(opts && opts.command || '');
  const m = cmd.match(/attach\s+'?([\w-]+)'?/);
  const a = [..._agents.values()].find(x => x.id === (m && m[1]) || x.session === (m && m[1]));
  if (!a) {
    return Promise.resolve({ ok: false, error: 'not_found', detail: `agent ${(m && m[1]) || '?'} not found on the demo node` });
  }
  if (a.status !== 'running') {
    return Promise.resolve({ ok: false, error: 'stale_session', detail: 'session ended (demo agent already completed)' });
  }

  // Replay any already-emitted transcript, then continue the script on a
  // timer. Input gets one canned reply. dispose() stops playback.
  let stopped = false;
  const timers = [];
  const play = (i) => {
    if (stopped || i >= a.script.length) return;
    const stage = a.script[i];
    timers.push(setTimeout(() => {
      if (stopped) return;
      a.transcript += stage.text;
      a.playhead = i + 1;
      onData(Buffer.from(stage.text, 'utf8'));
      if (i === a.script.length - 1) {
        a.state = 'idle';
      }
      play(i + 1);
    }, stage.wait));
  };
  if (a.transcript) onData(Buffer.from(a.transcript, 'utf8'));
  play(a.playhead || 0);

  return Promise.resolve({
    ok: true,
    write(data) {
      if (stopped) return false;
      a.transcript += String(data);
      const reply = `\r\n\x1b[2m(demo) got it — this is a scripted session, real work happens on your SSH nodes.\x1b[0m\r\n\x1b[36m❯\x1b[0m `;
      a.transcript += reply;
      onData(Buffer.from(reply, 'utf8'));
      return true;
    },
    resize() { /* demo PTY has no size constraints */ },
    dispose() {
      stopped = true;
      for (const t of timers) clearTimeout(t);
      try { onClose({ code: 0, signal: null }); } catch {}
    },
  });
}

/* ─────────────── Override dispatch (sshTransport.setOverride) ──────
 * execRemote calls _override(opts); openTerminalChannel passes
 * (opts, hooks); writeRemoteFile/listRemoteFiles/readRemoteFile pass
 * opts.operation. Route them all into the simulator. */

function _listFiles(args) {
  const out = [
    { name: 'src', type: 'dir' },
    { name: 'package.json', type: 'file', size: 1240 },
    { name: 'README.md', type: 'file', size: 860 },
  ];
  return { ok: true, stdout: JSON.stringify(out), stderr: '' };
}

function demoOverride(opts, hooks) {
  switch (opts && opts.operation) {
    case 'writeRemoteFile': return writeRemoteFile(opts);
    case 'listRemoteFiles': return Promise.resolve(_listFiles());
    case 'readRemoteFile':
      return Promise.resolve({ ok: true, stdout: '# Demo workspace\n\nThis file lives on the simulated demo node.\n', stderr: '' });
    case 'openTerminalChannel': return openAttachChannel(opts, hooks || {});
    default: return execRemote(opts);
  }
}

module.exports = { execRemote, writeRemoteFile, openAttachChannel, demoOverride, DEMO_HOST, _agents };
