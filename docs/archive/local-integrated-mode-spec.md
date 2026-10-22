# CAM Desktop — Local Tab Mode Spec (SUPERSEDED)

Status: **SUPERSEDED — DO NOT IMPLEMENT AS A SEPARATE SETTINGS TAB.**
Preserved for historical record and requirement-ID stability only.
Owner split: `camui-rev` writes and reviews this spec; `camui-dev` will not implement against it.
Primary requirements: `CAM-DESK-LOC-010` through `CAM-DESK-LOC-024` in `docs/desktop/requirements.md` (all marked `deferred` or `superseded`).

> # ⚠ SUPERSEDED — DO NOT IMPLEMENT FROM THIS DOC
>
> The active CAM Desktop product exposes **Direct / Relay** in Settings.
> Direct is the app-managed local CAM Hub path: Electron main
> starts/owns `cam serve`, generates the API token, connects over
> loopback, and lets that Hub manage Nodes/Remotes. Relay is for an
> existing CAM Hub behind a relay endpoint.
>
> This document describes an earlier experiment: exposing that local
> lifecycle as a separate **Local** Settings tab. The separate tab has
> been dropped. The reusable lifecycle idea has moved into active Direct
> requirements (CAM-DESK-DIRECT-010..019).
>
> Why this file is kept:
> 1. Requirement IDs LOC-010..024 are referenced from prior reviews,
>    commits, and `camc msg` history; deleting the spec would break
>    those cross-references.
> 2. The text records a non-trivial design exercise (port/token
>    policy, ownership semantics, foreign-cam-api handling) that
>    may inform a future, differently-scoped experiment.
>
> Do not act on this document in active Desktop work. Use
> `docs/desktop/requirements.md` DIRECT-010..019 for the Direct local
> Hub implementation. Use this file only to understand the old Local tab
> discussion and why those LOC IDs remain stable.

## Problem

The default architecture has three visible pieces:

```text
Desktop UI -> CAM Hub/API -> Remote Controller/Node -> tmux/agent runtime
```

For the single-machine fallback case — Desktop, hub, and a node all on
the same box — the relay hop and the remote-controller-provisioning
machinery add setup friction without buying anything.

User-facing **Local** mode keeps the same hub/controller model but
colocates the hub and a single node on the user's machine. The Desktop
renderer connects to the colocated `cam serve` directly over loopback:

```text
CAM Desktop
  Electron main
    starts/reuses one local `cam serve` bound to 127.0.0.1:8420
    (acts as both the hub and a single node for this user)
  Electron renderer
    connects via the existing Direct CamApi path
```

Additional remote machines, if any, are still handled by CAM contexts /
nodes and the existing `CamcPoller`. Desktop does not become a second
machine registry.

> Note: An earlier draft of this spec required Local to spawn a loopback
> `relay.py` and route the renderer through it. That direction was
> superseded after review — for a same-machine layout the relay hop has
> no value. Local is now a managed Direct profile; the relay is reserved
> for the user-typed Relay profile (unreachable hubs).

## Goals

- Give users a one-click local mode: open Desktop, verify local CAM, start
  the local `cam serve`, and show agents.
- Keep the renderer privileges narrow. The renderer is still a UI client
  and does not execute shell commands.
- Work first on Windows with WSL, then macOS/Linux with local CAM.
- Keep external Direct and Relay profiles available and unchanged.

## Non-Goals

- Do not install Python, WSL, Node, CAM, or relay dependencies in the
  first implementation.
- Do not manage public relay infrastructure.
- Do not add SSH key management or remote bootstrap UI in this pass.
- Do not replace existing CAM contexts/nodes with a Desktop-only inventory.
- Do not require mobile/PWA changes.

## Connection Modes (historical)

> **Historical.** The active product exposes only **Direct** and
> **Relay** in Settings. Direct is now the app-managed local CAM Hub
> path; Local is not a separate active tab. The mode table below is
> preserved as written at the time of the Local tab experiment.

At the time of this spec, Desktop was planned to expose four
connection modes (per `requirements.md` REMOTE-010..014 and
LOC-010..024). This spec was concerned only with the Local mode; the
others are listed here for context:

| Mode | User intent | Where it's covered |
| --- | --- | --- |
| Managed / Remote-first | "Use my org's hub." | Now: preconfigured Direct, no separate tab. REMOTE-010 / REMOTE-014. Not specified here. |
| Direct | "I have a reachable hub or controller-with-API." | REMOTE-011. Not specified here. |
| Relay | "Hub is unreachable; I have a relay." | REMOTE-012. Not specified here. |
| **Local (this spec — DEFERRED)** | "Run a tiny hub + node on this machine." | LOC-010..024 (all deferred). Originally specified Desktop to start/stop one owned `cam serve` on `127.0.0.1:8420` and connect through the Direct CamApi path. |

