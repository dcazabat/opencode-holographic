#!/usr/bin/env bash
# Prints the Python interpreter that runs the sidecar.
#
# Order:
#   1. $OPENCODE_HOLOGRAPHIC_PYTHON
#   2. the project's own virtualenv  (standalone install, .venv)
#   3. the newest Hermes environment / bundled interpreter with numpy   (shared install)
#
# Only numpy is required: Hermes' modules are only needed in shared mode, and the
# sidecar decides that itself (sidecar/hermes_shim.py stands in when they are absent).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

if [ -n "${OPENCODE_HOLOGRAPHIC_PYTHON:-}" ] && [ -x "${OPENCODE_HOLOGRAPHIC_PYTHON}" ]; then
  printf '%s\n' "${OPENCODE_HOLOGRAPHIC_PYTHON}"
  exit 0
fi

has_numpy() {
  "$1" -c "import numpy" >/dev/null 2>&1
}

if [ -x "$PROJECT/.venv/bin/python3" ] && has_numpy "$PROJECT/.venv/bin/python3"; then
  printf '%s\n' "$PROJECT/.venv/bin/python3"
  exit 0
fi

candidates=()
while IFS= read -r py; do candidates+=("$py"); done < <(
  ls -1dt "${HERMES_HOME}"/installs/*/environments/*/venv/bin/python3 \
            "${HERMES_HOME}"/tools/python-*-linux-x64/bin/python3 2>/dev/null || true
)

for py in "${candidates[@]:-}"; do
  [ -x "$py" ] || continue
  if has_numpy "$py"; then
    printf '%s\n' "$py"
    exit 0
  fi
done

echo "discover.sh: no interpreter with numpy found (looked in $PROJECT/.venv and $HERMES_HOME)." >&2
echo "Run ./install.sh, or set OPENCODE_HOLOGRAPHIC_PYTHON." >&2
exit 1
