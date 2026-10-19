/* warmConnection unit tests (mock ssh2 injection).
 * Covers: term-pool connect starts immediately on warm, openTerminalChannel
 * reuses the in-flight entry (single handshake), warm idempotence, exec-pool
 * warm, and that a failed warm never poisons the pool nor throws. */

'use strict';

const { EventEmitter } = require('node:events');
const path = require('node:path');

const transport = require(path.join(__dirname, '..', 'electron', 'ssh-transport.cjs'));

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; }
  else { fail++; console.log('FAIL', name, extra || ''); }
}

class FakeClient extends EventEmitter {
  static instances = [];
  constructor() { super(); FakeClient.instances.push(this); this.connectOpts = null; this.connectCalls = 0; }
  connect(opts) {
    this.connectOpts = opts;
    this.connectCalls++;
    if (FakeClient.failHosts.has(String(opts.host))) {
      setImmediate(() => this.emit('error', new Error('connect ECONNREFUSED')));
      return;
    }
    if (FakeClient.slowHosts.has(String(opts.host))) return; // never ready: stays 'connecting'
    const delay = FakeClient.readyDelayMs | 0;
    if (delay > 0) setTimeout(() => this.emit('ready'), delay);
    else setImmediate(() => this.emit('ready'));
  }
  exec(_cmd, _opts, cb) {
    const s = new EventEmitter();
    s.stderr = new EventEmitter();
    setImmediate(() => {
      cb(null, s);
      setImmediate(() => { s.emit('data', Buffer.from('out')); s.emit('close', 0); });
    });
  }
  end() {}
  destroy() {}
}
FakeClient.failHosts = new Set();
FakeClient.slowHosts = new Set();
FakeClient.readyDelayMs = 0;

function reset() {
  FakeClient.instances = [];
  FakeClient.failHosts = new Set();
  FakeClient.slowHosts = new Set();
  FakeClient.readyDelayMs = 0;
  transport.closeAll();
  transport._setSsh2ForTests({ Client: FakeClient });
}

const auth = { auth_method: 'password', password: 'x' };
const base = { host: 'warmhost', user: 'u', port: 22, ...auth };

