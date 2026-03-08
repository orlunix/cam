# Plan: Move bottom nav to header dropdown menu

## Goal
Remove the bottom navigation bar (Agents/New/Contexts/Settings), move navigation into a ⋮ menu on the top header bar. This frees up ~56px of bottom space for content.

## Changes

### 1. index.html
- Remove `<nav id="bottom-nav">...</nav>` entirely
- Add a ⋮ menu button + dropdown to `<header id="header">` right side (before conn-dot)

New header structure:
```
CAM  connected          ⋮ ●
                    ┌──────────┐
                    │ Agents   │
                    │ New      │
                    │ Contexts │
                    │ Settings │
                    └──────────┘
```

### 2. style.css
- Remove/repurpose `#bottom-nav` and `.nav-item` styles
- Add `.header-menu-btn` and `.header-menu` dropdown styles (reuse overflow-menu pattern)
- Remove all `var(--nav-h)` padding-bottom from `#content` and `#content.agent-detail-active`
- Remove `--nav-h` variable (or set to 0)
- Toast position: change from `bottom: calc(nav-h + ...)` to just `bottom: 12px`

### 3. app.js
- Update `updateNav()` to highlight active item in the header dropdown instead of bottom nav
- Update selector from `#bottom-nav .nav-item` to `#header-menu .nav-item`

### 4. No changes to agent-detail.js (it doesn't reference bottom-nav directly, only uses CSS classes)
