# CAM Desktop Assistant — Design

Status: **MVP IMPLEMENTED (0.2.21)** — the `assistant` built-in extension is a
pure iframe view (Chat + its own Settings tab) over a scoped `assistant.*`
bridge family; the main-process AssistantHost spawns the bundled pi agent
(`extensions/vendor/cam-assist/dist/cam-assist.js`, pi-agent-core + pi-ai,
openai-completions provider only) via `ELECTRON_RUN_AS_NODE`. v1 is pure Q&A
— no tools yet. Deviations from the original MVP text below:

- Settings live in the ext's **own Settings tab** (base URL / model / token,
  save = validate via `GET /models`, model list fetch, remembered across
  restarts) — NOT in the app-managed Extensions → Settings form. Since
  0.2.24, URL + model persist in `<userData>/ext-data/assistant/config.json`
  (legacy `<userData>/assistant.json` is migrated on first boot); the token
  goes to the credential store (safeStorage), ref `assistant:llm-token`.
  Empty URL/model fields resolve to the built-in defaults
  (`https://inference-api.nvidia.com/v1`, `nvidia/moonshotai/kimi-k3`).
- 0.2.24 UX+plumbing round: reasoning (`reasoning_content`) streams into a
  dim thinking block; sends during a running turn are **queued**
  (pi-agent-core `followUp`) instead of rejected; events reach the view over
  a **push rail** (host `onEvent` → main `assistant:event` broadcast →
  preload → ext-bridge → `camExt.onEvent('assistant.event')`), with the seq
  cursor deduping against the fallback poll; and the full transcript
  (including echoed `user` events) persists to
  `ext-data/assistant/events.jsonl`, so page navigation and app restarts
  restore the chat — only Reset wipes it.
- 0.2.25: **threads** — per-thread JSONL logs under
  `ext-data/assistant/threads/<id>.jsonl` + `threads.json` index; the
  view gains a Threads tab and New chat; switching threads re-seeds the
  child's transcript (`load` message) so conversations continue with
  context. Debug rail: host logs every event (`[assistant] ev#N …`) and
  the bundle stamps lifecycle markers on stderr (`[cam-assist +Ns] …`).
- 0.2.26: **bundle shadowing** — the host spawns
  `<userData>/extensions/assistant/cam-assist.js` when present
  (user-installed package) and falls back to the bundled copy. The
  assistant tar.gz ships view + minified agent bundle together
  (<4MB/file cap), so assistant iterations are ext-only from here on.
  (0.2.30 tightened this: the user copy wins only when its manifest
  version is strictly newer than the built-in's — ties go to the
  built-in, so an app reinstall/upgrade repairs stale shadows.)
- 0.2.27: opt-in local shell tool (`bash`, cmd.exe on Windows) —
  off by default, enabled from the assistant Settings tab, every command
  audit-logged (`[assistant] $ …`); output tail-capped, timeout-bounded.
- 0.2.29: **single-track cam-pi bridge** replaces the in-app spawn
  (supersedes 0.2.27/0.2.28). The app binary has no shell-exec code at
  all: the user starts `cam-pi.js` (stdlib-only, ships in the assistant
  package) in their own terminal and pastes its
  `cam-pi://127.0.0.1:<port>/<token>` line into Settings (the /health
  probe runs when the pair is first saved or changed — an unchanged,
  offline bridge never blocks saving LLM settings; token lives in the
  credential store). The bundle's `bash`
  tool is a loopback HTTP client of that bridge — identical behavior on
  MAS / MSI / DMG, no `process.mas` feature branches. MAS keeps exactly
  one gate: bundle shadowing stays disabled there (Apple 2.5.2).
- 0.2.30: review fixes — bridge probe only on pair change (offline cam-pi
  no longer blocks LLM saves), parseBridgeInput accepts the whole printed
  `cam-pi listening: …` line, replayed replies keep their original
  timestamps (ev.ts), concurrent Saves settle the superseded configure
  wait instead of hanging, native-ext chrome CSS no longer hides itself,
  shell.js import stamp unified (module-instance split).
