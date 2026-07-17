# FIXES-UNFINISHED-UI.md — hide unfinished desktop surfaces before DMG/MSI builds

Branch: `camui-desktop-v2`. Date: 2026-07-17.

Three desktop surfaces are unfinished. They are **hidden, not removed** —
all code, panels, and modules stay in place; only visibility and activation
guards changed. Everything below is reversible by deleting the `hidden`
attributes / whitelist guards listed here.

Marker convention: every hidden element carries an HTML comment
`UNFINISHED-HIDDEN(<name>)` right above it, so `grep -rn "UNFINISHED-HIDDEN" web/`
lists all touchpoints.

## 1. Workflow tab of Agent Settings — hidden

The Workflow tab (agent-settings tab list) is not finished.

- `web/desktop.html` — tab button
  `<button class="settings-tab" data-agent-settings-tab="workflow">` now has
  the `hidden` attribute. The tab panel
  (`#agent-settings-tab-workflow`, already `hidden` by default) and all
  workflow code in `shell.js` are unchanged.
- `web/js/desktop/shell.js` — in `setAgentSettingsTab()`, `'workflow'`
  removed from the tab whitelist
  (`['attributes', 'system-prompt', 'automation']`), so nothing can
  activate the panel programmatically; unknown values still fall back to
  `attributes`.

Re-enable: remove `hidden` from the tab button and restore `'workflow'` in
the whitelist.

## 2. Bots and Todos workspace modes — hidden

The Bots and Todos left-nav modes are not finished.

- `web/desktop.html` — nav buttons `<button class="mode-nav-btn"
  data-mode="bots">` and `data-mode="todos"` now have the `hidden`
  attribute. Their mode panels (`#mode-bots`, `#mode-todos`) and the
  `bots-mode.js` / `todos-mode.js` mounts in `app.js` are unchanged.
- `web/js/desktop/app.js` — new `HIDDEN_MODES = new Set(['bots', 'todos'])`;
  `MODES` is now filtered through it, and `PERSISTENT_MODES` is derived
  from `MODES`. Consequences:
  - `setMode('bots'|'todos')` coerces to `DEFAULT_MODE` (`agents`).
  - A stale `cam_desktop_mode=bots|todos` in localStorage from an earlier
    build no longer restores into a hidden mode.

Re-enable: remove the two entries from `HIDDEN_MODES` and remove `hidden`
from the nav buttons.

## 3. Sidebar brand header (logo placeholder) — RESOLVED (logo shipped)

~~The real logo asset does not exist yet; the text placeholder ("CAM" mark +
"Desktop" sub) is hidden until it ships.~~

**Resolved 2026-07-17**: a real logo was designed — final pick **"F3 relay
hub"** (thin-line blue ring = manager/control plane, green ring = running
agent, white hub node in the overlap linking both ring centers — a literal
picture of the CAM architecture: hub/relay connecting control plane and
agent; OpenAI-style thin strokes on the `#0d1117` terminal-dark tile).
Generator scripts `.tools/logo/gen_*.py` (gitignored); earlier drafts
A/B/C/D/E kept in `.tools/logo/` for reference. Shipped assets:

- `web/assets/icon-512.png` / `web/assets/icon-192.png` — regenerated
  (replace the old blue-rectangle placeholder; also used as favicon and
  PWA icon).
- `apps/cam-desktop/src-tauri/icons/icon.ico` — multi-size
  (16/24/32/48/64/128/256) Windows icon used by electron-builder
  (`build.win.icon`).
- `web/desktop.html` — `.sidebar-header` un-hidden and now renders
  `<img class="sidebar-logo-img" src="assets/icon-192.png">` beside the
  "CAM / Desktop" wordmark; `.sidebar-logo-img` rule added to
  `web/css/desktop.css` (20px, rounded).

Note (updated): the icon has since been relocated out of the legacy
`src-tauri/` tree to `apps/cam-desktop/build/` (`icon.ico` for Windows,
`icon.png` for macOS conversion); `build.win.icon` and `build.mac.icon` in
`package.json` both point there.

## Cache-busting versions

`web/desktop.html` import versions bumped for the changed modules:
`js/desktop/app.js` `?v=0.66.0` → `?v=0.66.1` (modulepreload + script tag),
`js/desktop/shell.js` `?v=0.65.0` → `?v=0.65.1` (modulepreload + the import
inside `app.js`), `css/desktop.css` `?v=0.64.2-scroll-owner` →
`?v=0.64.3-hidden-guard`.

## Follow-up fix (2026-07-17, same day): `hidden` attribute was not working

After installing the first rebuilt MSI the three surfaces were **still
visible**. Root cause: `.mode-nav-btn`, `.settings-tab`, and
`.sidebar-header` all set `display` explicitly in `web/css/desktop.css`,
and any author `display` rule silently defeats the UA stylesheet's
`[hidden] { display: none }` — the attribute was present in the markup but
had no effect. (The mode panels were unaffected because `.mode-panel` sets
no display.)

Fix: added a global guard at the top of `web/css/desktop.css`:

```css
[hidden] {
  display: none !important;
}
```

Checked for conflicts first: no desktop JS sets `style.display` (JS only
toggles the `hidden` attribute), so the `!important` guard cannot break
show/hide behavior elsewhere.

## Scope note

Desktop renderer only (`web/desktop.html` + `web/js/desktop/`). The mobile
PWA (`web/index.html`, `web/js/mobile/`) still exposes Todos/Bots/Workflow;
hide those separately if a mobile release needs the same treatment.

## Verification

- `node --check` passes on `web/js/desktop/app.js` and
  `web/js/desktop/shell.js` (as ESM).
- `npm run lint:electron`, `npm run test:hub`, `npm run test:term`,
  `npm run test:start` all pass (unchanged — no Electron/main-process code
  was touched).
- Packaged MSI rebuilt after this change; `resources/web/desktop.html`
  carries the markers.
