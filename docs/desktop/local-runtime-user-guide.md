# CAM Desktop — Local Tab Setup Guide (SUPERSEDED)

Status: **SUPERSEDED — DO NOT USE FOR ACTIVE DIRECT WORK.** Preserved
for historical reference only.

> # ⚠ SUPERSEDED — DO NOT FOLLOW THIS GUIDE FOR ACTIVE BUILDS
>
> The active CAM Desktop product exposes two connection modes:
> **Direct / Relay**. Direct is the app-managed local CAM Hub path:
> Desktop starts/owns `cam serve`, generates the token, connects over
> loopback, and uses Hub APIs to manage Nodes/Remotes. Relay is for an
> existing CAM Hub behind a relay endpoint.
>
> What was dropped is the separate **Local** Settings tab. This guide
> describes that older tab experiment and its manual WSL/macOS
> prerequisite thinking. Active Direct work is tracked by
> CAM-DESK-DIRECT-010..019 in `requirements.md`.
>
> This guide is kept on disk only so old references resolve and the
> historical setup notes (WSL prerequisites, port collisions,
> troubleshooting) remain searchable. Do not follow the
> separate-Local-tab "Start backend" / "Check" steps below in active
> product work. The active Direct tab may have its own Check / Start /
> Stop / Restart UI, but it is specified by DIRECT-010..019.

Audience (historical): people who, in the deferred experiment,
would run a CAM hub **and** a node on the same machine, and the
IT/admin who would provision the WSL distro on Windows for that
case.

This guide covers the prerequisites the deferred experiment expected
to be in place **before** CAM Desktop ran its `Local` connection mode.
In that experiment Desktop managed the service lifecycle once those
prerequisites existed; it did not install WSL or OS packages.

Related references:

- `docs/desktop/README.md` — architecture and product overview (active
  Settings has Direct + Relay only; Direct owns the app-managed local
  CAM Hub lifecycle).
- `docs/desktop/requirements.md` — canonical requirement IDs. Active
  Direct is covered by `CAM-DESK-DIRECT-010..019`; the old Local tab is
  covered by `CAM-DESK-LOC-010..024` (deferred/superseded).
- `docs/desktop/local-integrated-mode-spec.md` — architecture spec for
  the superseded Local tab experiment.

Architecture (for cross-doc consistency, same diagram as the active
spec): `Desktop UI → CAM Hub/API → Remote Controller/Node → tmux +
agent runtime`. Coding agents always run on the controller/node, never
inside Desktop. Active Settings exposes Direct and Relay; Direct starts
the local Hub that Desktop talks to.

## 1. Connection modes (historical)

> **Historical.** The active product exposes only **Direct** and
> **Relay** in Settings. **Direct** is now the app-managed local CAM Hub
> path; **Local** is not a separate active Settings tab. The table below
> is preserved as written at the time of the Local tab experiment.

At the time of this guide, CAM Desktop was planned to expose four
connection modes in **Settings**. In the active product, only **Direct**
and **Relay** are shown. The historical table below records the
experiment's intent.

| Mode | Use it when | Desktop manages (then) |
| --- | --- | --- |
| **Managed** | Your org runs a CAM hub for you. You have its URL and an API token. | Nothing. Desktop connected to the hub as an HTTP/WS client. (Now a preconfigured Direct profile — no separate tab.) |
| **Direct** | You have a reachable hub or controller-with-API somewhere (lab box, another LAN host, …) and its API token. | Nothing. You enter `Server URL` + `API token`. (Still active.) |
| **Relay** | The hub/controller lives behind NAT/firewall and you have a relay endpoint that fronts it. | Nothing. You enter `Relay URL` + `Relay token` + `CAM API token`. The relay proxies REST/WS; the CAM token authenticates the proxied API calls. (Still active.) |
| **Local** (DEFERRED) | You want to run a single-machine setup: Desktop colocates a hub and a node here. | `cam serve` on `127.0.0.1:8420` — Desktop would start/stop it, generate the API token, and reconnect the UI. **Not implemented in the active product.** The prerequisites in §2 below applied only to this mode. |

In the active product there is no Managed tab and no Local tab. The
app-managed-Hub lifecycle moved into Direct. The prerequisites in §2
below applied to the old Local tab experiment; active Direct may reuse
some runtime checks, but its UX and implementation are specified in
DIRECT-010..019.

## 2. Prerequisites

CAM Desktop is the **client and service supervisor**. It does **not**
install WSL, Python, `cam`, or agent CLIs. You (or your IT) need the
runtime in place first.

### 2.1 Windows: WSL must already be installed

CAM Desktop on Windows runs the local CAM stack inside WSL. Before
launching Desktop:

1. Install and enable WSL2 (Microsoft docs: `wsl --install`). A reboot
   may be required.
2. Install a Linux distro inside WSL (Ubuntu LTS is the validated
   default).
