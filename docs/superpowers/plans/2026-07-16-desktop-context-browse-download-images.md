# Desktop Context Browse, Download, and Image Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Desktop users browse any Node context without an agent, download one selected file through Save As, and preview PNG/JPEG/WebP/GIF/SVG files in the context browser.

**Architecture:** Reuse the existing shared Nodes callback and context file APIs. Extract the Mobile-only context browser into a parameterized shared view that Desktop mounts in a non-nav transient mode. Add a context-download HTTP endpoint that validates the same root-relative path as list/read and returns raw file bytes; Desktop saves those bytes through a narrow Electron IPC handler.

**Tech Stack:** ES modules, Electron IPC (`dialog`, `fs`), embedded Node HTTP Hub, existing NodeTransport file operations, DOM `Blob`/`<img>`.

## Global Constraints

- Browse is context-scoped and opens `context.path` even when the context has zero agents.
- Nodes context actions read `browse`, `duplicate`, `edit`, `delete` in that order.
- Download supports regular files only; directory download and archive creation are out of scope.
- Preview supports `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.svg`; GIF remains animated.
- SVG is rendered only as an image Blob URL, never injected as HTML or an executable `iframe` document.
- Existing `GET /files/read` retains its 5 MiB JSON preview cap. The download path returns raw bytes and has a separately bounded maximum response size.
- All context paths must reuse the existing absolute-path, `..`, symlink/root, and context id-or-name validation rules.
- Do not commit or push without separate user authorization.

---

### Task 1: Hub raw context-download contract

**Files:**
- Modify: `apps/cam-desktop/electron/embedded-hub.cjs:1917-2351,3995-4004`
- Modify: `apps/cam-desktop/test/hub.test.cjs`

**Interfaces:**
- Produces `GET /api/contexts/:name_or_id/files/download?path=<relative-file>`.
- Success: HTTP 200, raw bytes, `Content-Disposition: attachment; filename*=UTF-8''<encoded-basename>`, a conservative content type, and `Content-Length`.
- Failure: the existing JSON error convention (`400` invalid/traversal/directory, `404` missing context/file, `413` over limit, `502` transport failure).

- [ ] **Step 1: Write failing Hub tests for download routing and safety.**

  Add a local-context fixture containing a text file, a binary PNG fixture, a directory, and a traversal attempt. Assert: (a) a name and an id both download the exact text bytes; (b) binary bytes are unchanged; (c) the headers include attachment disposition and the final basename only; (d) directory and `../outside` fail without reading outside the root.

- [ ] **Step 2: Run the focused Hub test before production code.**

  Run: `cd apps/cam-desktop && node test/hub.test.cjs`

  Expected: the new download assertions fail because `/files/download` is not routed.

- [ ] **Step 3: Implement a byte-oriented context read helper and route.**

  Add a `CONTEXT_DOWNLOAD_MAX_BYTES` constant (100 MiB) and a helper that starts from `findContextByNameOrId`, `_cleanBrowseSubpath`, `_resolveBrowseRoot`, and the existing root safety checks. Do not call `_packBrowseRead`; that function is intentionally JSON/base64 preview behavior. For local contexts, read the validated file as a Buffer after size checking. For SSH contexts, call the selected transport's file read contract with `maxBytes: CONTEXT_DOWNLOAD_MAX_BYTES` and send the returned Buffer unchanged. Add a `sendBytes(res, status, buffer, headers)` helper instead of `sendJson`.

  Route exactly:

  ```js
  if (method === 'GET' && sub === '/files/download') {
    const out = await _browseContextDownload(existing.id || existing.name, url.searchParams.get('path') || '');
    return out.ok ? sendBytes(res, 200, out.content, out.headers) : sendJson(res, out.httpStatus, out.body);
  }
  ```

- [ ] **Step 4: Re-run the Hub test and regression suite.**

  Run: `cd apps/cam-desktop && node test/hub.test.cjs && npm run lint:electron`

  Expected: all Hub tests pass; syntax check is silent.

### Task 2: Electron Save As bridge and binary API client

**Files:**
- Modify: `apps/cam-desktop/electron/main.cjs:616-622`
- Modify: `apps/cam-desktop/electron/preload.cjs:90-106`
- Modify: `web/js/api.js:160-170,667-698`
- Test: `apps/cam-desktop/test/hub.test.cjs` (HTTP bytes); add focused Electron IPC unit coverage if the existing harness can load `main.cjs` without opening a window.

**Interfaces:**
- `window.cam.files.saveDownload({ suggestedName, bytes }) -> { ok: true, path } | { ok: false, cancelled?: true, error }`.
- `api.downloadContextFile(contextId, path) -> { bytes: Uint8Array, filename: string }`.

- [ ] **Step 1: Add a failing client-level test or isolated helper test.**

  Cover a binary response with `Content-Disposition` filename parsing and make the test fail before adding binary-response handling. The test must prove `downloadContextFile` does not parse the response as JSON.

