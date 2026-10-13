# Vendored upstream: holographic memory provider

This directory is a **verbatim copy** of the `holographic` provider (the code that also ships
inside Hermes), minus its tests, docs and language-dictionary build scripts.

- Upstream: <https://github.com/itpartypattaya/hermes-plugin-holographic>
  (the maintained home; the original copy lived at `NousResearch/hermes-plugin-holographic`,
  now archived)
- Version: 0.13.0
- Commit: `a6d009315bc72a7c4e68d6a0e60a6eeed68080fd`
- License: MIT — see `LICENSE` (Copyright (c) 2025 Nous Research, (c) 2026 itpartypattaya) and
  `LICENSES/` for the bundled dictionary data (Apache-2.0, CC-BY-4.0, CC0, MIT).

It is used **only in standalone mode** (no Hermes installation). When Hermes is present, the
sidecar loads the provider from `$HERMES_HOME/plugins/holographic` instead, so the two never
compete.

## Updating

```bash
git clone --depth 1 https://github.com/itpartypattaya/hermes-plugin-holographic /tmp/holographic
cd /tmp/holographic && git rev-parse HEAD
# copy the same file list, then update Version/Commit above and run: npm test
```

`sidecar/hermes_shim.py` provides the Hermes modules the provider imports
(`agent.memory_provider`, `hermes_cli.*`, `hermes_constants`, `hermes_state_wal`,
`hermes_time`, `tools.registry`, `utils`). After an upstream update, re-check that list:
`grep -rn "^\(from\|import\) " vendor/holographic/*.py`.