Default-tab behavior (historical): the experiment intended enterprise
builds to default Settings to the Managed tab. In the active product
there is no Managed tab and no Local tab; Settings defaults to Direct.

## Target Topology

First implementation:

```text
┌────────────────────────────────────────────────────────────────┐
│ CAM Desktop                                                    │
│                                                                │
│  Electron main                                                 │
│    └─ local CAM server                                         │
│         cam serve --host 127.0.0.1 --port 8420                 │
│                   --token <apiToken>                           │
│         CamcPoller scans configured CAM contexts/nodes         │
│                                                                │
│  Electron renderer                                             │
│    CamApi connects directly to http://127.0.0.1:8420           │
│    using <apiToken> from localStorage (Direct profile)         │
└────────────────────────────────────────────────────────────────┘
```

The Relay user-mode is reserved for the case where the renderer cannot
reach a remote CAM server directly. It is not part of the Local flow.

## Backend Supervisor

Electron main owns a small supervisor. The bridge surface is narrow:

```text
CamBridge.localBackend.check()
CamBridge.localBackend.start()
CamBridge.localBackend.stop()
CamBridge.localBackend.restart()
CamBridge.localBackend.logs()
CamBridge.localBackend.getProfile()
```

Rules:

- no renderer-provided command strings
- no renderer-provided executable path
- no renderer-provided arbitrary environment
- no shell execution except fixed, reviewed launch wrappers where unavoidable
  for WSL/login PATH discovery

The supervisor tracks only the `cam serve` process it started. It must
not kill a user-owned `cam serve` discovered on the same port.

## Startup Flow

This describes the path **after** the user has chosen Local. Enterprise
builds default Settings to the Managed tab (REMOTE-014); a single-user
or dev build may default Settings to Local when its build/profile
policy says so and no other profile exists (LOC-020). Local is never
the *de facto* default for managed deployments.

1. User opens Desktop and selects the Local tab.
2. If no Local profile exists yet, the Local panel renders with Check
   enabled and Start disabled until Check reports a usable runtime.
3. User clicks `Check`.
4. Electron main checks:
   - platform
   - WSL availability and selected distro on Windows
   - Python availability in the target runtime (Diagnostics row only)
   - `cam` CLI availability in the target runtime
   - CAM API port availability/status
5. If CAM is available and the port is free (or already owned by us),
   user clicks `Start`.
6. Electron main generates a fresh `apiToken` and spawns
   `cam serve --host 127.0.0.1 --port 8420 --token <apiToken>`.
7. Electron main waits for `GET /api/system/health` on the API port to
   return a valid CAM HealthResponse JSON shape (not just any 200).
8. Renderer stores `serverUrl=http://127.0.0.1:8420 + token=<apiToken>`
   in the existing Direct localStorage keys and calls `CamApi.connect()`.
9. Agents appear from the normal CAM server state/poller.

## Port And Token Policy

Defaults:

- CAM API: `127.0.0.1:8420`

If the default port is occupied:

- Healthy `cam serve` started by the **current** Desktop process —
  reuse it (Start is a no-op that returns the existing profile;
  LOC-018).
- Healthy `cam serve` started outside the current Desktop process —
  including one spawned by a previous Desktop session that has since
  quit or crashed — is treated as **foreign** (`foreign-cam-api`).
  Process ownership lives in Electron main memory only and is not
  recovered across app restarts in this phase; a persistent ownership
  marker is deferred to a later requirement. Local does not take
  ownership; the user can use Direct mode with the existing server's
  token instead.
- Non-CAM process — show a precise error and ask the user to free the
  port. The first implementation does not auto-pick alternates.

Tokens:

- Generate a fresh opaque `apiToken` (`crypto.randomBytes(24).toString('base64url')`).
- Store the token only in `localStorage.cam_token`, alongside
  `localStorage.cam_server_url = http://127.0.0.1:8420`. The shared
  `readConfig`/`saveConfig` API is unchanged.
- Do not log tokens in renderer console, UI text, or normal logs.
- Redact tokens in diagnostics — buffer-time filtering removes
  exact-match tokens and common token-emitting prefixes (`Auth token:`,
  `--token`, `token=`).

## Profile Marker

Settings may set a `cam_profile_kind` localStorage key to one of
`local | direct | relay` so Diagnostics can tell a Local-managed
Direct profile apart from a user-typed Direct profile. The marker is
settings-mode-only — `readConfig`/`saveConfig` and the rest of the
app do not depend on it. If the marker is absent, Settings infers
from the persisted credentials (relay creds → relay, server creds →
direct, neither → unset).

