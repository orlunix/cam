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
- [`docs/desktop/local-integrated-mode-spec.md`](../../docs/desktop/local-integrated-mode-spec.md)
  and
  [`docs/desktop/local-runtime-user-guide.md`](../../docs/desktop/local-runtime-user-guide.md)
  — **SUPERSEDED / historical** notes for the old separate Local tab
  experiment. The app-managed Hub lifecycle now belongs to Direct;
  these files are preserved only for requirement-ID stability.

## Architecture in one paragraph

```text
Desktop UI  ──HTTP/WS──▶  CAM Hub/API  ──poll/route──▶  Remote Controller/Node  ──spawns──▶  tmux + agent runtime
```

Coding agents run on the controller/node, never inside Desktop. The hub
aggregates the node/agent/context tables and routes commands. Desktop is
the Electron app under this folder; it loads the WebUI-derived renderer
from `../../web/desktop.html` and reuses `web/js/api.js` as its API
client. In Direct mode, Electron main starts an embedded Node/Electron CAM
Hub and the renderer connects to it through the same `CamApi` path. The
installed app must not require WSL, local Python, a host shell, local
`cam`, or local `cam serve`. tmux and agent CLI dependencies live on the
remote controller/node.

## Settings tabs

Active Settings exposes two connection modes (canonical IDs in
`docs/desktop/requirements.md`):

- **Direct** (`DIRECT-010..019`) — default app-managed embedded CAM Hub.
  Electron main checks readiness, starts the Node/Electron Hub on
  loopback, generates the API token, and connects the renderer to that
  Hub. The normal UI does not ask the user to type a Hub URL, run
  terminal commands, install WSL/Python, or install `cam`.
- **Relay** (`REMOTE-012`) — Desktop users enter Relay URL + Relay
  token. The source-side `camui start --profile ...` owns the CAM API
  token and the relay injects it for `/api/*` forwarding.

Future and deferred modes (not active Settings tabs):

- **SSH attach / transport** (`SSH-010..013`) — future, for terminal
  attach to the selected agent's controller. Status `proposed`.
- **Local tab** (`LOC-010..024`) — superseded. Old separate Local
  experiment; its useful lifecycle pieces moved into Direct. See the
  historical docs linked above.

## Relay Source Quick Start

Use this when a CAM Hub/source is behind NAT or only reachable through an
SSH tunnel. Start the relay server first, then connect the source with one
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
ssh -fN -L 127.0.0.1:17001:127.0.0.1:7001 hren@hlren.duckdns.org
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
