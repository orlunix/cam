import { useCallback, useState } from "react";
import { useProfile } from "./lib/useProfile";
import { useAgents } from "./lib/useAgents";
import { agentName, statusText } from "./lib/agentDisplay";
import { ConnectionPanel } from "./components/ConnectionPanel";
import { AgentList } from "./components/AgentList";
import { AgentOutputPane } from "./components/AgentOutputPane";
import { AgentComposer } from "./components/AgentComposer";
import { ErrorBanner } from "./components/ErrorBanner";

// V1 surface per docs/desktop-ui-spec.md: connection + agent list + selected
// output + composer/keys. AgentMetadataPanel, AdvancedPanel, RunAgentForm and
// MessagesPanel exist on disk but are intentionally not mounted here — they are
// dormant code awaiting later milestones.
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
        <AgentList
          agents={agents}
          selectedAgentId={selectedAgentId}
          onSelect={selectAgent}
        />
      </aside>

      <section className="workspace">
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
      </section>
    </main>
  );
}
