import { useMemo, useState } from "react";
import {
  checklistProgress,
  parseChecklist,
  relativeTime,
  useTodos,
  type TodoFilter,
  type TodoKind,
  type TodoRow,
  type TodoSort,
  type TodoStatus,
  type TodoTab,
} from "../lib/useTodos";

const TABS: { id: TodoTab; label: string }[] = [
  { id: "inbox",    label: "Inbox" },
  { id: "tasks",    label: "Tasks" },
  { id: "notes",    label: "Notes" },
  { id: "projects", label: "Projects" },
  { id: "archive",  label: "Archive" },
];

const SORTS: { id: TodoSort; label: string }[] = [
  { id: "updated", label: "Updated" },
  { id: "created", label: "Created" },
  { id: "title",   label: "Title" },
];

const STATUSES: TodoStatus[] = ["inbox", "open", "done", "archived"];

type DetailTab = "preview" | "raw" | "checklist" | "history";

function kindGlyph(kind: TodoKind): string {
  if (kind === "task")    return "☐";
  if (kind === "note")    return "≡";
  return "□";
}

// V0 store selector is a single-option dropdown; the underlying store
// is localStorage. A future slice will list Hub-mediated todocli stores
// here and let the user pick a remote node. Kept inline as a constant
// so the surface is visible to the reviewer without speculative code.
const V0_STORES = [{ id: "local", label: "Local (this device)" }];

