import type { AgentSummary } from "../lib/types";
import { agentName, agentTool, statusText } from "../lib/agentDisplay";

interface Props {
  agent: AgentSummary | undefined;
}

function field(label: string, value: string | null | undefined) {
  if (!value) return null;
  return (
    <div className="kv-row" key={label}>
      <span className="kv-key">{label}</span>
      <span className="kv-value" title={value}>
        {value}
      </span>
    </div>
  );
}

export function AgentMetadataPanel({ agent }: Props) {
  if (!agent) {
    return (
      <section className="panel metadata">
        <h2>Selected Agent</h2>
        <p className="muted">No agent selected</p>
      </section>
    );
  }

  return (
    <section className="panel metadata">
      <h2>{agentName(agent)}</h2>
      <div className="kv">
        {field("id", agent.id)}
        {field("tool", agentTool(agent))}
        {field("status", statusText(agent))}
        {field("context", agent.context_name)}
        {field("path", agent.context_path)}
        {field("host", agent.hostname)}
        {field("session", agent.tmux_session)}
        {field("started", agent.started_at)}
        {field("completed", agent.completed_at || undefined)}
        {field("exit_reason", agent.exit_reason || undefined)}
      </div>
    </section>
  );
}
