import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CamBackend } from "../lib/camBackend";
import type { AgentSummary } from "../lib/types";

interface Props {
  backend: CamBackend;
  agent: AgentSummary | undefined;
  onError: (message: string) => void;
}

const NEAR_BOTTOM_PX = 80;
const REFRESH_INTERVAL_MS = 3000;
const DEFAULT_LINES = 200;

export function AgentOutputPane({ backend, agent, onError }: Props) {
  const [output, setOutput] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<string>("");

  const scrollRef = useRef<HTMLPreElement | null>(null);
  const isNearBottomRef = useRef(true);
  const inflightRef = useRef(false);

  const agentId = agent?.id || "";

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    isNearBottomRef.current = distance <= NEAR_BOTTOM_PX;
  }, []);

  const refresh = useCallback(async () => {
    if (!agentId) return;
    if (inflightRef.current) return;
    inflightRef.current = true;
    setLoading(true);
    try {
      const result = await backend.captureAgent(agentId, DEFAULT_LINES);
      setOutput(result.output);
      setLastUpdated(new Date().toLocaleTimeString());
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      inflightRef.current = false;
      setLoading(false);
    }
  }, [agentId, backend, onError]);

  // Reset and load whenever the selected agent changes.
  useEffect(() => {
    setOutput("");
    setLastUpdated("");
    isNearBottomRef.current = true;
    if (agentId) void refresh();
  }, [agentId, refresh]);

  // Optional auto-refresh.
  useEffect(() => {
    if (!agentId || !autoRefresh) return;
    const timer = window.setInterval(() => {
      void refresh();
    }, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [agentId, autoRefresh, refresh]);

  // Auto-scroll only when the user is already near the bottom.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (isNearBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [output]);

  if (!agent) {
    return (
      <section className="output-pane">
        <header className="output-toolbar">
          <span className="muted">No agent selected</span>
        </header>
        <div className="empty">Select an agent to view its tmux output.</div>
      </section>
    );
  }

  return (
    <section className="output-pane">
      <header className="output-toolbar">
        <div className="output-meta">
          <span className="muted">capture</span>
          {lastUpdated ? <span className="muted small">@ {lastUpdated}</span> : null}
        </div>
        <div className="output-actions">
          <label className="checkbox" title="Refresh capture every 3s">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            auto
          </label>
          <button onClick={() => void refresh()} disabled={loading}>
            {loading ? "..." : "Refresh"}
          </button>
        </div>
      </header>
      <pre
        ref={scrollRef}
        onScroll={onScroll}
        className="capture"
        aria-label="agent output"
      >
        {output || (loading ? "loading…" : "(empty)")}
      </pre>
    </section>
  );
}
