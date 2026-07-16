'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..', '..', '..');
const html = fs.readFileSync(path.join(root, 'web', 'desktop.html'), 'utf8');
const hub = fs.readFileSync(path.join(__dirname, '..', 'electron', 'embedded-hub.cjs'), 'utf8');

assert(html.indexOf('id="start-name"') < html.indexOf('id="start-prompt"'), 'Task name must precede Prompt');
assert(!html.slice(html.indexOf('id="start-advanced"')).includes('id="start-name"'), 'Task name must not remain in Advanced');
assert(hub.includes('local_runtime_unsupported'), 'Hub must expose a stable Windows local-runtime error');
assert(hub.includes('Python 3') && hub.includes('tmux') && hub.includes('Linux SSH node'), 'Windows guidance must state prerequisites and recovery path');

console.log('4 passed, 0 failed');
