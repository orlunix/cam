import { useCallback, useState } from "react";
import { useProfile } from "./lib/useProfile";
import { useAgents } from "./lib/useAgents";
import { agentName, statusText } from "./lib/agentDisplay";
import { ConnectionPanel } from "./components/ConnectionPanel";
import { AgentList } from "./components/AgentList";
import { AgentOutputPane } from "./components/AgentOutputPane";
import { AgentComposer } from "./components/AgentComposer";
import { ErrorBanner } from "./components/ErrorBanner";
import { TodosMode } from "./components/TodosMode";

type WorkspaceMode = "agents" | "todos";

// V1 surface per docs/desktop-ui-spec.md: connection + agent list + selected
// output + composer/keys. AgentMetadataPanel, AdvancedPanel, RunAgentForm and
// MessagesPanel exist on disk but are intentionally not mounted here — they are
// dormant code awaiting later milestones.
//
// CAM-DESK-TODOS-010..017 add a second top-level mode: Todos. The mode
// switch is a thin top-bar nav. Agent hooks (`useProfile`, `useAgents`)
// keep running across the switch so Agents-side polling state is
// preserved when the user toggles back.
export default function App() {
  const { profile, setProfile, backend } = useProfile();
  const {
    health,
    agents,
    selectedAgentId,
    selectAgent,
    selectedAgent,
    refresh,
    refreshError,
    clearRefreshError,
    refreshing,
  } = useAgents(backend);

  const [error, setError] = useState<string>("");
  const [mode, setMode] = useState<WorkspaceMode>("agents");

  const handleError = useCallback((message: string) => {
    setError(message);
  }, []);

  const visibleError = error || refreshError;
  const dismissError = useCallback(() => {
    setError("");
    clearRefreshError();
  }, [clearRefreshError]);

  const inlineMeta = selectedAgent
    ? [statusText(selectedAgent), selectedAgent.context_path]
        .filter(Boolean)
        .join(" · ")
    : "Select an agent on the left.";

  const modeNav = (
    <nav className="mode-switch" aria-label="Workspace mode">
      <button
        type="button"
        className={mode === "agents" ? "mode-switch-btn active" : "mode-switch-btn"}
        aria-pressed={mode === "agents"}
        onClick={() => setMode("agents")}
      >
        Agents
      </button>
      <button
        type="button"
        className={mode === "todos" ? "mode-switch-btn active" : "mode-switch-btn"}
        aria-pressed={mode === "todos"}
        onClick={() => setMode("todos")}
      >
        Todos
      </button>
    </nav>
  );

  return (
    <main className="app">
      <aside className="sidebar">
        <ConnectionPanel
          profile={profile}
          setProfile={setProfile}
          health={health}
          onRefresh={() => void refresh()}
          refreshing={refreshing}
        />
        {modeNav}
        {mode === "agents" ? (
          <AgentList
            agents={agents}
            selectedAgentId={selectedAgentId}
            onSelect={selectAgent}
          />
        ) : (
          <div className="sidebar-todos-stub">
            <p className="muted">
              Todos workspace is open in the main pane. Switch to Agents
              to see the agent list.
            </p>
          </div>
        )}
      </aside>

      <section className="workspace">
        {mode === "agents" ? (
          <>
            <header className="topbar">
              <div className="topbar-title">
                <h2>{selectedAgent ? agentName(selectedAgent) : "No agent selected"}</h2>
                <p className="muted">{inlineMeta}</p>
              </div>
            </header>

            {visibleError ? (
              <ErrorBanner message={visibleError} onDismiss={dismissError} />
            ) : null}

            <div className="content">
              <AgentOutputPane
                backend={backend}
                agent={selectedAgent}
                onError={handleError}
              />
              <AgentComposer
                backend={backend}
                agent={selectedAgent}
                onError={handleError}
              />
            </div>
          </>
        ) : (
          <TodosMode />
        )}
      </section>
    </main>
  );
}
