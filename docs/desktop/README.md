# CAM Desktop Docs

This directory holds the active Desktop planning and review documents.

## Architecture in one screen

```text
Desktop UI  ──HTTP/WS──▶  CAM Hub/API  ──poll/route──▶  Remote Controller/Node  ──spawns──▶  tmux + agent runtime
```

- **CAM is a hub** (control plane). It aggregates the node/agent/context
  tables and routes commands back to the right controller.
- **Coding agents run on a controller/node**, never inside Desktop. The
  controller hosts Python, tmux, `cam` / `camc`, and the agent CLIs
  (Claude, Codex, Cursor, …).
- **Desktop is a thin UI/client** over the hub. It renders the hub's tables,
  sends user actions through `CamApi`, and (later) opens interactive
  attach streams to the selected agent's controller.
- **The Desktop flow should not require the user to open a terminal.**
  Direct starts and connects to an embedded Electron/Node CAM Hub from the
  app. The installed app must not require WSL, local Python, a host shell,
  local `cam`, or local `cam serve`. Controller dependencies such as tmux
  and agent CLIs live on the remote nodes and are provisioned there.
- **Workspace modes** in the left nav: **Agents** (default — agent
  list, output, composer, quick keys), **Start** (start a new agent),
  **Settings** (connection profile), and **Nodes** (read-mostly view
  of hub-provided controllers/nodes, see NODEUI-010..017). Contexts is
  a future placeholder. Workspace modes are separate from connection
  modes — switching to Nodes does not change how Desktop reaches the
  hub.
- **Active Settings exposes two connection modes**: Direct and Relay.
  Direct is the default app-managed embedded CAM Hub path: Desktop starts
  the Node/Electron Hub, generates the token, connects to it over
  loopback, and uses Hub APIs to manage Nodes/Remotes. Relay is for an
  existing CAM Hub that must be reached through a relay endpoint.
- **Direct starts by default** (CAM-DESK-DIRECT-013). On a cold
  launch with no saved profile, Desktop auto-starts the embedded Hub
  (no host CAM CLI, Python, WSL, or shell required) and lands the
  user on Agents — not on Settings. An empty `contexts:[]` Hub is a
  valid running state.
- **Nodes page is read+write** for the embedded Hub. Top action bar:
  Add Host (form-driven `POST /api/contexts`), Import SSH Config
  (reads `~/.ssh/config` via Node `fs`, suggestions only — never
  reads key contents), Refresh. Filter Agents + Sync stay
  (CAM-DESK-DIRECT-016/017).
- **Direct SSH transport pools connections** (CAM-DESK-DIRECT-019). The
  embedded Hub keeps a long-lived in-process `ssh2.Client` pool per
  endpoint+auth-identity — the self-contained equivalent of CAM's
  OpenSSH `ControlMaster` / `ControlPersist=600`. Repeat
  syncs/captures/uploads reuse the same authenticated connection
  instead of paying a TCP+auth handshake every call.
- **Browse is a workspace browser** (CAM-DESK-FILE-010..017). Agent Browse
  and Context Browse are two entry points into the same workspace-root file
  contract. Relay and Direct must expose the same list/read API shape so
  Desktop and mobile can share behavior without transport-specific renderer
  branches.
- **SSH attach** to the selected agent's controller is a future path
  (see SSH-010..013, status `proposed`).
- **The separate Local tab is retired.** Its useful app-managed-Hub
  lifecycle moved into Direct (DIRECT-010..019). LOC-010..024 is
  preserved only for requirement-ID stability and historical record.
- **Stable Req IDs** (`docs/desktop/requirements.md`) anchor all of the
  above. Implementation tasks and review replies cite them.

## Files

- [`requirements.md`](./requirements.md) — canonical requirement registry
  with stable Req IDs. **Read this first.** Starts with the Architecture
  and Connection Model section that this README summarizes.
- [`../desktop-ui-spec.md`](../desktop-ui-spec.md) — current milestone /
  product spec. Explains design direction; cite `requirements.md` IDs in
  reviews.
- [`local-integrated-mode-spec.md`](./local-integrated-mode-spec.md) —
  **SUPERSEDED / historical.** Architecture and design notes for the old
  separate Local tab experiment (LOC-010..024). The app-managed Hub
  lifecycle has moved into Direct.
- [`local-runtime-user-guide.md`](./local-runtime-user-guide.md) —
  **SUPERSEDED / historical.** End-user setup notes for the old Local tab
  experiment. Not a guide for the active Direct flow.
- [`../windows-installer.md`](../windows-installer.md) — Windows MSI
  packaging notes.
- [`../archive/`](../archive/) — old specs and reference evaluations.
  Treat as historical context only.

## Workflow

1. Add or update requirements in `requirements.md`, citing the architecture
   model above and using the appropriate area prefix
   (`ARCH/HUB/NODE/DIRECT/REMOTE/SSH/RUN/EDIT/INP/OUT/SET/PKG/TERM/LOC/SEC/VFY`).
2. Assign implementation by Req ID. Active work targets the hub/controller
   path (ARCH/HUB/NODE/DIRECT/REMOTE). Do **not** assign new
   implementation work against LOC requirements — the separate Local tab
   is superseded. Do not assign new work against TERM-010..017 either —
   they are superseded.
3. Review implementation by Req ID.
4. Move outdated milestone specs to `docs/archive/` only when the active
   requirement registry no longer references them.
