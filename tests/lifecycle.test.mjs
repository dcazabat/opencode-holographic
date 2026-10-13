/**
 * Lifecycle tests: the sidecar must be startable on demand, stop on SIGTERM
 * (systemd restarts and `opencode service restart` rely on it) and remove its socket.
 * These need no existing store: the provider initialises the database itself.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { SidecarClient } from "../dist/src/sidecar.js";
import { prepareDatabase, skipReason, sleep, waitFor } from "./helpers.mjs";

const ROOT = new URL("..", import.meta.url).pathname;
const LAUNCHER = join(ROOT, "sidecar", "run.sh");
const HERMES_HOME = process.env.HERMES_HOME ?? join(homedir(), ".hermes");
const skip = skipReason();

function options(socketPath, dbPath, extra = {}) {
  return {
    socketPath,
    launcher: LAUNCHER,
    hermesHome: HERMES_HOME,
    dbPath,
    timeoutMs: 20_000,
    spawn: true,
    debug: false,
    ...extra,
  };
}

describe("lifecycle", { skip }, () => {
  it("shuts down on SIGTERM and removes its socket", async () => {
    const workdir = mkdtempSync(
      join(tmpdir(), "opencode-holographic-sigterm-"),
    );
    const socketPath = join(workdir, "sidecar.sock");
    const dbPath = prepareDatabase(workdir);
    const child = spawn(LAUNCHER, [], {
      env: {
        ...process.env,
        OPENCODE_HOLOGRAPHIC_DB: dbPath,
        OPENCODE_HOLOGRAPHIC_SOCKET: socketPath,
      },
      stdio: "ignore",
    });
    try {
      await waitFor(() => existsSync(socketPath), 30_000, "the sidecar socket");
      const client = new SidecarClient(
        options(socketPath, dbPath, { spawn: false }),
        () => {},
      );
      await client.request("ping");
      await client.close();

      let exited = false;
      const exit = new Promise((resolve) =>
        child.once("exit", () => {
          exited = true;
          resolve();
        }),
      );
      child.kill("SIGTERM");
      await Promise.race([exit, sleep(15_000)]);
      assert.equal(exited, true, "the sidecar must exit on SIGTERM");
      await waitFor(
        () => !existsSync(socketPath),
        5_000,
        "the socket to be removed",
      );
    } finally {
      child.kill("SIGKILL");
      rmSync(workdir, { recursive: true, force: true });
    }
  });

  it("starts the sidecar on demand when the socket is missing", async () => {
    const workdir = mkdtempSync(join(tmpdir(), "opencode-holographic-spawn-"));
    const socketPath = join(workdir, "sidecar.sock");
    const dbPath = prepareDatabase(workdir);
    const client = new SidecarClient(options(socketPath, dbPath), () => {});
    try {
      const ping = await client.request("ping");
      assert.equal(ping.ok, true);
      assert.ok(
        existsSync(socketPath),
        "the client must have started the sidecar",
      );
    } finally {
      await client.request("shutdown").catch(() => {});
      await client.close();
      await waitFor(
        () => !existsSync(socketPath),
        5_000,
        "the spawned sidecar to stop",
      ).catch(() => {});
      rmSync(workdir, { recursive: true, force: true });
    }
  });
});