- [ ] **Step 2: Implement `CamApi.requestBytes` and `downloadContextFile`.**

  Add a private byte request path that uses the same direct/relay HTTP authentication headers as `request`, rejects non-2xx responses by parsing the JSON error body when available, and returns `Uint8Array` plus response headers. Do not route binary download through legacy REST-over-WebSocket; return a clear `binary_download_requires_http` error when the configured relay lacks HTTP forwarding.

- [ ] **Step 3: Implement the narrow Save As IPC handler.**

  In main, validate a non-empty basename with `path.basename`, present `dialog.showSaveDialog({ defaultPath: basename })`, then write the received bytes with `fs.promises.writeFile`. Expose only `saveDownload` from preload; the renderer must not receive filesystem paths or unrestricted write APIs.

- [ ] **Step 4: Verify client and Electron syntax.**

  Run: `cd apps/cam-desktop && npm run lint:electron && node --check ../../web/js/api.js`

  Expected: pass with no syntax errors.

### Task 3: Shared context browser and Desktop Node Browse action

**Files:**
- Create: `web/js/shared/context-file-browser.js`
- Modify: `web/js/views/file-browser.js`
- Modify: `web/js/desktop/app.js:18-25,311-345,481-486`
- Modify: `web/desktop.html:727-881`
- Modify: `web/css/desktop.css`
- Modify: `web/js/shared/nodes-mode.js:501-513,601-607` only if needed for accessible button copy/order.
- Test: add `apps/cam-desktop/test/context-browser.test.cjs` using a minimal DOM-like fixture, or add focused assertions to the existing DOM-capable Desktop test harness.

**Interfaces:**
- `mountContextFileBrowser({ container, api, state, onBack, onDownload }) -> cleanup`.
- `openContext(context)` selects a context and starts from its root path.

- [ ] **Step 1: Write failing UI tests.**

  Assert that Desktop passes `onBrowseContext`, context action order is browse/duplicate/edit/delete, clicking browse selects the context root and switches to the transient Desktop browser panel, and the back control restores Nodes. Assert image extensions are recognized case-insensitively and that SVG preview uses an `img.src = URL.createObjectURL(blob)` path rather than `innerHTML` or `srcdoc`.

- [ ] **Step 2: Extract the Mobile browser into `context-file-browser.js`.**

  Move listing, breadcrumbs, text/Markdown preview, and file selection from `web/js/views/file-browser.js` into the shared mount function. Keep the Mobile wrapper as the router adapter: it supplies its context id, hash-based `onBack`, and calls the shared mount function. Do not duplicate its directory listing implementation.

- [ ] **Step 3: Add the Desktop transient panel and callback.**

  Add `context-browse` to Desktop's internal `MODES`, but not to `PERSISTENT_MODES` or the left navigation. Add a `#mode-context-browse` panel. Pass:

  ```js
  onBrowseContext: (context) => contextBrowser.openContext(context)
  ```

  to `mountNodesMode`. `openContext` must call `setMode('context-browse')`; its Back control calls `setMode('nodes')`.

- [ ] **Step 4: Add preview and download UI.**

  In the preview header place `[Preview] [Raw] [↓ Download]` at the right. Show Preview/Raw for SVG (Preview is the safe image element; Raw is escaped source text). Show the Download button for every selected regular file, including binary files. For PNG/JPEG/WebP/GIF/SVG, construct a Blob from downloaded/read bytes, render it in an `<img>` with `object-fit: contain`, and revoke the old Blob URL when selection changes or unmounts. GIF must not be converted to a static frame.

- [ ] **Step 5: Run focused UI checks.**

  Run: `cd apps/cam-desktop && node test/context-browser.test.cjs && npm run test:hub && npm run test:term && npm run lint:electron`

  Expected: all pass.

### Task 4: End-to-end verification and review evidence

**Files:**
- Modify only if necessary: `apps/cam-desktop/FIXES-NODES.md`

- [ ] **Step 1: Verify direct Desktop behavior manually.**

  Start the Electron app with a registered SSH context that has no agents. In Nodes, confirm the context action row reads `browse / duplicate / edit / delete`. Browse root and a subdirectory; return to Nodes. Preview local fixtures for PNG, JPG, WebP, GIF, and SVG; confirm GIF animates and SVG Raw does not execute markup.

- [ ] **Step 2: Verify Download.**

  For a text file and a binary image, click Download, select a temporary local destination, compare SHA-256 of remote fixture and saved local file. Confirm a directory click does not offer Download, traversal is rejected, and a cancelled Save As reports no error toast.

- [ ] **Step 3: Run full required checks.**

  Run: `cd apps/cam-desktop && npm run test:transport && npm run test:hub && npm run test:term && npm run lint:electron && git diff --check`

  Expected: every test suite reports zero failures and `git diff --check` is silent.
