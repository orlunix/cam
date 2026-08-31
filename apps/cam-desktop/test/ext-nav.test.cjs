'use strict';

/* ext-nav.test.cjs — guards the extension navigation chains.
 *
 * Since 0.2.20 agent-doctor is a STANDARD iframe extension (package
 * carries index.html + main.py; updatable by tar.gz install — no app
 * release). Since 0.2.21 it ships as the examples/ DEMO (not built-in);
 * the built-in set is skills/todos/assistant, and built-ins may only be
 * disabled, never removed. This file now guards two things:
 *
 *   A. the native agent-doctor page is FULLY removed (a leftover
 *      mode/import/panel would split the open path in two);
 *   B. the generic ext chains every iframe ext relies on: Ext▾ menu
 *      filter, agent binding handoff (bindContext → app.context), the
 *      uniform ?v= import rule, the app-managed chrome for the
 *      remaining native pages (skills/todos), and the attribute chain.
 *
 * Every assertion maps to a bug that actually shipped during the ext
 * work: versionless duplicate module instances (silent handoff
 * failure), unregistered mode (setMode coerces to default), a local
 * `function setMode` shadowing the mount param (killed Ext▾ for weeks).
 * The project has no jsdom; this static chain check is the project's
 * established pattern. The click itself is verified by MSI smoke test.
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
const viewHost = read('web/js/shared/ext-view-host.js');
const manifest = read('extensions/examples/agent-doctor/manifest.yaml');
const doctorView = read('extensions/examples/agent-doctor/index.html');
const doctorMain = read('extensions/examples/agent-doctor/main.py');

let n = 0;
function ok(cond, msg) { assert(cond, msg); n++; }

// ── A. Native agent-doctor page: gone, completely. ──
ok(!html.includes('mode-agent-doctor'), 'desktop.html must not contain the native doctor panel');
ok(!/const MODES = \[[^\]]*'agent-doctor'/.test(appJs), 'app.js MODES must not include agent-doctor');
ok(!/agent-doctor-mode/.test(appJs + consoleJs + extMode),
  'no module may import the deleted agent-doctor-mode.js');
ok(!fs.existsSync(path.join(root, 'web', 'js', 'desktop', 'agent-doctor-mode.js')),
  'agent-doctor-mode.js must be deleted');
ok(!read('web/css/desktop.css').includes('#mode-agent-doctor'),
  'desktop.css must not keep doctor-namespaced rules');

// ── B1. The doctor package is a self-contained iframe extension. ──
ok(!/^native:/m.test(manifest), 'agent-doctor manifest must NOT declare native (iframe ext)');
ok(manifest.includes('capabilities:') && /-\s*exec/.test(manifest),
  'agent-doctor manifest must keep the exec capability (remote collector)');
ok(doctorView.includes("../client.js"), 'doctor view must load the bridge SDK (../client.js)');
ok(doctorView.includes("camExt.call('agents.list'"), 'doctor view lists agents via the bridge');
ok(doctorView.includes("camExt.call('app.context'"), 'doctor view reads the bound agent via app.context');
ok(/camExt\.call\('ext\.call'/.test(doctorView) && doctorView.includes("method: 'review'"),
  'doctor view must call the collector review method via ext.call');
ok(/def review\(/.test(doctorMain) && doctorMain.includes('workspace_path') && doctorMain.includes('agent_id'),
  'collector review must accept workspace_path + agent_id (single-path, no bound bridge needed)');
ok(/"review":\s*review/.test(doctorMain) || /'review':\s*review/.test(doctorMain),
  'collector METHODS must expose review');

// ── B2. Ext▾ menu: filter, agent binding handoff, native routing. ──
ok(consoleJs.includes('(x.hasView || x.native)'), 'Ext menu filter must admit view + native extensions');
ok(consoleJs.includes('setMode(ext.native)'), 'Ext menu must route native extensions via setMode(ext.native)');
ok(consoleJs.includes('bindContext: { agentId: a.id'),
  'Ext menu must hand off the selected agent to iframe views (bindContext)');
ok(viewHost.includes('context: bindContext'),
  'ext-view-host must register the frame with the bound context (app.context source)');
// Boot race guard: the view's boot() posts its first bridge call while its
// document is still parsing — that message reaches the parent BEFORE the
// iframe 'load' task. Registration must therefore happen before src is
// assigned, or the boot call is silently dropped and the view hangs on its
// first await (assistant settings stuck on the 'node cam-pi.js' placeholder).
ok(viewHost.indexOf('registerExtFrame(iframe.contentWindow') > -1 &&
   viewHost.indexOf('registerExtFrame(iframe.contentWindow') < viewHost.indexOf('iframe.src ='),
  'ext-view-host must register the frame BEFORE assigning iframe.src (boot race)');

// ── B3. THE shadowing bug that silently killed Ext▾ for weeks. ──
ok(!/function setMode\(/.test(consoleJs),
  'agent-console.js must not declare a local function setMode (shadows the app-level setMode param)');
ok(consoleJs.includes('function setOutputMode('),
  'output-mode switcher must be named setOutputMode');

// ── B4. App-managed chrome for the remaining native pages (skills/todos). ──
ok(extMode.includes('export function applyExtChrome'), 'extensions-mode must export applyExtChrome');
ok(extMode.includes('applyExtChrome({ nativeName: ext.native'), 'extensions-mode native open must apply the chrome');
ok(consoleJs.includes('applyExtChrome({ nativeName: ext.native'), 'agent Ext menu native open must apply the chrome');
ok(consoleJs.includes("import { openExtensionView, applyExtChrome } from './extensions-mode.js?v="),
  'agent-console must import applyExtChrome via the same ?v= URL as openExtensionView');
const css = read('web/css/desktop.css');
ok(css.includes('.mode-panel > .ext-chrome') && css.includes('.mode-panel.ext-chromed > .settings-header:not(.ext-chrome)'),
  'CSS must style the chrome and hide ONLY the baked-in header — without :not(.ext-chrome) the more-specific hide rule hides the injected chrome itself (title + Back gone)');
ok(extMode.includes('state.subscribe(') && extMode.includes("conn === 'direct'") && extMode.includes("mode === 'extensions'"),
  'Extensions list must re-refresh on connection-up and page activation — the mount-time refresh races autoStartConnection, so a boot-time "Not connected" otherwise sticks for the whole session');

// ── B4b. skills/todos de-nativized (0.2.31): self-contained ext packages
//    over the hub:api passthrough — no app-shell pages remain. ──
const extClient = read('extensions/host/ext-client.js');
const registry = read('extensions/host/registry.cjs');
const bridge = read('web/js/shared/ext-bridge.js');
const apiJs = read('web/js/api.js');
const hub = fs.readFileSync(path.join(__dirname, '..', 'electron', 'embedded-hub.cjs'), 'utf8');
const skillsManifest = read('extensions/packages/skills/manifest.yaml');
const todosManifest = read('extensions/packages/todos/manifest.yaml');
ok(!/^native:/m.test(skillsManifest) && !/^native:/m.test(todosManifest),
  'skills/todos manifests must NOT declare native — they are iframe extensions now');
ok(skillsManifest.includes('hub:api') && todosManifest.includes('hub:api'),
  'skills/todos must declare the hub:api capability (their views drive hub endpoints)');
ok(fs.existsSync(path.join(root, 'extensions', 'packages', 'skills', 'index.html'))
  && fs.existsSync(path.join(root, 'extensions', 'packages', 'todos', 'index.html'))
  && fs.existsSync(path.join(root, 'extensions', 'packages', 'todos', 'todos-workspace.js')),
  'skills/todos packages must ship their views (todos vendors the workspace modules)');
ok(!html.includes('id="mode-skills"') && !html.includes('id="mode-todos"'),
  'desktop.html must not contain the retired native skills/todos panels');
ok(!appJs.includes('skills-mode') && !appJs.includes('todos-mode')
  && !appJs.includes("'skills'") && !appJs.includes("'todos'"),
  'app.js must not import/mount the retired native pages or carry their modes');
ok(registry.includes("'hub:api'") && bridge.includes("case 'ext.hubCall'") && extClient.includes('hubCall'),
  'hub:api passthrough chain: registry capability + bridge case + client helper');
ok(bridge.includes("case 'ext.storageGet'") && bridge.includes("case 'ext.storageSet'")
  && extClient.includes('storageGet') && extClient.includes('storageSet')
  && apiJs.includes('extStorageGet') && hub.includes('/storage$/.exec(p)'),
  'ext storage chain: sandboxed views have no localStorage — bridge storageGet/Set → hub ext-data/<name>/storage.json');
const skillsView = read('extensions/packages/skills/index.html');
const todosView = read('extensions/packages/todos/index.html');
ok(skillsView.includes("Object.defineProperty(window, 'localStorage'") && todosView.includes("Object.defineProperty(window, 'localStorage'"),
  'both migrated views must install a localStorage shim (opaque-origin sandbox) before their modules load');
ok(todosView.includes('__todosStorageReady') && todosView.indexOf('__todosStorageReady') < todosView.indexOf("mountTodosWorkspace"),
  'todos must hydrate the storage shim BEFORE the controller mounts');
ok(!skillsView.includes('confirm('),
  'no window.confirm in sandboxed views (allow-modals is not granted) — use the armed-button pattern');

// ── B5. Per-extension attributes (Extensions → Settings): hub endpoints,
//    api helpers, bridge read, editor UI — the whole chain or nothing. ──
ok(apiJs.includes('extConfigGet') && apiJs.includes('extConfigSet'), 'api.js must expose extConfigGet/Set');
ok(bridge.includes("case 'ext.config'"), 'ext-bridge must answer ext.config');
ok(bridge.includes("case 'app.context'"), 'ext-bridge must answer app.context');
ok(/\/config\$/.test(hub) || hub.includes('/config'), 'hub must serve /api/extensions/<name>/config');
ok(html.includes('id="ext-edit-wrap"'), 'desktop.html must contain the attribute editor');
ok(html.includes('id="ext-edit-form"'), 'desktop.html must contain the attribute form container');
ok(extMode.includes('ext-edit-btn') && extMode.includes('openEditor'), 'extensions-mode must wire the Edit button');
ok(extMode.includes('parseAttrSpec') && extMode.includes('data-attr-key'), 'extensions-mode must render the schema-driven form');
ok(consoleJs.includes('show_in_agent_menu'), 'Ext menu must honor the show_in_agent_menu attribute');

// ── B6. Uniform row actions (product decision 2026-08-17 v2): every
//    extension row — built-in or user — gets Open/Settings/Enable/Remove.
//    The hub hides a removed built-in via the store `removed` flag
//    (restored on version change) and cascades its config on a full
//    removal (hub.test.cjs covers both). ──
ok(extMode.includes('ext-remove-btn') && !extMode.includes("x.source === 'user'"),
  'extensions-mode must render Remove for every extension row');
ok(!hub.includes('builtin_not_removable'),
  'hub must not refuse built-in removal (removed-flag hide instead)');
ok(hub.includes('ex.removed = true') && hub.includes('_extCascadeDelete'),
  'hub must hide removed built-ins via the removed flag and cascade config');

// ── B7. The built-in assistant chain (docs/desktop/assistant-design.md):
//    view → bridge (name-gated) → preload → main AssistantHost → the
//    bundled pi child. Every link must exist or the page dies silently. ──
const preloadJs = read('apps/cam-desktop/electron/preload.cjs');
const mainJs   = read('apps/cam-desktop/electron/main.cjs');
const assistHost = read('apps/cam-desktop/electron/assistant-host.cjs');
const assistView = read('extensions/packages/assistant/index.html');
const assistEntry = read('extensions/vendor/cam-assist/entry.js');
ok(fs.existsSync(path.join(root, 'extensions', 'vendor', 'cam-assist', 'dist', 'cam-assist.js')),
  'the cam-assist pi bundle must be built and committed');
ok(mainJs.includes("require('./assistant-host.cjs')") && mainJs.includes("ipcMain.handle('assistant:send'")
  && mainJs.includes("ipcMain.handle('assistant:poll'"),
  'main.cjs must wire assistant-host + the assistant:* IPC family');
ok(preloadJs.includes('assistant: {') && preloadJs.includes("ipcRenderer.invoke('assistant:status')"),
  'preload must expose the scoped assistant group');
ok(bridge.includes("reg.name !== 'assistant'"),
  'ext-bridge must gate assistant.* to the assistant extension only');
ok(bridge.includes('assistant_unavailable'),
  'ext-bridge must answer assistant_unavailable outside Electron (mobile web)');
ok(assistHost.includes('ELECTRON_RUN_AS_NODE') && assistHost.includes('assistant:llm-token'),
  'assistant-host must spawn via ELECTRON_RUN_AS_NODE and keep the token in the credential store');
ok(!assistHost.includes('embeddedHub') && !mainJs.includes('embeddedHub.startAssistant'),
  'the hub HTTP server must never spawn the assistant (main-process only)');
ok(assistView.includes('../client.js') && assistView.includes("assistant.configure")
  && assistView.includes("assistant.poll") && assistView.includes("assistant.send"),
  'assistant view must drive the assistant.* bridge family');
ok(!/<h1[ >]/.test(assistView),
  'assistant view must not paint its own title bar (app chrome owns it)');

// ── B7b. The push rail + per-ext storage (0.2.24): main broadcasts every
//    assistant event → preload → bridge → the view's camExt.onEvent, so
//    rendering never waits on a poll tick; config + transcript live under
//    <userData>/ext-data/assistant/ and survive page/app restarts. ──
ok(mainJs.includes("webContents.send('assistant:event'") && preloadJs.includes("ipcRenderer.on('assistant:event'"),
  'assistant push rail: main must broadcast, preload must listen');
ok(bridge.includes("event: 'assistant.event'") && extClient.includes('onEvent'),
  'assistant push rail: bridge must forward into assistant frames, client must expose onEvent');
ok(assistView.includes("camExt.onEvent('assistant.event'"),
  'assistant view must subscribe to the push rail');
ok(assistHost.includes("'ext-data', 'assistant'") && assistHost.includes('threads.json'),
  'assistant-host must persist config + thread transcripts under ext-data/assistant');
ok(assistHost.includes("'extensions', 'assistant', 'cam-assist.js'") && assistHost.includes('userCopy')
  && assistHost.includes('_userPkgWins') && registry.includes('resolvePackageDir') && registry.includes('compareVersions'),
  'bundle shadowing must be VERSION-GATED (registry.resolvePackageDir + assistant-host _userPkgWins): a newer user copy wins, a tie or older copy goes to the built-in — an app reinstall must repair stale shadows');
ok(!assistView.includes('parseBridgeInput') && !assistView.includes('btn-copy-pi') && !assistView.includes('btn-bridge-stop'),
  'direct flavor (0.6.0): the view must NOT carry bridge UI (no launch-command button, no bridge-line parser, no stop button) — local shell runs directly in the child');
ok(assistEntry.includes('LOCAL_SHELL') && assistEntry.includes('execLocal') && !assistEntry.includes('shellBridge'),
  'direct flavor (0.6.0): entry.js must spawn the shell itself (LOCAL_SHELL/execLocal) with no bridge-pair path left');
ok(!fs.existsSync(path.join(root, 'extensions', 'packages', 'assistant', 'cam-pi.js')),
  'bridge cleanup (0.2.36): cam-pi.js no longer ships in the package');
ok(assistView.includes('function stampTime') && /ev\.ts\) stampTime|if \(ev\.ts\) stampTime/.test(assistView),
  'replayed done events must restamp the bubble with ev.ts — otherwise past replies show the replay time after a restart');
ok(!preloadJs.includes('assistant:copy-text') && !bridge.includes('assistant.copyText')
  && !preloadJs.includes('assistant:bridge-status') && !bridge.includes('assistant.bridgeStatus')
  && !assistHost.includes('bridgeShutdown') && !assistHost.includes('_consumeBridgeDrop'),
  'bridge cleanup (0.2.36): copyText/bridgeStatus/bridgeShutdown/drop-poll chains fully removed from host, preload and ext-bridge');
ok(assistHost.includes('function threads()') && assistHost.includes('function newChat')
  && assistHost.includes('function openThread') && assistHost.includes('function deleteThread'),
  'assistant-host must implement the threads API');
ok(assistView.includes('assistant.threads') && assistView.includes('assistant.newChat')
  && assistView.includes('assistant.openThread') && preloadJs.includes('assistant:threads'),
  'threads chain: view → bridge → preload → main');

// ── B8. The CAM backoffice tool (0.2.22): ONE generic `cam` tool in the
//    bundle + hub pair injection + the audit rail. Generic by design —
//    future hub endpoints must NOT require a bundle rebuild. ──
ok(/name:\s*'cam'/.test(assistEntry) && assistEntry.includes('execute:'),
  'cam-assist entry must register the generic cam tool');
ok(assistEntry.includes("path_must_start_with_/api/"),
  'the cam tool must confine itself to /api/* paths');
ok(assistEntry.includes("case 'hub'") && assistEntry.includes("type: 'hub_call'"),
  'entry must accept hub injection and emit hub_call audit events');
ok(assistHost.includes('setHub') && assistHost.includes("type: 'hub'"),
  'assistant-host must forward the hub pair into the child');
ok(mainJs.includes('assistantHost.setHub({ url: r.apiUrl, token: r.apiToken })'),
  'main must re-inject the hub pair on every hub start/restart (token rotates)');
ok(!/assistantHost\.setHub[\s\S]{0,200}renderer/.test(mainJs),
  'the hub token must never be routed through the renderer');

console.log(`${n} passed, 0 failed`);
