import { invoke } from "@tauri-apps/api/core";
import type { CamBackend } from "./camBackend";
import type {
  AgentKey,
  AgentSummary,
  BackendHealth,
  BackendProfile,
  CaptureResult,
  CommandResult,
  MessageSummary,
  MessageThread,
  MessageTurn,
  ReplyMessageRequest,
  ReplyMessageResult,
  RunAgentRequest,
  SendMessageRequest,
  SendMessageResult,
  SendOptions,
} from "./types";

const DEFAULT_TIMEOUT_MS = 30_000;

function parseKv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split(/\r?\n/)) {
    const idx = line.indexOf("=");
    if (idx <= 0) continue;
    out[line.slice(0, idx)] = line.slice(idx + 1);
  }
  return out;
}

function assertOk(result: CommandResult, args: string[]): CommandResult {
  if (result.code === 0 && !result.timedOut) return result;
  const detail = result.stderr.trim() || result.stdout.trim() || "no output";
  throw new Error(`camc ${args.join(" ")} failed: ${detail}`);
}

function normalizeThread(msgId: string, payload: unknown): MessageThread {
  if (Array.isArray(payload)) {
    return { msgId, turns: payload as MessageTurn[] };
  }
  if (payload && typeof payload === "object" && "turns" in payload) {
    const turns = (payload as { turns?: MessageTurn[] }).turns || [];
    return { msgId, turns };
  }
  return { msgId, turns: [] };
}

export class CamcCliBackend implements CamBackend {
  constructor(private readonly profile: BackendProfile) {}

  private async exec(
    args: string[],
    timeoutMs = DEFAULT_TIMEOUT_MS,
  ): Promise<CommandResult> {
    return invoke<CommandResult>("camc_exec", {
      profile: this.profile,
      args,
      timeoutMs,
    });
  }

  private async json<T>(
    args: string[],
    timeoutMs = DEFAULT_TIMEOUT_MS,
  ): Promise<T> {
    const result = assertOk(await this.exec(args, timeoutMs), args);
    return JSON.parse(result.stdout) as T;
  }

  async health(): Promise<BackendHealth> {
    try {
      const result = assertOk(await this.exec(["version"], 10_000), ["version"]);
      const version = result.stdout.split(/\r?\n/)[0]?.trim();
      return { ok: true, version, checkedAt: new Date().toISOString() };
    } catch (error) {
      return {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
        checkedAt: new Date().toISOString(),
      };
    }
  }

  async listAgents(): Promise<AgentSummary[]> {
    return this.json<AgentSummary[]>(["--json", "list"], 30_000);
  }

  async captureAgent(agentId: string, lines = 200): Promise<CaptureResult> {
    const args = ["capture", agentId, "--lines", String(lines)];
    const result = assertOk(await this.exec(args, 60_000), args);
    return { agentId, output: result.stdout };
  }

  async sendToAgent(
    agentId: string,
    text: string,
    opts?: SendOptions,
  ): Promise<void> {
    const args = ["send", agentId, "--text", text];
    if (opts?.sendEnter === false) args.push("--no-enter");
    assertOk(await this.exec(args, 30_000), args);
  }

  async sendKey(agentId: string, key: AgentKey): Promise<void> {
    const args = ["key", agentId, "--key", key];
    assertOk(await this.exec(args, 15_000), args);
  }

  // --- Retained P0 ---

  async runAgent(req: RunAgentRequest): Promise<void> {
    const args = ["run", "--name", req.name, "--tool", req.tool];
    if (req.path.trim()) args.push("--path", req.path);
    if (req.prompt.trim()) args.push(req.prompt);
    assertOk(await this.exec(args, 60_000), args);
  }

  async listInbox(): Promise<MessageSummary[]> {
    return this.json<MessageSummary[]>(["msg", "read", "--json"], 30_000);
  }

  async readThread(msgId: string): Promise<MessageThread> {
    const payload = await this.json<unknown>(
      ["msg", "read", msgId, "--json"],
      30_000,
    );
    return normalizeThread(msgId, payload);
  }

  async sendMessage(req: SendMessageRequest): Promise<SendMessageResult> {
    const args = ["msg", "send", req.to, "-t", req.text, "--no-wait"];
    if (req.expectReply) args.push("--expect-reply");
    const result = assertOk(await this.exec(args, 30_000), args);
    const kv = parseKv(result.stdout);
    return {
      msgId: kv.MSG_ID,
      status: kv.STATUS,
      raw: result.stdout,
    };
  }

  async replyMessage(req: ReplyMessageRequest): Promise<ReplyMessageResult> {
    const args = ["msg", "reply", req.msgId, "-t", req.text];
    const result = assertOk(await this.exec(args, 30_000), args);
    const kv = parseKv(result.stdout);
    return {
      msgId: kv.REPLIED_TO,
      seq: kv.SEQ ? Number(kv.SEQ) : undefined,
      mailbox: kv.MAILBOX,
      raw: result.stdout,
    };
  }
}
