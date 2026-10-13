#!/usr/bin/env bash
# Prints the Python interpreter that can run the real Hermes `holographic` plugin.
# Order: $OPENCODE_HOLOGRAPHIC_PYTHON, then the newest Hermes isolated env that
# imports numpy + ruamel.yaml + hermes_cli, then the bundled Hermes interpreter.
set -euo pipefail

if [ -n "${OPENCODE_HOLOGRAPHIC_PYTHON:-}" ] && [ -x "${OPENCODE_HOLOGRAPHIC_PYTHON}" ]; then
  printf '%s\n' "${OPENCODE_HOLOGRAPHIC_PYTHON}"
  exit 0
fi

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

candidates=()
while IFS= read -r py; do candidates+=("$py"); done < <(
  ls -1dt "${HERMES_HOME}"/installs/*/environments/*/venv/bin/python3 \
            "${HERMES_HOME}"/tools/python-*-linux-x64/bin/python3 2>/dev/null || true
)

for py in "${candidates[@]:-}"; do
  [ -x "$py" ] || continue
  if "$py" -c "import numpy, ruamel.yaml, hermes_cli" >/dev/null 2>&1; then
    printf '%s\n' "$py"
    exit 0
  fi
done

echo "discover.sh: no Hermes interpreter found (need numpy + ruamel.yaml + hermes_cli)." >&2
echo "Set OPENCODE_HOLOGRAPHIC_PYTHON to override." >&2
exit 1
