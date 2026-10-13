/**
 * Plugin configuration.
 *
 * Values come from the plugin `options` in opencode.json(c), then the environment,
 * then sensible defaults. The database path itself is resolved by the sidecar: from
 * its own config file (standalone mode) or from Hermes' config.yaml (shared mode), so
 * OpenCode and Hermes share one store without duplicating the path here.
 */
import { homedir } from "node:os";
import { join } from "node:path";

export type Endpoint =
  { kind: "unix"; path: string } | { kind: "tcp"; host: string; port: number };

export interface HolographicConfig {
  /** Endpoint as written in the config: a unix path or `tcp://host:port`. */
  socketPath: string;
  /** Parsed endpoint used by the client. */
  endpoint: Endpoint;
  /** Optional shared secret (TCP endpoints on a non-loopback host should set it). */
  token?: string;
  /** Launcher used to start the sidecar when the endpoint is not up. */
  launcher: string;
  /** Optional Python interpreter override (skips sidecar/discover.sh). */
  pythonPath?: string;
  /** Hermes home directory. */
  hermesHome: string;
  /** Sidecar config file (mode, database path, provider settings). */
  configPath: string;
  /** Optional explicit SQLite path (overrides the Hermes config). */
  dbPath?: string;
  /** Per-request timeout in milliseconds. */
  timeoutMs: number;
  /** Start the sidecar on demand when it is not already running. */
  spawn: boolean;
  /** Log sidecar traffic to the OpenCode log. */
  debug: boolean;
}

const DEFAULTS = {
  timeoutMs: 8000,
  spawn: true,
  debug: false,
};

/**
 * Unix socket where the OS provides one; TCP loopback on Windows, because Node there
 * cannot connect to a Python AF_UNIX socket (it uses named pipes instead).
 */
function socketDefault(): string {
  if (process.platform === "win32") return "tcp://127.0.0.1:4748";
  if (process.platform === "darwin")
    return join(
      homedir(),
      "Library",
      "Caches",
      "opencode-holographic",
      "sidecar.sock",
    );
  const runtime = process.env.XDG_RUNTIME_DIR;
  if (runtime) return join(runtime, "opencode-holographic.sock");
  const uid = typeof process.getuid === "function" ? process.getuid() : "user";
  return join("/tmp", `opencode-holographic-${uid}.sock`);
}

export function parseEndpoint(value: string): Endpoint {
  const text = value.trim();
  if (text.startsWith("tcp://")) {
    const rest = text.slice("tcp://".length);
    const index = rest.lastIndexOf(":");
    const host = index >= 0 ? rest.slice(0, index) : "127.0.0.1";
    const port = index >= 0 ? Number(rest.slice(index + 1)) : 4748;
    return {
      kind: "tcp",
      host: host || "127.0.0.1",
      port: Number.isFinite(port) ? port : 4748,
    };
  }
  for (const prefix of ["unix://", "unix:"]) {
    if (text.startsWith(prefix))
      return { kind: "unix", path: text.slice(prefix.length) };
  }
  return { kind: "unix", path: text };
}

export function resolveConfig(
  options: Record<string, unknown> = {},
  pluginDir: string,
): HolographicConfig {
  const str = (value: unknown): string | undefined =>
    typeof value === "string" && value.trim() !== "" ? value.trim() : undefined;
  const bool = (value: unknown, fallback: boolean): boolean =>
    typeof value === "boolean" ? value : fallback;
  const num = (value: unknown, fallback: number): number => {
    const n = typeof value === "number" ? value : Number(value);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  };

  const hermesHome =
    str(options.hermesHome) ??
    process.env.HERMES_HOME ??
    join(homedir(), ".hermes");
  const socketPath =
    str(options.socketPath) ??
    process.env.OPENCODE_HOLOGRAPHIC_SOCKET ??
    socketDefault();
  return {
    socketPath,
    endpoint: parseEndpoint(socketPath),
    token: str(options.token) ?? process.env.OPENCODE_HOLOGRAPHIC_TOKEN,
    launcher:
      str(options.launcher) ??
      join(
        pluginDir,
        "sidecar",
        process.platform === "win32" ? "run.cmd" : "run.sh",
      ),
    pythonPath:
      str(options.pythonPath) ?? process.env.OPENCODE_HOLOGRAPHIC_PYTHON,
    hermesHome,
    configPath:
      str(options.configPath) ??
      process.env.OPENCODE_HOLOGRAPHIC_CONFIG ??
      join(homedir(), ".config", "opencode-holographic", "config.json"),
    dbPath: str(options.dbPath) ?? process.env.OPENCODE_HOLOGRAPHIC_DB,
    timeoutMs: num(options.timeoutMs, DEFAULTS.timeoutMs),
    spawn: bool(options.spawn, DEFAULTS.spawn),
    debug: bool(options.debug, DEFAULTS.debug),
  };
}
