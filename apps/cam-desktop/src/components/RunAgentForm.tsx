import { useState } from "react";
import type { CamBackend } from "../lib/camBackend";
import type { RunAgentRequest } from "../lib/types";

interface Props {
  backend: CamBackend;
  onError: (message: string) => void;
  onRan: () => void;
}

const INITIAL: RunAgentRequest = {
  name: "",
  path: "",
  prompt: "",
  tool: "claude",
};

export function RunAgentForm({ backend, onError, onRan }: Props) {
  const [req, setReq] = useState<RunAgentRequest>(INITIAL);
  const [busy, setBusy] = useState(false);

  return (
    <form
      className="stack"
      onSubmit={(e) => {
        e.preventDefault();
        if (!req.name.trim()) return;
        setBusy(true);
        backend
          .runAgent(req)
          .then(() => {
            setReq({ ...INITIAL });
            onRan();
          })
          .catch((err) => {
            onError(err instanceof Error ? err.message : String(err));
          })
          .finally(() => setBusy(false));
      }}
    >
      <input
        value={req.name}
        onChange={(e) => setReq({ ...req, name: e.target.value })}
        placeholder="name (required)"
      />
      <input
        value={req.path}
        onChange={(e) => setReq({ ...req, path: e.target.value })}
        placeholder="/path/to/workspace (optional)"
      />
      <select
        value={req.tool}
        onChange={(e) =>
          setReq({ ...req, tool: e.target.value as RunAgentRequest["tool"] })
        }
      >
        <option value="claude">claude</option>
        <option value="codex">codex</option>
        <option value="cursor">cursor</option>
      </select>
      <textarea
        value={req.prompt}
        onChange={(e) => setReq({ ...req, prompt: e.target.value })}
        placeholder="prompt (optional)"
      />
      <button disabled={busy || !req.name.trim()}>
        {busy ? "Starting…" : "Run agent"}
      </button>
    </form>
  );
}
