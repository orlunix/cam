# Mobile ↔ Desktop Parity Review (Sync + Terminal)

**Date:** 2026-06-20
**Scope:** After Sync Host / Terminal follow-ups — what Desktop Direct has vs Mobile Direct vs Mobile Relay.

Hub capabilities are advertised in `GET /api/system/health` → `capabilities` and consumed by `web/js/shared/hub-capabilities.js`.

---

## Connection modes (Mobile)

| Mode | Hub location | Use when |
|------|--------------|----------|
| **Direct** | Phone loopback (`MobileEmbeddedHub.java`) | Save SSH nodes locally on phone |
| **Relay** | Workstation (`camui start` embedded Hub) | Full agents, sync, terminal, skills |

**Rule (CAM-DESK-DIRECT-019):** Once connected, UI should not branch on transport — except where Hub explicitly lacks a capability.

---

## Feature matrix

| Feature | Desktop Direct | Mobile Direct | Mobile Relay |
|---------|----------------|---------------|--------------|
| **Nodes: Save/Edit/Delete host** | ✅ Hub CRUD | ✅ Hub CRUD | ✅ (read-only legacy UI) or workstation Hub |
| **Sync Host** (`POST …/sync`) | ✅ check camc + list | ✅ same (JSch SSH, key auth) | ✅ workstation Hub |
| **Agent list** | ✅ sync + poll | ⚠️ local store only (empty until Relay) | ✅ |
| **Agent output / input / keys** | ✅ | ❌ `agent_ops: false` | ✅ |
| **Terminal mode** | ✅ Electron SSH PTY (`CamBridge.term`) | ❌ hidden (`agent_terminal: false`) | ✅ capture poll (`term-bridge.js`) |
| **Start agent** | ✅ | ❌ | ✅ |
| **Skills (skillm)** | ✅ | ❌ | ✅ (if workstation Hub has skillm) |
| **SSH config import** | ✅ | ❌ | ✅ |
| **Private key picker** | ✅ Electron | ⚠️ Android SAF picker (Browse) | N/A |

---

## Sync Host flow

### Desktop
1. Nodes → expand host → **Sync Host**
2. Hub `_syncContextAgents()`: SSH → **check** `~/.cam/camc` exists (no deploy) → `camc --json list` → import agents
3. Missing camc → `camc_missing` error (run `cam sync` once on a workstation first)
4. Agent list refreshes; terminal attach uses pooled ssh2

### Mobile Direct (phone Hub)
1. **Sync Host button hidden** when `capabilities.context_sync === false`
2. If called anyway → HTTP 501 with message: use Desktop or `cam sync <context>` on PC
3. **Recommended workflow:** save node on phone → on PC run `cam sync <name>` or Desktop Sync Host → use **Relay** on phone to see agents

### Mobile Relay
- Legacy `machines.js` **Sync** button still works (calls workstation Hub)
- Direct Nodes UI (`shared/nodes-mode.js`) not used in Relay path today

---

## Terminal flow

### Desktop Direct
- Real PTY via `CamBridge.term` → `embedded-hub.cjs` `getAttachConnectOpts()` → ssh2 stream
- Default output mode: **terminal**

### Mobile Direct
- Phone Hub has no `/api/agents/{id}/output` or `/input`
- `canUseTerminalMode()` returns **false** when `agent_terminal` / `agent_ops` false
- Agent detail defaults to **live** capture mode; Terminal toggle hidden
- No false "Terminal attach failed" from empty bridge

### Mobile Relay
- `createRelayTermBridge()` polls `agentOutput`, sends via `sendInput`
- Terminal toggle visible; same xterm UI as Desktop (capture-based, not PTY)

---

## Implementation files

| Area | Desktop | Mobile |
|------|---------|--------|
| Hub | `apps/cam-desktop/electron/embedded-hub.cjs` | `android/.../MobileEmbeddedHub.java` |
| Capabilities | `healthBody().capabilities` full | `healthBody().capabilities` mobile-embedded |
| Capability client | `web/js/shared/hub-capabilities.js` | same |
| Terminal gate | `agent-console.js` + Electron bridge | `term-bridge.js` + `agent-detail.js` |
| Nodes UI | `shared/nodes-mode.js` | same (Direct) / `views/machines.js` (Relay) |
| Sync gate | default `syncHostSupported: true` | `() => syncHostSupported(state.hubCapabilities)` |

---

## Gaps to close (future work)

Priority order from `docs/mobile/android-direct-hub-port-plan.md`:

1. **Phase 2 — SSH + Sync on Android Hub** (port `_syncContextAgents`: SSH check camc exists + `camc --json list`; **no** bundled camc deploy — same as Desktop Sync Host)
2. **Agent ops on phone Hub** (output/input/key/stop — required before terminal or live view on Direct)
3. **Terminal on Direct mobile** — either capture-based agent ops or native SSH PTY (large)
4. **Relay Nodes UI** — migrate Relay path from legacy `machines.js` to `shared/nodes-mode.js` for UI parity
5. **Skills on Direct mobile** — skillm routes + SSH pool

Until Phase 2 ships, **Mobile Direct = node registry only**. Full parity with Desktop Direct requires **Relay to workstation Hub** or **Desktop**.

---

## Verification checklist (2.3.24+)

- [ ] Direct → Nodes: no Sync Host button; status mentions PC/Desktop sync
- [ ] Direct → Connect → Agents empty (expected without Relay)
- [ ] Direct → Agent detail: no Terminal toggle; live/full output only if agents existed
- [ ] Relay → Sync works; Terminal toggle works on running SSH agent
- [ ] `GET /api/system/health` on phone returns `capabilities.runtime: mobile-embedded`
