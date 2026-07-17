/**
 * Unit tests for electron/local-runtime.cjs — the local-node camc
 * runtime (native POSIX / WSL2-on-Windows). No real wsl.exe, no real
 * camc: the module's configure({ execFileImpl, platform }) seam
 * injects a fake execFile so argv shape + error mapping can be
 * asserted deterministically.
 *
 * Run:  node apps/cam-desktop/test/local-runtime.test.cjs
 * Exit: 0 = pass, 1 = fail (prints the failing assertion).
 */

'use strict';

const assert = require('assert');
const path = require('path');

const lr = require(path.join(__dirname, '..', 'electron', 'local-runtime.cjs'));

/** Fake execFile. `handler(file, args, opts)` returns either
 *  { stdout, stderr } (success) or { err } (spawn/exit failure, err
 *  shaped like node's: { code, killed, message }). Calls are recorded
 *  in `calls`. Returns a child-ish object capturing stdin writes. */
function makeExecFile(handler, calls, stdinWrites) {
  return function fakeExecFile(file, args, opts, cb) {
    calls.push({ file, args, opts });
    let out;
    try { out = handler(file, args, opts) || { stdout: '', stderr: '' }; }
    catch (e) { out = { err: e }; }
    const child = {
      stdin: {
        on() {},
        write(data) { stdinWrites.push(data); },
        end() {},
      },
    };
    // Callback-style like node: cb(err, stdout, stderr).
    if (out.err) cb(out.err, out.stdout || '', out.stderr || '');
    else cb(null, out.stdout || '', out.stderr || '');
    return child;
  };
}

