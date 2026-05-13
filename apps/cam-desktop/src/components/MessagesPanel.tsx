import { useEffect, useState } from "react";
import type { CamBackend } from "../lib/camBackend";
import type {
  AgentSummary,
  MessageSummary,
  MessageThread,
} from "../lib/types";

interface Props {
  backend: CamBackend;
  inbox: MessageSummary[];
  selectedAgent: AgentSummary | undefined;
  onError: (message: string) => void;
  onMessagesChanged: () => void;
}

function msgTitle(msg: MessageSummary): string {
  const from = msg.from_name || msg.from_id || "unknown";
  const seq = msg.seq ? `#${msg.seq}` : "";
  return `${msg.msg_id} ${seq} from ${from}`.trim();
}

function msgPreview(msg: MessageSummary): string {
  return msg.preview || msg.text || "";
}

function msgRead(msg: MessageSummary): boolean {
  if (typeof msg.read === "boolean") return msg.read;
  if (typeof msg.is_read === "boolean") return msg.is_read;
  return false;
}

function unreadCount(inbox: MessageSummary[]): number {
  return inbox.reduce((n, m) => (msgRead(m) ? n : n + 1), 0);
}

export function MessagesPanel({
  backend,
  inbox,
  selectedAgent,
  onError,
  onMessagesChanged,
}: Props) {
  const [selectedMsgId, setSelectedMsgId] = useState<string>("");
  const [thread, setThread] = useState<MessageThread | null>(null);
  const [composer, setComposer] = useState("");
  const [busy, setBusy] = useState(false);

  // If the inbox no longer contains the selected thread, clear it.
  useEffect(() => {
    if (!selectedMsgId) return;
    if (!inbox.some((m) => m.msg_id === selectedMsgId)) {
      setSelectedMsgId("");
      setThread(null);
    }
  }, [inbox, selectedMsgId]);

  async function selectMsg(msgId: string) {
    setBusy(true);
    try {
      setSelectedMsgId(msgId);
      setThread(await backend.readThread(msgId));
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!composer.trim()) return;
    setBusy(true);
    try {
      if (selectedMsgId) {
        await backend.replyMessage({ msgId: selectedMsgId, text: composer });
        setThread(await backend.readThread(selectedMsgId));
      } else if (selectedAgent) {
        await backend.sendMessage({ to: selectedAgent.id, text: composer });
      } else {
        return;
      }
      setComposer("");
      onMessagesChanged();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const canSend =
    composer.trim().length > 0 && (Boolean(selectedMsgId) || Boolean(selectedAgent));

  return (
    <section className="panel msg">
      <div className="panel-header">
        <h2>Messages</h2>
        <span className="muted">
          {unreadCount(inbox)} unread / {inbox.length}
        </span>
      </div>
      <div className="msg-grid">
        <div className="msg-list">
          <button
            type="button"
            className={!selectedMsgId ? "row active small" : "row small"}
            onClick={() => {
              setSelectedMsgId("");
              setThread(null);
            }}
          >
            <span>(send to selected agent)</span>
          </button>
          {inbox.map((msg) => {
            const cls = [
              "row",
              "small",
              msg.msg_id === selectedMsgId ? "active" : "",
              msgRead(msg) ? "read" : "unread",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <button
                type="button"
                className={cls}
                key={`${msg.msg_id}-${msg.seq || 0}`}
                onClick={() => void selectMsg(msg.msg_id)}
              >
                <span className="row-title">{msgTitle(msg)}</span>
                <small className="row-meta">{msgPreview(msg)}</small>
              </button>
            );
          })}
        </div>
        {thread ? (
          <div className="msg-thread">
            {thread.turns.map((turn) => (
              <article className="bubble" key={`${turn.msg_id}-${turn.seq}`}>
                <header>
                  <strong>{turn.from_name || turn.from_id || "unknown"}</strong>
                  <span>seq {turn.seq}</span>
                </header>
                <p>{turn.text}</p>
              </article>
            ))}
          </div>
        ) : null}
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <textarea
          value={composer}
          onChange={(e) => setComposer(e.target.value)}
          placeholder={
            selectedMsgId
              ? "Reply to this thread"
              : selectedAgent
                ? `Send async message to ${selectedAgent.task?.name || selectedAgent.id}`
                : "Select an agent or thread first"
          }
          rows={3}
        />
        <button disabled={busy || !canSend}>
          {selectedMsgId ? "Reply" : "Send"}
        </button>
      </form>
    </section>
  );
}
