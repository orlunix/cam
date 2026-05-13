import { useState } from "react";
import type { CamBackend } from "../lib/camBackend";
import type { AgentSummary, MessageSummary } from "../lib/types";
import { RunAgentForm } from "./RunAgentForm";
import { MessagesPanel } from "./MessagesPanel";

interface Props {
  backend: CamBackend;
  inbox: MessageSummary[];
  selectedAgent: AgentSummary | undefined;
  onError: (message: string) => void;
  onChanged: () => void;
}

type Tab = "messages" | "run";

function unreadCount(inbox: MessageSummary[]): number {
  return inbox.reduce((n, m) => {
    const read = typeof m.read === "boolean" ? m.read : Boolean(m.is_read);
    return read ? n : n + 1;
  }, 0);
}

export function AdvancedPanel({
  backend,
  inbox,
  selectedAgent,
  onError,
  onChanged,
}: Props) {
  const [tab, setTab] = useState<Tab>("messages");
  const unread = unreadCount(inbox);

  return (
    <div className="advanced">
      <div className="advanced-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "messages"}
          className={tab === "messages" ? "active" : ""}
          onClick={() => setTab("messages")}
        >
          Messages
          {unread > 0 ? <span className="badge">{unread}</span> : null}
        </button>
        <button
          role="tab"
          aria-selected={tab === "run"}
          className={tab === "run" ? "active" : ""}
          onClick={() => setTab("run")}
        >
          Run agent
        </button>
        <span className="advanced-note muted small">advanced</span>
      </div>
      {tab === "messages" ? (
        <MessagesPanel
          backend={backend}
          inbox={inbox}
          selectedAgent={selectedAgent}
          onError={onError}
          onMessagesChanged={onChanged}
        />
      ) : (
        <section className="panel run-form">
          <RunAgentForm backend={backend} onError={onError} onRan={onChanged} />
        </section>
      )}
    </div>
  );
}
