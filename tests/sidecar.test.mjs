/**
 * End-to-end tests for the sidecar protocol and the TypeScript client.
 *
 * Runs against a COPY of a holographic store (never the live one) and its own socket,
 * so Hermes is never touched. Suites that need existing facts skip when no store is
 * configured (`HOLOGRAPHIC_TEST_SOURCE`).
 *
 *   npm test
 */
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";

import { SidecarClient } from "../dist/src/sidecar.js";
import {
  prepareDatabase,
  resolveStore,
  skipReason,
  waitFor,
} from "./helpers.mjs";

const ROOT = new URL("..", import.meta.url).pathname;
const LAUNCHER = join(ROOT, "sidecar", "run.sh");
const STORE = resolveStore();
const skip = skipReason();

let workdir;
let socketPath;
let dbPath;
let child;
let client;

function clientOptions(extra = {}) {
  return {
    socketPath,
    launcher: LAUNCHER,
    hermesHome: process.env.HERMES_HOME ?? join(homedir(), ".hermes"),
    timeoutMs: 15_000,
    spawn: false,
    debug: false,
    ...extra,
  };
}

const ids = (block) => (block.match(/#(\d+)/g) ?? []).join(",");

/** Runs a query with the sqlite3 CLI; null when the CLI is not installed. */
function sqlite(database, query) {
  try {
    return execFileSync("sqlite3", [database, query], {
      encoding: "utf-8",
    }).trim();
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

before(async () => {
  workdir = mkdtempSync(join(tmpdir(), "opencode-holographic-sidecar-"));
  socketPath = join(workdir, "sidecar.sock");
  dbPath = prepareDatabase(workdir);
  child = spawn(LAUNCHER, [], {
    env: {
      ...process.env,
      OPENCODE_HOLOGRAPHIC_DB: dbPath,
      OPENCODE_HOLOGRAPHIC_SOCKET: socketPath,
    },
    stdio: "ignore",
  });
  await waitFor(() => existsSync(socketPath), 30_000, "the sidecar socket");
  client = new SidecarClient(clientOptions(), () => {});
});

after(async () => {
  if (client) await client.close();
  if (child) child.kill("SIGTERM");
  rmSync(workdir, { recursive: true, force: true });
});

describe("sidecar protocol", { skip }, () => {
  it("reports the shared database and the two original tools", async () => {
    const ping = await client.request("ping");
    assert.equal(ping.ok, true);
    assert.ok(ping.db_path.endsWith("memory_store.db"));
    assert.deepEqual(ping.tools.sort(), ["fact_feedback", "fact_store"]);
  });

  it("exposes the provider schemas verbatim", async () => {
    const schemas = await client.request("schemas");
    assert.deepEqual(schemas.map((s) => s.name).sort(), [
      "fact_feedback",
      "fact_store",
    ]);
    const action = schemas.find((s) => s.name === "fact_store").parameters
      .properties.action;
    assert.ok(action.enum.includes("add") && action.enum.includes("probe"));
  });

  it("refuses tool names outside the provider schemas", async () => {
    await assert.rejects(
      () => client.request("tool", { name: "rm -rf /", args: {} }),
      /unknown tool/,
    );
  });

  it("writes through the provider and reads it back", async () => {
    const content = `Integration test opencode-holographic ${Date.now()}`;
    const added = JSON.parse(
      await client.request("tool", {
        name: "fact_store",
        args: { action: "add", content, category: "nota" },
        session_id: "s-write",
      }),
    );
    assert.equal(added.status, "added");
    const found = JSON.parse(
      await client.request("tool", {
        name: "fact_store",
        args: {
          action: "search",
          query: "integration test opencode-holographic",
          limit: 5,
        },
        session_id: "s-write",
      }),
    );
    assert.ok(found.results.some((fact) => fact.fact_id === added.fact_id));
  });

  it("keeps its writes in the copy, never in the source store", async (t) => {
    const query =
      "SELECT count(*) FROM facts WHERE content LIKE 'Integration test opencode-holographic%';";
    const inCopy = sqlite(dbPath, query);
    if (inCopy === null) return t.skip("sqlite3 CLI not installed");
    assert.ok(Number(inCopy) >= 1);
    if (STORE) assert.equal(sqlite(STORE, query), "0");
  });
});

describe("recall (needs a populated store)", { skip }, () => {
  it("recalls relevant facts before a turn", async () => {
    const block = await client.request("prefetch", {
      text: "deploy de mygym en el VPS",
      session_id: "s-1",
    });
    assert.match(block, /## Holographic Memory/);
    assert.match(block, /#\d+ \[[0-9.]+\]/);
  });

  it("recalls nothing for a greeting (relevance gate)", async () => {
    const block = await client.request("prefetch", {
      text: "buenos dias",
      session_id: "s-gate",
    });
    assert.equal(block, "");
  });

  it("does not repeat the same facts within the repeat window", async () => {
    const query = "que middleware de idempotencia usamos";
    const first = await client.request("prefetch", {
      text: query,
      session_id: "s-repeat",
    });
    const second = await client.request("prefetch", {
      text: query,
      session_id: "s-repeat",
    });
    assert.ok(ids(first).length > 0);
    assert.notEqual(ids(first), ids(second));
  });

  it("keeps one recall window per session", async () => {
    const query = "deploy de mygym en el VPS";
    const a = await client.request("prefetch", {
      text: query,
      session_id: "s-iso-a",
    });
    const b = await client.request("prefetch", {
      text: query,
      session_id: "s-iso-b",
    });
    assert.equal(ids(a), ids(b));
  });

  it("reports recall status for the last prefetch", async () => {
    await client.request("prefetch", {
      text: "stack de laravel",
      session_id: "s-status",
    });
    const status = await client.request("status");
    assert.ok(status.count >= 0);
  });
});
