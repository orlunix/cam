# Bots Page Development Plan

Status: UI v0 implemented; Hub bot APIs pending
Owner split: camui-rev owns product shape/review; camui-dev may implement slices.
Last updated: 2026-06-15

## Implementation Status

2026-06-15 UI v0 is implemented in Desktop:

- Top-level **Bots** nav item and full-width page.
- Fixture-backed bot inventory for reviewing agent-bundle and CamFlow-package
  shapes before Hub APIs land.
- Search, target/source filters, sort, bot cards, detail tabs, raw manifest,
  workflow preview, and dry-run action preview.
- Launch and Import are visible but explicitly staged until `/api/bots`
  list/dry-run/launch endpoints are implemented.
- No runtime shell execution is performed by the renderer.

Next implementation slice should replace fixtures with `GET /api/bots`, then
wire `POST /api/bots/:id/dry-run` and `POST /api/bots/:id/launch`.

## Goal

Add a top-level **Bots** page to CAM Desktop. A Bot is a reusable, versionable
agent/workflow template bundle: settings, files, scripts, configs, loops,
workflow YAML, skills, MCP references, and launch defaults that can be used to
start a prepared agent or a prepared workflow.

This is not a second runtime. Bots are a Desktop/Hub-managed packaging and launch
surface over existing CAM primitives:

```text
Bot template/bundle
  -> selects node/context/workspace
  -> installs/copies files/scripts/configs/skills as needed
  -> applies agent settings/system prompt/loops/workflow defaults
  -> starts one agent or a workflow through existing Hub/camc APIs
```

## Definitions

| Term | Meaning |
| --- | --- |
| Bot | A reusable template for one prepared agent or workflow. |
| Bot bundle | The on-disk directory or repository that contains a bot manifest plus assets. |
| Bot instance | A started agent/workflow created from a bot bundle. |
| Bot asset | A file/script/config/prompt/workflow/skill reference included by a bot. |
| Launch profile | The chosen node, workspace path, tool, model/profile, and runtime options used when starting from a bot. |

## CamFlow Package Command Check

CamFlow already has a package command surface that should be reused for workflow
Bots instead of inventing a parallel workflow package format.

```bash
camflow package create --from-run RUN_DIR --name NAME --version VERSION --out PATH.camflowpkg [--description TEXT] [--allow-halted]
camflow package validate TARGET
camflow package inspect TARGET [--json]
camflow package install PATH.camflowpkg [--project]
camflow package list [--project]
camflow package uninstall NAME@VERSION [--project]
camflow run --package NAME@VERSION
```

Package semantics observed from `/home/hren/.openclaw/workspace/camflow`:

- `.camflowpkg` is a deterministic gzipped POSIX tar rooted at `camflowpkg/`.
- Required package files: `manifest.yaml`, `lock.json`, `workflow.yaml`, bundled `skills/<name>/SKILL.md`.
- Installed scopes: user-global under `~/.camflow/packages`, or project-local under `./.camflow/packages` with `--project`.
- Runtime execution uses `camflow run --package NAME@VERSION`, which materializes package assets into the run directory and does not invoke Planner for the initial run.
- P0 rejects direct-command workflow nodes; command behavior should live inside skills or `verify.command`.
- P0 rejects host-skill fallback; packaged workflow skills must be bundled in the package manifest.

Implication for Bots: workflow-type Bots should support CamFlow packages as a
first-class source:

```yaml
target:
  type: workflow
  package: code_review@0.1.0
  package_scope: project   # project | user
```

The Bots page can list installed CamFlow packages through `camflow package list`,
inspect details with `camflow package inspect --json`, validate with
`camflow package validate`, and launch with `camflow run --package`. This should
be preferred for frozen workflow Bots. Raw `workflows/workflow.yaml` remains
useful for editable/development Bots.

## Product Model

A Bot bundle should be concrete and inspectable. V0 should support both:

1. Frozen workflow package references backed by `.camflowpkg` / installed
   `NAME@VERSION`.
