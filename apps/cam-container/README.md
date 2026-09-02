# CAM WebUI (cam-container)

CAM Desktop's feature set as a containerized plain-Node web server — no
Electron. One public port serves the desktop web UI (`web/desktop.html`),
the embedded hub API (`/api/*`, `/ext/*`), the assistant channel
(`/api/assistant/*`), and a WebSocket rail (`/ws`) for terminal attach
and assistant push events.

## Run

```bash
docker build -f apps/cam-container/Dockerfile -t cam-webui .   # context = repo root
docker run -d --name cam-webui \
  -p 8420:8420 \
  -v cam-webui-data:/data \
  -e CAM_API_TOKEN='pick-a-long-random-token' \
  cam-webui
```

Open `http://<server>:8420/?token=<CAM_API_TOKEN>` once — the UI stores
the token and the parameter is removed from the address bar. Later visits
just need `http://<server>:8420/`.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `CAM_API_TOKEN` | — | Bearer token for the UI + API. If unset, one is generated and persisted to `/data/.api-token` (0600) and printed to the container log on first boot. |
| `CAM_PORT` | `8420` | Public listen port. |
| `CAM_BIND` | `0.0.0.0` | Public bind host. |
| `CAM_DATA_DIR` | `/data` | All state (hub store, credentials, extensions, assistant threads, logs). Back it up. |
| `CAM_SECRET_KEY` | — | 32-byte key (64-hex or base64) encrypting `embedded-hub-credentials.json`. If unset, generated to `/data/.secret-key` (0600) on first boot. |
| `HOME` | `/data/cam-home` | The assistant's pi child keeps its data under `$HOME/.cam/assistant/` (SPEC §6). |

## Security notes

- Single-user, single bearer token — same trust level as the desktop
  hub. Put a TLS-terminating reverse proxy in front for real exposure.
- SSH passwords/passphrases and the assistant LLM token live in
  `/data/embedded-hub-credentials.json`, AES-256-GCM encrypted with
  `CAM_SECRET_KEY` (or `/data/.secret-key`). Weaker than the desktop's
  OS-keychain binding: anyone with both files can decrypt. Treat the
  volume as sensitive.
- The assistant's `bash` tool runs commands **inside this container**
  (its "local machine" is the container). It can reach anything the
  container can.

## Local development

```bash
cd apps/cam-container && npm install
CAM_DATA_DIR=/tmp/cam-container-data CAM_PORT=18420 node server.mjs
```

ssh2 is resolved from `apps/cam-desktop/node_modules` (the reused
backend modules' own dependency), so `apps/cam-desktop` needs
`npm ci --omit=dev` (or a full install) once.

## Architecture

The server **reuses** `apps/cam-desktop/electron/*.cjs` (embedded-hub,
ssh-transport, tmux-controls, assistant-host, credential-store) as-is —
the desktop app and the WebUI share one backend codebase, and **desktop
files carry zero container-aware lines**. Every browser/container
adaptation lives in this directory: the browser shim, the WS term
channel, and the serve-time HTML transforms in `server.mjs` (shim
injection + `frame-src 'self'` for same-origin extension iframes).

- `lib/bootstrap.mjs` — assembly (mirrors main.cjs wiring)
- `lib/aes-safestorage.mjs` — safeStorage-compatible AES shim
- `lib/cam-web-shim.js` — browser `CamBridge` (preload replacement)
- `lib/term-ws.mjs` — the term:* stack over WS (ported from main.cjs;
  duplicated deliberately — keep in sync manually)
- `server.mjs` — HTTP routing (static / proxy / assistant / server ops)
  + the WS rail