async function main() {
  // ── winToWslPath ─────────────────────────────────────────────────
  assert.strictEqual(lr.winToWslPath('C:\\foo\\bar'), '/mnt/c/foo/bar', 'drive letter + backslashes');
  assert.strictEqual(lr.winToWslPath('D:/proj/x'), '/mnt/d/proj/x', 'drive letter + forward slashes');
  assert.strictEqual(lr.winToWslPath('c:\\'), '/mnt/c/', 'bare drive root');
  assert.strictEqual(lr.winToWslPath('/home/user/proj'), '/home/user/proj', 'already-Linux passes through');
  assert.strictEqual(lr.winToWslPath('\\\\wsl$\\Ubuntu\\home\\u'), '\\\\wsl$\\Ubuntu\\home\\u', 'UNC \\\\wsl$ passes through');
  assert.strictEqual(lr.winToWslPath('\\\\wsl.localhost\\Ubuntu\\home\\u'), '\\\\wsl.localhost\\Ubuntu\\home\\u', 'UNC \\\\wsl.localhost passes through');
  assert.strictEqual(lr.winToWslPath('relative/dir'), 'relative/dir', 'relative path passes through');
  assert.strictEqual(lr.winToWslPath(''), '', 'empty stays empty');

  // ── execCamc POSIX: bare argv + ENOENT → camc_missing ────────────
  {
    const calls = [], stdinWrites = [];
    lr.configure({
      platform: 'linux',
      camcPath: '/opt/camc/camc',
      execFileImpl: makeExecFile((file, args) => {
        if (args[0] === '--json') return { stdout: '[]', stderr: '' };
        return { stdout: '', stderr: '' };
      }, calls, stdinWrites),
    });
    const res = await lr.execCamc(['--json', 'list'], { timeoutMs: 5000 });
    assert.strictEqual(res.ok, true, 'posix execCamc ok');
    assert.strictEqual(res.stdout, '[]');
    assert.strictEqual(calls.length, 1, 'one spawn');
    assert.strictEqual(calls[0].file, '/opt/camc/camc', 'posix execs the configured camc path directly');
    assert.deepStrictEqual(calls[0].args, ['--json', 'list']);
    assert.strictEqual(calls[0].opts.timeout, 5000);

    lr.configure({
      execFileImpl: makeExecFile(() => ({ err: Object.assign(new Error('spawn camc ENOENT'), { code: 'ENOENT' }) }), calls, stdinWrites),
    });
    const miss = await lr.execCamc(['--json', 'list']);
    assert.strictEqual(miss.ok, false);
    assert.strictEqual(miss.error, 'camc_missing', 'posix ENOENT maps to camc_missing');
  }

  // ── execCamc win32: wsl.exe argv shape, $HOME resolution ─────────
  {
    const calls = [], stdinWrites = [];
    lr.configure({
      platform: 'win32',
      wslDistro: 'Ubuntu-22.04',
      execFileImpl: makeExecFile((file, args) => {
        // $HOME probe: wsl.exe -d Ubuntu-22.04 --exec bash -c 'printf %s "$HOME"'
        if (args.includes('bash')) return { stdout: '/home/tester', stderr: '' };
        return { stdout: '  ID: abcd1234\n', stderr: '' };
      }, calls, stdinWrites),
    });
    const res = await lr.execCamc(['--json', 'run', '-t', 'claude', '-p', '/mnt/c/proj', 'hi'], { timeoutMs: 30000 });
    assert.strictEqual(res.ok, true, 'win32 execCamc ok');
    const run = calls.find(c => c.args.includes('run'));
    assert(run, 'a run call was made');
    assert.strictEqual(run.file, 'wsl.exe', 'win32 execs wsl.exe');
    assert.deepStrictEqual(
      run.args.slice(0, 5),
      ['-d', 'Ubuntu-22.04', '--exec', '/home/tester/.cam/camc', '--json'],
      'argv shape: wsl.exe -d <distro> --exec <home>/.cam/camc <args>',
    );
    assert.strictEqual(run.args[run.args.length - 1], 'hi', 'prompt passed literally as last arg');
    assert.strictEqual(run.opts.timeout, 30000);

    // $HOME is cached: a second execCamc must not re-probe.
    const before = calls.filter(c => c.args.includes('bash')).length;
    await lr.execCamc(['--json', 'list']);
    const after = calls.filter(c => c.args.includes('bash')).length;
    assert.strictEqual(after, before, '$HOME resolution is cached');
  }

  // ── execCamc win32: stdin piping for `send --stdin` ──────────────
  {
    const calls = [], stdinWrites = [];
    lr.configure({
      platform: 'win32',
      execFileImpl: makeExecFile((file, args) => {
        if (args.includes('bash')) return { stdout: '/home/tester', stderr: '' };
        return { stdout: 'Sent.\n', stderr: '' };
      }, calls, stdinWrites),
    });
    const res = await lr.execCamc(['send', 'abcd1234', '--stdin'], { stdin: Buffer.from('hello agent', 'utf8') });
    assert.strictEqual(res.ok, true);
    assert.strictEqual(stdinWrites.length, 1, 'stdin was written once');
    assert.strictEqual(String(stdinWrites[0]), 'hello agent');
  }

  // ── execCamc win32 error mapping ─────────────────────────────────
  {
    const calls = [], stdinWrites = [];
    // wsl.exe itself missing (ENOENT on the wsl.exe spawn).
    lr.configure({
      platform: 'win32',
      execFileImpl: makeExecFile(() => ({ err: Object.assign(new Error('spawn wsl.exe ENOENT'), { code: 'ENOENT' }) }), calls, stdinWrites),
    });
    const noWsl = await lr.execCamc(['--json', 'list']);
    assert.strictEqual(noWsl.ok, false);
    assert.strictEqual(noWsl.error, 'wsl_missing', 'ENOENT on wsl.exe maps to wsl_missing');

    // Distro missing: $HOME probe fails, then `wsl.exe --status` works
    // (WSL present) → wsl_distro_missing.
    lr.configure({
      execFileImpl: makeExecFile((file, args) => {
        if (args[0] === '--status') return { stdout: '', stderr: '' };
        return { err: Object.assign(new Error('exit 1'), { code: 1 }), stderr: 'Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND' };
      }, calls, stdinWrites),
    });
    const noDistro = await lr.execCamc(['--json', 'list']);
    assert.strictEqual(noDistro.ok, false);
    assert.strictEqual(noDistro.error, 'wsl_distro_missing', 'unusable distro maps to wsl_distro_missing');

    // camc not bootstrapped in the distro: $HOME resolves, camc exec
    // exits nonzero with "No such file or directory" → camc_missing.
    lr.configure({
      execFileImpl: makeExecFile((file, args) => {
        if (args.includes('bash')) return { stdout: '/home/tester', stderr: '' };
        return { err: Object.assign(new Error('exit 127'), { code: 127 }), stderr: 'wsl: /home/tester/.cam/camc: No such file or directory' };
      }, calls, stdinWrites),
    });
    const noCamc = await lr.execCamc(['--json', 'list']);
    assert.strictEqual(noCamc.ok, false);
    assert.strictEqual(noCamc.error, 'camc_missing', 'missing in-distro camc maps to camc_missing');
  }

  // ── ensureCamc ───────────────────────────────────────────────────
  {
    // POSIX: no-op.
    lr.configure({ platform: 'linux' });
    const native = await lr.ensureCamc({ content: Buffer.from('x'), path: '/x', hash: 'abc123' });
    assert.strictEqual(native.ok, true);
    assert.strictEqual(native.skipped, 'native');

    const calls = [], stdinWrites = [];
    const BUNDLED = { content: Buffer.from('#!/bin/sh\npolyglot'), path: '/bundled/camc', hash: 'deadbeeff00d'.slice(0, 12) };

    // win32 happy path: probe reports the same hash → present, cached.
    // (The real probe pipes md5sum through `cut -c1-12`, so stdout is
    // just the short hash.)
    lr.configure({
      platform: 'win32',
      execFileImpl: makeExecFile((file, args) => {
        const cmd = args.join(' ');
        if (/md5sum/.test(cmd)) return { stdout: BUNDLED.hash + '\n', stderr: '' };
        return { stdout: '', stderr: '' };
      }, calls, stdinWrites),
    });
    const present = await lr.ensureCamc(BUNDLED);
    assert.strictEqual(present.ok, true);
    assert.strictEqual(present.present, true, 'hash match → already present');
    assert(stdinWrites.length === 0, 'no upload when hashes match');
    const cached = await lr.ensureCamc(BUNDLED);
    assert.strictEqual(cached.cached, true, 'second call hits the ready-cache');

    // win32 upload path: probe finds nothing → mkdir + cat>tmp (stdin =
    // bundled content) + chmod/mv + version verify.
    calls.length = 0; stdinWrites.length = 0;
    lr.configure({
      execFileImpl: makeExecFile((file, args) => {
        const cmd = args.join(' ');
        if (/md5sum/.test(cmd)) return { err: Object.assign(new Error('exit 1'), { code: 1 }), stderr: '' };
        if (/camc version/.test(cmd)) return { stdout: 'camc 1.2.3\n', stderr: '' };
        return { stdout: '', stderr: '' };
      }, calls, stdinWrites),
    });
    const installed = await lr.ensureCamc(BUNDLED);
    assert.strictEqual(installed.ok, true, 'bootstrap ok: ' + (installed.detail || ''));
    assert.strictEqual(installed.installed, true);
    assert.strictEqual(stdinWrites.length, 1, 'bundled content uploaded via stdin');
    assert.strictEqual(String(stdinWrites[0]), String(BUNDLED.content));
    const cmds = calls.map(c => c.args.join(' '));
    assert(cmds.some(c => /mkdir -p ~\/.cam/.test(c)), 'mkdir ran');
    assert(cmds.some(c => /chmod 700/.test(c) && /mv /.test(c)), 'chmod+mv ran');

    // win32 failure: wsl.exe missing → wsl_missing.
    lr.configure({
      execFileImpl: makeExecFile(() => ({ err: Object.assign(new Error('spawn wsl.exe ENOENT'), { code: 'ENOENT' }) }), calls, stdinWrites),
    });
    const noWsl = await lr.ensureCamc(BUNDLED);
    assert.strictEqual(noWsl.ok, false);
    assert.strictEqual(noWsl.error, 'wsl_missing');
  }

  // ── checkEnvironment (native) ────────────────────────────────────
  {
    const calls = [], stdinWrites = [];
    lr.configure({
      platform: 'linux',
      execFileImpl: makeExecFile(() => ({
        stdout: JSON.stringify({
          issues: [],
          resolved: { tmux: '/usr/bin/tmux', tool: '/usr/bin/claude' },
          warnings: [],
        }),
        stderr: '',
      }), calls, stdinWrites),
    });
    const env = await lr.checkEnvironment('claude');
    assert.strictEqual(env.ok, true, 'native env ok');
    assert.strictEqual(env.runtime, 'native');
    assert.deepStrictEqual(env.checks, { python3: true, tmux: true, tool: true, tool_auth: true });
  }

  lr._resetForTests();
  console.log('local-runtime: core assertions passed');
}

