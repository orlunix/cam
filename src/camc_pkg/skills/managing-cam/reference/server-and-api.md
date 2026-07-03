# `cam serve` — API server, web, relay

`cam serve` runs the FastAPI + uvicorn HTTP/WebSocket server backing
the web UI, mobile app, and relay (NAT traversal for users behind
firewalls).

## Run

```bash
cam serve                                   # defaults: 127.0.0.1:8420, no auth
cam serve --host 0.0.0.0 --port 8420 \
  --token cam-web-token-2026                # token-protected
cam serve --relay ws://localhost:18001      # relay mode (NAT traversal)
```

Auto-restart daemon: `~/.local/bin/cam-serve-daemon.sh` (a `while true`
loop that restarts `cam serve` on exit). PID file at
`/tmp/cam-serve-daemon.pid`. Logs at `/tmp/cam-serve.log`.

## What it provides

- REST endpoints under `/api/v1/...` for agents/contexts/events
- WebSocket `/ws` for live event streaming
- Static web UI at `/` (vanilla JS PWA, served from `web/`)
- Relay client when `--relay` is set (registers with the relay server,
  serves REST-over-WS)

## Background tasks

When `cam serve` runs, it also spawns:

1. **CamcPoller** — polls every camc every 5s (see `poller-and-sync.md`)
2. **Heal cron** (server-side) — `cam heal` hourly (config in code)
3. **Event rotator** — daily, prunes events older than 30 days

Killing `cam serve` stops all three.

## Token auth

`--token <T>` requires `Authorization: Bearer <T>` on every API call.
Web UI prompts for the token in localStorage. WebSocket auth via the
same token in the connect URL.

## Relay

Mobile clients hit a public relay server (zero-dep WebSocket bridge,
RFC 6455 stdlib-only). cam serve registers with the relay over WS and
serves REST-over-WS so mobile users behind NAT can reach a corp
network's cam server.

```bash
# On the cam server:
cam serve --relay ws://relay-host:18001 --token <T>

# Mobile client connects to the relay, not to cam directly.
```

Relay code: `relay/relay.py` (standalone, runs anywhere with Python).

## API quick reference

```
GET  /api/v1/agents                     list agents
GET  /api/v1/agents/<id>                detail
POST /api/v1/agents                     run new (body = task spec)
POST /api/v1/agents/<id>/stop
POST /api/v1/agents/<id>/kill
GET  /api/v1/agents/<id>/capture
POST /api/v1/agents/<id>/send
GET  /api/v1/contexts                   list
GET  /api/v1/events?since=<iso>         events feed
WS   /ws                                live event stream
```

Hash-based conditional capture: client sends last seen hash, server
returns `{"unchanged": true}` (50 bytes) if no diff. TTL cache (2s)
on capture endpoint to absorb concurrent clients.

## Web UI

`web/` is a vanilla JS PWA with a service worker. Key files:
- `web/index.html` + `web/app.js`
- `web/js/api.js` — works for direct HTTP and relay (REST-over-WS)
- `web/js/views/` — agent list, detail, capture, attach
- `web/sw.js` — service worker (offline cache)

Cache busting: bump `?v=XX` query strings in index.html / app.js / sw.js
together when you change client code.

## When NOT to use cam serve

- Single-user CLI workflow → just use `cam` and `camc` directly, no
  server needed.
- Don't run `cam serve` on shared NFS hosts unless you understand the
  poller's effect on the cluster (it polls every machine in
  machines.json from wherever it runs).
