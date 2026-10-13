# opencode-holographic

Give **OpenCode** a long-term fact memory, with or without Hermes:

- **With Hermes** (default): OpenCode reuses **the real `holographic` provider** and **the same
  SQLite store** Hermes uses, so both agents remember the same things.
- **Without Hermes**: the provider is loaded from the copy vendored in this repository and the
  database lives **wherever you choose** at install time.

Either way you get:

- **Recall before every turn**: facts relevant to your message are injected into the system
  context (HRR + FTS5 + trust ranking, relevance gate, per-session repeat window).
- **The original tools**: `fact_store` and `fact_feedback`, with the exact schemas upstream ships.
- **A local store**: SQLite, no API key, no network, no model calls.

[Español](README.es.md) · [English](#why-this-exists)

```
OpenCode turn ── context hook ──┐
                                ├──▶ sidecar (Unix socket) ──▶ holographic.prefetch()
fact_store / fact_feedback ─────┘                          └──▶ holographic.handle_tool_call()
                                       │
                                  memory_store.db  ◀── shared with Hermes (WAL) or standalone
```

## Why this exists

`holographic` is a **local** memory: a SQLite fact store with FTS5 search, trust scoring, entity
resolution and HRR (holographic reduced representation) retrieval.

OpenCode has no such provider, so there are three ways to get one:

1. **Use another memory plugin** (opencode-mem, Hindsight, Mem0, …). Good products, but they keep
   _their own_ store: nothing you taught Hermes is recalled in OpenCode, and vice versa.
2. **Reimplement `holographic` in TypeScript.** It is ~5.700 lines of Python (`text.py` alone is
   2.031: stopwords, stem forms, CJK dictionaries). A port drifts from upstream on every release.
   The existing sibling port
   ([`tw2818/holographic-opencode-plugin`](https://github.com/tw2818/holographic-opencode-plugin))
   proves the point: same table DDL, but **raw float64 vectors without the `HRR1` prefix**,
   **RRF fusion** instead of the weighted `0.4 FTS5 + 0.3 jaccard + 0.3 HRR`, no IDF relevance
   gate, no per-session repeat window, and it targets the OpenCode **V1** plugin API. It cannot
   read a Hermes store.
3. **Reuse the real provider behind a small boundary** ← _this project_.

A ~250-line sidecar runs the actual provider and exposes it over a Unix socket. The OpenCode plugin
is a thin client: one hook for recall, two tools for writes. Exact parity, and upstream fixes
arrive for free.

## Modes and where the database lives

| Mode           | When                      | Provider comes from                                   | Database                                                                                                          |
| -------------- | ------------------------- | ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| **shared**     | a Hermes install is found | `$HERMES_HOME/plugins/holographic` (Hermes' own copy) | Hermes' `config.yaml → plugins.hermes-memory-store.db_path`                                                       |
| **standalone** | no Hermes                 | `vendor/holographic` + `sidecar/hermes_shim.py`       | **you choose it** at install time (`--db`, or the prompt), stored in `~/.config/opencode-holographic/config.json` |

`./install.sh` decides (`auto`) and asks only in standalone mode. Override the database at any time
with `--db`, the `OPENCODE_HOLOGRAPHIC_DB` environment variable, or `"dbPath"` in the plugin options.
In shared mode the provider reads Hermes' config itself, so the path is never duplicated.

Resolution order for the store: `--db-path` → `OPENCODE_HOLOGRAPHIC_DB` → `dbPath` in the config
file → (shared) Hermes' `config.yaml` → default `~/.local/share/opencode-holographic/memory_store.db`.

## How it works

| Piece                    | Role                                                                                                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `install.sh`             | Picks the mode, asks where the database goes, writes the config, installs the service, verifies it.                                                                               |
| `sidecar/server.py`      | Runs the provider and serves the protocol below.                                                                                                                                  |
| `sidecar/hermes_shim.py` | Stand-in for the Hermes modules the provider imports (`agent.memory_provider`, `hermes_cli.*`, `hermes_constants`, `hermes_state_wal`, `hermes_time`, `tools.registry`, `utils`). |
| `sidecar/discover.sh`    | Finds an interpreter with `numpy`: `$OPENCODE_HOLOGRAPHIC_PYTHON` → project `.venv` → Hermes environments.                                                                        |
| `vendor/holographic/`    | The provider itself (MIT, pinned commit — see `vendor/holographic/UPSTREAM.md`).                                                                                                  |
| `index.ts` + `src/`      | OpenCode V2 plugin: hooks, tools, socket client, config.                                                                                                                          |

**Protocol** (newline-delimited JSON over a Unix socket, one response per request):

| Command                         | Purpose                                                               |
| ------------------------------- | --------------------------------------------------------------------- |
| `ping`                          | Sidecar status: mode, provider source, database, tools.               |
| `schemas`                       | The provider's tool schemas, so the plugin mirrors them verbatim.     |
| `prefetch {text, session_id}`   | The recall block to inject before a turn (`""` = nothing).            |
| `tool {name, args, session_id}` | `handle_tool_call(...)` — the same path the provider uses for writes. |
| `status`                        | Recall indicator for the last prefetch.                               |
| `shutdown`                      | Stop the sidecar.                                                     |

**OpenCode side:**

- `ctx.session.hook("context")` → extract the last user message, ask for recall, push it into
  `event.system` as data.
- `ctx.session.hook("compaction")` → re-inject the same block so recalled facts survive a
  transcript rewrite.
- `ctx.tool.transform(...)` → register `fact_store` / `fact_feedback`, executed through the sidecar.

Two details that matter:

- OpenCode runs the `context` hook again for **every model call** (including tool continuations).
  The plugin computes recall **once per user message** and reuses the block, so the repeat window
  advances once per turn (like Hermes) while the facts stay in context across the turn.
- Writes go through `handle_tool_call`, so the write filters (facts that look like instructions or
  credentials are withheld from recall), the `HRR1` float32 vector and the `fact_versions` history
  row are produced by the provider itself. The plugin never issues raw SQL and never migrates the
  schema.

## Requirements

- OpenCode **V2**.
- **Python 3.11+ with `numpy`**. Shared mode also needs a Hermes installation (whose interpreter
  already has it); standalone mode is happy with any system interpreter, and `install.sh` creates a
  virtualenv for you if none has numpy.

## Install

```bash
git clone https://github.com/dcazabat/opencode-holographic.git ~/Documentos/opencode-holographic
cd ~/Documentos/opencode-holographic
npm install            # dev dependencies only (types + typescript)
./install.sh           # interactive: picks the mode and asks where the database goes
```

Non-interactive examples:

```bash
./install.sh --mode standalone --db ~/memoria/memory_store.db --yes   # no Hermes
./install.sh --mode shared --yes                                      # reuse Hermes' store
./install.sh --help
```

The installer writes `~/.config/opencode-holographic/config.json`, installs the
`opencode-holographic.service` **user** service (`Restart=always`), starts it and prints a summary:

```
==> mode: standalone
==> database: /home/you/memoria/memory_store.db (will be created)
  mode=standalone  provider=vendored  db=/home/you/memoria/memory_store.db  tools=fact_feedback,fact_store
```

Then register the plugin in `~/.config/opencode/opencode.json`. The path must be a **directory**
(OpenCode rejects a file path with _"configured plugin path must be a directory"_), and OpenCode
loads `<directory>/index.ts`, so editing the TypeScript needs **no rebuild**:

```jsonc
{
  "plugins": [
    {
      "package": "/home/<you>/Documentos/opencode-holographic",
      "options": { "socketPath": "/run/user/1000/opencode-holographic.sock" },
    },
  ],
}
```

Restart OpenCode and check:

```bash
opencode service restart
~/Documentos/opencode-holographic/sidecar/run.sh --check   # mode, provider, database, tools
```

In a session, the tools should now be available:

```
fact_store({ action: "stats" })
fact_store({ action: "search", query: "deploy" })
fact_store({ action: "add", content: "Something worth remembering." })
```

### Manual install (no installer)

```bash
npm test                                   # 24 tests, against a copy of the store
cp systemd/opencode-holographic.service ~/.config/systemd/user/   # adjust the paths inside
systemctl --user daemon-reload && systemctl --user enable --now opencode-holographic
```

## Configuration

`~/.config/opencode-holographic/config.json` (written by the installer):

| Key                                                                                                                  | Meaning                                                      |
| -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `mode`                                                                                                               | `shared` \| `standalone` (`auto` by default in the sidecar). |
| `dbPath`                                                                                                             | The store file (standalone).                                 |
| `socketPath`                                                                                                         | Where the sidecar listens.                                   |
| `hermesHome`                                                                                                         | Hermes installation to use in shared mode.                   |
| `hrr_dim`, `hrr_weight`, `prefetch_min_coverage`, `prefetch_repeat_turns`, `min_trust_threshold`, `recall_source`, … | Passed straight to the provider.                             |

Plugin options in `opencode.json` (all optional; environment variables are read as defaults):

| Option       | Env                           | Default                                      | Meaning                                                 |
| ------------ | ----------------------------- | -------------------------------------------- | ------------------------------------------------------- |
| `socketPath` | `OPENCODE_HOLOGRAPHIC_SOCKET` | `$XDG_RUNTIME_DIR/opencode-holographic.sock` | Socket the sidecar listens on.                          |
| `configPath` | `OPENCODE_HOLOGRAPHIC_CONFIG` | `~/.config/opencode-holographic/config.json` | Config file passed to the sidecar.                      |
| `dbPath`     | `OPENCODE_HOLOGRAPHIC_DB`     | –                                            | Explicit store path (overrides the config file).        |
| `launcher`   | –                             | `<project>/sidecar/run.sh`                   | Starts the sidecar when the socket is missing.          |
| `pythonPath` | `OPENCODE_HOLOGRAPHIC_PYTHON` | auto-detected                                | Interpreter override.                                   |
| `hermesHome` | `HERMES_HOME`                 | `~/.hermes`                                  | Hermes home (shared mode).                              |
| `timeoutMs`  | –                             | `8000`                                       | Per-request timeout; a failure degrades to "no memory". |
| `spawn`      | –                             | `true`                                       | Start the sidecar on demand when the service is down.   |
| `debug`      | –                             | `false`                                      | Log sidecar traffic to the OpenCode log.                |

## Operations

```bash
systemctl --user status opencode-holographic.service
journalctl --user -u opencode-holographic -f      # mode, recall, write-filter and error logs
systemctl --user restart opencode-holographic     # after upgrading Hermes
sidecar/run.sh --check --verbose                  # what the sidecar would do, without starting it
npm run sidecar                                   # run the sidecar in the foreground
```

## Troubleshooting

| Symptom                                            | Cause / fix                                                                                                       |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `configured plugin path must be a directory`       | Point `plugins[].package` at the project directory, not at `dist/index.js`.                                       |
| Tools missing in a session                         | Restart OpenCode (`opencode service restart`) after changing `opencode.json`.                                     |
| `no database path configured; using the default …` | Standalone with nothing configured: run `./install.sh` (it writes `dbPath`), or pass `--db`.                      |
| `standalone mode needs the vendored provider at …` | The clone is incomplete: `vendor/holographic/` must exist.                                                        |
| `discover.sh: no interpreter with numpy found`     | Run `./install.sh` (it creates a `.venv`) or set `OPENCODE_HOLOGRAPHIC_PYTHON`.                                   |
| Recall finds nothing                               | Check the database with `fact_store({action:"stats"})`; greetings and short messages deliberately recall nothing. |
| Tests skip with _"requires a Hermes install"_      | Expected without Hermes. With Hermes but no facts, set `HOLOGRAPHIC_TEST_SOURCE` to a `memory_store.db`.          |

**Inspecting the store by hand:** copy the database **with its `-wal` and `-shm`** before reading it.
A store's `.db` file can be far behind the live state (vectors, new tables) while the pending changes
sit in the WAL; and Hermes' _default_ path can exist as an empty 0-byte decoy while the real store is
the configured `db_path`.

## Limitations

- Single-user: the provider runs in `profile` scope (the per-user / per-chat scopes are not used).
- `fact_store` writes are not mirrored into Hermes' `MEMORY.md` / `USER.md`; only the fact store is shared.
- Facts that look like instructions or credentials are withheld from **recall** by the provider (by design); `search` still finds them.
- Standalone mode follows the vendored commit; see `vendor/holographic/UPSTREAM.md` to update it.
- Shared mode depends on Hermes' internal layout. `discover.sh` re-detects the interpreter, but restart the service after upgrading Hermes.
- Not affiliated with or endorsed by Nous Research or the OpenCode project.

## Vendored upstream

`vendor/holographic/` is a verbatim copy of the MIT-licensed provider
([`itpartypattaya/hermes-plugin-holographic`](https://github.com/itpartypattaya/hermes-plugin-holographic),
originally `NousResearch/hermes-plugin-holographic`), pinned by commit in
`vendor/holographic/UPSTREAM.md`. Its `LICENSE` and `LICENSES/` (dictionary data) are kept alongside.
It is only used in standalone mode; when Hermes is present its own copy wins.