(async () => {
  // 1. warm(term) starts the handshake immediately; openTerminalChannel
  //    reuses the in-flight entry — exactly one client, one connect().
  reset();
  const w1 = transport.warmConnection({ ...base, command: 'true' }, { pool: 'term' });
  ok('warm returns ok', w1.ok === true);
  ok('warm starts a client synchronously', FakeClient.instances.length === 1);
  const opened = await transport.openTerminalChannel({ ...base, command: 'camc attach x' }, {});
  ok('openTerminalChannel ok on warmed entry', opened.ok === true, JSON.stringify(opened));
  ok('single client across warm+open', FakeClient.instances.length === 1);
  ok('single connect() across warm+open', FakeClient.instances[0].connectCalls === 1);
  if (opened && typeof opened.dispose === 'function') opened.dispose();

  // 2. Idempotent: a second warm on the same key reuses the entry.
  reset();
  transport.warmConnection({ ...base, command: 'true' }, { pool: 'term' });
  transport.warmConnection({ ...base, command: 'true' }, { pool: 'term' });
  await new Promise((r) => setImmediate(r));
  ok('repeat warm keeps one client', FakeClient.instances.length === 1);

  // 3. Exec-pool warm: execRemote reuses it (same single handshake).
  reset();
  transport.warmConnection({ ...base, command: 'true' }, { pool: 'exec' });
  const ex = await transport.execRemote({ ...base, command: 'echo hi' });
  ok('execRemote ok on warmed exec entry', ex && ex.ok === true, JSON.stringify(ex));
  ok('exec warm single client', FakeClient.instances.length === 1);

  // 4. Failed warm: rejection swallowed (no unhandled rejection), pool not
  //    poisoned — the later open reports the connect error itself.
  reset();
  FakeClient.failHosts.add('deadhost');
  let unhandled = false;
  const onUnhandled = () => { unhandled = true; };
  process.on('unhandledRejection', onUnhandled);
  const w4 = transport.warmConnection({ ...base, host: 'deadhost', command: 'true' }, { pool: 'term' });
  ok('warm on dead host still returns ok', w4.ok === true);
  await new Promise((r) => setTimeout(r, 20));
  const openedDead = await transport.openTerminalChannel({ ...base, host: 'deadhost', command: 'camc attach x' }, {});
  ok('open on dead host returns connect error', openedDead.ok === false && !!openedDead.error, JSON.stringify(openedDead));
  await new Promise((r) => setTimeout(r, 20));
  process.removeListener('unhandledRejection', onUnhandled);
  ok('no unhandled rejection from failed warm', unhandled === false);

  // 5. term vs exec pools stay independent under warm (two clients, one per pool).
  reset();
  transport.warmConnection({ ...base, command: 'true' }, { pool: 'term' });
  transport.warmConnection({ ...base, command: 'true' }, { pool: 'exec' });
  await new Promise((r) => setImmediate(r));
  ok('term+exec warms use separate clients', FakeClient.instances.length === 2);

  // 6. Timing proof (2026-10-14): with a 250ms handshake, the attach path
  //    as main.cjs drives it — warm(term) + camc probe (exec) in parallel,
  //    then openTerminalChannel — costs ≈ ONE handshake, while the old
  //    serial order (probe, then channel open) costs ≈ TWO.
  const T = 250;
  reset();
  FakeClient.readyDelayMs = T;
  let t0 = Date.now();
  await transport.execRemote({ ...base, command: 'test -x ~/.cam/camc' }); // serial: probe first
  let chOld = await transport.openTerminalChannel({ ...base, command: 'camc attach x' }, {});
  const tSerial = Date.now() - t0;
  ok('old serial path opens channel', chOld.ok === true);
  if (chOld && typeof chOld.dispose === 'function') chOld.dispose();

  reset();
  FakeClient.readyDelayMs = T;
  t0 = Date.now();
  transport.warmConnection({ ...base, command: 'true' }, { pool: 'term' });  // hub warms term…
  await transport.execRemote({ ...base, command: 'test -x ~/.cam/camc' });   // …while exec probes
  const chNew = await transport.openTerminalChannel({ ...base, command: 'camc attach x' }, {});
  const tWarm = Date.now() - t0;
  ok('warm attach path opens channel', chNew.ok === true);
  if (chNew && typeof chNew.dispose === 'function') chNew.dispose();
  ok(`warm attach ≈ one handshake (${tWarm}ms vs T=${T}ms)`, tWarm < T * 1.6, `tWarm=${tWarm}`);
  ok(`old serial ≈ two handshakes (${tSerial}ms)`, tSerial > T * 1.8, `tSerial=${tSerial}`);
  ok(`warm attach is much faster than serial (${tWarm}ms vs ${tSerial}ms)`, tWarm < tSerial * 0.7);

  // 7. Regression (2026-10-14): a dropped predecessor's late end/close/error
  //    events share the pool key and must NOT assassinate its replacement
  //    (closeAll + immediate re-attach used to wedge the replacement's
  //    readyPromise — every attach stalled ~20s).
  reset();
  FakeClient.slowHosts.add('racehost'); // predecessor never becomes ready
  transport.warmConnection({ ...base, host: 'racehost', command: 'true' }, { pool: 'term' });
  const predecessor = FakeClient.instances[0];
  transport.closeAll(); // drops the predecessor mid-handshake (state 'closed')
  FakeClient.slowHosts.delete('racehost');
  FakeClient.readyDelayMs = 40;
  const racedOpenP = transport.openTerminalChannel({ ...base, host: 'racehost', command: 'camc attach x' }, {});
  // Predecessor's late socket events arrive while the replacement connects:
  predecessor.emit('end');
  predecessor.emit('close');
  predecessor.emit('error', new Error('late reset'));
  const raced = await racedOpenP;
  ok('replacement survives predecessor late events', raced && raced.ok === true, JSON.stringify(raced));
  if (raced && typeof raced.dispose === 'function') raced.dispose();

  transport.closeAll();
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
})().catch((e) => { console.error(e); process.exitCode = 1; });
