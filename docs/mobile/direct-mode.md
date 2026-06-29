# Mobile Direct vs Relay (aligned with Desktop)

**Product rule:** Direct mode is a **superset** of Relay. It runs a **local CAM Hub** on the device (same embedded Hub as Desktop). Relay mode remains fully supported as an alternate transport to a **remote** Hub. **Do not delete or replace Relay code paths when adding Direct.**

Canonical Desktop reference: `docs/desktop/requirements.md` (CAM-DESK-DIRECT-010..019, CAM-DESK-REMOTE-012..014).

---

## One Hub API, two transports

Both modes talk to the **same** `/api/...` surface via `web/js/api.js` (`CamApi`). Once connected, Agents / Start / Nodes / Skills must **not** branch on mode (CAM-DESK-DIRECT-019, CAM-DESK-REMOTE-013).

| Layer | Role |
|-------|------|
| **Hub** | Control plane: contexts, agents, SSH pool, camc delegation |
| **Renderer** | Thin client (`CamApi` + `state`) |
| **Direct transport** | HTTP + Bearer token → loopback Hub URL |
| **Relay transport** | Relay URL + relay token → relay forwards `/api/*` to a Hub |

---

## Desktop Direct (reference implementation)

```text
Electron renderer
  ──HTTP Bearer──▶ embedded-hub.cjs (127.0.0.1:8420+)
                      ──ssh2 pool──▶ remote nodes → camc/tmux
```

**How it works:**

1. **Electron main** owns Hub lifecycle via `CamBridge.directHub.{check,start,stop,restart}` (`apps/cam-desktop/electron/main.cjs` → `embedded-hub.cjs`).
2. **Start** generates `apiUrl` + `apiToken`, persisted as `cam_server_url` / `cam_token` (user never types these in the normal UI).
3. **Renderer** calls `api.configure({ serverUrl, token })` then `api.connect()` → `api.mode === 'direct'`.
4. **Settings → Direct**: Enable / Start / Stop embedded Hub (`web/js/shared/direct-settings.js`, `web/js/desktop/settings-mode.js`).
5. **Nodes**: full CRUD via Hub APIs; SSH keys via `CamBridge.files.pickPrivateKey()`; Hub stores path refs + encrypted secrets in main process.
6. **Default startup** (`web/js/desktop/app.js` `autoStartConnection`): if Relay profile saved → use Relay; else auto-start embedded Hub on loopback.

**Key files:**

| File | Purpose |
|------|---------|
| `apps/cam-desktop/electron/embedded-hub.cjs` | Embedded CAM Hub (HTTP server, contexts, agents, ssh2) |
| `apps/cam-desktop/electron/preload.cjs` | `CamBridge.directHub`, `CamBridge.files` |
| `web/js/shared/direct-settings.js` | Direct settings panel (shared shape) |
| `web/js/desktop/app.js` | `autoStartConnection`, `startEmbeddedHubAndPersist` |

---

## Desktop Relay (still first-class)

```text
Electron renderer
  ──relay WS/HTTP──▶ relay server
                       ──▶ Hub (often same embedded-hub on another machine)
```

- User enters **Relay URL + Relay token** only (CAM API token is source-managed; relay injects it).
- Settings tab switches between Direct and Relay; **both tabs stay**.
- `autoStartConnection` **honors saved Relay profile** and does not auto-start Direct on top (see `web/js/desktop/app.js`).

---

## Workstation `camui start` (Relay **source**)

```text
camui start --profile NAME --relay-url ws://… --relay-token …
  ├─ embedded-hub.cjs     (local Hub — same as Desktop Direct)
  └─ relay connector      (registers with relay; forwards client REST to local Hub)
```

Phone **Relay** mode:

```text
Phone renderer ──relay──▶ relay ──▶ workstation embedded Hub ──ssh2──▶ nodes
```

The workstation Hub is the same binary/shape as Desktop Direct. The phone is UI + relay credentials only.

---

## Mobile Direct (target — mirror Desktop, not phone-as-SSH)

**Correct model (user intent, matches Desktop):**

```text
Phone renderer
  ──HTTP Bearer──▶ local embedded Hub on phone (TODO: bundle like Electron)
                      ──ssh2──▶ remote nodes → camc/tmux
```

- Direct = **local Hub on the phone** (superset: full control plane locally).
- Relay tab = **unchanged**; connect to remote workstation Hub when desired.
- Same Nodes UI as Desktop (`shared/nodes-mode.js`), same Hub APIs — **not** a parallel localStorage node registry.
- Settings Direct tab = Enable/Start/Stop local Hub (like Desktop), **not** "import phone SSH key only".

**Wrong path (deprecated):** treating Mobile Direct as "phone is the SSH client" with `authorized_keys` on nodes and no Hub. That contradicts Desktop and CAM-DESK-DIRECT-*.

---

## Mobile implementation rules (locked)

| Rule | Detail |
|------|--------|
| Relay frozen | `settings-relay.js`, `nodes.js`, Relay branch in `app.js` — additive only |
| Direct additive | New files: `settings-direct.js`, `nodes-direct.js`, `direct-init.js`, future `embedded-hub` on Android |
| UI parity | Connected UI uses same `CamApi`; mode affects connection + diagnostics labels only |
| No replacement | Direct must not remove Relay tab, Relay auto-connect, or Relay Nodes UI |

---

## Current gap (Mobile)

See **`docs/mobile/desktop-parity-review.md`** for the full matrix (Sync, Terminal, Agents, Skills).

| Piece | Status |
|-------|--------|
| Relay client (v2.2.0+) | ✅ Full Hub via workstation — Sync + Terminal work |
| Embedded Hub on Android | ⚠️ **Minimal** — context CRUD only (`MobileEmbeddedHub.java`) |
| Hub capabilities | ✅ `GET /api/system/health` → `capabilities`; UI gates Sync/Terminal |
| Sync Host on phone | ✅ SSH + `camc --json list` (key auth; remote camc must exist) |
| Terminal on phone Direct | ❌ `agent_terminal: false` — use Relay or Desktop |
| `CamBridge.directHub` on Android | ✅ native bridge + in-process `directHub_request` |

---

## Requirement cross-reference

| ID | Meaning for Mobile |
|----|-------------------|
| CAM-DESK-DIRECT-010 | Direct = app-managed embedded Hub, not user-typed external URL |
| CAM-DESK-DIRECT-012 | Renderer uses same CamApi REST/WS as Relay |
| CAM-DESK-DIRECT-013 | Default Direct auto-starts Hub when no Relay profile (Desktop); Mobile TBD once Hub ships on device |
| CAM-DESK-DIRECT-019 | Agent/Nodes/Start UI identical after connect |
| CAM-DESK-REMOTE-012 | Relay source = `camui start` + embedded Hub + optional relay connector |
| CAM-DESK-REMOTE-013 | Downstream UI must not branch on Direct vs Relay |
| CAM-DESK-REMOTE-014 | Settings exposes **both** Direct and Relay tabs |

---

## Related

- [`android-direct-hub-port-plan.md`](./android-direct-hub-port-plan.md) — Android port checklist (embedded Hub + `CamBridge.directHub`)
- [`relay-first-plan.md`](./relay-first-plan.md) — Relay client (unchanged)
