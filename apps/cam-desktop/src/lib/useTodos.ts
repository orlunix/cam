import { useCallback, useEffect, useMemo, useState } from "react";

// V0 worklog data model — mock-safe, localStorage-backed. The shape is
// chosen to match a future Markdown-on-disk schema so a later slice can
// swap localStorage for a Hub-mediated `todocli` API without renderer
// changes (see CAM-DESK-TODOS-016).

export type TodoKind = "task" | "note" | "project";
export type TodoStatus = "inbox" | "open" | "done" | "archived";

export interface TodoRow {
  id: string;
  kind: TodoKind;
  title: string;
  body: string;           // Markdown — checklist parsed from `- [ ]`/`- [x]`
  status: TodoStatus;
  tags: string[];
  project: string;
  created_at: string;     // ISO 8601
  updated_at: string;     // ISO 8601
}

export type TodoTab = "inbox" | "tasks" | "notes" | "projects" | "archive";
export type TodoSort = "updated" | "created" | "title";

export interface TodoFilter {
  tag: string;
  project: string;
  status: TodoStatus | "";
}

const STORE_KEY = "cam-desktop.todos.v0";

function _now(): string {
  return new Date().toISOString();
}

function _id(): string {
  const r = Math.random().toString(36).slice(2, 8);
  return `${Date.now().toString(36)}-${r}`;
}

function _load(): TodoRow[] {
  if (typeof window === "undefined") return _seed();
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    if (!raw) return _seed();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return _seed();
    return parsed.filter(_isRow);
  } catch {
    return _seed();
  }
}

function _save(rows: TodoRow[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(rows));
  } catch {
    // localStorage may be unavailable (e.g. private mode, quota); fall
    // through silently — the in-memory state still works for the session.
  }
}

function _isRow(r: unknown): r is TodoRow {
  if (!r || typeof r !== "object") return false;
  const o = r as Record<string, unknown>;
  return (
    typeof o.id === "string" &&
    typeof o.title === "string" &&
    typeof o.body === "string" &&
    (o.kind === "task" || o.kind === "note" || o.kind === "project")
  );
}

function _seed(): TodoRow[] {
  // Three small seed rows so the empty state is illustrative on first
  // open. The user can delete or archive them; nothing else depends on
  // these specific ids.
  const t = _now();
  return [
    {
      id: _id(),
      kind: "task",
      title: "Review last week's agent runs",
      body: "- [ ] Inspect failures\n- [x] Sample three transcripts\n- [ ] File one follow-up",
      status: "open",
      tags: ["review"],
      project: "weekly",
      created_at: t,
      updated_at: t,
    },
    {
      id: _id(),
      kind: "note",
      title: "Worklog model sketch",
      body: "Markdown rows backed by `todocli`. V0 is local-only.\nA future slice will sync over the embedded Hub.",
      status: "open",
      tags: ["spec"],
      project: "todos",
      created_at: t,
      updated_at: t,
    },
    {
      id: _id(),
      kind: "project",
      title: "TODOS-V0",
      body: "Cluster of items related to landing the Todos page.",
      status: "open",
      tags: [],
      project: "",
      created_at: t,
      updated_at: t,
    },
  ];
}

export interface UseTodos {
  rows: TodoRow[];
  tab: TodoTab;
  setTab: (t: TodoTab) => void;
  query: string;
  setQuery: (q: string) => void;
  sort: TodoSort;
  setSort: (s: TodoSort) => void;
  filter: TodoFilter;
  setFilter: (next: Partial<TodoFilter>) => void;
  selectedId: string | null;
  selectRow: (id: string | null) => void;
  visibleRows: TodoRow[];
  selectedRow: TodoRow | null;
  addTask: (title: string) => TodoRow | null;
  addNote: (body: string, title?: string) => TodoRow | null;
  updateRow: (id: string, patch: Partial<TodoRow>) => void;
  archiveRow: (id: string) => void;
  toggleChecklist: (id: string, lineIndex: number) => void;
  /** True iff persistent storage is functioning (localStorage usable). */
  storeOk: boolean;
}

const _EMPTY_FILTER: TodoFilter = { tag: "", project: "", status: "" };

function _rowMatchesTab(row: TodoRow, tab: TodoTab): boolean {
  if (tab === "archive") return row.status === "archived";
  if (row.status === "archived") return false;
  if (tab === "projects") return row.kind === "project";
  if (tab === "notes")    return row.kind === "note";
  if (tab === "tasks")    return row.kind === "task";
  // Inbox = newly captured items still tagged `inbox` (or fresh rows
  // without a status). Excludes projects (they live in Projects).
  return row.status === "inbox" && row.kind !== "project";
}

function _rowMatchesFilter(row: TodoRow, f: TodoFilter, q: string): boolean {
  if (f.tag && !row.tags.includes(f.tag)) return false;
  if (f.project && row.project !== f.project) return false;
  if (f.status && row.status !== f.status) return false;
  if (q) {
    const needle = q.toLowerCase();
    const hay = `${row.title}\n${row.body}`.toLowerCase();
    if (!hay.includes(needle)) return false;
  }
  return true;
}

