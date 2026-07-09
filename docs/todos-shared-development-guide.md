# Shared Todos Development Guide

## Purpose

Build one adaptive Todos experience for CAM Mobile and CAM Desktop. The feature
must share its task behavior, data model, and card semantics; the two apps may
use different layout composition where their available space and input model
require it.

This guide is the acceptance contract for the first consolidation pass. It
does not change the Hub/API or storage format.

## Scope

The Todos primary navigation contains exactly three tabs:

1. **Tasks**
2. **Projects**
3. **Archive**

`Inbox` and top-level `Notes` are not primary tabs. Store operations such as
Check, Initialize, and Refresh are controls within the Todos workspace, not
tabs.

The first pass includes task browsing, task details, task/project mapping,
project archive/unarchive, notes, checklists, history, and raw task content.
It excludes changes to the Hub API, workspace storage, agent terminal,
Nodes, and Settings.

## Canonical Project and Task Model

Project archival is the sole archival authority:

```text
task.projectId -> project.id
project.status = active | archived
```

- A task belongs to one project. An explicitly supported `Inbox` project may
  represent an unassigned task.
- Do not maintain an independent per-task archived state for normal project
  archival. A task's visibility follows the status of its project.
- Archiving a project preserves the project, its tasks, notes, checklists, and
  history. It removes that project and its tasks from the active Tasks view.
- Unarchiving restores the same project and all its tasks to the Tasks view.
- Project deletion is allowed only when the project has no tasks. Permanent
  deletion of archived projects is out of scope for this pass.
- Existing legacy standalone note records must remain readable and must not be
  silently deleted. Their migration to task-attached notes is a separate,
  explicit follow-up.

## Tasks Tab

The Tasks tab displays tasks from active projects only.

### Toolbar and filters

The tab provides:

- New Task
- Search across title, goal, body, project, and tags
- Project filter
- Status filter
- Sort control

Filter and sort state must have the same meaning on Mobile and Desktop.

### Task cards

Each task is one adaptive card. Cards are folded by default and retain their
folded/unfolded state while the task list refreshes.

#### Folded card

Show enough information to decide whether to open the task:

- Completion toggle
- Title, project, status, and priority where present
- Goal
- A compact body preview
- Notes count
- Checklist progress, for example `3/5`

Clicking the non-control part of a card toggles it. Buttons, checkboxes,
links, and selects inside the card must not toggle it accidentally.

#### Unfolded card

Keep the folded preview, then show a compact detail area containing:

1. Full goal and body
2. Notes
3. Checklist
4. History
5. Raw source

Notes and checklist rows must be compact, readable, and directly manageable.
They use subtle separators rather than nested, oversized cards. Completed
checklist items are visually subdued but remain operable. History and Raw are compact in-card tabs so they remain available without
dominating the card.

Every task card exposes Preview, Raw, Notes, Checklists, and History through compact in-card tabs; only the selected detail pane is visible.

## Projects Tab

Projects is the only place to manage task-to-project mapping and project
lifecycle.

It provides:

- Create project
- Rename project
- Show active task count per project
- Browse its task list
- Move a task between projects
- Archive project
- Delete an empty project

Archiving occurs at project level. There is no duplicate per-task Archive
action in the normal Tasks flow.

## Archive Tab

Archive displays archived projects, not a duplicate flat list of archived
tasks.

Each archived project shows its name, task count, archive metadata when
available, and an expandable read-only task summary. The primary action is
**Unarchive**, which restores the whole project and all associated tasks.

## Responsive Contract

The same task data, card markup semantics, actions, and state transitions must
work on Mobile and Desktop. Breakpoints may only change layout, density, and
presentation order; they must not change what information or actions exist.

| Viewport | Required layout |
| --- | --- |
| Phone, 360px+ | Single column. Unfolded card content flows vertically. Interactive controls have at least 44px targets. |
| Narrow/tablet, about 768px | Card remains usable in a single-column or compact two-column layout without horizontal clipping. |
| Desktop, 1280px+ | Unfolded detail may use two columns: body/notes on the left, checklist/history summary on the right. It falls back cleanly to one column as width decreases. |

Mobile does not get a reduced data model. Desktop does not get a separate card
interaction model.

## Shared Implementation Boundary

Use the existing `worklog-core.js` as the data/storage foundation. Add shared
Todos modules rather than allowing mobile and desktop controllers to diverge:

```text
web/js/shared/todos-controller.js  # load, filter, sort, selection, mutations
web/js/shared/todos-workspace.js   # shared task/project/archive markup and action contract
web/js/mobile/todos.js             # mobile mounting and layout adapter
web/js/desktop/todos-mode.js       # desktop mounting and layout adapter
```

The shared controller owns behavior. Platform adapters own only host DOM
mounting, navigation integration, and platform-specific layout hooks. CSS may
be platform-scoped, but must consume the same semantic class names and
`data-*` actions emitted by the shared renderer.

## Migration Sequence

1. Add fixtures that cover active projects, archived projects, tasks, legacy
   standalone notes, notes, checklists, and history.
2. Introduce the shared controller without changing either current UI.
3. Introduce the shared folded/unfolded task card and migrate Tasks first.
4. Migrate Projects, including task mapping and project archive.
5. Migrate Archive as the archived-project browser and remove legacy primary
   tabs/actions.
6. Remove duplicated mobile/desktop Todo rendering only after parity checks
   pass.

Each migration slice must be independently revertible.

## Acceptance Checks

- Tasks, Projects, and Archive are the only primary tabs.
- A task appears exactly once in its active project's Tasks view.
- Archiving a project removes all of its tasks from Tasks without data loss.
- Unarchiving restores the same project and task mapping.
- Folded cards show goal, body preview, notes summary, and checklist progress.
- Unfolded cards expose full Preview, Raw, Notes, Checklists, and History.
- Filters, sorting, task completion, note edits, and checklist edits behave
  identically on Mobile and Desktop.
- The same fixture is verified at 360px, 768px, and 1280px widths.
- No Hub/API/storage migration is required for this UI consolidation.
