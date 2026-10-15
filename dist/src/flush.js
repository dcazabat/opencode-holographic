/**
 * Auto-flush: the reminder that makes OpenCode save like Hermes did.
 *
 * Hermes kept `flush_min_turns: 6` and `nudge_interval: 10` in its config: a nudge
 * the *agent* (the LLM) reacts to by calling `fact_store add`. The plugin repeats
 * that cadence here — it counts user turns since the last save and, once the
 * threshold is crossed, hands back a short reminder to inject next to the recall
 * block. It never writes facts itself: curating what is durable is the model's
 * job, and a TypeScript plugin could only dump raw transcripts.
 *
 * The reminder fires at `flushMinTurns` and then again every `nudgeInterval` turns
 * until a `fact_store add` resets the counter, so an ignored nudge does not repeat
 * on every model call. Nothing here throws: a broken flush degrades to no reminder.
 */
/** Default reminder text (overridable through the plugin options). */
export const DEFAULT_FLUSH_MESSAGE = "💾 Auto-flush: this session has been running for a while and the last " +
    "`fact_store add` may be several turns behind. If a durable fact was learned " +
    "(a decision, fix, root cause, reusable pattern, gotcha), store it now with " +
    "`fact_store add` (short standalone sentence, 3-8 keywords, right category).";
const MAX_TRACKED_SESSIONS = 64;
export class FlushTracker {
    config;
    log;
    sessions = new Map();
    constructor(config, log = () => { }) {
        this.config = config;
        this.log = log;
    }
    /**
     * Records one user turn for the session and returns the reminder to inject,
     * or "" when it is too early (or flushing is disabled).
     *
     * `turnKey` identifies the user message: the `context` hook re-runs for every
     * tool continuation, so the same key never counts twice.
     */
    reminderFor(sessionID, turnKey = "") {
        if (!this.config.flushEnabled)
            return "";
        const state = this.sessions.get(sessionID) ?? {
            turns: 0,
            sinceNudge: 0,
            nudged: false,
        };
        if (turnKey && state.turnKey === turnKey)
            return "";
        state.turnKey = turnKey;
        state.turns += 1;
        state.sinceNudge += 1;
        const due = state.turns >= this.config.flushMinTurns &&
            (!state.nudged || state.sinceNudge >= this.config.nudgeInterval);
        if (!due) {
            this.track(sessionID, state);
            return "";
        }
        state.nudged = true;
        state.sinceNudge = 0;
        this.track(sessionID, state);
        const text = this.config.flushMessage || DEFAULT_FLUSH_MESSAGE;
        this.log(`flush reminder for ${sessionID} (turn ${state.turns})`);
        return text;
    }
    /** A successful `fact_store add`: the session is saved, restart the cadence. */
    markSaved(sessionID) {
        const state = this.sessions.get(sessionID);
        if (!state)
            return;
        state.turnKey = undefined;
        state.turns = 0;
        state.sinceNudge = 0;
        state.nudged = false;
    }
    /** Turns counted so far, for tests and diagnostics. */
    turnsSinceSave(sessionID) {
        return this.sessions.get(sessionID)?.turns ?? 0;
    }
    track(sessionID, state) {
        this.sessions.delete(sessionID);
        this.sessions.set(sessionID, state);
        while (this.sessions.size > MAX_TRACKED_SESSIONS) {
            const oldest = this.sessions.keys().next().value;
            if (oldest === undefined)
                break;
            this.sessions.delete(oldest);
        }
    }
}