function _sortRows(rows: TodoRow[], sort: TodoSort): TodoRow[] {
  const copy = rows.slice();
  if (sort === "title") {
    copy.sort((a, b) => a.title.localeCompare(b.title));
  } else if (sort === "created") {
    copy.sort((a, b) => b.created_at.localeCompare(a.created_at));
  } else {
    copy.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  }
  return copy;
}

export function useTodos(): UseTodos {
  const [rows, setRows] = useState<TodoRow[]>(() => _load());
  const [tab, setTab] = useState<TodoTab>("inbox");
  const [query, setQuery] = useState<string>("");
  const [sort, setSort] = useState<TodoSort>("updated");
  const [filter, setFilterState] = useState<TodoFilter>(_EMPTY_FILTER);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [storeOk, setStoreOk] = useState<boolean>(true);

  useEffect(() => {
    try {
      _save(rows);
      setStoreOk(true);
    } catch {
      setStoreOk(false);
    }
  }, [rows]);

  const setFilter = useCallback((next: Partial<TodoFilter>) => {
    setFilterState((prev) => ({ ...prev, ...next }));
  }, []);

  const selectRow = useCallback((id: string | null) => {
    setSelectedId(id);
  }, []);

  const visibleRows = useMemo(() => {
    const filtered = rows.filter(
      (r) => _rowMatchesTab(r, tab) && _rowMatchesFilter(r, filter, query),
    );
    return _sortRows(filtered, sort);
  }, [rows, tab, filter, query, sort]);

  const selectedRow = useMemo(() => {
    if (!selectedId) return null;
    return rows.find((r) => r.id === selectedId) || null;
  }, [rows, selectedId]);

  const addTask = useCallback((title: string): TodoRow | null => {
    const t = title.trim();
    if (!t) return null;
    const now = _now();
    const row: TodoRow = {
      id: _id(),
      kind: "task",
      title: t,
      body: "- [ ] " + t,
      status: "inbox",
      tags: [],
      project: "",
      created_at: now,
      updated_at: now,
    };
    setRows((prev) => [row, ...prev]);
    setSelectedId(row.id);
    return row;
  }, []);

  const addNote = useCallback((body: string, title?: string): TodoRow | null => {
    const b = body.trim();
    if (!b) return null;
    const now = _now();
    const t = (title || b.split("\n", 1)[0] || "Untitled note").trim().slice(0, 80);
    const row: TodoRow = {
      id: _id(),
      kind: "note",
      title: t,
      body: b,
      status: "inbox",
      tags: [],
      project: "",
      created_at: now,
      updated_at: now,
    };
    setRows((prev) => [row, ...prev]);
    setSelectedId(row.id);
    return row;
  }, []);

  const updateRow = useCallback((id: string, patch: Partial<TodoRow>) => {
    setRows((prev) =>
      prev.map((r) =>
        r.id === id ? { ...r, ...patch, updated_at: _now() } : r,
      ),
    );
  }, []);

  const archiveRow = useCallback((id: string) => {
    setRows((prev) =>
      prev.map((r) =>
        r.id === id ? { ...r, status: "archived", updated_at: _now() } : r,
      ),
    );
  }, []);

  const toggleChecklist = useCallback((id: string, lineIndex: number) => {
    setRows((prev) =>
      prev.map((r) => {
        if (r.id !== id) return r;
        const lines = r.body.split("\n");
        if (lineIndex < 0 || lineIndex >= lines.length) return r;
        const line = lines[lineIndex];
        if (/^\s*-\s*\[\s*\]\s+/.test(line)) {
          lines[lineIndex] = line.replace(/\[\s*\]/, "[x]");
        } else if (/^\s*-\s*\[x\]\s+/i.test(line)) {
          lines[lineIndex] = line.replace(/\[x\]/i, "[ ]");
        } else {
          return r;
        }
        return { ...r, body: lines.join("\n"), updated_at: _now() };
      }),
    );
  }, []);

  return {
    rows,
    tab,
    setTab,
    query,
    setQuery,
    sort,
    setSort,
    filter,
    setFilter,
    selectedId,
    selectRow,
    visibleRows,
    selectedRow,
    addTask,
    addNote,
    updateRow,
    archiveRow,
    toggleChecklist,
    storeOk,
  };
}

// Pure helpers exposed for the detail panel + tests.
export function parseChecklist(body: string): {
  lineIndex: number;
  text: string;
  done: boolean;
}[] {
  const out: { lineIndex: number; text: string; done: boolean }[] = [];
  const lines = body.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const m = line.match(/^\s*-\s*\[([ xX])\]\s+(.*)$/);
    if (m) {
      out.push({
        lineIndex: i,
        text: m[2].trim(),
        done: m[1].toLowerCase() === "x",
      });
    }
  }
  return out;
}

export function checklistProgress(body: string): { done: number; total: number } {
  const items = parseChecklist(body);
  return {
    done: items.filter((x) => x.done).length,
    total: items.length,
  };
}

export function relativeTime(iso: string): string {
  try {
    const dt = new Date(iso).getTime();
    if (!Number.isFinite(dt)) return "";
    const diff = Math.max(0, Date.now() - dt) / 1000;
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  } catch {
    return "";
  }
}
