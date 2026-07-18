# Remote Tool Runtime Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the built Skillm, TodoCLI, and Camflow runtimes available through one authenticated CAM Hub API that Desktop and Mobile can consume without executing tool binaries in their renderers.

**Architecture:** Electron Desktop packages the runtime artifacts and the embedded Hub owns local/remote execution, deployment, redaction, and job state. Mobile remains a thin API/WebSocket client and does not bundle Linux/Python executables. Desktop and Mobile share API contracts, state models, actions, and design tokens while keeping platform-specific page renderers.

**Tech Stack:** Electron/Node.js CommonJS Hub, vanilla ES modules in `web/js`, Android WebView client, SSH transport pool, tracked standalone binaries under `dist/`, JSON/HTTP APIs, WebSocket events, Node test runner.

## Global Constraints

- Renderer code must never execute shell commands or receive SSH credentials.
- Direct and Relay connections must resolve to the same `CamApi` surface.
- Desktop may execute bundled tools through the embedded Hub; Android Mobile must use the remote CAM API and must not bundle Linux/Python executables.
- Remote commands must use argv arrays, bounded timeouts, redacted diagnostics, and explicit workspace/context scoping.
- Skillm remains the source of truth for skills; TodoCLI remains the source of truth for worklog data; Camflow remains the source of truth for workflow execution state.
- Existing Skillm APIs remain compatible while new Tool Runtime APIs are introduced incrementally.
- No user tokens, SSH passwords, or raw Git credentials may be persisted in `embedded-hub.json`, client localStorage, or API responses.
- Every task ends with focused tests and a small commit; do not commit regenerated `apps/cam-desktop/package-lock.json` unless a dependency change is intentional.

---

## File Map

### Existing files to modify

- `apps/cam-desktop/package.json` — package the three runtime artifacts as Electron `extraResources`.
- `apps/cam-desktop/electron/embedded-hub.cjs` — resolve artifacts, deploy/verify remote copies, execute allowlisted commands, expose APIs, and publish events.
- `apps/cam-desktop/test/hub.test.cjs` — Hub route, command-argv, timeout, redaction, and artifact-resolution tests.
- `web/js/api.js` — shared client methods for runtime status, Todo operations, and Camflow operations.
- `web/js/shared/hub-capabilities.js` — advertise per-runtime capabilities without assuming Direct mode.
- `web/js/shared/todos-controller.js` — replace direct persistence calls with a store interface while retaining the local fallback.
- `web/js/shared/todos-workspace.js` — consume asynchronous store actions and shared Todo state.
- `web/js/desktop/todos-mode.js` — connect the desktop renderer to the Hub-backed store.
- `web/js/mobile/todos.js` — connect the mobile renderer to the same Hub-backed store and workspace selector.
- `web/mobile.html` and `web/js/mobile/app.js` — add Mobile entry points only after the API/store contract is stable.

### New files to create

- `dist/todocli` — versioned executable artifact supplied by the TodoCLI build.
- `dist/camflow` — versioned executable artifact supplied by the Camflow build.
- `dist/runtime-manifest.json` — artifact names, versions, SHA-256 digests, supported platforms, and protocol version.
- `apps/cam-desktop/electron/tool-runtime.cjs` — pure artifact resolution, hash verification, argv construction, timeout, and redaction helpers.
- `apps/cam-desktop/electron/todocli-service.cjs` — TodoCLI command adapter and normalized response mapper.
- `apps/cam-desktop/electron/camflow-service.cjs` — Camflow package/run/status/cancel adapter and durable run-state mapper.
- `web/js/shared/tool-runtime.js` — shared capability/status/job state model and event normalization.
- `web/js/shared/todos-store.js` — `LocalTodoStore` and `RemoteTodoStore` implementations.
- `web/js/shared/camflow-store.js` — workflow list/run/status/cancel client state.
- `apps/cam-desktop/test/tool-runtime.test.cjs` — artifact and command-safety unit tests.
- `apps/cam-desktop/test/todocli-api.test.cjs` — TodoCLI API contract tests.
- `apps/cam-desktop/test/camflow-api.test.cjs` — Camflow API contract tests.
- `docs/desktop/remote-tool-runtime.md` — deployment, API, artifact, security, and troubleshooting contract.

