const MAX_TRACKED_SESSIONS = 64;
/** Text of the last user message in a request, with a stable identity for that turn. */
export function lastUserTurn(messages) {
    if (!Array.isArray(messages))
        return undefined;
    for (let index = messages.length - 1; index >= 0; index--) {
        const message = messages[index];
        if (!message || message.role !== "user")
            continue;
        const text = messageText(message.content);
        if (!text)
            return undefined;
        const id = typeof message.id === "string" && message.id ? message.id : undefined;
        return { key: id ?? `text:${text.slice(0, 512)}`, text };
    }
    return undefined;
}
function messageText(content) {
    if (typeof content === "string")
        return content.trim();
    if (!Array.isArray(content))
        return "";
    const parts = [];
    for (const part of content) {
        if (typeof part === "string") {
            parts.push(part);
            continue;
        }
        const candidate = part;
        if (candidate &&
            candidate.type === "text" &&
            typeof candidate.text === "string")
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
    client;
    log;
    sessions = new Map();
    constructor(client, log = () => { }) {
        this.client = client;
        this.log = log;
    }
    async blockFor(sessionID, messages) {
        const turn = lastUserTurn(messages);
        if (!turn)
            return "";
        const cached = this.sessions.get(sessionID);
        if (cached && cached.key === turn.key)
            return cached.block;
        let block = "";
        try {
            block =
                (await this.client.request("prefetch", {
                    text: turn.text,
                    session_id: sessionID,
                })) ?? "";
        }
        catch (error) {
            this.log(`recall failed for ${sessionID}: ${error instanceof Error ? error.message : String(error)}`);
        }
        this.sessions.delete(sessionID);
        this.sessions.set(sessionID, { key: turn.key, block });
        while (this.sessions.size > MAX_TRACKED_SESSIONS) {
            const oldest = this.sessions.keys().next().value;
            if (oldest === undefined)
                break;
            this.sessions.delete(oldest);
        }
        return block;
    }
}