async function attachTests() {
  const { EventEmitter } = require('events');
  const calls = [];
  function makeSpawn() {
    return function fakeSpawn(file, args, opts) {
      calls.push({ file, args, opts, stdinWrites: [], killed: [] });
      const rec = calls[calls.length - 1];
      const child = new EventEmitter();
      child.stdin = { write(d) { rec.stdinWrites.push(d); }, end() {}, on() {} };
      child.stdout = new EventEmitter();
      child.stderr = { resume() {} };
      child.kill = (sig) => { rec.killed.push(sig); };
      return child;
    };
  }
  const homeProbe = makeExecFile((file, args) => {
    if (args.includes('bash')) return { stdout: '/home/tester', stderr: '' };
    return { stdout: '', stderr: '' };
  }, [], []);

  // ── openAttachChannel: win32 → wsl.exe script-PTY argv ────────────
  calls.length = 0;
  lr.configure({ platform: 'win32', wslDistro: 'Ubuntu', execFileImpl: homeProbe, spawnImpl: makeSpawn() });
  {
    const ch = await lr.openAttachChannel('abcd1234', { cols: 120, rows: 40 });
    assert.strictEqual(ch.ok, true, 'win32 attach channel opens');
    const sp = calls[0];
    assert.strictEqual(sp.file, 'wsl.exe');
    assert.deepStrictEqual(sp.args.slice(0, 5), ['-d', 'Ubuntu', '--exec', 'script', '-qec']);
    const inner = sp.args[5];
    assert(inner.includes('stty cols 120 rows 40'), 'stty sizes the PTY: ' + inner);
    assert(inner.includes('/home/tester/.cam/camc'), 'in-distro camc path: ' + inner);
    assert(inner.includes("attach 'abcd1234'"), 'attach command: ' + inner);
    assert.strictEqual(sp.args[6], '/dev/null');
    ch.write('ls\n');
    assert.strictEqual(String(sp.stdinWrites[0]), 'ls\n', 'write() reaches child stdin');
    ch.resize(200, 50); // documented no-op, must not throw
    ch.dispose();
    assert.deepStrictEqual(sp.killed, ['SIGTERM'], 'dispose kills the child');
  }

  // ── openAttachChannel: linux util-linux script shape ──────────────
  calls.length = 0;
  lr.configure({ platform: 'linux', camcPath: '/opt/camc/camc', spawnImpl: makeSpawn() });
  {
    const ch = await lr.openAttachChannel('abcd1234', { cols: 'not-a-number', rows: 24 });
    assert.strictEqual(ch.ok, true);
    const sp = calls[0];
    assert.strictEqual(sp.file, 'script');
    assert.strictEqual(sp.args[0], '-qec');
    assert(sp.args[1].includes('stty cols 80 rows 24'), 'bogus cols clamps to 80: ' + sp.args[1]);
    assert(sp.args[1].includes("exec '/opt/camc/camc' attach 'abcd1234'"), sp.args[1]);
    assert.strictEqual(sp.args[2], '/dev/null');
  }

  // ── openAttachChannel: darwin BSD script shape ────────────────────
  calls.length = 0;
  lr.configure({ platform: 'darwin', camcPath: '/opt/camc/camc', spawnImpl: makeSpawn() });
  {
    const ch = await lr.openAttachChannel('abcd1234', { cols: 100, rows: 30 });
    assert.strictEqual(ch.ok, true);
    const sp = calls[0];
    assert.strictEqual(sp.file, 'script');
    assert.deepStrictEqual(sp.args.slice(0, 3), ['-q', '/dev/null', 'sh']);
    assert.strictEqual(sp.args[3], '-c');
    assert(sp.args[4].includes('stty cols 100 rows 30'), sp.args[4]);
  }

  // ── openAttachChannel: win32 with no WSL → structured error ───────
  lr.configure({
    platform: 'win32',
    execFileImpl: makeExecFile((file, args) => {
      if (args[0] === '--status') return { stdout: '', stderr: '' };
      return { err: Object.assign(new Error('exit 1'), { code: 1 }), stderr: 'WSL_E_DISTRO_NOT_FOUND' };
    }, [], []),
    spawnImpl: makeSpawn(),
  });
  {
    const ch = await lr.openAttachChannel('abcd1234', {});
    assert.strictEqual(ch.ok, false);
    assert.strictEqual(ch.error, 'wsl_distro_missing', ch.error + ' ' + (ch.detail || ''));
  }
}

main()
  .then(attachTests)
  .then(() => {
    lr._resetForTests();
    console.log('local-runtime: all assertions passed');
    process.exit(0);
  })
  .catch((e) => { console.error('FATAL', e); process.exit(1); });