---

## Task 1: Establish the runtime artifact contract

**Files:**
- Create: `dist/runtime-manifest.json`
- Create: `docs/desktop/remote-tool-runtime.md`
- Modify: `apps/cam-desktop/package.json:61-73`
- Test: `apps/cam-desktop/test/tool-runtime.test.cjs`

**Interfaces:**
- Produces manifest shape `{ protocol: 1, artifacts: { skillm, todocli, camflow } }` where each artifact has `{ file, version, sha256, executable, platforms }`.
- `package.json` maps `../../dist/skillm`, `../../dist/todocli`, and `../../dist/camflow` to `resources/tools/`.

- [ ] **Step 1: Write failing manifest and packaging assertions.** Assert that all three artifact entries exist, each file is relative to `dist/`, and Electron `extraResources` includes the three exact source/destination mappings.
- [ ] **Step 2: Run the focused test.** Run `node --test test/tool-runtime.test.cjs`; expect failure because the manifest and two package mappings do not exist.
- [ ] **Step 3: Add the manifest and package mappings.** Record the actual built versions and SHA-256 values from the supplied binaries; do not invent placeholder versions or hashes.
- [ ] **Step 4: Document the contract.** Describe Desktop packaging, remote upload to `~/.cam/tools/<name>`, Mobile API-only behavior, artifact compatibility, and the rule that runtime state belongs on the server.
- [ ] **Step 5: Run the test and package lint.** Run `node --test test/tool-runtime.test.cjs` and `npm run lint:electron`; expect PASS.
- [ ] **Step 6: Commit.** `git add dist/runtime-manifest.json apps/cam-desktop/package.json apps/cam-desktop/test/tool-runtime.test.cjs docs/desktop/remote-tool-runtime.md && git commit -m "feat(desktop): define remote tool runtime artifacts"`

## Task 2: Extract safe runtime execution and deployment helpers

**Files:**
- Create: `apps/cam-desktop/electron/tool-runtime.cjs`
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `apps/cam-desktop/test/tool-runtime.test.cjs`

**Interfaces:**
- `resolveBundledTool(name, { resourcesPath, sourceRoot }) -> { path, version, sha256 } | { error, detail }`.
- `buildToolArgv(name, operation, input) -> string[]`.
- `redactToolOutput(text) -> string`.
- `verifyRemoteTool(name, context) -> Promise<{ ok, action, version, error?, detail? }>`.
- `runRemoteTool(name, operation, input, context) -> Promise<{ ok, stdout, stderr, exitCode, error?, detail? }>`.

- [ ] **Step 1: Write unit tests** for path traversal rejection, unknown tool rejection, argv-only construction, timeout mapping, token redaction, SHA mismatch detection, and missing artifact errors.
- [ ] **Step 2: Run `node --test test/tool-runtime.test.cjs`** and confirm the new helper imports fail.
- [ ] **Step 3: Implement the helper module.** Allow only `skillm`, `todocli`, and `camflow`; resolve packaged resources first and development `dist/` second; never concatenate user input into a shell command.
- [ ] **Step 4: Refactor existing Skillm deployment code** in `embedded-hub.cjs` to call the shared resolver/verifier while preserving `/api/skillm/*` response shapes.
- [ ] **Step 5: Run focused tests and Hub syntax checks.** Run `node --test test/tool-runtime.test.cjs` and `node --check electron/embedded-hub.cjs`; expect PASS.
- [ ] **Step 6: Commit.** `git add apps/cam-desktop/electron/tool-runtime.cjs apps/cam-desktop/electron/embedded-hub.cjs apps/cam-desktop/test/tool-runtime.test.cjs && git commit -m "feat(hub): centralize safe tool runtime execution"`

## Task 3: Add TodoCLI Hub APIs and remote Todo store

**Files:**
- Create: `apps/cam-desktop/electron/todocli-service.cjs`
- Create: `web/js/shared/todos-store.js`
- Create: `apps/cam-desktop/test/todocli-api.test.cjs`
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `web/js/api.js`
- Modify: `web/js/shared/todos-controller.js`
- Modify: `web/js/shared/todos-workspace.js`
- Modify: `web/js/desktop/todos-mode.js`
- Modify: `web/js/mobile/todos.js`