2. Editable manifest-first bot folders for agent templates and work-in-progress
   workflows.

For the editable folder path, use a simple manifest-first layout:

```text
.bot/
  bot.yaml
  files/
  scripts/
  configs/
  workflows/
    workflow.yaml
  prompts/
    AGENTS.md
    CLAUDE.md
  loops/
    review-loop.yaml
  skills/
    skills.yaml
  mcp/
    mcp.json
```

The recommended manifest shape:

```yaml
schema: cam-bot/1
id: code-reviewer
name: Code Reviewer
version: 0.1.0
description: Reviews a repo, summarizes risks, and proposes fixes.
tags: [review, coding]

target:
  type: agent        # agent | workflow
  default_tool: codex
  default_workspace: .

settings:
  auto_confirm: true
  tags: [review]
  system_prompt_file: prompts/AGENTS.md

assets:
  files:
    - from: files/review-checklist.md
      to: .cam/bots/code-reviewer/review-checklist.md
  scripts:
    - from: scripts/preflight.sh
      to: .cam/bots/code-reviewer/preflight.sh
      executable: true
  configs:
    - from: configs/reviewer.json
      to: .cam/bots/code-reviewer/reviewer.json

workflow:
  file: workflows/workflow.yaml

loops:
  - name: idle-review
    prompt_file: loops/review-loop.md
    schedule: every 30m

skills:
  repositories:
    - name: main
  install:
    - managing-cam
    - reviewer

mcp:
  references:
    - mcp/mcp.json

launch:
  prompt: Review the current workspace and report blockers first.
  env_setup: ''
  timeout: 3600
```

V0 does not need to support every field above as executable behavior. It should
render all fields, validate the known subset, and preserve unknown manifest keys
in Raw mode.

## Storage and Sync

Use a repository/directory model first, not a Desktop-owned database.

Recommended local/remote paths:

```text
<workspace>/.cam/bots/<bot-id>/        # workspace-local bot bundle
~/.cam/bots/<bot-id>/                  # node-global installed bot bundle
```

V0 source options:

1. Local workspace bot: detect `<agent workspace>/.cam/bots/*/bot.yaml`.
2. Node-global bot: detect `~/.cam/bots/*/bot.yaml` on a selected node.
3. CamFlow package install: detect `camflow package list [--project]` and treat installed `NAME@VERSION` as workflow Bots.
4. Git-backed bot library: later, mirror Skillm style with repository add/pull/list.

Reasoning:

- Workspace-local bots travel with the project and are easy to inspect in Browse.
- Node-global bots are useful for personal defaults and repeated launch templates.
- Git-backed libraries should be added later once the UX proves useful.

## Desktop IA

Add top-level nav item: **Bots**.

Page structure should match the existing full-width management pages such as
Nodes / Skills / Todos, not Agent output mode.

```text
Bots
├─ Header
│  ├─ Search
│  ├─ Filter: tag / source / target type / tool
│  ├─ Sort: recently used / name / updated
│  └─ Add / Import Bot
├─ Bot list
│  └─ Bot cards
│     ├─ name, description, tags
│     ├─ source badge: workspace | node | git
│     ├─ target badge: agent | workflow
│     ├─ assets summary: files/scripts/configs/loops/skills/mcp
│     └─ actions: Launch, Edit, Duplicate, Export, Remove
└─ Bot detail/editor
   ├─ Overview
   ├─ Settings
   ├─ Files & Scripts
   ├─ Workflow
   ├─ Loops
   ├─ Skills
   ├─ MCP
   └─ Raw Manifest
```

Avoid a huge multi-column form. Use one vertical editor with tabs/cards. The
most important workflow is: select bot -> review what it will install/start ->
Launch.

## Card Design

Bot card should show operationally useful information at a glance:

```text
[Code Reviewer]                         agent · codex
Reviews repo changes and flags risks.   workspace source
#review #coding
Files 2 · Scripts 1 · Workflow 1 · Loops 1 · Skills 2 · MCP 0
[Launch] [Edit] [...]
```

