import type { AgentSummary } from "../lib/types";
import { agentName, agentTool, statusText } from "../lib/agentDisplay";

interface Props {
  agents: AgentSummary[];
  selectedAgentId: string;
  onSelect: (id: string) => void;
}

export function AgentList({ agents, selectedAgentId, onSelect }: Props) {
  return (
    <section className="panel fill agents">
      <div className="panel-header">
        <h2>Agents</h2>
        <span className="muted">{agents.length}</span>
      </div>
      <div className="list" role="listbox" aria-label="Agents">
        {agents.length === 0 ? (
          <div className="empty small">No agents</div>
        ) : (
          agents.map((agent) => (
            <button
              type="button"
              role="option"
              aria-selected={agent.id === selectedAgentId}
              className={agent.id === selectedAgentId ? "row active" : "row"}
              key={agent.id}
              onClick={() => onSelect(agent.id)}
            >
              <span className="row-title">{agentName(agent)}</span>
              <small className="row-meta">
                <span>{agentTool(agent)}</span>
                <span className="dot-sep">·</span>
                <span>{statusText(agent)}</span>
              </small>
              {agent.context_path ? (
                <small className="row-path" title={agent.context_path}>
                  {agent.context_path}
                </small>
              ) : null}
            </button>
          ))
        )}
      </div>
    </section>
  );
}
