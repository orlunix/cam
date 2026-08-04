/* ssh-transport ProxyJump chaining unit tests (mock ssh2 injection).
 * Covers: direct vs chained pool keys, forwardOut wiring, sock connect,
 * jump failure attribution (jump_unreachable), pool sharing of the
 * jump entry between direct and chained use. */

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
  constructor() { super(); FakeClient.instances.push(this); this.connectOpts = null; this.forwardCalls = []; }
  connect(opts) {
    this.connectOpts = opts;
    if (FakeClient.failHosts.has(String(opts.host))) {
      setImmediate(() => this.emit('error', new Error('connect ECONNREFUSED')));
      return;
    }
    setImmediate(() => this.emit('ready'));
  }
  exec(_cmd, _opts, cb) {
    const s = new EventEmitter();
    s.stderr = new EventEmitter();
    setImmediate(() => {
      cb(null, s);
      setImmediate(() => { s.emit('data', Buffer.from('out')); s.emit('close', 0); });
    });
  }
  forwardOut(srcIP, srcPort, dstHost, dstPort, cb) {
    this.forwardCalls.push([srcIP, srcPort, dstHost, dstPort]);
    setImmediate(() => cb(null, new EventEmitter()));
  }
  end() {}
  destroy() {}
}
FakeClient.failHosts = new Set();

function reset() {
  FakeClient.instances = [];
  FakeClient.failHosts = new Set();
  transport.closeAll();
  transport._setSsh2ForTests({ Client: FakeClient });
}

const auth = { auth_method: 'password', password: 'x' };
const directOpts = { host: 'target', user: 'u', port: 22, ...auth, command: 'echo hi' };
const jump = { host: 'jumphost', user: 'ju', port: 2200, ...auth };
const chainedOpts = { ...directOpts, jump };

(async () => {
  // 1. Direct connect: host/port used, no sock, no forwardOut.
  reset();
  let r = await transport.execRemote(directOpts);
  ok('direct exec ok', r && r.ok === true, JSON.stringify(r));
  ok('direct connects with host/port, no sock',
    FakeClient.instances.length === 1
      && FakeClient.instances[0].connectOpts.host === 'target'
      && !FakeClient.instances[0].connectOpts.sock);
  ok('direct makes no forwardOut', FakeClient.instances[0].forwardCalls.length === 0);

  // 2. Chained: jump connects directly, target connects over sock via forwardOut.
  reset();
  r = await transport.execRemote(chainedOpts);
  ok('chained exec ok', r && r.ok === true, JSON.stringify(r));
  const jumpClient = FakeClient.instances.find(c => c.connectOpts && c.connectOpts.host === 'jumphost');
  const targetClient = FakeClient.instances.find(c => c.connectOpts && c.connectOpts.sock);
  ok('jump host connected directly (host/port)', !!jumpClient);
  ok('forwardOut targets the real target', !!jumpClient
    && jumpClient.forwardCalls.length === 1
    && jumpClient.forwardCalls[0][2] === 'target'
    && jumpClient.forwardCalls[0][3] === 22);
  ok('target connected over the forwardOut sock (no direct host)', !!targetClient
    && !targetClient.connectOpts.host);

  // 3. Jump entry is shared: a direct exec to the jump host reuses it.
  reset();
  await transport.execRemote(chainedOpts);
  const before = FakeClient.instances.length;
  r = await transport.execRemote({ host: 'jumphost', user: 'ju', port: 2200, ...auth, command: 'echo j' });
  ok('direct exec on jump host ok', r && r.ok === true);
  ok('jump pool entry shared between chained and direct use', FakeClient.instances.length === before,
    `instances grew to ${FakeClient.instances.length}`);

  // 4. Jump unreachable: error attributed to the jump hop.
  reset();
  FakeClient.failHosts.add('jumphost');
  r = await transport.execRemote(chainedOpts);
  ok('jump failure surfaces as jump_unreachable', r && r.ok === false && r.error === 'jump_unreachable',
    JSON.stringify(r && { error: r.error, detail: r.detail }));

  // 5. Pool separation: chained target and direct target are different entries.
  reset();
  await transport.execRemote(directOpts);
  await transport.execRemote(chainedOpts);
  const directTargets = FakeClient.instances.filter(c => c.connectOpts && c.connectOpts.host === 'target');
  const sockTargets = FakeClient.instances.filter(c => c.connectOpts && c.connectOpts.sock);
  ok('direct and chained target pool separately', directTargets.length === 1 && sockTargets.length === 1);

  transport.closeAll();
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
})().catch((e) => { console.error(e); process.exit(1); });
