/**
 * Shared test helpers.
 *
 * The tests never touch the live database: they take a copy (including the WAL and
 * shared-memory files) or, when there is no store to copy, let the provider create a
 * fresh one. Point HOLOGRAPHIC_TEST_SOURCE at a `memory_store.db` to run the recall
 * assertions; without it those suites skip instead of failing.
 */
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname;

/** Path of the store to copy, or undefined when none is configured/available. */
export function resolveStore() {
  const hermesHome = process.env.HERMES_HOME ?? join(homedir(), ".hermes");
  const candidates = [
    process.env.HOLOGRAPHIC_TEST_SOURCE,
    dbPathFromHermesConfig(hermesHome),
    join(hermesHome, "memory_store.db"),
  ];
  for (const candidate of candidates) {
    if (candidate && isUsableStore(candidate)) return candidate;
  }
  return undefined;
}

/** `db_path` from the Hermes config, when it is spelled out there. */
function dbPathFromHermesConfig(hermesHome) {
  const file = join(hermesHome, "config.yaml");
  if (!existsSync(file)) return undefined;
  const match = readFileSync(file, "utf-8").match(/^\s*db_path:\s*(\S+)\s*$/m);
  if (!match) return undefined;
  return match[1].replace(/^["']|["']$/g, "").replace(/^~/, homedir());
}

/** An empty file is not a store: the default path can exist as a 0-byte decoy. */
function isUsableStore(path) {
  try {
    return statSync(path).size > 0;
  } catch {
    return false;
  }
}

export const SKIP_REASON =
  "requires a Hermes install (interpreter with numpy/ruamel.yaml/hermes_cli) and a holographic store";

/**
 * `false` when the suites can run, otherwise the reason to skip them. The plugin needs
 * a Hermes installation for the interpreter and the `holographic` package; the recall
 * assertions additionally need a populated store to copy.
 */
export function skipReason() {
  if (!hasHermesInterpreter()) {
    return "requires a Hermes install: sidecar/discover.sh found no interpreter with numpy/ruamel.yaml/hermes_cli";
  }
  if (!resolveStore()) {
    return "requires a holographic store: set HOLOGRAPHIC_TEST_SOURCE to a memory_store.db";
  }
  return false;
}

function hasHermesInterpreter() {
  try {
    execFileSync(join(ROOT, "sidecar", "discover.sh"), { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

/**
 * Returns a database path inside `directory`: a copy of the live store when there is
 * one, otherwise a path the provider will initialise itself.
 */
export function prepareDatabase(directory) {
  const target = join(directory, "memory_store.db");
  const source = resolveStore();
  if (!source) return target;
  for (const suffix of ["", "-wal", "-shm"]) {
    if (existsSync(`${source}${suffix}`))
      cpSync(`${source}${suffix}`, `${target}${suffix}`);
  }
  return target;
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function waitFor(condition, timeoutMs, what) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (condition()) return;
    await sleep(200);
  }
  throw new Error(`timed out waiting for ${what}`);
}
