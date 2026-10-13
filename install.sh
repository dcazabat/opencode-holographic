#!/usr/bin/env bash
# Installs opencode-holographic: picks the mode, decides where the memory database
# lives, writes the sidecar config, installs the always-on user service and verifies it.
#
#   ./install.sh                                  # interactive
#   ./install.sh --mode standalone --db ~/mem/memory_store.db --yes
#   ./install.sh --mode shared --yes               # reuse Hermes' store
#
# Modes
#   shared      a Hermes installation provides the provider and the store path
#   standalone  no Hermes: the vendored provider runs here and the store lives wherever
#               you say (--db, or the default below)
#   auto        shared when Hermes is usable, otherwise standalone   (default)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$HERE"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode-holographic"
CONFIG_FILE="$CONFIG_DIR/config.json"
DEFAULT_DB="$HOME/.local/share/opencode-holographic/memory_store.db"
OS="$(uname -s 2>/dev/null || echo unknown)"
case "$OS" in
  Darwin) DEFAULT_SOCKET="$HOME/Library/Caches/opencode-holographic/sidecar.sock" ;;
  *)      DEFAULT_SOCKET="$( [ -n "${XDG_RUNTIME_DIR:-}" ] && printf '%s' "$XDG_RUNTIME_DIR/opencode-holographic.sock" || printf '%s' "/tmp/opencode-holographic-$(id -u).sock" )" ;;
esac
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/opencode-holographic.service"

case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*)
    cat >&2 <<'MSG'
install.sh is for Linux and macOS. On Windows use the documented steps:
  1. py -3 -m venv .venv && .venv\Scripts\python -m pip install numpy
  2. run: .venv\Scripts\python sidecar\server.py --check --mode standalone --db C:\path\memory_store.db
  3. schedule sidecar\run.cmd at logon (Task Scheduler) and set the plugin options with
     "socketPath": "tcp://127.0.0.1:4748"
See the README section "Windows" for the full walkthrough.
MSG
    exit 1
    ;;
esac

MODE="auto"
DB_PATH=""
SOCKET_PATH="$DEFAULT_SOCKET"
PYTHON_PATH=""
ASSUME_YES=0
WITH_SERVICE=1
WITH_VENV=1

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:?}"; shift 2 ;;
    --db) DB_PATH="${2:?}"; shift 2 ;;
    --socket) SOCKET_PATH="${2:?}"; shift 2 ;;
    --python) PYTHON_PATH="${2:?}"; shift 2 ;;
    --hermes-home) HERMES_HOME="${2:?}"; shift 2 ;;
    --no-service) WITH_SERVICE=0; shift ;;
    --no-venv) WITH_VENV=0; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done
case "$MODE" in auto|shared|standalone) ;; *) die "--mode must be auto, shared or standalone" ;; esac

ask() { # ask <variable> <prompt> <default>
  local __var="$1" __prompt="$2" __default="$3" __answer=""
  if [ "$ASSUME_YES" = 1 ]; then printf -v "$__var" '%s' "$__default"; return; fi
  read -r -p "$__prompt [$__default]: " __answer || true
  printf -v "$__var" '%s' "${__answer:-$__default}"
}

has_numpy() { [ -n "${1:-}" ] && [ -x "$1" ] && "$1" -c "import numpy" >/dev/null 2>&1; }
hermes_present() { [ -f "$HERMES_HOME/plugins/holographic/__init__.py" ]; }

# ---------------------------------------------------------------- mode
if [ "$MODE" = auto ]; then
  if hermes_present; then MODE=shared; else MODE=standalone; fi
fi
if [ "$MODE" = shared ] && ! hermes_present; then
  die "shared mode needs a Hermes install at $HERMES_HOME (plugins/holographic). Use --mode standalone."
fi
info "mode: $MODE"

# ---------------------------------------------------------------- interpreter
if [ -z "$PYTHON_PATH" ]; then
  if [ -x "$PROJECT/.venv/bin/python3" ] && has_numpy "$PROJECT/.venv/bin/python3"; then
    PYTHON_PATH="$PROJECT/.venv/bin/python3"
  fi
fi
if [ -z "$PYTHON_PATH" ] && hermes_present; then
  PYTHON_PATH="$(OPENCODE_HOLOGRAPHIC_PYTHON="" HERMES_HOME="$HERMES_HOME" "$HERE/sidecar/discover.sh" 2>/dev/null || true)"
  has_numpy "$PYTHON_PATH" || PYTHON_PATH=""
fi
if [ -z "$PYTHON_PATH" ]; then
  # A system interpreter with numpy is enough for standalone mode: no venv needed.
  for candidate in /usr/bin/python3 /usr/local/bin/python3 python3; do
    if has_numpy "$candidate"; then PYTHON_PATH="$candidate"; break; fi
  done
fi

if [ -z "$PYTHON_PATH" ] || ! has_numpy "$PYTHON_PATH"; then
  if [ "$WITH_VENV" = 0 ]; then die "no interpreter with numpy found and --no-venv was given"; fi
  info "creating a virtualenv at $PROJECT/.venv (needs numpy only)"
  python3 -m venv "$PROJECT/.venv" || die "python3 -m venv failed (install python3-venv?)"
  "$PROJECT/.venv/bin/python3" -m pip install --quiet --upgrade pip
  "$PROJECT/.venv/bin/python3" -m pip install --quiet numpy || die "pip install numpy failed"
  PYTHON_PATH="$PROJECT/.venv/bin/python3"
fi
info "interpreter: $PYTHON_PATH"

