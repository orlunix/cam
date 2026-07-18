/**
 * Unit tests for electron/demo-transport.cjs — the offline demo node.
 * Covers the camc routing, agent lifecycle, and the fake attach stream.
 * Run:  node apps/cam-desktop/test/demo-transport.test.cjs
 */

'use strict';

const assert = require('assert');
const path = require('path');

const demo = require(path.join(__dirname, '..', 'electron', 'demo-transport.cjs'));

async function main() {
  // list returns seeded agents
  const list = await demo.execRemote({ command: '~/.cam/camc --json list' });
  assert.strictEqual(list.ok, true);
  const agents = JSON.parse(list.stdout);
  assert(agents.length >= 2, 'seeded agents present');
  assert(agents.some(a => a.status === 'running'), 'a running agent exists');
  assert(agents.some(a => a.status === 'completed'), 'a completed agent exists');

  // ids are hex (camc `ID:` parser requires [0-9a-f]{6,})
  for (const a of agents) assert(/^[0-9a-f]{6,}$/.test(a.id), `hex id: ${a.id}`);

  // status by id
  const running = agents.find(a => a.status === 'running');
  const st = await demo.execRemote({ command: `~/.cam/camc --json status ${running.id}` });
  assert.strictEqual(JSON.parse(st.stdout).id, running.id);

  // capture returns staged output
  const cap = await demo.execRemote({ command: `~/.cam/camc capture ${running.id} --lines 5` });
  assert.strictEqual(cap.ok, true);
  assert(cap.stdout.length > 0, 'capture has content');

  // run creates a new agent with an ID line parseable by the hub regex
  const run = await demo.execRemote({ command: `~/.cam/camc run -t claude -n 'demo-x' do something` });
  assert.strictEqual(run.ok, true);
  const m = /^\s*ID:\s+([0-9a-fA-F]{6,})\b/m.exec(run.stdout);
  assert(m, 'run prints a parseable ID line');
  const after = JSON.parse((await demo.execRemote({ command: '~/.cam/camc --json list' })).stdout);
  assert(after.length === agents.length + 1, 'run appended the new agent');

  // env check is green
  const env = await demo.execRemote({ command: '~/.cam/camc env check --tool claude --json' });
  const envJson = JSON.parse(env.stdout);
  assert(envJson.resolved && envJson.resolved.tmux, 'env check reports tmux');

  // tmux lists
  const wins = await demo.execRemote({ command: 'tmux -S x list-windows' });
  assert(wins.stdout.includes('0:'), 'list-windows has rows');
  const clients = await demo.execRemote({ command: 'tmux -S x list-clients' });
  assert(/\/dev\/pts\//.test(clients.stdout), 'list-clients has a tty');

  // attach to a running agent streams staged output then closes cleanly
  const attachId = after.find(a => a.status === 'running').id;
  let bytes = 0;
  const ch = await demo.openAttachChannel({ command: `~/.cam/camc attach ${attachId}` }, {
    onData: (b) => { bytes += b.length; },
    onClose: () => {},
  });
  assert.strictEqual(ch.ok, true, 'attach opens');
  assert.strictEqual(typeof ch.write, 'function');
  assert.strictEqual(ch.write('hello\n'), true, 'write accepted');
  await new Promise(r => setTimeout(r, 2600));
  assert(bytes > 0, `streamed ${bytes} bytes`);
  ch.dispose();

  // attach to a completed agent is refused with a useful error
  const doneId = after.find(a => a.status === 'completed').id;
  const dead = await demo.openAttachChannel({ command: `~/.cam/camc attach ${doneId}` }, {});
  assert.strictEqual(dead.ok, false);
  assert.strictEqual(dead.error, 'stale_session');

  // unknown attach id errors
  const nope = await demo.openAttachChannel({ command: '~/.cam/camc attach zzzzzz' }, {});
  assert.strictEqual(nope.ok, false);

  console.log('demo-transport: all assertions passed');
  process.exit(0);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
