# CAM Desktop

CAM Desktop is a thin UI/client for CAM, the agent control plane.

## Where to read

Authoritative documentation lives under `docs/desktop/`. Start there:

- [`docs/desktop/README.md`](../../docs/desktop/README.md) — architecture
  in one screen plus a file index.
- [`docs/desktop/requirements.md`](../../docs/desktop/requirements.md) —
  canonical requirement registry with stable IDs (read first).
- [`docs/desktop-ui-spec.md`](../../docs/desktop-ui-spec.md) — current
  milestone / product spec.
- [`docs/archive/local-integrated-mode-spec.md`](../../docs/archive/local-integrated-mode-spec.md)
  and
  [`docs/archive/local-runtime-user-guide.md`](../../docs/archive/local-runtime-user-guide.md)
  — **SUPERSEDED / historical** notes for the old separate Local tab
  experiment. The app-managed Hub lifecycle now belongs to Direct;
  these files are preserved only for requirement-ID stability.

Change logs and fix records live in this directory (`FIXES-*.md`):

- `TERMINAL-ARCHITECTURE.md` — the terminal tab model: invariants,
  state ownership, data flow, and the reasoning (Tabby-reference
  redesign, 2026-07-17).
- `MAS-SPIKE.md` — Mac App Store sandbox smoke checklist.
- `MAS-REVIEW-RISKS.md` — pre-submission rejection risks + demo-mode
  plan (2026-07-18).
- `FIXES-2026-07-17-SESSION.md` — index of the 2026-07-17 session:
  MSI datapath prep, hidden unfinished UI, logo, local-node datapath
  (WSL2), attach latency + terminal tab semantics.
- `FIXES-UNFINISHED-UI.md` — hidden surfaces + `[hidden]` guard + logo.
- `LOCAL-NODE-DATAPATH.md` — local-node runtime design and error codes.
- `FIXES-ATTACH-LATENCY.md` — remote attach latency + tab semantics.
- `FIXES-NODES.md` — earlier Nodes/sync fixes.

## Architecture in one paragraph

```text
Desktop UI  ──HTTP/WS──▶  Desktop Client (embedded Hub/API)  ──poll/route──▶  Remote Node  ──▶  legacy camc + agent runtime
```

Coding agents run on the remote node, never inside Desktop. The embedded
Hub is Desktop's current client-side control-plane implementation, not a
third product role; the canonical target is CAM Client + CAM Server (see
`docs/cam-client-server-spec.md`). Desktop loads the WebUI-derived
renderer from `../../web/desktop.html` and reuses `web/js/api.js` as its
API client. In supported Direct mode, Electron main starts an embedded
Node/Electron Hub and the renderer connects through the same `CamApi`
path. The installed app must not require WSL, local Python, a host shell,
local `cam`, or legacy `cam serve`. tmux and agent CLI dependencies live
on the remote node.

## Settings tabs

Settings exposes the supported Direct mode and the legacy Relay entry
(canonical IDs in `docs/desktop/requirements.md`):

- **Direct** (`DIRECT-010..019`) — default app-managed embedded CAM Hub,
  currently supported. Electron main checks readiness, starts the
  Node/Electron Hub on loopback, generates the API token, and connects
  the renderer to that Hub. The normal UI does not ask the user to type a
  Hub URL, run terminal commands, install WSL/Python, or install `cam`.
- **Relay** (`REMOTE-012`, legacy / unsupported) — Desktop users entered
  Relay URL + Relay token. The source-side `camui start --profile ...`
  owned the CAM API token and the relay injected it for `/api/*`
  forwarding. Relay is not part of the current architecture.

Future and deferred modes (not active Settings tabs):

- **SSH attach / transport** (`SSH-010..013`) — future, for terminal
  attach to the selected agent's controller. Status `proposed`.
- **Local tab** (`LOC-010..024`) — superseded. Old separate Local
  experiment; its useful lifecycle pieces moved into Direct. See the
  historical docs linked above.

## Relay Source Quick Start (historical — unsupported)

> Relay is legacy / unsupported and is not part of the current CAM
> Client + CAM Server architecture. These commands are preserved only for
> old deployments.

Use this only when maintaining an old CAM Hub/source behind NAT or an SSH
tunnel. Start the relay server first, then connect the source with one
foreground command:

```bash
node apps/cam-desktop/cli/camui-cli.cjs start \
  --profile hren7001 \
  --relay-url ws://127.0.0.1:7001 \
  --relay-token <RELAY_TOKEN>
```

`--profile hren7001` creates or reuses
`~/.cam/camui/relay/hren7001/profile.json`. That file stores the stable
CAM API token with mode `0600`; Desktop/mobile clients do not type this
token. They enter only the Relay URL and Relay token.

If the relay is reachable only through SSH, create the tunnel first and
point `camui start` at the local tunnel:

```bash
ssh -fN -L 127.0.0.1:17001:127.0.0.1:7001 hren@relay.example.com
node apps/cam-desktop/cli/camui-cli.cjs start \
  --profile hren7001 \
  --relay-url ws://127.0.0.1:17001 \
  --relay-token <RELAY_TOKEN>
```

On a Windows Desktop that cannot see the WSL loopback tunnel, expose the
WSL tunnel on a routable local address and use that URL in Desktop, for
example `http://10.124.11.38:17002`.

## Development

Build / run / package commands live under this folder; see `package.json`.
The `electron/` subdirectory contains the main + preload entry points;
`scripts/` carries small helpers (`copy-vendor.cjs` etc.); the renderer
lives at `../../web/` and is bundled via electron-builder
`extraResources`.

## Non-Goals

- Running agents natively inside Desktop. Agents still run on
  controller/nodes; Direct only runs the embedded Hub/control-plane code.
- Owning SSH keys, vendor credentials, or agent CLI logins. The future
  SSH attach path (SSH-010..013) is server-mediated.
- Becoming a second machine registry. Remotes are existing CAM
  contexts/nodes from the hub.

## Legacy folders

Earlier iterations explored a Tauri scaffold (`src/`, `src-tauri/`) and
a ChatShell-style baseline (`baseline/chatshell-desktop/` at the repo
root). Those remain as reference but are no longer the product direction.
The active runtime is Electron under `apps/cam-desktop/electron/`.
