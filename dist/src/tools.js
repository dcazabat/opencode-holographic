export const FALLBACK_SCHEMAS = [
    {
        name: "fact_store",
        description: "Long-term facts (local store, shared with Hermes). " +
            "search {query, as_of?} · probe {entity} · reason {entities} · add {content, entities?, keywords?, valid_from?} · " +
            "change/end/correct/revert {fact_id, ...} · forget {fact_id} · history {fact_id} · update {fact_id, keywords/tags/category} · " +
            "list · stats · export · review · related · contradict · enrich · entities · alias {entity, name}. " +
            "Recalled facts show their id as #N: pass it to fact_feedback.",
        parameters: {
            type: "object",
            properties: {
                action: {
                    type: "string",
                    enum: [
                        "add",
                        "search",
                        "probe",
                        "related",
                        "reason",
                        "contradict",
                        "update",
                        "change",
                        "end",
                        "correct",
                        "revert",
                        "forget",
                        "history",
                        "remove",
                        "list",
                        "stats",
                        "export",
                        "review",
                        "entities",
                        "alias",
                        "enrich",
                    ],
                },
                content: { type: "string" },
                query: { type: "string" },
                fact_id: { type: "integer" },
                entity: { type: "string" },
                entities: { type: "array", items: { type: "string" } },
                keywords: { type: "array", items: { type: "string" } },
                category: { type: "string" },
                tags: { type: "string" },
                valid_from: { type: "string" },
                valid_to: { type: "string" },
                as_of: { type: "string" },
                limit: { type: "integer" },
            },
            required: ["action"],
            additionalProperties: true,
        },
    },
    {
        name: "fact_feedback",
        description: "Rate a recalled fact helpful or unhelpful (moves its trust score).",
        parameters: {
            type: "object",
            properties: {
                action: { type: "string", enum: ["helpful", "unhelpful"] },
                fact_id: { type: "integer" },
            },
            required: ["action", "fact_id"],
            additionalProperties: true,
        },
    },
];
/**
 * Builds one OpenCode tool per provider schema. Execution is proxied to the sidecar,
 * so writes go through the same code path Hermes uses (write filters, HRR vectors,
 * fact_versions history).
 */
export function buildTools(client, schemas) {
    const usable = schemas.filter((schema) => schema.name && schema.parameters);
    return usable.map((schema) => ({
        name: schema.name,
        description: schema.description || `Holographic memory: ${schema.name}`,
        input: schema.parameters,
        options: { codemode: true },
        execute: async (input, context) => {
            try {
                const result = await client.request("tool", {
                    name: schema.name,
                    args: input ?? {},
                    session_id: context?.sessionID ?? "",
                });
                return {
                    content: typeof result === "string"
                        ? result
                        : JSON.stringify(result, null, 2),
                    metadata: { tool: schema.name, ok: true },
                };
            }
            catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                return {
                    content: JSON.stringify({
                        error: `holographic sidecar unavailable: ${message}`,
                    }),
                    metadata: { tool: schema.name, ok: false },
                };
            }
        },
    }));
}
