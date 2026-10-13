/**
 * Plugin configuration.
 *
 * Values come from the plugin `options` in opencode.json(c), then the environment,
 * then sensible defaults. The database path itself is resolved by the sidecar from
 * ~/.hermes/config.yaml (plugins.hermes-memory-store.db_path), so OpenCode and Hermes
 * share the same store without duplicating the path here.
 */
import { homedir } from "node:os";
import { join } from "node:path";
const DEFAULTS = {
    timeoutMs: 8000,
    spawn: true,
    debug: false,
};
function socketDefault() {
    const runtime = process.env.XDG_RUNTIME_DIR || "/tmp";
    return join(runtime, "opencode-holographic.sock");
}
export function resolveConfig(options = {}, pluginDir) {
    const str = (value) => typeof value === "string" && value.trim() !== "" ? value.trim() : undefined;
    const bool = (value, fallback) => typeof value === "boolean" ? value : fallback;
    const num = (value, fallback) => {
        const n = typeof value === "number" ? value : Number(value);
        return Number.isFinite(n) && n > 0 ? n : fallback;
    };
    const hermesHome = str(options.hermesHome) ??
        process.env.HERMES_HOME ??
        join(homedir(), ".hermes");
    return {
        socketPath: str(options.socketPath) ??
            process.env.OPENCODE_HOLOGRAPHIC_SOCKET ??
            socketDefault(),
        launcher: str(options.launcher) ?? join(pluginDir, "sidecar", "run.sh"),
        pythonPath: str(options.pythonPath) ?? process.env.OPENCODE_HOLOGRAPHIC_PYTHON,
        hermesHome,
        configPath: str(options.configPath) ??
            process.env.OPENCODE_HOLOGRAPHIC_CONFIG ??
            join(homedir(), ".config", "opencode-holographic", "config.json"),
        dbPath: str(options.dbPath) ?? process.env.OPENCODE_HOLOGRAPHIC_DB,
        timeoutMs: num(options.timeoutMs, DEFAULTS.timeoutMs),
        spawn: bool(options.spawn, DEFAULTS.spawn),
        debug: bool(options.debug, DEFAULTS.debug),
    };
}