export function TodosMode() {
  const t = useTodos();
  const [storeId, setStoreId] = useState<string>("local");
  const [detailTab, setDetailTab] = useState<DetailTab>("preview");
  const [filterOpen, setFilterOpen] = useState<boolean>(false);
  const [newTaskTitle, setNewTaskTitle] = useState<string>("");
  const [newNoteBody, setNewNoteBody] = useState<string>("");

  const tagOptions = useMemo(() => {
    const s = new Set<string>();
    for (const r of t.rows) for (const tag of r.tags) s.add(tag);
    return Array.from(s).sort();
  }, [t.rows]);

  const projectOptions = useMemo(() => {
    const s = new Set<string>();
    for (const r of t.rows) if (r.project) s.add(r.project);
    return Array.from(s).sort();
  }, [t.rows]);

  const onAddTask = () => {
    const row = t.addTask(newTaskTitle);
    if (row) {
      setNewTaskTitle("");
      setDetailTab("preview");
    }
  };
  const onAddNote = () => {
    const row = t.addNote(newNoteBody);
    if (row) {
      setNewNoteBody("");
      setDetailTab("preview");
    }
  };

  const statusLabel = t.storeOk
    ? `${V0_STORES.find((s) => s.id === storeId)?.label || "Local"} · ${t.rows.length} rows`
    : "Local store unavailable";

  return (
    <section className="todos-mode" aria-label="Todos workspace">
      <header className="todos-header">
        <div className="todos-header-row todos-header-row-top">
          <h2 className="todos-title">Todos &amp; Notes</h2>
          <div className="todos-status" aria-live="polite">
            <span className={t.storeOk ? "todos-status-dot ok" : "todos-status-dot fail"} />
            <span className="todos-status-label">{statusLabel}</span>
          </div>
        </div>

        <div className="todos-header-row todos-header-controls">
          <label className="todos-inline-field">
            <span className="todos-inline-label">Store</span>
            <select
              value={storeId}
              onChange={(e) => setStoreId(e.target.value)}
              aria-label="Worklog store"
            >
              {V0_STORES.map((s) => (
                <option key={s.id} value={s.id}>{s.label}</option>
              ))}
            </select>
          </label>

          <label className="todos-inline-field todos-search">
            <span className="todos-inline-label">Search</span>
            <input
              type="search"
              value={t.query}
              onChange={(e) => t.setQuery(e.target.value)}
              placeholder="Title or body…"
              aria-label="Search worklog"
            />
          </label>

          <label className="todos-inline-field">
            <span className="todos-inline-label">Sort</span>
            <select
              value={t.sort}
              onChange={(e) => t.setSort(e.target.value as TodoSort)}
              aria-label="Sort order"
            >
              {SORTS.map((s) => (
                <option key={s.id} value={s.id}>{s.label}</option>
              ))}
            </select>
          </label>

          <button
            type="button"
            className="todos-filter-btn"
            aria-expanded={filterOpen}
            onClick={() => setFilterOpen((v) => !v)}
          >
            Filter
            {(t.filter.tag || t.filter.project || t.filter.status) ? (
              <span className="todos-filter-dot" aria-hidden="true" />
            ) : null}
          </button>
        </div>

        {filterOpen ? (
          <FilterPopover
            filter={t.filter}
            setFilter={t.setFilter}
            tagOptions={tagOptions}
            projectOptions={projectOptions}
            onClose={() => setFilterOpen(false)}
          />
        ) : null}

        <nav className="todos-tabs" role="tablist" aria-label="Worklog tabs">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={t.tab === tab.id}
              className={t.tab === tab.id ? "todos-tab active" : "todos-tab"}
              onClick={() => t.setTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>

      <div className="todos-quickadd">
        <div className="todos-quickadd-row">
          <input
            type="text"
            placeholder="New task title…"
            value={newTaskTitle}
            onChange={(e) => setNewTaskTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") onAddTask(); }}
            aria-label="New task title"
          />
          <button type="button" className="todos-btn-primary" onClick={onAddTask}>
            Add Task
          </button>
        </div>
        <div className="todos-quickadd-row">
          <textarea
            placeholder="New note (Markdown ok)…"
            value={newNoteBody}
            onChange={(e) => setNewNoteBody(e.target.value)}
            rows={2}
            aria-label="New note body"
          />
          <button type="button" className="todos-btn-secondary" onClick={onAddNote}>
            Add Note
          </button>
        </div>
      </div>

      <div className="todos-body">
        <ul className="todos-list" aria-label="Worklog rows">
          {t.visibleRows.length === 0 ? (
            <li className="todos-empty">No rows in this tab.</li>
          ) : (
            t.visibleRows.map((row) => (
              <RowCard
                key={row.id}
                row={row}
                selected={t.selectedId === row.id}
                onSelect={() => {
                  t.selectRow(row.id);
                  setDetailTab("preview");
                }}
              />
            ))
          )}
        </ul>

        <aside className="todos-detail" aria-label="Selected row detail">
          {t.selectedRow ? (
            <DetailPanel
              row={t.selectedRow}
              tab={detailTab}
              setTab={setDetailTab}
              onArchive={() => t.archiveRow(t.selectedRow!.id)}
              onUpdate={(patch) => t.updateRow(t.selectedRow!.id, patch)}
              onToggleChecklist={(lineIndex) =>
                t.toggleChecklist(t.selectedRow!.id, lineIndex)
              }
            />
          ) : (
            <div className="todos-detail-empty">
              Select a row to see Preview / Raw / Checklist / History.
            </div>
          )}
        </aside>
      </div>
    </section>
  );
}

interface FilterPopoverProps {
  filter: TodoFilter;
  setFilter: (next: Partial<TodoFilter>) => void;
  tagOptions: string[];
  projectOptions: string[];
  onClose: () => void;
}

function FilterPopover({
  filter, setFilter, tagOptions, projectOptions, onClose,
}: FilterPopoverProps) {
  return (
    <div className="todos-filter-popover" role="region" aria-label="Filters">
      <label className="todos-inline-field">
        <span className="todos-inline-label">Tag</span>
        <select
          value={filter.tag}
          onChange={(e) => setFilter({ tag: e.target.value })}
        >
          <option value="">All tags</option>
          {tagOptions.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </label>
      <label className="todos-inline-field">
        <span className="todos-inline-label">Project</span>
        <select
          value={filter.project}
          onChange={(e) => setFilter({ project: e.target.value })}
        >
          <option value="">All projects</option>
          {projectOptions.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </label>
      <label className="todos-inline-field">
        <span className="todos-inline-label">Status</span>
        <select
          value={filter.status}
          onChange={(e) => setFilter({ status: e.target.value as TodoStatus | "" })}
        >
          <option value="">All status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className="todos-btn-secondary"
        onClick={() => setFilter({ tag: "", project: "", status: "" })}
      >
        Clear
      </button>
      <button type="button" className="todos-btn-secondary" onClick={onClose}>
        Close
      </button>
    </div>
  );
}

interface RowCardProps {
  row: TodoRow;
  selected: boolean;
  onSelect: () => void;
}

function RowCard({ row, selected, onSelect }: RowCardProps) {
  const progress = checklistProgress(row.body);
  const preview = row.body.split("\n").find((l) => l.trim()) || "";
  return (
    <li
      className={selected ? "todos-row selected" : "todos-row"}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="todos-row-head">
        <span className="todos-row-glyph" aria-hidden="true">
          {kindGlyph(row.kind)}
        </span>
        <span className="todos-row-title">{row.title}</span>
        {progress.total > 0 ? (
          <span className="todos-row-progress" title="Checklist progress">
            {progress.done}/{progress.total}
          </span>
        ) : null}
        <span className="todos-row-time">{relativeTime(row.updated_at)}</span>
      </div>
      {preview ? (
        <div className="todos-row-preview">{preview.slice(0, 160)}</div>
      ) : null}
      {row.tags.length || row.project ? (
        <div className="todos-row-chips">
          {row.project ? (
            <span className="todos-chip todos-chip-project">{row.project}</span>
          ) : null}
          {row.tags.map((tag) => (
            <span key={tag} className="todos-chip">#{tag}</span>
          ))}
        </div>
      ) : null}
    </li>
  );
}

interface DetailPanelProps {
  row: TodoRow;
  tab: DetailTab;
  setTab: (t: DetailTab) => void;
  onArchive: () => void;
  onUpdate: (patch: Partial<TodoRow>) => void;
  onToggleChecklist: (lineIndex: number) => void;
}

function DetailPanel({
  row, tab, setTab, onArchive, onUpdate, onToggleChecklist,
}: DetailPanelProps) {
  const checklistItems = parseChecklist(row.body);
  return (
    <div className="todos-detail-inner">
      <header className="todos-detail-head">
        <h3 className="todos-detail-title">{row.title}</h3>
        <div className="todos-detail-meta">
          <span>{row.kind}</span>
          <span>{row.status}</span>
          <span>{relativeTime(row.updated_at)}</span>
        </div>
      </header>

      <nav className="todos-detail-tabs" role="tablist">
        {(["preview", "raw", "checklist", "history"] as DetailTab[]).map((id) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            className={tab === id ? "todos-detail-tab active" : "todos-detail-tab"}
            onClick={() => setTab(id)}
          >
            {id.charAt(0).toUpperCase() + id.slice(1)}
          </button>
        ))}
      </nav>

      <div className="todos-detail-pane">
        {tab === "preview" ? (
          <pre className="todos-detail-preview">{row.body || "(empty)"}</pre>
        ) : null}
        {tab === "raw" ? (
          <textarea
            className="todos-detail-raw"
            value={row.body}
            onChange={(e) => onUpdate({ body: e.target.value })}
            rows={16}
          />
        ) : null}
        {tab === "checklist" ? (
          checklistItems.length === 0 ? (
            <div className="todos-detail-empty-pane">
              No checklist items. Add lines like <code>- [ ] do thing</code>
              {" "}in the Raw tab.
            </div>
          ) : (
            <ul className="todos-checklist">
              {checklistItems.map((item) => (
                <li key={item.lineIndex} className="todos-checklist-item">
                  <label>
                    <input
                      type="checkbox"
                      checked={item.done}
                      onChange={() => onToggleChecklist(item.lineIndex)}
                    />
                    <span className={item.done ? "todos-checklist-done" : ""}>
                      {item.text}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )
        ) : null}
        {tab === "history" ? (
          <div className="todos-detail-empty-pane">
            No history yet. A future slice will record edit/sync events here.
          </div>
        ) : null}
      </div>

      <footer className="todos-detail-actions">
        {row.status !== "archived" ? (
          <button
            type="button"
            className="todos-btn-secondary"
            onClick={onArchive}
          >
            Archive
          </button>
        ) : null}
      </footer>
    </div>
  );
}
