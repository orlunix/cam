export type BackendProfile =
  | { kind: "local"; camcPath?: string }
  | { kind: "wsl"; distro?: string; camcPath?: string }
  | {
      kind: "ssh";
      host: string;
      user?: string;
      port?: number;
      camcPath?: string;
    };

export interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

export interface BackendHealth {
  ok: boolean;
  version?: string;
  error?: string;
  checkedAt?: string;
}

export interface AgentTask {
  name?: string;
  tool?: string;
  prompt?: string;
  tags?: string[];
}

export interface AgentSummary {
  id: string;
  task?: AgentTask;
  status?: string;
  state?: string;
  context_path?: string;
  context_name?: string;
  tmux_session?: string;
  hostname?: string;
  started_at?: string;
  completed_at?: string | null;
  exit_reason?: string | null;
}

export interface RunAgentRequest {
  name: string;
  prompt: string;
  path: string;
  tool: "claude" | "codex" | "cursor";
}

export interface SendOptions {
  /** When false, append `--no-enter` so the text is buffered without submitting. Default true. */
  sendEnter?: boolean;
}

export type AgentKey = "Enter" | "Escape" | "C-c" | "C-d" | string;

export interface CaptureResult {
  agentId: string;
  output: string;
}

export interface MessageSummary {
  msg_id: string;
  seq?: number;
  from_name?: string;
  from_id?: string;
  to_name?: string;
  to_id?: string;
  /** Short snippet emitted by `camc msg read --json` (preferred). */
  preview?: string;
  /** Full text — older builds; fall back to this if preview is absent. */
  text?: string;
  ts?: string;
  /** `camc msg read --json` returns this; older alias `is_read` kept for compatibility. */
  read?: boolean;
  is_read?: boolean;
}

export interface MessageTurn {
  msg_id: string;
  seq: number;
  from_name?: string;
  from_id?: string;
  to_name?: string;
  to_id?: string;
  text: string;
  ts?: string;
}

export interface MessageThread {
  msgId: string;
  turns: MessageTurn[];
}

export interface SendMessageRequest {
  to: string;
  text: string;
  expectReply?: boolean;
}

export interface SendMessageResult {
  msgId?: string;
  status?: string;
  raw: string;
}

export interface ReplyMessageRequest {
  msgId: string;
  text: string;
}

export interface ReplyMessageResult {
  msgId?: string;
  seq?: number;
  mailbox?: string;
  raw: string;
}
