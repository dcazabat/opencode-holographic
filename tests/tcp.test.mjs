/**
 * TCP endpoint: what Windows uses, because Node there cannot connect to a Python
 * AF_UNIX socket (it uses named pipes). Also covers the optional shared secret.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { SidecarClient } from "../dist/src/sidecar.js";
import {
  prepareDatabase,
  skipReason,
  sleep,
  waitForValue,
} from "./helpers.mjs";

const ROOT = new URL("..", import.meta.url).pathname;
const LAUNCHER = join(ROOT, "sidecar", "run.sh");
const skip = skipReason();

async function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

describe("tcp endpoint", { skip }, () => {
  it("serves the same protocol over tcp, with and without a token", async () => {
    const workdir = mkdtempSync(join(tmpdir(), "opencode-holographic-tcp-"));
    const port = await freePort();
    const socketPath = `tcp://127.0.0.1:${port}`;
    const dbPath = prepareDatabase(workdir);
    const token = "s3cret";
    const child = spawn(LAUNCHER, [], {
      env: {
        ...process.env,
        OPENCODE_HOLOGRAPHIC_DB: dbPath,
        OPENCODE_HOLOGRAPHIC_SOCKET: socketPath,
        OPENCODE_HOLOGRAPHIC_TOKEN: token,
      },
      stdio: "ignore",
    });
    const base = {
      socketPath,
      launcher: LAUNCHER,
      hermesHome: process.env.HERMES_HOME ?? join(process.env.HOME, ".hermes"),
      timeoutMs: 15_000,
      spawn: false,
      debug: false,
    };
    try {
      const authed = new SidecarClient({ ...base, token }, () => {});
      const ping = await waitForValue(
        async () => {
          try {
            return await authed.request("ping");
          } catch {
            return undefined;
          }
        },
        30_000,
        "the tcp sidecar",
      );
      assert.equal(ping.ok, true);
      assert.equal(ping.db_path, dbPath);

      // Without the token the sidecar refuses, and it says so instead of hanging.
      const unauthed = new SidecarClient(base, () => {});
      await assert.rejects(() => unauthed.request("ping"), /unauthorized/);
      await unauthed.close();
      await authed.close();
    } finally {
      child.kill("SIGTERM");
      await sleep(500);
      child.kill("SIGKILL");
      rmSync(workdir, { recursive: true, force: true });
    }
  });
});
