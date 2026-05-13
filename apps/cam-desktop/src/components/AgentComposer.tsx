import { useCallback, useRef, useState } from "react";
import type { CamBackend } from "../lib/camBackend";
import type { AgentKey, AgentSummary } from "../lib/types";
import { isRunning, statusText } from "../lib/agentDisplay";

interface Props {
  backend: CamBackend;
  agent: AgentSummary | undefined;
  onError: (message: string) => void;
}

const KEY_BUTTONS: Array<{ label: string; key: AgentKey; danger?: boolean }> = [
  { label: "Enter", key: "Enter" },
  { label: "Esc", key: "Escape" },
  { label: "Ctrl-C", key: "C-c", danger: true },
  { label: "Ctrl-D", key: "C-d", danger: true },
];

export function AgentComposer({ backend, agent, onError }: Props) {
  const [text, setText] = useState("");
  const [sendEnter, setSendEnter] = useState(true);
  const [busy, setBusy] = useState(false);
  const composingRef = useRef(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const running = isRunning(agent);
  // Disable input/key controls unless a running agent is selected. tmux send/key
  // against terminal agents either errors silently or no-ops, so we gate at the UI.
  const disabled = !agent || !running || busy;
  const placeholder = !agent
    ? "Select a running agent to send input."
    : !running
      ? `Agent is ${statusText(agent)} — send/keys disabled.`
      : "Send text to the selected agent. Enter sends, Shift+Enter newline.";

  const submitText = useCallback(async () => {
    if (!agent) return;
    const value = text;
    if (!value.trim()) return;
    setBusy(true);
    try {
      await backend.sendToAgent(agent.id, value, { sendEnter });
      setText("");
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      textareaRef.current?.focus();
    }
  }, [agent, backend, onError, sendEnter, text]);

  const submitKey = useCallback(
    async (key: AgentKey) => {
      if (!agent) return;
      setBusy(true);
      try {
        await backend.sendKey(agent.id, key);
      } catch (e) {
        onError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
        textareaRef.current?.focus();
      }
    },
    [agent, backend, onError],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key !== "Enter") return;
      // IME composition: never submit while composing.
      if (composingRef.current || e.nativeEvent.isComposing) return;
      // Shift+Enter / modifier+Enter: insert newline.
      if (e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return;
      e.preventDefault();
      void submitText();
    },
    [submitText],
  );

  return (
    <section className="composer-pane">
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          void submitText();
        }}
      >
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
          }}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          disabled={disabled}
          rows={3}
        />
        <div className="composer-actions">
          <label className="checkbox" title="Append Enter after the text (uncheck to buffer only)">
            <input
              type="checkbox"
              checked={sendEnter}
              onChange={(e) => setSendEnter(e.target.checked)}
            />
            send Enter
          </label>
          <button type="submit" disabled={disabled || !text.trim()}>
            Send
          </button>
        </div>
      </form>
      <div className="key-row" role="group" aria-label="Send special key">
        {KEY_BUTTONS.map((b) => (
          <button
            key={b.key}
            type="button"
            className={b.danger ? "danger" : undefined}
            disabled={disabled}
            onClick={() => void submitKey(b.key)}
            title={`Send ${b.label} to selected agent`}
          >
            {b.label}
          </button>
        ))}
      </div>
    </section>
  );
}