**Interfaces:**
- `GET /api/todos?context=<name>&workspace=<path>` returns `{ ok, items, projects, cursor?, source }`.
- `POST /api/todos/items` accepts `{ contextName, workspacePath, item }` and returns `{ ok, item }`.
- `PATCH /api/todos/items/:id` accepts a validated operation body and returns `{ ok, item }`.
- `DELETE /api/todos/items/:id` returns `{ ok, id }`.
- `RemoteTodoStore` implements `load()`, `create(item)`, `update(id, patch)`, `remove(id)`, and `subscribe(listener)`.
- `LocalTodoStore` preserves the current localStorage fallback for offline/dev mode.

- [ ] **Step 1: Write failing API tests** for list/create/update/delete, workspace scoping, `todocli` missing, invalid item input, and redacted command failures.
- [ ] **Step 2: Run `node --test test/todocli-api.test.cjs`** and confirm the routes/service are absent.
- [ ] **Step 3: Implement `todocli-service.cjs`.** Translate normalized API operations into allowlisted TodoCLI argv, parse JSON when available, otherwise parse the documented Markdown/worklog output, and return stable normalized records.
- [ ] **Step 4: Add Hub routes and event publication.** Route all mutations through the existing context/SSH transport, publish `todos.changed` with context and workspace identifiers, and never return raw credentials.
- [ ] **Step 5: Add `CamApi` methods and the store abstraction.** Keep UI-independent action names; select `RemoteTodoStore` when the Hub advertises `todocli`, otherwise use `LocalTodoStore`.
- [ ] **Step 6: Update Desktop and Mobile adapters.** Keep shared Todo behavior and terminology, but let each renderer own its layout and loading/error presentation.
- [ ] **Step 7: Run tests.** Run `node --test test/todocli-api.test.cjs test/hub.test.cjs`, then syntax-check the changed browser modules with the repository’s existing JS checks.
- [ ] **Step 8: Commit.** `git add apps/cam-desktop/electron/todocli-service.cjs apps/cam-desktop/electron/embedded-hub.cjs apps/cam-desktop/test/todocli-api.test.cjs web/js/api.js web/js/shared/todos-store.js web/js/shared/todos-controller.js web/js/shared/todos-workspace.js web/js/desktop/todos-mode.js web/js/mobile/todos.js && git commit -m "feat(todos): add Hub-backed TodoCLI store"`

## Task 4: Add Camflow package and run APIs

