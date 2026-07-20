# Start form and local-runtime feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make the Desktop Start form place Task name before Prompt, allow interactive empty-prompt launches, and provide actionable Windows-local runtime guidance.

**Architecture:** web/desktop.html owns Start form order. The embedded Hub owns Direct-mode request validation and the Windows runtime boundary, so it accepts an empty prompt while returning a stable preflight error before a Windows process spawn. Existing start-agent-mode.js continues to present Hub errors through its status line and toast.

**Tech Stack:** Static HTML, browser ES modules, Electron CommonJS, Node built-in HTTP test harness.

## Global Constraints

- Retain the automatic local Node/context; do not alter Nodes persistence or deletion behavior.
- An empty prompt launches the selected CLI interactively; whitespace is normalized to an empty prompt.
- Windows-native Local Start must not try to execute the bundled POSIX CAMC script.
- The preflight message names /bin/sh, Python 3, tmux, CAMC, and the selected CLI/authentication requirement, and directs the user to a configured Linux SSH node.
- Do not commit or push unless the user later explicitly authorizes it.

---

### Task 1: Lock the Start layout and empty-prompt API contract

**Files:**
- Modify: apps/cam-desktop/test/hub.test.cjs
- Create: apps/cam-desktop/test/start-form.test.cjs
- Modify: apps/cam-desktop/package.json

**Interfaces:**
- Consumes: POST /api/agents with tool, prompt, context, and path.
- Produces: npm run test:start, which validates source-level Start layout and local-runtime preflight copy; Hub test coverage validates the API response.

- [ ] **Step 1: Write a failing Hub behavior test for an empty prompt**

Add this test after the existing remote-start parity test in apps/cam-desktop/test/hub.test.cjs:

~~~js
setRemoteHandler((opts) => {
  if (/'run'/.test(opts.command)) return { ok: true, stdout: '  ID: feedbeef\n', stderr: '' };
  if (/'status'/.test(opts.command)) return { ok: true, stdout: JSON.stringify({
    id: 'feedbeef', status: 'running', state: 'initializing',
    task: { tool: 'claude', name: '', prompt: '' },
    context_path: '/home/ren/src', transport_type: 'ssh',
  }), stderr: '' };
  return { ok: false, error: 'exec_failed', detail: 'unhandled cmd' };
});
r = await request('POST', '/api/agents', {
  tool: 'claude', prompt: '', context: 'ren01', path: '/home/ren/src',
});
eq('empty prompt start accepted', r.status, 201);
eq('empty prompt start agent id', r.body.agentId, 'feedbeef');
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: npm run test:hub

Expected: the new assertion fails because POST /api/agents returns HTTP 400 with missing_prompt.

- [ ] **Step 3: Write a failing Start source-contract test**

Create apps/cam-desktop/test/start-form.test.cjs:

~~~js
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
~~~

Add this package script:

~~~json
"test:start": "node test/start-form.test.cjs"
~~~

- [ ] **Step 4: Run the Start source-contract test to verify it fails**

Run: npm run test:start

Expected: it fails because Task name remains in Advanced and the Hub has no local_runtime_unsupported preflight.

### Task 2: Implement the form and Hub behavior

**Files:**
- Modify: web/desktop.html at the Task and Advanced sections.
- Modify: apps/cam-desktop/electron/embedded-hub.cjs in _startLocalAgent and POST /api/agents.

**Interfaces:**
- Consumes: Start form DOM ids start-name and start-prompt; POST /api/agents body.
- Produces: an interactive CAMC run for an empty prompt, or an error object with local_runtime_unsupported and detail on Windows Local Start.

- [ ] **Step 1: Move Task name into the Task section**

Replace the existing Prompt block in web/desktop.html with:

~~~html
<label>Task name (optional)
  <input type="text" id="start-name" placeholder="auto-generated" autocomplete="off">
</label>
<label>Prompt (optional)
  <textarea id="start-prompt" rows="6"
    placeholder="Describe the task…"
    autocomplete="off" autocorrect="off"
    autocapitalize="off" spellcheck="false"></textarea>
</label>
~~~

Delete the duplicate start-name label from Advanced Options. Do not alter start-agent-mode.js because it already uses the id.

- [ ] **Step 2: Allow empty prompts at the HTTP boundary**

Delete the prompt trimming/rejection immediately after JSON-body validation. After target resolution succeeds, construct the run body as:

~~~js
const runBody = {
  ...body,
  prompt: String(body.prompt || '').trim(),
  path: body.path || (target.ctx && target.ctx.path) || '',
};
~~~

The target failure response stays unchanged. CAMC receives an empty positional prompt and starts an interactive session.

- [ ] **Step 3: Add the Windows Local Start preflight**

At the beginning of _startLocalAgent, before building argv, add:

~~~js
if (process.platform === 'win32') {
  return {
    ok: false,
    error: 'local_runtime_unsupported',
    detail: 'Local agents cannot run directly on Windows. Select a configured Linux SSH node. Local execution requires /bin/sh, Python 3, tmux, CAMC, and the selected ' + String((body && body.tool) || 'agent') + ' CLI installed and authenticated in the target runtime.',
  };
}
~~~

This applies only to local targets. Remote SSH starts remain on _startRemoteAgent.

- [ ] **Step 4: Run the focused tests to verify implementation**

Run: npm run test:hub && npm run test:start

Expected: both exit 0; empty prompt returns 201 and source-contract assertions pass.

### Task 3: Verify the Desktop package source

**Files:**
- Verify: apps/cam-desktop/electron/embedded-hub.cjs
- Verify: web/desktop.html
- Verify: apps/cam-desktop/package.json

**Interfaces:**
- Consumes: completed tests from Tasks 1 and 2.
- Produces: syntax-valid, lint-clean package source ready for a user-authorized MSI build.

- [ ] **Step 1: Run full relevant checks**

Run:

~~~bash
npm run test:hub
npm run test:start
npm run lint:electron
git diff --check
~~~

Expected: every command exits 0 with no whitespace errors.

- [ ] **Step 2: Inspect final diff**

Run:

~~~bash
git diff -- web/desktop.html apps/cam-desktop/electron/embedded-hub.cjs apps/cam-desktop/package.json apps/cam-desktop/test/hub.test.cjs apps/cam-desktop/test/start-form.test.cjs
git status --short
~~~

Expected: only listed Start files and their new test are modified by this work; preserve unrelated pre-existing changes.

- [ ] **Step 3: Do not commit or package without authorization**

Leave verified source changes uncommitted. Build or publish an MSI only if the user explicitly asks.

## Self-review

- Spec coverage: Task 2 retains the local Node by making no Node-store change, moves Task name, permits interactive empty prompts, and adds Windows-local guidance. Tasks 1 and 3 cover regression and source verification.
- Placeholder scan: no implementation placeholders remain.
- Type consistency: the new route error uses the existing error/detail response shape; renderer error handling already consumes it.