3. Confirm the distro is usable: `wsl -l -v` shows it in state
   `Running` or `Stopped`, and `wsl -d <distro> -- echo ok` returns
   `ok`.
4. Make sure your Windows user has permission to launch `wsl.exe`
   without admin elevation.

If `wsl --status` errors out or no distro is listed, **stop here** and
get WSL installed before continuing. Desktop's Local tab will report
`cam-missing` or refuse to start if WSL is absent.

### 2.2 Inside WSL (Windows) or directly on macOS

Install these inside the WSL distro on Windows, or directly on macOS:

| Component | Why | How (typical) |
| --- | --- | --- |
| **Python 3.10+** | `cam serve` is a Python (FastAPI) app. | Ubuntu: `sudo apt install python3 python3-venv python3-pip`. macOS: `brew install python@3.12` or system Python ≥ 3.10. |
| **tmux** | CAM hosts each agent in a tmux session; `cam attach` and the monitor both need it. | Ubuntu: `sudo apt install tmux`. macOS: `brew install tmux`. |
| **git, curl** | Standard dev essentials for CAM and most agent CLIs. | Ubuntu: `sudo apt install git curl`. macOS: usually present (`xcode-select --install`). |
| **`cam` with server extras** | Provides the `cam serve` HTTP API Desktop talks to. | `pip install --user "cam[server]"` (or whatever pip path your IT uses; see `pyproject.toml`'s `server` extra). |
| **Agent CLIs (optional but expected)** | `cam` spawns Claude Code / Codex / Cursor / etc. through their own CLIs. Each CLI must be installed and **logged in as your user** before CAM can drive it. | Per-vendor install + login. CAM does not authenticate them on your behalf. |

Verify after install (run inside WSL on Windows, in a normal shell on macOS):

```bash
python3 --version       # 3.10+
tmux -V                  # any recent version
which cam && cam --help  # cam binary discoverable on PATH
which claude || true     # whichever agent CLIs you plan to use
```

If `cam` is installed via `pip install --user`, make sure
`~/.local/bin` is on the login `PATH`. Desktop spawns `cam serve` via
`bash -lc` so it picks up your login `PATH`, but other tooling may not.

## 3. What CAM Desktop manages

Once the prerequisites above are in place, Desktop's Local tab owns
this much:

- **Runtime check**: `Settings → Local → Check` scans for WSL (Windows),
  Python, `cam`, and the API port. Results show in the status badge and
  the Advanced/Diagnostics disclosure.
- **Start**: spawns one `cam serve --host 127.0.0.1 --port 8420 --token <generated>`
  as an app-owned child process and waits for `/api/system/health` to
  return a CAM-shaped response. On success it persists the URL and the
  token into local Desktop state and connects the UI.
- **Stop / Restart**: signals the child process Desktop started. The
  underlying tmux sessions and agents keep running; only the API
  service is bounced.
- **Reconnect**: when the app is reopened, Desktop reconnects via the
  Local/Direct profile it persisted (server URL + token). If a `cam
  serve` is still listening on `127.0.0.1:8420` and the saved token
  still authenticates, the agent list returns immediately. Note that
  **process ownership is not recovered across app restarts** in this
  phase — see §5.

Tokens are generated by Desktop and stored in local profile state.
Diagnostics shows only redacted sha256 fingerprints; the raw token
never appears in the UI or in buffered logs.

## 4. What CAM Desktop does NOT manage (this phase)

- **Enabling or installing WSL.** Windows users must have WSL2 and a
  distro available before launching Local. Phase 2C will address full
  Windows bootstrap.
- **Installing OS packages.** Python, tmux, git, curl, and other base
  tools come from your distro's package manager, not from Desktop.
- **Installing `cam` or upgrading it.** Desktop detects whether `cam`
  is on PATH and refuses Start with an actionable error if it's not.
  Phase 2B will address WSL-side `pip install --user "cam[server]"`.
- **Agent CLI login.** Each agent CLI (Claude, Codex, etc.) is logged
  in as your user via that vendor's tooling. Desktop never sees those
  credentials.
- **SSH keys, vendor API keys, or other secrets.** Desktop's renderer
  is sandboxed; secrets stay where you put them (e.g. `~/.ssh`,
  `~/.config/<vendor>`).
- **Arbitrary system service installation.** Desktop does not register
  `cam serve` with `systemd`, `launchd`, or Windows Services. See §5.

## 5. Service lifecycle policy

The current phase treats the local `cam serve` as an **app-owned child
process**, not a persistent OS service:

- Desktop spawns it when you click `Start`.
- Desktop tracks the spawned process by PID **in Electron main process
  memory**. There is no on-disk ownership marker. This means ownership
  state only exists for the duration of the current Desktop process.
- A `cam serve` started outside the current Desktop process — whether
  by you in a terminal, by another tool, or by a previous Desktop
  process that has since quit or crashed — is treated as a foreign /
  user-owned server. Local refuses to take ownership; use the
  **Direct** tab with that server's token, or stop the server
  yourself from the runtime.
- On Desktop quit, the supervisor kills its own child via
  `app.on('before-quit')`. This is the conservative default — it
  prevents orphaned background processes from the normal-exit case.
  A future phase may add a "keep running until Stop" option together
  with a persistent ownership marker so ownership can survive Desktop
  restarts.
- After a Desktop crash or kill, the `cam serve` it spawned may
  outlive Desktop. The next Desktop launch will see that server as
  foreign (no in-memory PID record). It will not be adopted or
  killed by Desktop in this phase.

Restarting a freshly killed `cam serve` is idempotent: tmux sessions
and per-agent state live in `~/.cam/` and persist across `cam serve`
bounces.

## 6. Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Local badge shows `cam-missing` after Check. | `cam` is not on the runtime's `PATH`. | Inside the runtime: `pip install --user "cam[server]"`, ensure `~/.local/bin` is on your login `PATH`, then click Check again. |
| Local Check is blocked / Windows shows "No WSL distro detected". | WSL isn't installed, or no usable distro exists. | Open PowerShell as your user: `wsl --install`, then install a distro (e.g. Ubuntu LTS). Reboot if WSL prompts. Re-launch Desktop and click Check. |
| Diagnostics row "has Python" is false. | Python is missing from the runtime. | Inside WSL (Windows) or macOS: install Python 3.10+ (`apt install python3 python3-venv python3-pip` / `brew install python`), then Check again. |
| `cam serve` starts but agents never appear in the list. | `tmux` is missing in the runtime; `cam` cannot host agent sessions. | Install `tmux` (`apt install tmux` / `brew install tmux`) and Restart from the Local tab. |
| Local badge shows `port-conflict`. | Port `8420` is held by a non-CAM service. | Free the port (find the owning process with `lsof -i :8420` / `Get-NetTCPConnection -LocalPort 8420`) and click Start again. |
| Local badge shows `foreign-cam-api` (a CAM server is already there). | You (or another tool) already started a `cam serve` on `127.0.0.1:8420`. | Either stop that server and click Start, or switch to the **Direct** tab and enter its existing API token. Desktop refuses to take ownership of a `cam serve` it didn't start. |
| Start succeeds but the UI shows "Backend started but client could not connect." | Token mismatch or `cam serve` exited shortly after start. | Open **Advanced / Diagnostics** → `Refresh logs`. If the log shows a Python / port / config error, fix it inside the runtime and click Restart. If logs look clean, click Restart to regenerate the token and reconnect. |
| Direct tab connects but `/api/system/health` is 401. | The token entered does not match the `cam serve` you're pointing at. | Find the actual token (e.g. `~/.cam/config.toml` `[server].auth_token`, or the `--token` you passed when starting `cam serve` manually) and paste it back. |
| Relay tab connects (websocket up) but agents stay empty / 401 on `/api/...`. | Relay token is correct but the **CAM API token** field is empty. | Relay tab requires three fields: Relay URL + Relay token + CAM API token. Paste the CAM server's API token in the third field and save again. |
| An agent shows in the list but `cam` says it can't spawn. | The agent CLI (e.g. `claude`) isn't installed or isn't logged in **as your user**. | Install the vendor CLI inside the runtime and complete its login flow at a terminal. Desktop doesn't manage those credentials. |
| Desktop quit, but `cam serve` is still listening on 8420. | A previous Desktop session crashed before `before-quit` ran, or another user/tool started a server. | Desktop will not kill a server it does not currently own (process ownership lives in memory, not on disk; it does not survive Desktop restarts). Either stop the server manually from the runtime (e.g. `pkill -f "cam serve"`), or switch to the **Direct** tab and connect with the server's known token. |
| Settings keeps falling back to the Local tab even though you typed Direct creds. | Direct credentials weren't saved (e.g. you switched tabs without clicking **Save & Connect**). | Re-enter Server URL + API token under **Direct** and click **Save & Connect**; the tab default will follow your saved profile after that. |

If a problem isn't in this table, open **Advanced / Diagnostics**
inside the Local tab. The rolling log buffer there carries the
managed `cam serve`'s stdout/stderr (tokens redacted), which usually
points at the underlying issue.

## 7. Notes / next phases

- This guide describes the **Phase 2E direct-only Local** path. A
  later phase (Phase 2B / 2C) will add Desktop-driven CAM install
  inside WSL and full Windows bootstrap.
- Local mode is intentionally per-user, not system-wide. A future
  iteration may offer "register as a user-level service" if there's
  demand.
- For remote/unreachable CAM servers, the Relay tab remains the right
  tool — it has nothing to do with Local mode's lifecycle.