# ---------------------------------------------------------------- database
if [ "$MODE" = shared ]; then
  DB_FROM_HERMES="$(grep -E '^\s*db_path:' "$HERMES_HOME/config.yaml" 2>/dev/null | tail -1 | sed 's/^[[:space:]]*db_path:[[:space:]]*//' | tr -d '"'"'"'' || true)"
  DB_FROM_HERMES="${DB_FROM_HERMES/\~/$HOME}"
  info "database: ${DB_FROM_HERMES:-$HERMES_HOME/memory_store.db} (from Hermes' config.yaml, shared)"
  [ -n "${DB_FROM_HERMES:-}" ] && [ ! -f "$DB_FROM_HERMES" ] && warn "that file does not exist yet; Hermes creates it on first use"
  DB_PATH="" # shared mode: the sidecar reads it from Hermes
else
  if [ -z "$DB_PATH" ]; then
    printf '\nWhere should the memory database live?\n'
    printf '  (a single SQLite file; the directory is created if needed)\n'
    ask DB_PATH "Database path" "$DEFAULT_DB"
  fi
  DB_PATH="${DB_PATH/#\~/$HOME}"
  mkdir -p "$(dirname "$DB_PATH")"
  [ -e "$DB_PATH" ] && info "database: $DB_PATH (existing file, will be reused)" || info "database: $DB_PATH (will be created)"
fi

# ---------------------------------------------------------------- config
mkdir -p "$CONFIG_DIR"
DB_JSON="$DB_PATH" python3 - "$CONFIG_FILE" "$MODE" "$SOCKET_PATH" "$HERMES_HOME" <<'PY'
import json, os, sys
target, mode, socket, hermes_home = sys.argv[1:5]
data = {"mode": mode, "socketPath": socket, "hermesHome": hermes_home}
if mode == "standalone":
    data["dbPath"] = os.environ["DB_JSON"]
with open(target, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
PY
info "config written: $CONFIG_FILE"

# ---------------------------------------------------------------- service
if [ "$WITH_SERVICE" = 1 ] && [ "$OS" = "Darwin" ]; then
  AGENT_DIR="$HOME/Library/LaunchAgents"
  AGENT_FILE="$AGENT_DIR/com.opencode.holographic.plist"
  LOG_DIR="$HOME/Library/Logs"
  mkdir -p "$AGENT_DIR" "$LOG_DIR" "$(dirname "$SOCKET_PATH")"
  cat > "$AGENT_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.opencode.holographic</string>
  <key>ProgramArguments</key>
  <array><string>$PROJECT/sidecar/run.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HERMES_HOME</key><string>$HERMES_HOME</string>
    <key>OPENCODE_HOLOGRAPHIC_CONFIG</key><string>$CONFIG_FILE</string>
    <key>OPENCODE_HOLOGRAPHIC_SOCKET</key><string>$SOCKET_PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/opencode-holographic.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/opencode-holographic.log</string>
</dict>
</plist>
EOF
  launchctl unload "$AGENT_FILE" 2>/dev/null || true
  launchctl load -w "$AGENT_FILE"
  info "service: launchd ($AGENT_FILE) — logs: $LOG_DIR/opencode-holographic.log"
elif [ "$WITH_SERVICE" = 1 ]; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=opencode-holographic sidecar — holographic memory (fact_store) for OpenCode
Documentation=file:$PROJECT/README.md
After=default.target

[Service]
Type=simple
ExecStart=$PROJECT/sidecar/run.sh
Environment=HERMES_HOME=$HERMES_HOME
Environment=OPENCODE_HOLOGRAPHIC_CONFIG=$CONFIG_FILE
Environment=OPENCODE_HOLOGRAPHIC_SOCKET=$SOCKET_PATH
Restart=always
RestartSec=3
TimeoutStopSec=15

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now opencode-holographic.service
  info "service: $(systemctl --user is-active opencode-holographic.service) ($UNIT_FILE)"
fi

# ---------------------------------------------------------------- verify
info "verifying the setup"
CHECK="$(OPENCODE_HOLOGRAPHIC_CONFIG="$CONFIG_FILE" OPENCODE_HOLOGRAPHIC_PYTHON="$PYTHON_PATH" HERMES_HOME="$HERMES_HOME" \
  "$PROJECT/sidecar/run.sh" --check 2>/dev/null)" || die "the sidecar could not start; run: OPENCODE_HOLOGRAPHIC_PYTHON=$PYTHON_PATH $PROJECT/sidecar/run.sh --check --verbose"
printf '%s\n' "$CHECK" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  mode=%s  provider=%s  db=%s  tools=%s" % (d["mode"], d["provider"], d["db_path"] or "(from Hermes config)", ",".join(d["tools"])))'

cat <<EOF

Done.

Add the plugin to ~/.config/opencode/opencode.json and restart OpenCode:

  {
    "plugins": [
      {
        "package": "$PROJECT",
        "options": { "socketPath": "$SOCKET_PATH" }
      }
    ]
  }

  opencode service restart

Useful commands:
$(if [ "$OS" = "Darwin" ]; then cat <<'CMDS'
  launchctl list | grep holographic
  tail -f ~/Library/Logs/opencode-holographic.log
  launchctl unload -w ~/Library/LaunchAgents/com.opencode.holographic.plist   # stop
CMDS
else cat <<'CMDS'
  systemctl --user status opencode-holographic
  journalctl --user -u opencode-holographic -f
CMDS
fi)
  $PROJECT/sidecar/run.sh --check
EOF
