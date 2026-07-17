'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..', '..', '..');
const html = fs.readFileSync(path.join(root, 'web', 'desktop.html'), 'utf8');
const hub = fs.readFileSync(path.join(__dirname, '..', 'electron', 'embedded-hub.cjs'), 'utf8');

assert(html.indexOf('id="start-name"') < html.indexOf('id="start-prompt"'), 'Task name must precede Prompt');
assert(!html.slice(html.indexOf('id="start-advanced"')).includes('id="start-name"'), 'Task name must not remain in Advanced');
// Local sessions retired 2026-07-17 (product decision): the local-node
// datapath (local-runtime.cjs, WSL routing, readiness hint) is removed.
// The hub refuses local targets with a structured local_unsupported
// error that carries the SSH-node guidance.
assert(hub.includes('local_unsupported'), 'Hub must refuse local sessions with local_unsupported');
assert(hub.includes('run an SSH server on it and add it as an SSH node'), 'Hub refusal must carry the SSH-node guidance');
assert(!fs.existsSync(path.join(__dirname, '..', 'electron', 'local-runtime.cjs')), 'local-runtime.cjs must be deleted');
assert(!html.includes('id="start-local-runtime-hint"'), 'Start form must no longer expose a local-runtime readiness hint');

console.log('6 passed, 0 failed');