## Windows / WSL Behavior

First implementation targets the Windows-with-WSL model already used
in Phase 2A:

- `cam serve` runs inside the selected WSL distro via
  `wsl.exe -d <distro> -- bash -lc "cam serve --host 127.0.0.1 --port 8420 --token <apiToken>"`.
- Desktop renderer connects via Windows localhost. WSL2's localhost
  forwarding makes this work; WSL1 already validated in VDI runs.
- Use fixed argv forms from Electron main. The only string component
  is `serveArgs`, which contains the generated token and validated port
  number — never any renderer-supplied content.

Full WSL installation and Python/CAM bootstrap remain later phases.

## UI

The Settings tab layout (Managed/Direct/Relay/Local) and per-tab
default behavior are owned by the Connection Modes section above and
by REMOTE-010..014 + LOC-020 in `docs/desktop/requirements.md`. This
section describes only the **Local panel** that lives inside the Local
tab.

The Local panel exposes product concepts:

- current state: stopped, checking, starting, running, foreign-cam-api,
  cam-missing, port-conflict, degraded, error
- runtime target: local Linux/macOS or selected WSL distro
- Remotes count and summary (from existing CAM contexts/nodes)
- actions: Check, Start, Stop, Restart, Manage Remotes

Server/token/port details are hidden from the default path. They appear
only under an `Advanced / Diagnostics` disclosure:

- API URL
- API port + observed port state
- redacted token fingerprint (sha256 prefix only)
- process ID for the owned `cam serve`
- recent backend logs (redacted)

The user should be able to set up Local mode by managing Remotes only.
A typical first-run path:

```text
Settings -> Local -> Check -> Start
Agents -> Remotes (via CAM contexts/nodes) appear automatically
```

Remote management reuses existing CAM context/node concepts. The UI
label is `Remotes`; the backing store remains CAM contexts.

The rest of the app does not need to know whether agents came through
Local, Direct, or external Relay mode. Agent list, Start Agent, Edit
Agent, and output keep using `CamApi`.

## Process Lifecycle

Owned local backend:

- Desktop may stop it when the user clicks Stop.
- Desktop may restart it when health fails and the user confirms.
- On app exit, the first implementation kills the owned `cam serve`
  via an `app.on('before-quit')` hook. This is a defensible default —
  it prevents orphaned background processes — but is intentionally
  flippable in a later revision if users prefer "keep running until
  Stop" semantics.

User-owned backend:

- Desktop may connect to it (via Direct mode).
- Desktop must not stop or restart it.

## Failure Handling

Local mode distinguishes:

- CAM not installed (`cam-missing`)
- CAM API port occupied by a non-CAM process (`port-conflict`)
- CAM API port occupied by a user-owned CAM server (`foreign-cam-api`;
  Start refuses, Direct mode suggested)
- `cam serve` started but health failed (start returns ok:false with the
  underlying error excerpt)

Each error message names what failed and the next step.

## Security

- Bind the managed `cam serve` to loopback only (`--host 127.0.0.1`).
- Generate a fresh API token per Start.
- Keep renderer sandbox and context isolation.
- Do not grant the renderer arbitrary command execution.
- Do not expose SSH keys or remote credentials to renderer JavaScript.
- Do not log tokens; redact buffered child stdout/stderr.
- The Relay user-mode is private to the user's configured relay; Local
  itself does not stand up any relay.

## Verification

Minimum verification for implementation:

- Linux/macOS local: start Local from no profile, see agents.
- Windows VDI + WSL: start Local from no profile, see agents.
- Existing Direct profile still connects.
- Existing external Relay profile still connects.
- Mobile/PWA files remain unchanged.
- Port occupied by non-CAM process: UI does not misidentify it as healthy.
- Port occupied by user-owned CAM server: Local refuses to take it over.
- App relaunch after a Desktop quit/crash: an existing `cam serve` on
  the port is treated as foreign (`foreign-cam-api`) per LOC-017/018.
  Desktop reconnects via the saved Direct profile (URL + token) if the
  saved token still authenticates; it does not adopt the orphan process
  or kill it. A persistent ownership marker that would survive restarts
  is deferred.
- Diagnostics logs never contain raw tokens; `Auth token:` / `--token`
  / `token=` prefixes are redacted.

## Open Decisions

1. Should the owned `cam serve` survive Desktop exit? Current default is
   kill-on-exit; spec recommends revisiting if users prefer persistence.
2. Should Local auto-pick a free alternate port when 8420 is occupied,
   or always require the user to free it? Current implementation errors
   out; the safer of the two options.
