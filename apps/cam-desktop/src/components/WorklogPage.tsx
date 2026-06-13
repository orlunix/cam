import { useMemo, useState } from "react";

type WorklogTab = "inbox" | "tasks" | "notes" | "projects" | "archive";
type DetailTab = "preview" | "raw" | "checklist" | "history";
type WorklogType = "task" | "note";
type WorklogStatus = "open" | "active" | "done" | "archived";

interface WorklogItem {
  id: string;
  type: WorklogType;
  title: string;
  status: WorklogStatus;
  project: string;
  priority?: "P0" | "P1" | "P2" | "P3";
  tags: string[];
  updatedLabel: string;
  dueLabel?: string;
  body: string;
  checklist: { id: string; text: string; done: boolean }[];
  history: string[];
}

const TABS: { id: WorklogTab; label: string }[] = [
  { id: "inbox", label: "Inbox" },
  { id: "tasks", label: "Tasks" },
  { id: "notes", label: "Notes" },
  { id: "projects", label: "Projects" },
  { id: "archive", label: "Archive" },
];

const DETAIL_TABS: { id: DetailTab; label: string }[] = [
  { id: "preview", label: "Preview" },
  { id: "raw", label: "Raw" },
  { id: "checklist", label: "Checklist" },
  { id: "history", label: "History" },
];

const INITIAL_ITEMS: WorklogItem[] = [
  {
    id: "todo-20260610-layout",
    type: "task",
    title: "Fix Nodes layout",
    status: "open",
    project: "camui",
    priority: "P1",
    tags: ["ui", "desktop"],
    updatedLabel: "5m ago",
    dueLabel: "Jun 14",
    body:
      "Need align Nodes, Skills, and Todos pages on the same full-width rail. Keep the list structural and mobile-friendly.",
    checklist: [
      { id: "api", text: "Add API contract", done: false },
      { id: "ui", text: "Add outline UI", done: true },
      { id: "win", text: "Test Windows install", done: false },
    ],
    history: ["created from Inbox", "moved to project camui", "priority set to P1"],
  },
  {
    id: "note-20260610-design",
    type: "note",
    title: "Todo UI design",
    status: "open",
    project: "camui",
    tags: ["design"],
    updatedLabel: "1h ago",
    body:
      "Use a Notion-like row equals page model, but keep the default view as a todo outline. Markdown remains the source of truth.",
    checklist: [
      { id: "mobile", text: "Avoid hover-only actions", done: true },
      { id: "sheet", text: "Mobile detail can become a sheet", done: false },
    ],
    history: ["captured as note", "linked to project camui"],
  },
  {
    id: "todo-20260610-wrapper",
    type: "task",
    title: "Add todocli API wrapper",
    status: "active",
    project: "camui",
    priority: "P2",
    tags: ["backend"],
    updatedLabel: "2h ago",
    body:
      "Bundle todo.py like camc and call it with --config <workspace>/.cam/worklog. Keep Markdown files as source of truth.",
    checklist: [
      { id: "bundle", text: "Bundle todo.py", done: false },
      { id: "routes", text: "Expose context-scoped routes", done: false },
    ],
    history: ["created from implementation plan", "status set to active"],
  },
  {
    id: "todo-20260610-personal",
    type: "task",
    title: "Capture mobile interaction notes",
    status: "open",
    project: "personal",
    priority: "P3",
    tags: ["mobile"],
    updatedLabel: "yesterday",
    body: "Document bottom-sheet filters and full-screen item detail behavior for the mobile version.",
    checklist: [],
    history: ["created in Inbox"],
  },
];

function projectNames(items: WorklogItem[]) {
  return Array.from(new Set(items.map((item) => item.project))).sort();
}

function statusLabel(status: WorklogStatus) {
  if (status === "active") return "active";
  if (status === "done") return "done";
  if (status === "archived") return "archived";
  return "open";
}

function itemMatchesTab(item: WorklogItem, tab: WorklogTab) {
  if (tab === "tasks") return item.type === "task" && item.status !== "archived";
  if (tab === "notes") return item.type === "note" && item.status !== "archived";
  if (tab === "archive") return item.status === "archived";
  if (tab === "projects") return item.status !== "archived";
  return item.status !== "archived";
}

