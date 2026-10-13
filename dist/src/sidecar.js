/**
 * Client for the holographic sidecar.
 *
 * Connects to the always-on sidecar (systemd / launchd / Task Scheduler) and, when the
 * endpoint is not up and `spawn` is enabled, starts it through the launcher and retries.
 * One JSON object per line, one response per request.
 *
 * Endpoints: a unix socket path on Linux/macOS, `tcp://host:port` (loopback) elsewhere.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, openSync } from "node:fs";
import { connect } from "node:net";
import { dirname, join } from "node:path";
import { parseEndpoint, } from "./config.js";
export class SidecarError extends Error {
    constructor(message) {
        super(message);
        this.name = "SidecarError";
    }
}
export class SidecarClient {
    config;
    log;
    socket;
    buffer = "";
    nextId = 1;
    pending = new Map();
    connecting;
    child;
    spawnAttempted = false;
    closed = false;
    /** Parsed endpoint; callers may pass only `socketPath`. */
    endpoint;
    constructor(config, log = () => { }) {
        this.config = config;
        this.log = log;
        this.endpoint = config.endpoint ?? parseEndpoint(config.socketPath);
    }
    get target() {
        return this.endpoint.kind === "unix"
            ? this.endpoint.path
            : `${this.endpoint.host}:${this.endpoint.port}`;
    }
    get socketPath() {
        return this.config.socketPath;
    }
    /** True when a connection is established. */
    get connected() {
        return this.socket !== undefined && !this.socket.destroyed;
    }
    async ensure() {
        if (this.connected)
            return;
        if (!this.connecting) {
            this.connecting = this.connect().finally(() => {
                this.connecting = undefined;
            });
        }
        return this.connecting;
    }
    async connect() {
        try {
            await this.attach();
            return;
        }
        catch (error) {
            if (!this.config.spawn || this.spawnAttempted)
                throw this.wrap(error);
        }
        this.startSidecar();
        const deadline = Date.now() + 30_000;
        let lastError;
        while (Date.now() < deadline) {
            await sleep(250);
            try {
                await this.attach();
                return;
            }
            catch (error) {
                lastError = error;
            }
        }
        throw new SidecarError(`sidecar did not start within 30s (endpoint ${this.target}, launcher ${this.config.launcher}): ` +
            `${lastError instanceof Error ? lastError.message : String(lastError)}`);
    }
    attach() {
        const endpoint = this.endpoint;
        const target = this.target;
        return new Promise((resolve, reject) => {
            const socket = endpoint.kind === "unix"
                ? connect(endpoint.path)
                : connect({ host: endpoint.host, port: endpoint.port });
            const onError = (error) => {
                socket.destroy();
                reject(new SidecarError(`cannot connect to ${target}: ${error.message}`));
            };
            socket.once("error", onError);
            socket.once("connect", () => {
                socket.off("error", onError);
                socket.setEncoding("utf-8");
                this.socket = socket;
                socket.on("data", (chunk) => this.onData(chunk));
                socket.on("error", (error) => this.onClose(error));
                socket.on("close", () => this.onClose());
                this.log(`connected to ${target}`);
                resolve();
            });
        });
    }
    logFile() {
        const base = this.endpoint.kind === "unix"
            ? `${this.config.socketPath}.log`
            : join(dirname(this.config.configPath), "sidecar.log");
        try {
            mkdirSync(dirname(base), { recursive: true });
        }
        catch {
            /* best effort */
        }
        return base;
    }
    startSidecar() {
        this.spawnAttempted = true;
        if (!existsSync(this.config.launcher)) {
            this.log(`launcher not found: ${this.config.launcher}`);
            return;
        }
        const env = {
            ...process.env,
            HERMES_HOME: this.config.hermesHome,
            OPENCODE_HOLOGRAPHIC_SOCKET: this.config.socketPath,
        };
        if (this.config.token)
            env.OPENCODE_HOLOGRAPHIC_TOKEN = this.config.token;
        if (this.config.pythonPath)
            env.OPENCODE_HOLOGRAPHIC_PYTHON = this.config.pythonPath;
        if (this.config.configPath)
            env.OPENCODE_HOLOGRAPHIC_CONFIG = this.config.configPath;
        if (this.config.dbPath)
            env.OPENCODE_HOLOGRAPHIC_DB = this.config.dbPath;
        let stdio = "ignore";
        try {
            const fd = openSync(this.logFile(), "a");
            stdio = ["ignore", fd, fd];
        }
        catch {
            stdio = "ignore";
        }
        this.log(`starting sidecar: ${this.config.launcher}`);
        this.child = spawn(this.config.launcher, [], {
            env,
            detached: true,
            stdio,
        });
        this.child.on("error", (error) => this.log(`sidecar spawn failed: ${error.message}`));
        this.child.unref();
    }
    onData(chunk) {
        this.buffer += chunk;
        let index = this.buffer.indexOf("\n");
        while (index >= 0) {
            const line = this.buffer.slice(0, index).trim();
            this.buffer = this.buffer.slice(index + 1);
            if (line)
                this.onLine(line);
            index = this.buffer.indexOf("\n");
        }
    }
    onLine(line) {
        let message;
        try {
            message = JSON.parse(line);
        }
        catch {
            this.log(`unparsable sidecar line: ${line.slice(0, 200)}`);
            return;
        }
        if (typeof message.id !== "number")
            return;
        const pending = this.pending.get(message.id);
        if (!pending)
            return;
        this.pending.delete(message.id);
        clearTimeout(pending.timer);
        if (message.ok)
            pending.resolve(message.result);
        else
            pending.reject(new SidecarError(message.error ?? "sidecar error"));
    }
    onClose(error) {
        this.socket = undefined;
        const reason = error?.message ?? "connection closed";
        for (const [, pending] of this.pending) {
            clearTimeout(pending.timer);
            pending.reject(new SidecarError(reason));
        }
        this.pending.clear();
        if (error)
            this.log(`sidecar connection error: ${reason}`);
    }
    wrap(error) {
        return error instanceof Error ? error : new SidecarError(String(error));
    }
    async request(cmd, args = {}) {
        if (this.closed)
            throw new SidecarError("client is closed");
        await this.ensure();
        const socket = this.socket;
        if (!socket)
            throw new SidecarError("not connected");
        const id = this.nextId++;
        const payload = JSON.stringify(this.config.token
            ? { id, cmd, args, token: this.config.token }
            : { id, cmd, args }) + "\n";
        if (this.config.debug)
            this.log(`-> ${cmd} ${JSON.stringify(args).slice(0, 300)}`);
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this.pending.delete(id);
                reject(new SidecarError(`timeout after ${this.config.timeoutMs}ms on ${cmd}`));
            }, this.config.timeoutMs);
            timer.unref?.();
            this.pending.set(id, {
                resolve: resolve,
                reject,
                timer,
            });
            socket.write(payload, (error) => {
                if (!error)
                    return;
                this.pending.delete(id);
                clearTimeout(timer);
                reject(this.wrap(error));
            });
        });
    }
    async close() {
        this.closed = true;
        this.onClose();
        this.socket?.end();
        this.socket?.destroy();
        this.socket = undefined;
        void this.child;
    }
}
function sleep(ms) {
    return new Promise((resolve) => {
        const timer = setTimeout(resolve, ms);
        timer.unref?.();
    });
}
