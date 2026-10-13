#!/usr/bin/env bash
# Launches the sidecar with the Hermes interpreter and the holographic package on PYTHONPATH.
# Used by systemd (always-on) and by the OpenCode plugin when the socket is missing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

PY="${OPENCODE_HOLOGRAPHIC_PYTHON:-}"
if [ -z "$PY" ]; then
  PY="$("$HERE/discover.sh")"
fi

# The plugin package lives in $HERMES_HOME/plugins; Hermes' own modules come from the venv.
export PYTHONPATH="${HERMES_HOME}/plugins${PYTHONPATH:+:$PYTHONPATH}"
export OPENCODE_HOLOGRAPHIC_DB="${OPENCODE_HOLOGRAPHIC_DB:-}"

exec "$PY" "$HERE/server.py" "$@"
