/**
 * Integration test for the plugin itself: loads the built plugin, runs `setup` with a
 * mock context, and exercises the context/compaction hooks and the registered tools
 * against a real sidecar on a temporary socket + database copy.
 *
 *   npm test
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";

import plugin from "../dist/index.js";
import { SidecarClient } from "../dist/src/sidecar.js";
import { buildTools } from "../dist/src/tools.js";
import { prepareDatabase, skipReason, waitFor } from "./helpers.mjs";

const ROOT = new URL("..", import.meta.url).pathname;
const LAUNCHER = join(ROOT, "sidecar", "run.sh");
const HERMES_HOME = process.env.HERMES_HOME ?? join(homedir(), ".hermes");
const skip = skipReason();

let workdir;
let socketPath;
let child;
let cleanup;
const tools = new Map();
const hooks = new Map();

function mockContext() {
  return {
    options: {
      socketPath,
      launcher: LAUNCHER,
      spawn: false,
      timeoutMs: 15_000,
    },
    tool: {
      transform: async (callback) => {
        callback({
          list: () => [],
          get: () => undefined,
          namespace: () => {},
          add: (tool) => tools.set(tool.name, tool),
          update: () => {},
          remove: () => {},
        });
        return { dispose: async () => {} };
      },
    },
    session: {
      hook: async (name, callback) => {
        hooks.set(name, callback);
        return { dispose: async () => {} };
      },
    },
  };
}

const userMessage = (id, text) => ({
  id,
  role: "user",
  content: [{ type: "text", text }],
});

before(async () => {
  workdir = mkdtempSync(join(tmpdir(), "opencode-holographic-plugin-"));
  socketPath = join(workdir, "sidecar.sock");
  const dbPath = prepareDatabase(workdir);
  child = spawn(LAUNCHER, [], {
    env: {
      ...process.env,
      OPENCODE_HOLOGRAPHIC_DB: dbPath,
      OPENCODE_HOLOGRAPHIC_SOCKET: socketPath,
    },
    stdio: "ignore",
  });
  await waitFor(() => existsSync(socketPath), 30_000, "the sidecar socket");
  cleanup = await plugin.setup(mockContext());
});

after(async () => {
  if (cleanup) await cleanup();
  if (child) child.kill("SIGTERM");
  rmSync(workdir, { recursive: true, force: true });
});

describe("plugin wiring", { skip }, () => {
  it("registers exactly the original holographic tools", () => {
    assert.deepEqual([...tools.keys()].sort(), ["fact_feedback", "fact_store"]);
    const schema = tools.get("fact_store").input;
    assert.ok(schema.properties.action.enum.includes("probe"));
  });

  it("registers the context and compaction hooks", () => {
    assert.ok(hooks.has("context"));
    assert.ok(hooks.has("compaction"));
  });

  it("runs fact_store through the tool executor", async () => {
    const result = await tools.get("fact_store").execute(
      {
        action: "add",
        content: `Plugin test ${Date.now()}`,
        category: "nota",
      },
      { sessionID: "plugin-session" },
    );
    assert.equal(result.metadata.ok, true);
    assert.equal(JSON.parse(result.content).status, "added");
  });

  it("returns an error message instead of throwing when the sidecar is down", async () => {
    const dead = new SidecarClient(
      {
        socketPath: join(workdir, "missing.sock"),
        launcher: join(workdir, "missing.sh"),
        hermesHome: HERMES_HOME,
        timeoutMs: 1_000,
        spawn: false,
        debug: false,
      },
      () => {},
    );
    const [tool] = buildTools(dead, [
      { name: "fact_store", parameters: { type: "object" } },
    ]);
    const result = await tool.execute({ action: "stats" }, { sessionID: "s" });
    assert.equal(result.metadata.ok, false);
    assert.match(result.content, /sidecar unavailable/);
  });
});

describe("auto-flush through the context hook", { skip }, () => {
  const session = "plugin-flush";

  it("nudges after the threshold and restarts after a save", async () => {
    // Six distinct user turns (the default flushMinTurns) stay silent until the last.
    for (let index = 1; index <= 6; index++) {
      const event = {
        sessionID: session,
        messages: [userMessage(`f${index}`, `turn ${index}`)],
        system: [],
      };
      await hooks.get("context")(event);
      const nudge = event.system.find((block) => /Auto-flush/.test(block.text));
      if (index < 6) assert.equal(nudge, undefined);
      else assert.ok(nudge, "expected a flush reminder on the sixth turn");
    }

    // A real save resets the cadence: the next turn must not nudge again.
    const saved = await tools
      .get("fact_store")
      .execute(
        {
          action: "add",
          content: `Flush test ${Date.now()}`,
          category: "nota",
        },
        { sessionID: session },
      );
    assert.equal(saved.metadata.ok, true);
    const after = {
      sessionID: session,
      messages: [userMessage("f7", "turn 7")],
      system: [],
    };
    await hooks.get("context")(after);
    assert.equal(
      after.system.find((block) => /Auto-flush/.test(block.text)),
      undefined,
    );
  });
});

describe("recall through the hooks (needs a populated store)", { skip }, () => {
  it("injects recalled facts before a turn", async () => {
    const event = {
      sessionID: "plugin-session",
      messages: [userMessage("m1", "deploy de mygym en el VPS")],
      system: [],
    };
    await hooks.get("context")(event);
    assert.equal(event.system.length, 1);
    assert.match(event.system[0].text, /Holographic Memory/);
  });

  it("injects nothing for a greeting", async () => {
    const event = {
      sessionID: "plugin-greeting",
      messages: [userMessage("g1", "buenos dias")],
      system: [],
    };
    await hooks.get("context")(event);
    assert.equal(event.system.length, 0);
  });

  it("recalls once per user message, not once per model call", async () => {
    const session = "plugin-dedupe";
    const first = {
      sessionID: session,
      messages: [userMessage("t1", "que middleware de idempotencia usamos")],
      system: [],
    };
    await hooks.get("context")(first);
    const second = {
      sessionID: session,
      messages: [userMessage("t1", "que middleware de idempotencia usamos")],
      system: [],
    };
    await hooks.get("context")(second);
    assert.equal(second.system[0].text, first.system[0].text);
    const third = {
      sessionID: session,
      messages: [userMessage("t2", "que middleware de idempotencia usamos")],
      system: [],
    };
    await hooks.get("context")(third);
    assert.notEqual(third.system[0].text, first.system[0].text);
  });

  it("re-injects the same facts into a compaction request", async () => {
    const event = {
      sessionID: "plugin-compaction",
      messages: [userMessage("c1", "stack de qs")],
      system: [],
    };
    await hooks.get("compaction")(event);
    const again = {
      sessionID: "plugin-compaction",
      messages: [userMessage("c1", "stack de qs")],
      system: [],
    };
    await hooks.get("context")(again);
    if (again.system.length > 0)
      assert.equal(event.system[0].text, again.system[0].text);
  });
});
