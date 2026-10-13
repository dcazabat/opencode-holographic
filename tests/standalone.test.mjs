/**
 * Standalone mode: no Hermes installation at all.
 *
 * The sidecar must load the vendored provider (with sidecar/hermes_shim.py standing in
 * for the Hermes modules) and create the store at the path it was told to use.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";

import { SidecarClient } from "../dist/src/sidecar.js";
import { numpyPython, waitFor } from "./helpers.mjs";

const ROOT = new URL("..", import.meta.url).pathname;
const LAUNCHER = join(ROOT, "sidecar", "run.sh");
const PYTHON = numpyPython();
const skip = PYTHON
  ? false
  : "standalone mode needs a Python interpreter with numpy (set OPENCODE_HOLOGRAPHIC_PYTHON)";

let workdir;
let socketPath;
let dbPath;
let child;
let client;

before(async () => {
  if (skip) return;
  workdir = mkdtempSync(join(tmpdir(), "opencode-holographic-standalone-"));
  socketPath = join(workdir, "sidecar.sock");
  // A deliberately empty Hermes home: no provider, no config, no store.
  const emptyHermes = join(workdir, "hermes-home");
  // The database lives where we say, in a directory that does not exist yet.
  dbPath = join(workdir, "elegida", "memoria", "memory_store.db");
  child = spawn(LAUNCHER, [], {
    env: {
      ...process.env,
      HERMES_HOME: emptyHermes,
      OPENCODE_HOLOGRAPHIC_PYTHON: PYTHON,
      OPENCODE_HOLOGRAPHIC_DB: dbPath,
      OPENCODE_HOLOGRAPHIC_SOCKET: socketPath,
      OPENCODE_HOLOGRAPHIC_CONFIG: join(workdir, "config.json"),
    },
    stdio: "ignore",
  });
  await waitFor(
    () => existsSync(socketPath),
    60_000,
    "the standalone sidecar socket",
  );
  client = new SidecarClient(
    {
      socketPath,
      launcher: LAUNCHER,
      hermesHome: emptyHermes,
      configPath: join(workdir, "config.json"),
      dbPath,
      pythonPath: PYTHON,
      timeoutMs: 30_000,
      spawn: false,
      debug: false,
    },
    () => {},
  );
});

after(async () => {
  if (client) await client.close();
  if (child) child.kill("SIGTERM");
  if (workdir) rmSync(workdir, { recursive: true, force: true });
});

describe("standalone mode (no Hermes)", { skip }, () => {
  it("loads the vendored provider", async () => {
    const ping = await client.request("ping");
    assert.equal(ping.mode, "standalone");
    assert.equal(ping.provider, "vendored");
    assert.deepEqual(ping.tools.sort(), ["fact_feedback", "fact_store"]);
  });

  it("creates the store exactly where it was told to", async () => {
    const ping = await client.request("ping");
    assert.equal(ping.db_path, dbPath);
    assert.ok(existsSync(dbPath), `expected the store at ${dbPath}`);
  });

  it("writes and searches without any Hermes module", async () => {
    const content = `Standalone test ${Date.now()}: la memoria vive donde yo quiero.`;
    const added = JSON.parse(
      await client.request("tool", {
        name: "fact_store",
        args: { action: "add", content, category: "nota" },
        session_id: "sa",
      }),
    );
    assert.equal(added.status, "added");
    const found = JSON.parse(
      await client.request("tool", {
        name: "fact_store",
        args: { action: "search", query: "standalone memoria", limit: 5 },
        session_id: "sa",
      }),
    );
    assert.ok(found.results.some((fact) => fact.fact_id === added.fact_id));
  });

  it("reports a healthy store through stats", async () => {
    const stats = JSON.parse(
      await client.request("tool", {
        name: "fact_store",
        args: { action: "stats" },
      }),
    );
    assert.ok(stats.facts >= 1);
    assert.equal(stats.structural_recall, "on");
  });
});