Detail page cards:

- **Overview**: id, name, description, tags, source path, target type.
- **Settings**: tool, auto-confirm, launch prompt, system prompt file.
- **Files & Scripts**: source/target mapping, executable flag, conflicts.
- **Workflow**: embedded Workflow renderer for `workflow.yaml`.
- **Loops**: reuse Agent Settings Automation card style.
- **Skills**: reuse Skills install/list concepts; do not duplicate Skillm repo management.
- **MCP**: show references/config files; launch-time copy only in V0.
- **Raw Manifest**: selectable YAML, later editable.

## Launch Flow

Use a launch drawer/panel, not a modal-only flow:

1. User clicks **Launch** on a bot.
2. Select target node/context/workspace.
3. Confirm tool/profile and prompt.
4. Preview install actions:
   - copy files/scripts/configs
   - install skills
   - write system prompt file if selected
   - create loop/cron items if enabled
   - attach workflow file if target is workflow
5. Click **Start Bot**.
6. Hub executes the plan and returns either an agent id or workflow run id.
7. Desktop navigates to the new agent or workflow view.

V0 should support a dry-run preview before modifying files.

## Hub/API Plan

Add a small bot API surface behind the embedded Hub / external Hub contract.

Proposed endpoints:

```text
GET    /api/bots?context=<ctx>&source=workspace|node|all
GET    /api/bots/:id
POST   /api/bots/import
PATCH  /api/bots/:id
DELETE /api/bots/:id
POST   /api/bots/:id/dry-run
POST   /api/bots/:id/launch
```

V0 can be narrower:

```text
GET  /api/bots?context=<ctx>
GET  /api/bots/:id
POST /api/bots/:id/dry-run
POST /api/bots/:id/launch
```

Implementation rules:

- Renderer never runs shell commands.
- Hub reads/writes bot files through local fs or pooled SSH/SFTP using the same
  context resolution rules as Browse/Skills.
- Secrets such as Git tokens are one-shot form values and are never persisted in
  bot manifests unless explicitly stored as references.
- Dry-run must list every write before launch.

## Relationship to Existing Features

| Existing feature | Bot usage |
| --- | --- |
| Start Agent | Bot launch reuses the same start API body plus defaults from `bot.yaml`. |
| Agent Settings | Bot settings define defaults; started agent settings remain editable. |
| System Prompt | Bot may provide `AGENTS.md` / `CLAUDE.md` source files. |
| Workflow | Bot may include editable `workflows/workflow.yaml`; detail embeds the Workflow renderer. Frozen workflow Bots should prefer installed CamFlow packages (`camflow package inspect --json`, `camflow run --package`). |
| Automation | Bot may define loops/cron defaults; launch asks before creating them. |
| Skills | Bot may request skill installation; actual repo management remains on Skills page. |
| Browse | Bot assets are ordinary workspace files and can be inspected through Browse. |
| Todos | Later: Bot can create seed tasks/checklists, but V0 should not depend on Todos. |
| MCP | V0 stores/copies config references only; no embedded MCP runtime is introduced. |

## MVP Scope

MVP should be useful without solving every future problem.

### MVP 0: Read-only Bot Inventory

- Add top-level Bots nav/page.
- Detect workspace-local `.cam/bots/*/bot.yaml` for selected context/node.
- Render bot cards and detail tabs.
- Show Raw Manifest.
- Validate required fields: schema, id, name, target.type.
- No launch yet.

### MVP 1: Launch Prepared Agent

- Launch bot as one agent through existing start-agent path.
- Apply name/tool/prompt/auto_confirm/tags.
- Copy declared files/scripts/configs into workspace before launch.
- Dry-run preview required.
- Navigate to created agent.

### MVP 2: Workflow Bot

- Support `target.type = workflow`.
- Support installed CamFlow packages (`NAME@VERSION`) as the preferred frozen workflow source.
- Render package summary from `camflow package inspect --json`.
- Render editable `workflow.yaml` through the Workflow tab renderer when a bot folder contains raw workflow YAML.
- Launch frozen packages with `camflow run --package NAME@VERSION`.
- Runtime overlay is read-only at first.

