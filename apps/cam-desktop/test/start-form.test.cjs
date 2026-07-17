'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..', '..', '..');
const html = fs.readFileSync(path.join(root, 'web', 'desktop.html'), 'utf8');
const hub = fs.readFileSync(path.join(__dirname, '..', 'electron', 'embedded-hub.cjs'), 'utf8');
const localRuntime = fs.readFileSync(path.join(__dirname, '..', 'electron', 'local-runtime.cjs'), 'utf8');

assert(html.indexOf('id="start-name"') < html.indexOf('id="start-prompt"'), 'Task name must precede Prompt');
assert(!html.slice(html.indexOf('id="start-advanced"')).includes('id="start-name"'), 'Task name must not remain in Advanced');
// Local-node datapath: the hub no longer refuses local agents on Windows —
// win32 execution routes through local-runtime.cjs into WSL2 instead.
assert(!hub.includes('local_runtime_unsupported'), 'Hub must no longer refuse local agents on Windows');
assert(hub.includes("require('./local-runtime.cjs')"), 'Hub must wire the local-runtime module');
assert(localRuntime.includes('wsl.exe'), 'Local runtime must route win32 camc execution through wsl.exe');
assert(localRuntime.includes('python3') && localRuntime.includes('tmux'), 'Local runtime preflight must name the python3/tmux prerequisites');
assert(html.includes('id="start-local-runtime-hint"'), 'Start form must expose a local-runtime readiness hint element');

console.log('6 passed, 0 failed');
