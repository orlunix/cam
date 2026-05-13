import { useCallback, useEffect, useRef, useState } from "react";
import type { CamBackend } from "./camBackend";
import type { AgentSummary, BackendHealth, MessageSummary } from "./types";

const POLL_INTERVAL_MS = 5000;

export interface UseAgents {
  health: BackendHealth;
  agents: AgentSummary[];
  /**
   * Inbox is dormant in V1 — the default UI does not render messages — but
   * polling stays wired so the dormant MessagesPanel can be revived later
   * without re-plumbing the hook. Failures are swallowed and never break the
   * agent list.
   */
  inbox: MessageSummary[];
  selectedAgentId: string;
  selectAgent: (id: string) => void;
  selectedAgent: AgentSummary | undefined;
  refresh: () => Promise<void>;
  refreshError: string;
  clearRefreshError: () => void;
  refreshing: boolean;
}

export function useAgents(backend: CamBackend): UseAgents {
  const [health, setHealth] = useState<BackendHealth>({ ok: false });
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [inbox, setInbox] = useState<MessageSummary[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");
  const [refreshError, setRefreshError] = useState<string>("");
  const [refreshing, setRefreshing] = useState(false);
  const inflightRef = useRef(false);

  const clearRefreshError = useCallback(() => {
    // Suppress the current refresh error until the next refresh produces a new
    // error or a successful refresh implicitly clears it. This lets ErrorBanner
    // dismiss work for both UI-triggered and refresh-triggered failures.
    setRefreshError("");
  }, []);

  const refresh = useCallback(async () => {
    if (inflightRef.current) return;
    inflightRef.current = true;
    setRefreshing(true);
    try {
      const nextHealth = await backend.health();
      setHealth(nextHealth);

      let nextAgents: AgentSummary[] = [];
      if (nextHealth.ok) {
        try {
          nextAgents = await backend.listAgents();
          setRefreshError("");
        } catch (e) {
          setRefreshError(e instanceof Error ? e.message : String(e));
        }
      } else {
        setRefreshError(nextHealth.error || "backend unavailable");
      }
      setAgents(nextAgents);

      // Keep current selection if it still exists; otherwise default to first.
      setSelectedAgentId((prev) => {
        if (prev && nextAgents.some((a) => a.id === prev)) return prev;
        return nextAgents[0]?.id || "";
      });

      // Dormant scaffold: inbox is polled but the V1 UI does not render it.
      // Failures must not break the core loop.
      try {
        setInbox(await backend.listInbox());
      } catch {
        setInbox([]);
      }
    } finally {
      inflightRef.current = false;
      setRefreshing(false);
    }
  }, [backend]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const selectedAgent = agents.find((a) => a.id === selectedAgentId);

  return {
    health,
    agents,
    inbox,
    selectedAgentId,
    selectAgent: setSelectedAgentId,
    selectedAgent,
    refresh,
    refreshError,
    clearRefreshError,
    refreshing,
  };
}
