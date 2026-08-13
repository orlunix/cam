'use strict';

/* ext-nav.test.cjs — guards the agent-console Ext▾ → native mode
 * navigation chain for the agent-doctor extension.
 *
 * Every assertion maps to a bug that actually shipped during the ext
 * work: versionless duplicate module instances (silent handoff
 * failure), unregistered mode (setMode coerces to default), missing
 * mode panel (nothing to unhide), native filtered out of the menu,
 * native package still carrying an iframe view. The project has no
 * jsdom; this static chain check is the project's established pattern
 * (see start-form.test.cjs). The click itself is verified by MSI
 * smoke test.
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..', '..', '..');
const read = p => fs.readFileSync(path.join(root, p), 'utf8');

const html     = read('web/desktop.html');
const appJs    = read('web/js/desktop/app.js');
const consoleJs = read('web/js/desktop/agent-console.js');
const extMode  = read('web/js/desktop/extensions-mode.js');
const doctor   = read('web/js/desktop/agent-doctor-mode.js');
const manifest = read('extensions/packages/agent-doctor/manifest.yaml');

let n = 0;
function ok(cond, msg) { assert(cond, msg); n++; }

// 1. Target panel exists with the data-mode marker applyModeToDom needs.
ok(html.includes('<section class="mode-panel" id="mode-agent-doctor" data-mode="agent-doctor"'),
  'desktop.html must contain #mode-agent-doctor with data-mode="agent-doctor"');

// 2. Mode registered — setMode() coerces unknown modes to the default.
ok(/const MODES = \[[^\]]*'agent-doctor'/.test(appJs),
  'app.js MODES must include agent-doctor');

// 3. Single module instance: app.js, agent-console.js, extensions-mode.js
//    must import agent-doctor-mode through the IDENTICAL ?v= URL. A
//    versionless import creates a second module instance whose private
//    handoff state is disconnected — clicks then fail silently.
const importRe = /from '\.\/agent-doctor-mode\.js(\?v=[0-9.]+)?'/g;
const urls = [appJs, consoleJs, extMode].map(src => (src.match(importRe) || [])[0] || '');
ok(urls.every(Boolean), 'app.js, agent-console.js, extensions-mode.js must all import agent-doctor-mode');
ok(urls[0].includes('?v='), 'agent-doctor-mode import must carry a ?v= query');
ok(urls.every(u => u === urls[0]), 'all agent-doctor-mode imports must share one identical ?v= URL');

// 4. Mount + exports line up.
ok(appJs.includes('mountAgentDoctorMode('), 'app.js must mount the doctor mode');
ok(doctor.includes('export function mountAgentDoctorMode'), 'doctor module must export mountAgentDoctorMode');
ok(doctor.includes('export function setDoctorAgent'), 'doctor module must export setDoctorAgent (handoff)');

// 5. Ext▾ menu admits native extensions and routes them to their mode.
ok(consoleJs.includes('(x.hasView || x.native)'), 'Ext menu filter must admit native extensions');
ok(consoleJs.includes('setMode(ext.native)'), 'Ext menu must route native extensions via setMode(ext.native)');
ok(consoleJs.includes("setDoctorAgent(a, { returnTo: 'agents' })"), 'Ext menu must hand off the selected agent');

// 5b. THE shadowing bug that silently killed Ext▾ for weeks:
//     agent-console.js must never declare a local `function setMode` —
//     it hoists and shadows the mountAgentConsole({ setMode }) parameter
//     (the app-level page-mode setter), so every setMode() call inside
//     the module silently hits the wrong function.
ok(!/function setMode\(/.test(consoleJs),
  'agent-console.js must not declare a local function setMode (shadows the app-level setMode param)');
ok(consoleJs.includes('function setOutputMode('),
  'output-mode switcher must be named setOutputMode');

// 6. Extensions page native branch hands off the Back target.
ok(extMode.includes("setDoctorAgent(null, { returnTo: 'extensions' })"),
  'extensions-mode must hand off returnTo for the doctor Back button');

// 7. Manifest/native wiring: registry yields native + tool, no view.
ok(manifest.includes('native: agent-doctor'), 'agent-doctor manifest must declare native: agent-doctor');
ok(!fs.existsSync(path.join(root, 'extensions', 'packages', 'agent-doctor', 'index.html')),
  'native package must not carry an iframe view (index.html)');

console.log(`${n} passed, 0 failed`);
