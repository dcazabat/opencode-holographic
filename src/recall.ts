/**
 * Recall before a turn (the fragment Hermes' `prefetch` injects) plus the small
 * per-turn cache that keeps the recall once per user message instead of once per
 * model call (OpenCode runs the `context` hook again for every tool continuation).
 */
import type { SidecarClient } from "./sidecar.js";

export interface TurnRecall {
  /** Identity of the user message the block belongs to. */
  key: string;
  /** Recalled block, "" when nothing was recalled. */
  block: string;
}

const MAX_TRACKED_SESSIONS = 64;

/** Text of the last user message in a request, with a stable identity for that turn. */
export function lastUserTurn(
  messages: unknown,
): { key: string; text: string } | undefined {
  if (!Array.isArray(messages)) return undefined;
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index] as
      { id?: unknown; role?: unknown; content?: unknown } | undefined;
    if (!message || message.role !== "user") continue;
    const text = messageText(message.content);
    if (!text) return undefined;
    const id =
      typeof message.id === "string" && message.id ? message.id : undefined;
    return { key: id ?? `text:${text.slice(0, 512)}`, text };
  }
  return undefined;
}

function messageText(content: unknown): string {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const part of content) {
    if (typeof part === "string") {
      parts.push(part);
      continue;
    }
    const candidate = part as { type?: unknown; text?: unknown } | undefined;
    if (
      candidate &&
      candidate.type === "text" &&
      typeof candidate.text === "string"
    )
      parts.push(candidate.text);
  }
  return parts.join("\n").trim();
}

/**
 * Computes the recall block for the current turn, reusing the previous result while
 * the user message is unchanged. A failed recall never throws: it degrades to no
 * memory so the turn continues.
 */
export class RecallCache {
  private readonly sessions = new Map<string, TurnRecall>();

  constructor(
    private readonly client: SidecarClient,
    private readonly log: (message: string) => void = () => {},
  ) {}

  async blockFor(sessionID: string, messages: unknown): Promise<string> {
    const turn = lastUserTurn(messages);
    if (!turn) return "";
    const cached = this.sessions.get(sessionID);
    if (cached && cached.key === turn.key) return cached.block;

    let block = "";
    try {
      block =
        (await this.client.request<string>("prefetch", {
          text: turn.text,
          session_id: sessionID,
        })) ?? "";
    } catch (error) {
      this.log(
        `recall failed for ${sessionID}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    this.sessions.delete(sessionID);
    this.sessions.set(sessionID, { key: turn.key, block });
    while (this.sessions.size > MAX_TRACKED_SESSIONS) {
      const oldest = this.sessions.keys().next().value;
      if (oldest === undefined) break;
      this.sessions.delete(oldest);
    }
    return block;
  }
}