### MVP 3: Libraries and Install

- Add Git-backed bot repositories, modeled after Skillm Repositories.
- Pull/refresh libraries on demand.
- Install bot to workspace or node-global path.

## Validation Rules

Minimum manifest validation:

- `schema` must be `cam-bot/1`.
- `id` must match `^[A-Za-z0-9_.-]{1,64}$`.
- `name` must be non-empty.
- `target.type` must be `agent` or `workflow`.
- Asset paths must be relative and must not contain `..` or absolute Windows/UNC/POSIX paths.
- Target paths must stay under workspace or approved node-global bot path.
- Scripts marked executable must be explicit; never infer executability from extension.
- `skills.install[]` entries must be names only, not shell commands.
- MCP entries are config references only in V0.

## UX Safeguards

- Every launch shows a dry-run write/action preview.
- Existing files are never overwritten silently; show conflict choices:
  - skip
  - overwrite
  - write as copy
- Loops/cron creation is opt-in at launch time.
- Git tokens are one-shot and redacted from logs.
- Raw mode is always available so future manifest fields remain inspectable.
- A bot card must never hide the source path; users need to know where the bundle came from.

## Suggested Files

```text
web/desktop.html                         # top-level Bots mode shell
web/js/desktop/bots-mode.js              # renderer UI/state
web/js/desktop/app.js                    # mount Bots mode, nav wiring
web/css/desktop.css                      # .bots-* scoped styles
web/js/api.js                            # CamApi bot methods
apps/cam-desktop/electron/embedded-hub.cjs # /api/bots routes + fs/SFTP helpers
docs/desktop/bots-page-plan.md           # this plan
docs/desktop/requirements.md             # CAM-DESK-BOTS rows
```

Optional later shared modules:

```text
web/js/shared/bots.js                    # manifest normalization/validation
web/js/shared/files.js                   # safe relative path helpers
```

## Implementation Slices

### Slice A: Doc + Manifest Fixtures

- Add this plan.
- Add CAM-DESK-BOTS requirements.
- Add one sample bot under a non-packaged fixture path, e.g.
  `docs/fixtures/bots/code-reviewer/.bot/bot.yaml`.
- No runtime code.

### Slice B: Read-only Bots Page

- Add nav item and empty Bots page.
- Add local mock/fixture loader first if Hub route is not ready.
- Render cards/detail from normalized manifest objects.
- Add validation messages.

### Slice C: Hub Bot List/Read

- Implement `GET /api/bots` and `GET /api/bots/:id` for workspace-local bots.
- Use Browse path safety helpers.
- Renderer switches from fixture data to API.

### Slice D: Dry-run and Launch Agent

- Add dry-run endpoint.
- Add launch endpoint for target.type = agent.
- Reuse Start Agent path and existing file-copy/write helpers.

### Slice E: Workflow and Library Integration

- Embed workflow renderer for workflow assets.
- Add Git-backed bot libraries if the local bundle model is validated.

## Open Questions

1. Should Bots be top-level only, or also appear inside Agent Settings as
   "Save current agent as bot"?
2. Should a Bot bundle use `.bot/` inside each bot folder, or should the bot
   folder itself contain `bot.yaml` at the top? V0 should choose one and support
   import aliases later.
3. Should bot launch copy assets into the target workspace, or reference them in
   place? Safer default: copy into `.cam/bots/<bot-id>/`.
4. How should workflow target launch map to CamFlow once workflow execution APIs
   are stable?
5. Should node-global bots live under `~/.cam/bots` or `~/.cam/botlib`? Prefer
   `~/.cam/bots` unless it conflicts with existing tools.

## Non-goals for V0

- No second agent runtime.
- No Desktop-owned SQLite bot database.
- No embedded MCP runtime.
- No automatic overwrite of user files.
- No hidden sync daemon.
- No mobile changes in the first Desktop slice.
