import type {
  AgentKey,
  AgentSummary,
  BackendHealth,
  CaptureResult,
  MessageSummary,
  MessageThread,
  ReplyMessageRequest,
  ReplyMessageResult,
  RunAgentRequest,
  SendMessageRequest,
  SendMessageResult,
  SendOptions,
} from "./types";

/**
 * Typed boundary between React UI and CAM operations. The UI should call
 * domain methods only; never build argv or REST paths in components.
 *
 * Core P0 surface (must be reliable):
 *   health, listAgents, captureAgent, sendToAgent, sendKey
 *
 * Retained P0 surface (kept if backend supports it; failures are visible
 * but non-fatal to Core P0):
 *   runAgent, listInbox, readThread, sendMessage, replyMessage
 */
export interface CamBackend {
  // --- Core P0 ---
  health(): Promise<BackendHealth>;
  listAgents(): Promise<AgentSummary[]>;
  captureAgent(agentId: string, lines?: number): Promise<CaptureResult>;
  sendToAgent(agentId: string, text: string, opts?: SendOptions): Promise<void>;
  sendKey(agentId: string, key: AgentKey): Promise<void>;

  // --- Retained P0 ---
  runAgent(req: RunAgentRequest): Promise<void>;
  listInbox(): Promise<MessageSummary[]>;
  readThread(msgId: string): Promise<MessageThread>;
  sendMessage(req: SendMessageRequest): Promise<SendMessageResult>;
  replyMessage(req: ReplyMessageRequest): Promise<ReplyMessageResult>;
}
