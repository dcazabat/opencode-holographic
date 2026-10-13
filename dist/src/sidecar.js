/**
 * Unix-socket client for the holographic sidecar.
 *
 * Connects to the always-on sidecar (systemd user unit) and, when the socket is
 * missing and `spawn` is enabled, starts it through sidecar/run.sh and retries.
 * One JSON object per line, one response per request.
 */
import { spawn } from "node:child_process";
import { existsSync, openSync } from "node:fs";
import { connect } from "node:net";
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
    constructor(config, log = () => { }) {
        this.config = config;
        this.log = log;
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
        throw new SidecarError(`sidecar did not start within 30s (socket ${this.config.socketPath}, launcher ${this.config.launcher}): ${lastError instanceof Error ? lastError.message : String(lastError)}`);
    }
    attach() {
        return new Promise((resolve, reject) => {
            const socket = connect(this.config.socketPath);
            const onError = (error) => {
                socket.destroy();
                reject(new SidecarError(`cannot connect to ${this.config.socketPath}: ${error.message}`));
            };
            socket.once("error", onError);
            socket.once("connect", () => {
                socket.off("error", onError);
                socket.setEncoding("utf-8");
                this.socket = socket;
                socket.on("data", (chunk) => this.onData(chunk));
                socket.on("error", (error) => this.onClose(error));
                socket.on("close", () => this.onClose());
                this.log(`connected to ${this.config.socketPath}`);
                resolve();
            });
        });
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
        if (this.config.pythonPath)
            env.OPENCODE_HOLOGRAPHIC_PYTHON = this.config.pythonPath;
        if (this.config.dbPath)
            env.OPENCODE_HOLOGRAPHIC_DB = this.config.dbPath;
        const logPath = `${this.config.socketPath}.log`;
        let stdio = "ignore";
        try {
            const fd = openSync(logPath, "a");
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
        const payload = JSON.stringify({ id, cmd, args }) + "\n";
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