- 0.6.0 bundle / 0.2.36 app: **direct local shell** supersedes the cam-pi
  bridge (supersedes 0.2.29). The cam-assist child is already a full Node
  process, so on non-MAS builds its `bash` tool spawns the platform shell
  itself (`CAM_LOCAL_SHELL` build flag; MAS builds flip it off and
  register no shell tool at all). All cam-pi machinery is removed:
  `cam-pi.js` no longer ships, the host's bridge APIs
  (`bridgeStatus`/`bridgeShutdown`/drop-file auto-connect/`copyText`) are
  gone, and a legacy `shellUrl` config + stored shell token are stripped
  on first boot. Same round: **delta-free transcript persistence** —
  streaming `delta`/`thinking` events stay live-only (ring buffer + push
  rail) and never hit disk; they had flooded the thread JSONLs (~97% of
  lines) and truncated the visible history at the ring/rewrite caps.
  Rehydration also skips legacy streaming lines, so pre-cleanup thread
  files replay their full durable history.
- The **CAM backoffice toolkit landed in 0.2.22** as ONE generic `cam`
  tool (method + path + body → authenticated hub call, confined to
  /api/*): every present and future hub endpoint is covered without a
  bundle rebuild. The hub pair is injected by main on every hub
  start/restart (token rotates on app reload); every assistant-originated
  hub call is audit-logged `[assistant] METHOD path → status`.
- No persona-skills workspace yet, no nav-bar entry (opens from
  Extensions → assistant, or the agent Ext▾ menu when pinned).
- LATER items (home access, cross-node messaging, computer use) remain
  reference designs below.

## Goal

A local, convenient, low-friction smart assistant: fleet-wide visibility
(agent status across all nodes), conversational diagnosis/Q&A, and controlled
execution of app-level operations. It is part of CAM Desktop, not a separate
product.

## Scope (finalized 2026-08-13)

**MVP** — a `cam-assistant` extension plus ONE small app-side addition
(AssistantHost + a scoped `assistant.*` bridge family — in the MSI-justified
"new bridge capabilities" category):

1. Works with **zero nodes configured**: the user only configures LLM URL +
   token, and the assistant runs as an app-hosted local child process.
2. Full **CAM backoffice** capability: the assistant can drive the hub API —
   add node, sync host, start/stop agent, edit settings — with every call
   audit-logged.
3. A bundled set of CAM diagnostic skills (`cam-desktop-netdiag`,
   `camc-diagnose`, `managing-camc`, …) plus a generated hub-api skill, so the
   assistant can actually investigate, not just chat.
4. Per-OS onboarding cards for the local SSH connection — now an optional
   power-up (host-level diagnostics on MAS), not a prerequisite.

**Later — separate problems** (reference designs kept below, not part of the
MVP, no scheduling commitment):

- Home access (reverse tunnel: remote agents operate the local machine)
- Cross-node agent messaging, and node↔CAM tooling communication generally
- GUI control (computer use)

## UI

- First-class **Assistant** entry in the nav bar (same level as Nodes/Settings)
- Three states:
  1. **Unconfigured** → setup card (LLM URL / key / model). Works with zero
     nodes configured
  2. **Configured and running** → terminal-styled transcript inside the ext
     page column (see the detailed design below)
  3. **Stopped/lost** → Start assistant button
- Quick-action buttons (fleet report / diagnose) use one-shot ext.call, no
  session needed
- **Single persistent session**: the assistant is resident and remembers fleet
  context across days; "reset conversation" = restart the agent. No
  multi-session management

## cam-assistant extension — MVP detailed design

### Architecture — app-hosted pi (pivoted 2026-08-13)

"Works before any node exists" breaks the pure-ext assumption: an ext backend
can only execute *on a node*, so zero-node operation means **the app itself
hosts pi**. Cost: ONE app-shell addition (a small assistant host + one bridge
family) — squarely inside the MSI-justified category "new bridge
capabilities". The ext side stays a pure iframe (view + setup card); no
native page.

- **pi runs as a child of the app's main process**, spawned as
  `ELECTRON_RUN_AS_NODE=1 <own binary> cam-assist.js` — the app *is* a Node 20
  runtime, so no Bun standalone binaries, no download step, no per-platform
  matrix. The "recompile" shrinks to **bundling pi + our thin entry into one
  JS file** (see next section).
- **Our entry owns the protocol**: JSON-lines over stdio (in:
  `{type:"send"|"config"|"stop"}`, out: `{type:"delta"|"tool"|"done"|"error"}`).
  We control this contract no matter how upstream pi's CLI evolves — that is
  the real job of the fork entry.
- **Config via env at spawn**: `CAM_API_URL` / `CAM_API_KEY` / `CAM_MODEL`
  plus `CAM_HUB_URL` / `CAM_HUB_TOKEN` (the backoffice toolkit, below). The
  hub token rotates on hub restart (resetApp) — AssistantHost watches the hub
  lifecycle and respawns/re-injects.
- **Lifecycle**: AssistantHost in the main process — spawn / supervise /
  restart, single persistent session. The child belongs to the main process,
  so it survives renderer reloads; it dies with the app.
- **Capability layers** (honest split):
  - *DMG/MSI* (unsandboxed): pi gets full local power — shell, files,
    everything a CLI agent does on the user's machine.
  - *MAS* (child inherits the sandbox): pi can still do the **CAM backoffice**
    (hub loopback API via `network.client`), read the app's own container
    (logs + config — exactly what the diagnostic skills need), and call the
    LLM API. Host shell / arbitrary files stay out of reach; the local-node
    self-setup card remains as the escape hatch for host-level diagnostics on
    MAS, but it is no longer a prerequisite for anything.

### The CAM backoffice toolkit (hub API access)

- At spawn, inject `CAM_HUB_URL` + the per-launch hub token. pi talks to the
  hub loopback API with plain HTTP: add node, sync host, start/stop agent,
  edit settings, read diagnostics — the full backend surface.
- **Write access is full** (user decision 2026-08-13 — supersedes the earlier
  "writes need confirmation" capability model): the assistant is the user's
  own, running locally as the user. The safety rail is auditability instead:
  every assistant-originated hub call is logged to `cam-desktop.log`
  (`[assistant] POST /api/contexts …`) so anything it does is traceable.
- A generated **hub-api skill** (endpoint list + curl examples) ships with the
  persona skills so the model reads the exact API surface instead of
  guessing.

### The pi build ("recompile to embed") — a JS bundle, not a binary

- esbuild bundle: pi + our entry → `cam-assist.js` (a few MB), shipped in the
  app's Resources. No Bun, no per-platform binaries, no GitHub-release
  bootstrap download — the app's own Electron binary is the runtime
  (`ELECTRON_RUN_AS_NODE=1`).
- Fork surface stays thin: one entry module (env→provider config mapping +
  the stdio JSON-lines protocol). No patches to pi internals;
  `extensions/vendor/pi` pins the upstream commit so tracking upstream is a
  re-bundle, not a rebase fight.
- CAM skills + AGENTS.md persona are **not baked into the bundle**; they are
  data files (persona skills + the generated hub-api skill), written into the
  assistant workspace at first start. Skills stay editable without a rebuild.
- MAS viability comes from this shape: the child is our own signed binary
  running a data file — no new executable to sign, and **nothing is
  downloaded** (MAS forbids downloading executables). The review narrative
  gains one sentence: "the app may run an embedded assistant process that
  talks only to the app's own loopback hub and the user-configured LLM
  endpoint."
- Custom first-class CAM tools (typed tools instead of curl-to-hub) remain a
  v2 option, only if pi's tool API allows shallow integration.

### App-side surface (the one MSI-justified change)

- main process: **AssistantHost** — spawn/supervise the child, config
  management, hub-token injection (re-inject on hub restart), ring buffer of
  output events with a poll cursor
- preload/bridge: one scoped family `assistant.status | configure | start |
  stop | send | poll`, exposed to the cam-assistant ext origin only — not a
  general-purpose ext API
- diagnostics: assistant lifecycle events + every assistant-originated hub
  call logged to `cam-desktop.log`
- tests: hub/bridge tests for the new surface; a bundle smoke test (spawn via
  `ELECTRON_RUN_AS_NODE`, echo a protocol round-trip) run against the
  **packaged** runtime per the connection-layer verification rule

### Settings UI (app-managed ext Settings page)

Manifest attributes (rendered by the existing Extensions → Settings form):

- `api_url` — OpenAI/Anthropic-compatible endpoint
- `model` — free text with suggestions
- `show_in_agent_menu` — the standard default attribute every ext gets

**api_key** is entered once in the interact view's setup card and stored via
the app's **credential store (safeStorage)** through `assistant.configure` —
encrypted at rest, never in plaintext ext config. (The earlier
node-side-chmod-600 design died with the node-hosted model.)

No `run_node`, no local-node prerequisite: the assistant is local-only and
works with zero nodes. The local-SSH onboarding card demotes to an optional
"unlock host-level diagnostics on MAS" hint.

### Interact UI (inside the ext page column)

Visual distinction from normal CAM agent terminals is by construction: the
view renders inside the **ext page width** (narrow centered column), never
the full-bleed terminal mode.

- **State A — not configured**: setup card (api_url/model reflected from
  attributes, api_key input → `assistant.configure`, "Start assistant").
  No node gate — this works with zero nodes configured
- **State B — running**: transcript area (monospace, subtle background)
  filling the ext column + bottom input row (text field + Send; Enter sends,
  Shift+Enter newline). Transport is the bridge: `assistant.send(text)` and
  `assistant.poll(cursor)` every ~1 s (paused when hidden). The structured
  event stream (delta / tool / done) lets us render tool calls as dimmed
  sub-blocks — the ChatGPT-ish hierarchy — instead of scraping ANSI
- **State C — stopped/lost**: last transcript dimmed + Restart button
- Quick-action row above the input: **Fleet report** / **Netdiag** /
  **Heal check** — one-shot canned prompts
- Transcript styling v1 = plain text + tool-call dimming; full markdown is a
  v1.1 candidate (in-iframe rendering, no new bridge)
- Width: the view renders inside the **ext page column** by construction —
  never the full-bleed terminal mode — which is the visual distinction from
  normal CAM agent terminals

### Tests

- `extensions.test.cjs` / `ext-nav.test.cjs`: manifest validity, package
  self-containment, no native marker
- bundle smoke test: spawn `cam-assist.js` via `ELECTRON_RUN_AS_NODE` with a
  mock LLM endpoint, verify the stdio protocol round-trip — run against the
  **packaged** runtime
- bridge tests for the `assistant.*` family; bar stays: all seven desktop
  suites green

## Local machine access: user self-setup (finalized 2026-08-13, no tooling)

**Decision: no cam-pi, no bootstrap artifact of any kind.** CAM Desktop users
are developers who already know how to configure SSH; the app provides the
commands, the user runs them — a one-time 30-second action. Identical across
all three builds; no build matrix, no distribution, no added compliance
surface.

The Assistant page shows an onboarding card with per-OS commands:

| OS | command |
|---|---|
| macOS | `sudo systemsetup -setremotelogin on` (or System Settings → Sharing → Remote Login) |
| Windows | start sshd inside WSL, or admin PowerShell: `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Set-Service sshd -StartupType Automatic; Start-Service sshd` |
| Linux | `sudo systemctl enable --now sshd` (`ssh` on Debian/Ubuntu) |

Key flow (shared with Home access provisioning below): the app generates a
keypair in pure JS (Node crypto) and displays the public key together with an
`echo '...' >> ~/.ssh/authorized_keys` command for the user to paste — the MAS
sandbox cannot write `~/.ssh`, so we make the user run it uniformly and keep
DMG/MSI identical rather than special-casing them.
Verification: the app runs the standard add-node (127.0.0.1) + sync; the
result is the answer.

## Home access: a "phone home" tool for remote agents (reverse tunnel) — LATER, separate problem

The natural next step once local sshd exists: when connecting to a node, also
establish a reverse tunnel, and every agent on that node gains the ability to
operate the local machine.

```
app (SSH client) ──connects to──> remote sshd
      └─ forwardIn: remote 127.0.0.1:<rport> → local 127.0.0.1:22
agent on remote:  ssh -p <rport> user@127.0.0.1  →  tunnel home → local shell
```

Changes (all in desktop scope):

1. **Remote forwarding in ssh-transport**: `forwardIn(127.0.0.1, 0)` (port 0 =
   remote auto-assign), pipe `tcp connection` events to `127.0.0.1:22`; attach
   to the exec-pool connection, re-establish on reconnect. Connection-layer
   change — **must be verified against the packaged Electron runtime**
   (BoringSSL lesson); WSL node tests are reference only.
2. **Per-node "Home access" attribute, default OFF** — this hands the house
   key to a remote host; explicit opt-in is mandatory.
3. **Provisioning** (on enable): app generates a per-node keypair in pure JS →
   writes the private key to remote `~/.ssh/cam-home-<node8>` over the existing
   SSH channel → displays the public-key authorize command for the user to
   paste locally.
4. **Remote discovery**: every time the tunnel is (re)built, (re)write remote
   `~/.cam/home.json` (current port + user + key path) plus a `cam-home "cmd"`
   wrapper script. Agents always read the file, never hardcode the port;
   codex/claude/pi can use it with zero integration — document it in
   AGENTS.md / a skill for ergonomics.
5. **Precondition probe**: remote sshd needs `AllowTcpForwarding yes`
   (default; corporate images may disable it) — probe during provisioning and
   fail with a clean error.

Security model (the trust boundary inverts here, so be conservative):

- Enabling = **any process on that remote** can shell into this machine.
  Default off, explicit per-node opt-in.
- The authorized_keys entry carries `from="127.0.0.1",restrict` (tunnel-origin
  only; no port forwarding / X11 / agent forwarding).
- Revoke = toggle off → user is given the command to remove the
  authorized_keys line + tunnel is torn down.
- Optional v2 hardening: forced-command wrapper with a command whitelist.

Compliance: the app only does SSH forwarding and **never spawns local
processes** — the shell is started by the system sshd, so the MAS "no local
shell processes" narrative stays intact; forwarding to 127.0.0.1:22 is
outbound loopback, covered by `network.client`; no new artifact.

## Cross-node agent messaging — LATER, separate problem

Goal: let agents on different nodes talk to each other. Nodes cannot reach
each other directly (both behind NAT/VPN), so the desktop is the natural
broker — it is the only party that can reach every node. Two tiers, differing
by an order of magnitude in latency and complexity:

### Tier 1: outbox polling (no new transport; do this first)

Pure files over the existing SSH channels; zero new app network code:

- The app drops a wrapper script `cam-send <node>:<agent> "text"` on each node;
  an agent invokes it and the message lands in local `~/.cam/outbox/*.jsonl`.
- The hub already connects to every node periodically (sync/keepalive/exec
  activity); it reads and clears outboxes along the way.
- Routing: resolve the node in the address, deliver with `camc msg send`
  (camc's existing mailbox, already used for same-node messaging).

Addressing is the natural extension of camc's same-node model:
`<node>:<agent>`. Latency = poll interval (seconds, 5–15 s). Fine for
agent-to-agent coordination ("I pushed, run the tests") — this is not a human
chat surface.

### Tier 2: reverse tunnel to a narrow mailbox endpoint (near-real-time)

If sub-second delivery ever matters, reuse the Home access `forwardIn` work —
but note the refinement: **the tunnel target is not local sshd, it is a tiny
loopback endpoint that only accepts messages** (hub adds a dedicated listener,
`POST /agent-msg` with a per-node token):

- An agent on the remote runs `curl 127.0.0.1:<rport>/agent-msg` (port and
  token provisioned into `~/.cam/relay.json`).
- The hub routes and delivers via `camc msg send` on the target node.

Much smaller blast radius than shell-level Home access: the remote gets a
token-scoped message port, not a shell into your machine. The forwardIn
transport work is shared unchanged.

| | Tier 1 outbox | Tier 2 tunnel |
|---|---|---|
| latency | 5–15 s | sub-second |
| new transport code | none | forwardIn (shared with Home access) |
| remote sshd requirement | none | AllowTcpForwarding yes |
| security surface | none new | token-scoped endpoint |
| MAS impact | none | none (forwarding only) |

Do Tier 1 first; Tier 2 only if real usage proves the latency matters.

## GUI control (computer use) — LATER, separate problem

Premise: once the assistant has terminal capability on a machine, GUI control
is "call OS-specific tools from that same shell" — **zero app changes**, the
SSH datapath is untouched. The real work is the per-OS toolchain, permissions,
and the safety model. "Computer use" (Anthropic) is a model capability + tool
schema, GA since early 2026 — not a product you point at a Mac; the official
reference demo drives its own Dockerized Linux/X11 desktop, not the host OS.
Three integration levels, decision deferred to implementation time:

1. **Own tools + computer-use tool schema** (max control): pi registers
   `computer`-style tools; macOS backends = `screencapture` (see),
   `cliclick`/`osascript` System Events (act), **AX tree first, vision
   fallback** (text-based UI dump is more reliable than coordinates for
   structured apps). Works with any vision-capable model; Claude is the
   best-trained for coordinate prediction.
2. **Adopt a framework** (least engineering): `computer_use_ootb` or
   `self-operating-computer` package the see→decide→act loop for
   macOS/Windows. They run on the local node; the assistant ext embeds or
   delegates to them. Costs: python deps on the target, uneven maturity.
3. **Don't build** (reality check): Codex app already ships macOS computer
   use. Ours is justified only by integration with the CAM fleet — keep the
   feature scoped to "operate this machine for CAM workflows".

Platform reality:

- **macOS**: works. SSH sessions can reach the WindowServer; TCC grants
  (Screen Recording + Accessibility for `sshd-keygen-wrapper`) are the
  one-time user action — fits the self-setup command-card pattern.
- **Windows**: v1 skips GUI control. OpenSSH Server runs in Session 0 and
  cannot see the interactive desktop (Session 1); workarounds need a
  user-session helper = new artifact. Terminal-level control only.
- **Linux**: `xdotool`/`scrot` on X11; Wayland needs `ydotool`/`grim`.

Safety model (GUI control is the highest-risk capability — it can click
"confirm payment"):

- Default off, per-machine opt-in; TCC grants are the natural gate.
- Steal the Codex app's hard rule: **never automate terminal applications** —
  our assistant itself lives in a terminal on the machine it controls;
  self-typing is both a correctness and a security hole.
- App whitelist in the system prompt; action-level confirmation for anything
  destructive (v2).
- Cost note: screenshot loops burn tokens fast (historically dollars per
  task); cap iterations per task.

## Runtime route (by build)

| build | route |
|---|---|
| all builds | **App-hosted child process** (`ELECTRON_RUN_AS_NODE`): zero-node ready, CAM backoffice via hub API everywhere |
| **DMG / MSI** (non-sandboxed) | child is unsandboxed → **full local power** (shell, files, host diagnostics) |
| **MAS** (sandboxed) | child inherits sandbox → **CAM backoffice + app container + LLM API**; host shell needs the local-node escape hatch (optional) |
| any build | pi ships as a **JS bundle** inside the app (few MB) — no standalone binary, no download, no platform matrix |

## Capability model (updated 2026-08-13)

- **Full hub API access** (read + write): add node, sync host, start/stop
  agent, edit settings, read diagnostics — user decision: the assistant is
  trusted, it is the user's own process running locally as the user
- **Safety rail = auditability**: every assistant-originated hub call is
  logged to `cam-desktop.log` with an `[assistant]` marker
- **gen ssh key**: pure JS in the app (Node crypto); private key goes to
  Keychain / a user-chosen path, never through a shell
- **App-side addition**: AssistantHost + scoped `assistant.*` bridge family
  (the one MSI-justified change; supersedes the earlier generic
  `agents.start` bridge note)

## Boundaries (explicit non-goals)

- **The hub (loopback HTTP server) never executes local commands.** The main
  process spawns exactly ONE local child — the user-configured assistant
  itself. No general local-exec API, ever
- The assistant **can never bootstrap host-level access on MAS by itself**
  (chicken-egg): enabling Remote Login / key authorization is a manual user
  action; the app provides command cards and verifies the result (see "Local
  machine access")
- No taking over already-running free processes (Windows consoles cannot be
  attached; psmux spike verified 17/17 compatible; Windows native node awaits
  the camc Windows shim — cam-dev's work)
- No online extension store (MAS: no downloading executables)

## Dependencies and prerequisites

- Extensions mainline: tool-proxy directory deployment (done); the scoped
  `assistant.*` bridge family is part of this work, not a precondition
- pi fork: `extensions/vendor/pi` pins the upstream commit; our entry module
  is the only custom code; esbuild produces `cam-assist.js`
- Home access: `forwardIn` support in ssh-transport + per-node attribute +
  provisioning (no new artifact; connection-layer change, verified on the
  packaged runtime)
- Fleet summary feed: hub periodically packs agent status → camc msg push to
  the assistant agent

## Implementation order (after unfreezing)

**MVP** (one app release for the bridge/AssistantHost; ext updates ride
tar.gz thereafter):

1. pi fork entry + esbuild bundle (`cam-assist.js`) + mock-LLM smoke test
2. App side: AssistantHost + scoped `assistant.*` bridge + credential-store
   wiring + hub-token injection → packaged-runtime verification
3. `cam-assistant` extension package (interact view + setup card + skills
   bundle incl. generated hub-api skill)
4. MSI/DMG/MAS hands-on verification

**Later** (each is its own change, gated on real demand):

- Home access (forwardIn + per-node toggle + provisioning + home.json)
  → packaged-runtime verification
- Cross-node messaging Tier 1 (cam-send wrapper + outbox polling + routing)
- GUI control (evaluate frameworks first, then decide own-tools vs adopt)
- Fleet summary feed
