# opencode-holographic

Give **OpenCode** the same long-term memory **Hermes** has — by reusing Hermes' real
`holographic` provider: same code, same SQLite store, no reimplementation.

- **Recall before every turn**: facts relevant to your message are injected into the system
  context (HRR + FTS5 + trust ranking, with a relevance gate and a per-session repeat window).
- **The original tools**: `fact_store` and `fact_feedback`, with the exact schemas Hermes ships.
- **One shared store**: OpenCode and Hermes read and write the same `memory_store.db` (WAL), so a
  fact learned in one shows up in the other.

[Español](README.es.md) · [English](#why-this-exists)

```
OpenCode turn ── context hook ──┐
                                ├──▶ sidecar (Unix socket) ──▶ holographic.prefetch()
fact_store / fact_feedback ─────┘                          └──▶ holographic.handle_tool_call()
                                       │
                                  memory_store.db  ◀── shared with Hermes (WAL)
```

## Why this exists

Hermes' `holographic` provider is a **local** memory: a SQLite fact store with FTS5 search, trust
scoring, entity resolution and HRR (holographic reduced representation) retrieval. No API key, no
network, no model calls.

OpenCode has no such provider, so there are three ways to get one:

1. **Use another memory plugin** (opencode-mem, Hindsight, Mem0, …). Great products, but they keep
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

A ~150-line sidecar runs the actual `holographic` provider inside Hermes' own isolated Python
environment and exposes it over a Unix socket. The OpenCode plugin is a thin client: a hook for
recall, two tools for writes. Exact parity, and upstream fixes arrive for free.

## How it works

| Piece                 | Role                                                                                                   |
| --------------------- | ------------------------------------------------------------------------------------------------------ |
| `sidecar/server.py`   | Runs the real provider (`HolographicMemoryProvider`) and serves the protocol below.                    |
| `sidecar/discover.sh` | Picks the newest Hermes environment whose interpreter imports `numpy`, `ruamel.yaml` and `hermes_cli`. |
| `sidecar/run.sh`      | Launcher: sets `PYTHONPATH=<hermes home>/plugins` and execs the server.                                |
| `index.ts` + `src/`   | OpenCode V2 plugin: hooks, tools, socket client, config.                                               |
| `systemd/…service`    | Always-on sidecar (user service, `Restart=always`).                                                    |

**Protocol** (newline-delimited JSON over a Unix socket, one response per request):

| Command                         | Purpose                                                           |
| ------------------------------- | ----------------------------------------------------------------- |
| `ping`                          | Sidecar status: database path, tools, current session.            |
| `schemas`                       | The provider's tool schemas, so the plugin mirrors them verbatim. |
| `prefetch {text, session_id}`   | The recall block to inject before a turn (`""` = nothing).        |
| `tool {name, args, session_id}` | `handle_tool_call(...)` — the same path Hermes uses for writes.   |
| `status`                        | Recall indicator for the last prefetch.                           |
| `shutdown`                      | Stop the sidecar.                                                 |

**OpenCode side:**

- `ctx.session.hook("context")` → extract the last user message, ask for recall, push it into
  `event.system` as data.
- `ctx.session.hook("compaction")` → re-inject the same block so recalled facts survive a
  transcript rewrite.
- `ctx.tool.transform(...)` → register `fact_store` / `fact_feedback`, executed through the
  sidecar.

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
- A **Hermes** installation whose `holographic` plugin is enabled, with `memory.provider: holographic`
  and a `plugins.hermes-memory-store.db_path` pointing at a `memory_store.db`.
  The sidecar resolves the database path from `~/.hermes/config.yaml`, so there is nothing to
  duplicate.
- Python 3.11+ with `numpy` (Hermes' bundled environment already has it).

## Install

```bash
# 1. sources
git clone https://github.com/dcazabat/opencode-holographic.git ~/Documentos/opencode-holographic
cd ~/Documentos/opencode-holographic
npm install          # dev dependencies only (types + typescript)
npm run build        # optional: compiles dist/ (a fallback + used by the tests)
npm test             # 20 tests; they copy the store and never touch the live one

# 2. sidecar as a user service (adjust the path in the unit if you cloned elsewhere)
cp systemd/opencode-holographic.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now opencode-holographic.service
systemctl --user status opencode-holographic.service
```

Register the plugin in `~/.config/opencode/opencode.json`. The path must be a **directory**
(OpenCode rejects a file path with _"configured plugin path must be a directory"_), and OpenCode
loads `<directory>/index.ts`, so editing the TypeScript needs **no rebuild**:

```jsonc
{
  "plugins": [
    {
      "package": "/home/<you>/Documentos/opencode-holographic",
      "options": {
        "socketPath": "/run/user/1000/opencode-holographic.sock",
        "timeoutMs": 8000,
        "spawn": true,
        "debug": false,
      },
    },
  ],
}
```

Then restart the service and check:

```bash
opencode service restart
python3 - <<'EOF'
import socket, json, os
path = f"{os.environ['XDG_RUNTIME_DIR']}/opencode-holographic.sock"
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(path)
f = s.makefile("rwb")
f.write((json.dumps({"id": 1, "cmd": "ping", "args": {}}) + "\n").encode()); f.flush()
print(json.loads(f.readline())["result"])
EOF
```

In a session, the tools should now be available:

```
fact_store({ action: "stats" })
fact_store({ action: "search", query: "deploy" })
fact_store({ action: "add", content: "Something worth remembering." })
```

## Configuration

Options go in the plugin entry of `opencode.json`; the environment variables are read as defaults.

| Option       | Env                           | Default                                      | Meaning                                                 |
| ------------ | ----------------------------- | -------------------------------------------- | ------------------------------------------------------- |
| `socketPath` | `OPENCODE_HOLOGRAPHIC_SOCKET` | `$XDG_RUNTIME_DIR/opencode-holographic.sock` | Socket the sidecar listens on.                          |
| `launcher`   | –                             | `<project>/sidecar/run.sh`                   | Starts the sidecar when the socket is missing.          |
| `pythonPath` | `OPENCODE_HOLOGRAPHIC_PYTHON` | auto-detected                                | Overrides `sidecar/discover.sh`.                        |
| `hermesHome` | `HERMES_HOME`                 | `~/.hermes`                                  | Where the `holographic` package and its config live.    |
| `dbPath`     | `OPENCODE_HOLOGRAPHIC_DB`     | from Hermes config                           | Explicit store path.                                    |
| `timeoutMs`  | –                             | `8000`                                       | Per-request timeout; a failure degrades to "no memory". |
| `spawn`      | –                             | `true`                                       | Start the sidecar on demand when the service is down.   |
| `debug`      | –                             | `false`                                      | Log sidecar traffic to the OpenCode log.                |

## Operations

```bash
systemctl --user status opencode-holographic.service
journalctl --user -u opencode-holographic -f      # recall, write-filter and error logs
systemctl --user restart opencode-holographic     # after upgrading Hermes
npm run sidecar                                   # run the sidecar in the foreground (verbose)
```

## Troubleshooting

| Symptom                                                  | Cause / fix                                                                                                    |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `configured plugin path must be a directory`             | Point `plugins[].package` at the project directory, not at `dist/index.js`.                                    |
| Tools missing in a session                               | Restart OpenCode (`opencode service restart`) after changing `opencode.json`.                                  |
| `sidecar not reachable at setup, using fallback schemas` | The service is down; the client will start it if `spawn: true`.                                                |
| `discover.sh: no Hermes interpreter found`               | No environment imports `numpy` + `ruamel.yaml` + `hermes_cli`; set `OPENCODE_HOLOGRAPHIC_PYTHON`.              |
| Recall finds nothing                                     | Check `db_path` with `fact_store({action:"stats"})`; greetings and short messages deliberately recall nothing. |
| Tests skip with _"requires a Hermes install"_            | Expected without Hermes. With Hermes but no facts, set `HOLOGRAPHIC_TEST_SOURCE` to a `memory_store.db`.       |

**Inspecting the store by hand:** copy the database **with its `-wal` and `-shm`** before reading it.
A store's `.db` file can be far behind the live state (vectors, new tables) while the pending
changes sit in the WAL; and Hermes' _default_ path can exist as an empty 0-byte decoy while the real
store is the configured `db_path`.

## Limitations

- Single-user: the provider runs in `profile` scope (the per-user / per-chat scopes of Hermes are not used).
- `fact_store` writes are not mirrored into Hermes' `MEMORY.md` / `USER.md`; only the fact store is shared.
- Facts that look like instructions or credentials are withheld from **recall** by the provider (by design); `search` still finds them.
- Depends on Hermes' internal layout. `discover.sh` re-detects the interpreter automatically, but after a Hermes upgrade restart the service.
- Not affiliated with or endorsed by Nous Research or the OpenCode project.