**Files:**
- Create: `apps/cam-desktop/electron/camflow-service.cjs`
- Create: `web/js/shared/camflow-store.js`
- Create: `apps/cam-desktop/test/camflow-api.test.cjs`
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs`
- Modify: `web/js/api.js`
- Modify: `web/js/shared/hub-capabilities.js`

**Interfaces:**
- `GET /api/camflow/packages?context=<name>&workspace=<path>` returns `{ ok, packages }`.
- `POST /api/camflow/packages/install` accepts `{ contextName, workspacePath, packagePath, scope }` and returns `{ ok, package }`.
- `POST /api/camflow/runs` accepts `{ contextName, workspacePath, packageName, inputs }` and returns `{ ok, run }`.
- `GET /api/camflow/runs/:id` returns `{ ok, run, events }`.
- `POST /api/camflow/runs/:id/cancel` returns `{ ok, run }`.
- `CamflowRun` has `{ id, packageName, status, startedAt, finishedAt, error, agentIds }`.

- [ ] **Step 1: Write failing API tests** for package list/inspect, install path validation, run creation, status transitions, cancellation, reconnecting to an existing run, and unknown package/run errors.
- [ ] **Step 2: Run `node --test test/camflow-api.test.cjs`** and confirm failure.
- [ ] **Step 3: Implement the service.** Use `camflow package ...` and `camflow run ...` argv arrays, store run metadata under the server-owned CAM state directory, and associate spawned agent IDs with the run.
- [ ] **Step 4: Add Hub routes and WebSocket events.** Emit `camflow.run.started`, `camflow.run.updated`, and `camflow.run.finished`; make status recoverable after Desktop/Mobile disconnects.
- [ ] **Step 5: Add capability and client methods.** Relay and Direct must expose the same methods; unsupported servers return capability-aware disabled states rather than renderer exceptions.
- [ ] **Step 6: Run tests and commit.** Run `node --test test/camflow-api.test.cjs test/hub.test.cjs`; commit with `git commit -m "feat(camflow): expose durable workflow runs through Hub"`.

## Task 5: Expose the shared runtime contract to Mobile

**Files:**
- Modify: `web/js/api.js`
- Modify: `web/js/shared/hub-capabilities.js`
- Modify: `web/mobile.html`
- Modify: `web/js/mobile/app.js`
- Modify: `web/js/mobile/todos.js`
- Create or modify: `web/js/mobile/skills.js`
- Create or modify: `web/js/mobile/camflow.js`
- Modify: `web/css/mobile.css`

**Interfaces:**
- Mobile calls only the same `/api/skillm/*`, `/api/todos/*`, and `/api/camflow/*` routes as Desktop.
- Mobile renders server capability, loading, offline, and reconnect states without assuming a local executable.
- Mobile receives event updates through the existing Relay/Direct WebSocket abstraction.

- [ ] **Step 1: Add client contract tests or fixtures** covering capability combinations: all tools available, only Skillm available, and a remote server with no local runtime.
- [ ] **Step 2: Implement Mobile service adapters.** Reuse shared API/state modules; keep compact mobile-specific screens and navigation.
- [ ] **Step 3: Add Skills and Camflow entry points.** Use read/list/run actions first; keep destructive operations behind explicit confirmation.
- [ ] **Step 4: Run the Android build and packaged-asset checks.** Verify the APK contains the client modules but no Linux/Python tool binaries, and verify remote API calls use the configured server/relay connection.
- [ ] **Step 5: Commit.** `git add web/mobile.html web/js/mobile web/js/api.js web/js/shared/hub-capabilities.js web/css/mobile.css && git commit -m "feat(mobile): consume remote tool runtime APIs"`

## Task 6: Build, package, and end-to-end verify

**Files:**
- Modify: `docs/desktop/remote-tool-runtime.md`
- Modify: `dist/BUILD_LOG.md`
- Test: `apps/cam-desktop/test/*.test.cjs`

- [ ] **Step 1: Verify artifact inputs.** Run `sha256sum dist/skillm dist/todocli dist/camflow` and compare each result to `dist/runtime-manifest.json`.
- [ ] **Step 2: Run all Hub tests.** Run `cd apps/cam-desktop && npm run lint:electron && npm run test:hub`; expect all existing and new tests to pass.
- [ ] **Step 3: Build Desktop.** Run `npm run build:win-msi` from `apps/cam-desktop`; inspect the MSI/app resources and assert `resources/tools/skillm`, `resources/tools/todocli`, and `resources/tools/camflow` exist.
- [ ] **Step 4: Verify remote deployment.** Against an isolated SSH test context, confirm missing tools are uploaded, matching hashes are not re-uploaded, commands execute in the selected workspace, and stderr never contains the test token.
- [ ] **Step 5: Build Mobile.** Run the existing Android build command and assert the APK contains the Mobile API modules but none of the three executable artifacts.
- [ ] **Step 6: Run the product smoke flow.** Connect Desktop and Mobile to one server, list/install a Skillm skill, create/update a TodoCLI item, launch/cancel a Camflow run, disconnect one client, reconnect, and verify both clients converge through WebSocket events.
- [ ] **Step 7: Update troubleshooting documentation.** Record artifact version mismatch, unsupported capability, remote tool missing, expired token, disconnected relay, and resumable Camflow run diagnostics.
- [ ] **Step 8: Commit the verified documentation/build metadata.** `git add docs/desktop/remote-tool-runtime.md dist/BUILD_LOG.md && git commit -m "docs: verify desktop and mobile remote tool runtime"`

## Self-review

- Skillm packaging and existing routes are covered by Tasks 1–2.
- TodoCLI server integration, local fallback, Desktop, and Mobile are covered by Task 3.
- Camflow execution, persistence, cancellation, and events are covered by Task 4.
- Mobile is explicitly API-only and is covered by Task 5.
- Artifact hashes, Desktop resources, Android exclusions, and end-to-end convergence are covered by Task 6.
- No task requires the renderer to receive credentials or execute a binary.
- No task requires a new frontend framework or a duplicated backend database.

