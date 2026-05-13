import type { AgentSummary } from "./types";

export function agentName(agent: AgentSummary): string {
  return agent.task?.name || agent.id;
}

export function agentTool(agent: AgentSummary): string {
  return agent.task?.tool || "agent";
}

export function statusText(agent: AgentSummary): string {
  return [agent.status, agent.state].filter(Boolean).join(" / ") || "unknown";
}

export function isRunning(agent: AgentSummary | undefined): boolean {
  if (!agent) return false;
  const s = (agent.status || "").toLowerCase();
  return s === "running" || s === "starting" || s === "pending";
}
