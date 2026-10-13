#!/usr/bin/env bash
# Launches the sidecar. Interpreter: --python, $OPENCODE_HOLOGRAPHIC_PYTHON, the project
# .venv, or Hermes' environment (see discover.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

PY="${OPENCODE_HOLOGRAPHIC_PYTHON:-}"
if [ -z "$PY" ] && [ -x "$PROJECT/.venv/bin/python3" ]; then
  PY="$PROJECT/.venv/bin/python3"
fi
if [ -z "$PY" ]; then
  PY="$("$HERE/discover.sh")"
fi

# Shared mode resolves the provider from $HERMES_HOME/plugins; standalone mode uses
# vendor/holographic. Both are handled by server.py.
export PYTHONPATH="${HERMES_HOME}/plugins${PYTHONPATH:+:$PYTHONPATH}"

exec "$PY" "$HERE/server.py" "$@"