function itemMarkdown(item: WorklogItem) {
  const tags = item.tags.map((tag) => `"${tag}"`).join(", ");
  return `---\nid: ${item.id}\ntype: ${item.type}\ntitle: ${item.title}\nstatus: ${item.status}\nproject: ${item.project}\npriority: ${item.priority || ""}\ntags: [${tags}]\n---\n\n${item.body}`;
}

export function WorklogPage() {
  const [items, setItems] = useState<WorklogItem[]>(INITIAL_ITEMS);
  const [activeTab, setActiveTab] = useState<WorklogTab>("inbox");
  const [detailTab, setDetailTab] = useState<DetailTab>("preview");
  const [selectedId, setSelectedId] = useState(INITIAL_ITEMS[0]?.id || "");
  const [query, setQuery] = useState("");
  const [projectFilter, setProjectFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("open");
  const [sortMode, setSortMode] = useState("updated");

  const projects = useMemo(() => projectNames(items), [items]);

  const visibleItems = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = items.filter((item) => {
      if (!itemMatchesTab(item, activeTab)) return false;
      if (projectFilter !== "all" && item.project !== projectFilter) return false;
      if (statusFilter !== "all" && item.status !== statusFilter) return false;
      if (!q) return true;
      return [item.title, item.body, item.project, ...item.tags]
        .join(" ")
        .toLowerCase()
        .includes(q);
    });
    return filtered.sort((a, b) => {
      if (sortMode === "priority") return (a.priority || "P9").localeCompare(b.priority || "P9");
      if (sortMode === "title") return a.title.localeCompare(b.title);
      return INITIAL_ITEMS.findIndex((item) => item.id === a.id) - INITIAL_ITEMS.findIndex((item) => item.id === b.id);
    });
  }, [activeTab, items, projectFilter, query, sortMode, statusFilter]);

  const grouped = useMemo(() => {
    return visibleItems.reduce<Record<string, WorklogItem[]>>((acc, item) => {
      acc[item.project] = acc[item.project] || [];
      acc[item.project].push(item);
      return acc;
    }, {});
  }, [visibleItems]);

  const selectedItem = items.find((item) => item.id === selectedId) || visibleItems[0];

  const updateItemStatus = (id: string, status: WorklogStatus) => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, status } : item)));
  };

  const addItem = (type: WorklogType) => {
    const id = `${type}-${Date.now()}`;
    const newItem: WorklogItem = {
      id,
      type,
      title: type === "task" ? "New task" : "New note",
      status: "open",
      project: projectFilter === "all" ? "inbox" : projectFilter,
      priority: type === "task" ? "P2" : undefined,
      tags: [],
      updatedLabel: "now",
      body: type === "task" ? "Describe the task in Markdown." : "Write the note in Markdown.",
      checklist: [],
      history: ["created locally in V0 UI"],
    };
    setItems((current) => [newItem, ...current]);
    setSelectedId(id);
    setDetailTab("raw");
  };

  return (
    <section className="worklog-page">
      <header className="worklog-hero">
        <div>
          <h2>Todos</h2>
          <p className="muted">
            Structured Markdown worklog for tasks, notes, checklists, and project history.
          </p>
        </div>
        <div className="worklog-actions">
          <button type="button" onClick={() => addItem("note")}>New note</button>
          <button type="button" onClick={() => addItem("task")}>New task</button>
        </div>
      </header>

      <div className="worklog-card">
        <nav className="worklog-tabs" aria-label="Todos views">
          {TABS.map((tab) => (
            <button
              type="button"
              key={tab.id}
              className={tab.id === activeTab ? "active" : ""}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </nav>

        <section className="worklog-store-card" aria-label="Todo store">
          <div>
            <span className="worklog-label">Context</span>
            <strong>Current workspace context</strong>
          </div>
          <div>
            <span className="worklog-label">Store</span>
            <code>/workspace/.cam/worklog</code>
          </div>
          <div className="worklog-store-actions">
            <button type="button">Check</button>
            <button type="button">Initialize</button>
            <button type="button">Refresh</button>
          </div>
        </section>

        <section className="worklog-controls" aria-label="Todo filters">
          <label>
            Search
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search todos, notes, tags..."
            />
          </label>
          <label>
            Project
            <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)}>
              <option value="all">All</option>
              {projects.map((project) => (
                <option value={project} key={project}>{project}</option>
              ))}
            </select>
          </label>
          <label>
            Status
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="all">All</option>
              <option value="open">Open</option>
              <option value="active">Active</option>
              <option value="done">Done</option>
              <option value="archived">Archived</option>
            </select>
          </label>
          <label>
            Sort
            <select value={sortMode} onChange={(e) => setSortMode(e.target.value)}>
              <option value="updated">Updated</option>
              <option value="priority">Priority</option>
              <option value="title">Title</option>
            </select>
          </label>
        </section>

        <section className="worklog-outline" aria-label="Structured todos and notes">
          {Object.keys(grouped).length === 0 ? (
            <div className="empty small">No matching items</div>
          ) : (
            Object.entries(grouped).map(([project, projectItems]) => (
              <article className="worklog-project" key={project}>
                <header>
                  <button type="button" className="worklog-project-toggle">v</button>
                  <h3>{project}</h3>
                  <span className="muted small">{projectItems.length} item(s)</span>
                </header>
                <div className="worklog-item-list">
                  {projectItems.map((item) => (
                    <button
                      type="button"
                      className={item.id === selectedItem?.id ? "worklog-row active" : "worklog-row"}
                      key={item.id}
                      onClick={() => {
                        setSelectedId(item.id);
                        setDetailTab("preview");
                      }}
                    >
                      <span className="worklog-row-kind">{item.type === "task" ? "[ ]" : "Note"}</span>
                      <span className="worklog-row-main">
                        <span className="worklog-row-title">
                          {item.priority ? <strong>{item.priority}</strong> : null}
                          {item.title}
                        </span>
                        <span className="row-meta">
                          <span>{statusLabel(item.status)}</span>
                          <span className="dot-sep">·</span>
                          <span>updated {item.updatedLabel}</span>
                          {item.dueLabel ? <span className="dot-sep">·</span> : null}
                          {item.dueLabel ? <span>due {item.dueLabel}</span> : null}
                        </span>
                      </span>
                      <span className="worklog-tags">
                        {item.tags.map((tag) => (
                          <span className="worklog-tag" key={tag}>#{tag}</span>
                        ))}
                      </span>
                    </button>
                  ))}
                </div>
              </article>
            ))
          )}
        </section>

        {selectedItem ? (
          <section className="worklog-detail" aria-label="Selected todo detail">
            <header>
              <div>
                <h3>{selectedItem.title}</h3>
                <p className="muted small">
                  {selectedItem.type} · {selectedItem.project} · {statusLabel(selectedItem.status)}
                </p>
              </div>
              <div className="worklog-detail-actions">
                <button type="button" onClick={() => updateItemStatus(selectedItem.id, "active")}>Start</button>
                <button type="button" onClick={() => updateItemStatus(selectedItem.id, "done")}>Done</button>
                <button type="button" onClick={() => updateItemStatus(selectedItem.id, "archived")}>Archive</button>
              </div>
            </header>
            <dl className="worklog-properties">
              <div><dt>Project</dt><dd>{selectedItem.project}</dd></div>
              <div><dt>Priority</dt><dd>{selectedItem.priority || "-"}</dd></div>
              <div><dt>Tags</dt><dd>{selectedItem.tags.length ? selectedItem.tags.join(", ") : "-"}</dd></div>
              <div><dt>Due</dt><dd>{selectedItem.dueLabel || "-"}</dd></div>
            </dl>
            <nav className="worklog-detail-tabs" aria-label="Todo detail tabs">
              {DETAIL_TABS.map((tab) => (
                <button
                  type="button"
                  key={tab.id}
                  className={tab.id === detailTab ? "active" : ""}
                  onClick={() => setDetailTab(tab.id)}
                >
                  {tab.label}
                </button>
              ))}
            </nav>
            <div className="worklog-detail-body">
              {detailTab === "preview" ? <p>{selectedItem.body}</p> : null}
              {detailTab === "raw" ? <pre>{itemMarkdown(selectedItem)}</pre> : null}
              {detailTab === "checklist" ? (
                <ul className="worklog-checklist">
                  {selectedItem.checklist.length ? selectedItem.checklist.map((item) => (
                    <li key={item.id}>{item.done ? "[x]" : "[ ]"} {item.text}</li>
                  )) : <li>No checklist items yet.</li>}
                </ul>
              ) : null}
              {detailTab === "history" ? (
                <ul className="worklog-history">
                  {selectedItem.history.map((entry) => <li key={entry}>{entry}</li>)}
                </ul>
              ) : null}
            </div>
          </section>
        ) : null}
      </div>
    </section>
  );
}
