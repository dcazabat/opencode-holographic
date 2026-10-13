# Third-party notices

## Vendored provider: `holographic`

`vendor/holographic/` is a verbatim copy of the `holographic` memory provider (minus its tests,
docs and dictionary build scripts), used only in **standalone** mode.

- Upstream: <https://github.com/itpartypattaya/hermes-plugin-holographic>
  (maintained home; the original copy lived at `NousResearch/hermes-plugin-holographic`, now archived)
- Version 0.13.0, pinned by commit — see `vendor/holographic/UPSTREAM.md`
- License: **MIT**
  - Copyright (c) 2025 Nous Research
  - Copyright (c) 2026 itpartypattaya
  - Full text: `vendor/holographic/LICENSE`

### Bundled dictionary data

`vendor/holographic/dictionaries/` ships language data with their own licenses, kept in
`vendor/holographic/LICENSES/`:

| Data                        | License          |
| --------------------------- | ---------------- |
| DerivBase.Ru                | Apache-2.0       |
| jieba                       | MIT              |
| Open English WordNet (OEWN) | CC-BY-4.0 / WNDB |
| OpenCC                      | Apache-2.0       |
| SudachiDict                 | Apache-2.0       |
| Wikidata                    | CC0              |

When Hermes is installed, its own copy of the provider is used instead and nothing from
`vendor/` is loaded.
