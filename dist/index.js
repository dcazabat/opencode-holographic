/**
 * opencode-holographic — OpenCode V2 plugin.
 *
 * Reuses Hermes' `holographic` memory provider (same code, same SQLite file) through
 * the always-on sidecar: automatic recall before every turn, and the original
 * `fact_store` / `fact_feedback` tools for compatibility.
 *
 *   OpenCode turn ──▶ context hook ──▶ sidecar ──▶ holographic.prefetch()  (HRR + FTS5)
 *   fact_store tool ──────────────────▶ sidecar ──▶ holographic.handle_tool_call()
 */
import { basename, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { Plugin } from "@opencode/plugin";
import { resolveConfig } from "./src/config.js";
import { FlushTracker } from "./src/flush.js";
import { lastUserTurn, RecallCache } from "./src/recall.js";
import { SidecarClient } from "./src/sidecar.js";
import { buildTools, FALLBACK_SCHEMAS, } from "./src/tools.js";
const PLUGIN_ID = "opencode.holographic";
function pluginDirectory() {
    const meta = import.meta;
    const here = meta.dirname ??
        (meta.url ? dirname(fileURLToPath(meta.url)) : process.cwd());
    // When loaded through the compiled entrypoint the module lives in dist/: the
    // sidecar launcher is one level up, next to the sources.
    return basename(here) === "dist" ? dirname(here) : here;
}
export default Plugin.define({
    id: PLUGIN_ID,
    async setup(ctx) {
        const config = resolveConfig((ctx.options ?? {}), pluginDirectory());
        const debug = config.debug;
        const log = (message) => console.warn(`[${PLUGIN_ID}] ${message}`);
        const trace = (message) => {
            if (debug)
                log(message);
        };
        const client = new SidecarClient(config, trace);
        const recall = new RecallCache(client, log);
        const flush = new FlushTracker(config, trace);
        let schemas = FALLBACK_SCHEMAS;
        try {
            const fetched = await client.request("schemas");
            if (Array.isArray(fetched) && fetched.length > 0)
                schemas = fetched;
            trace(`tool schemas: ${schemas.map((schema) => schema.name).join(", ")}`);
        }
        catch (error) {
            log(`sidecar not reachable at setup, using fallback schemas: ${message(error)}`);
        }
        // A successful `fact_store add` is a save: it restarts the flush cadence.
        const watched = buildTools(client, schemas).map((tool) => watchForSave(tool, flush));
        await ctx.tool.transform((editor) => {
            for (const definition of watched)
                editor.add(definition);
        });
        await ctx.session.hook("context", async (event) => {
            const block = await recall.blockFor(event.sessionID, event.messages);
            if (block)
                event.system.push({ type: "text", text: block });
            const turnKey = lastUserTurn(event.messages)?.key ?? "";
            const nudge = flush.reminderFor(event.sessionID, turnKey);
            if (nudge)
                event.system.push({ type: "text", text: nudge });
        });
        // Compaction rebuilds the transcript: keep the recalled facts in the new context.
        await ctx.session.hook("compaction", async (event) => {
            const block = await recall.blockFor(event.sessionID, event.messages);
            if (block)
                event.system.push({ type: "text", text: block });
        });
        log(`ready (socket ${client.socketPath}, tools: ${schemas.map((schema) => schema.name).join(", ")})`);
        return async () => {
            await client.close();
        };
    },
});
function message(error) {
    return error instanceof Error ? error.message : String(error);
}
/**
 * Wraps a registered tool so a successful `fact_store add` restarts the flush
 * cadence. Only that action counts: reading or updating a fact is not a save.
 */
function watchForSave(tool, flush) {
    if (tool.name !== "fact_store")
        return tool;
    const inner = tool.execute;
    return {
        ...tool,
        execute: async (input, context) => {
            const result = await inner(input, context);
            const action = input?.action;
            const ok = result?.metadata?.ok === true;
            if (ok && action === "add") {
                const sessionID = context?.sessionID ?? "";
                if (sessionID)
                    flush.markSaved(sessionID);
            }
            return result;
        },
    };
}
