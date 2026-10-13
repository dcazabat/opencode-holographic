#!/usr/bin/env python3
"""opencode-holographic sidecar.

Runs the *real* Hermes `holographic` memory provider (same code, same SQLite file)
and exposes it to OpenCode over a Unix socket with newline-delimited JSON.

Protocol (one JSON object per line, one response per request):

    -> {"id": 1, "cmd": "ping"}
    <- {"id": 1, "ok": true, "result": {...}}

Commands
    ping                                  -> sidecar status (db path, tools, session)
    schemas                               -> the provider's tool schemas (fact_store, fact_feedback)
    prefetch {text, session_id}           -> recall block to inject before a turn ("" = nothing)
    tool {name, args, session_id?}        -> provider.handle_tool_call(...) result (JSON string)
    status                                -> recall indicator for the last prefetch
    shutdown                              -> stop the sidecar

Everything stays local: no network, no API keys.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import traceback
from pathlib import Path

logger = logging.getLogger("opencode-holographic")

_HERMES_HOME = str(Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser())

_PROVIDER = None
_CONFIG: dict = {}
_DB_PATH = ""
_CURRENT_SESSION = ""
_LOCK = threading.RLock()
_ALLOWED_TOOLS: set[str] = set()
_SERVER = None
_STOPPING = False


def _load_provider(args: argparse.Namespace) -> None:
    global _PROVIDER, _CONFIG, _DB_PATH, _CURRENT_SESSION, _ALLOWED_TOOLS

    import holographic as HL  # needs PYTHONPATH=<hermes home>/plugins

    try:
        _CONFIG = dict(HL._load_plugin_config() or {})
    except Exception as e:  # config loading is best effort
        logger.warning("could not load the Hermes plugin config: %s", e)
        _CONFIG = {}

    if args.db_path:
        _CONFIG["db_path"] = args.db_path
    _DB_PATH = str(_CONFIG.get("db_path") or "")

    _PROVIDER = HL.HolographicMemoryProvider(config=_CONFIG)
    _CURRENT_SESSION = args.session_id
    _PROVIDER.initialize(args.session_id, hermes_home=args.hermes_home, platform="opencode")

    schemas = _PROVIDER.get_tool_schemas() or []
    _ALLOWED_TOOLS = {s.get("name") for s in schemas if s.get("name")}
    logger.info(
        "ready (db_path=%s, tools=%s, hrr_dim=%s)",
        _DB_PATH,
        sorted(_ALLOWED_TOOLS),
        _CONFIG.get("hrr_dim", 1024),
    )


def _ensure_session(session_id: str) -> None:
    """Re-initialise when the OpenCode session changes: the provider records the source
    of every written version, and recall state is keyed per session."""
    global _CURRENT_SESSION
    session_id = str(session_id or "")
    if not session_id or session_id == _CURRENT_SESSION:
        return
    _PROVIDER.initialize(session_id, hermes_home=_HERMES_HOME, platform="opencode")
    _CURRENT_SESSION = session_id


def _handle(cmd: str, args: dict) -> object:
    with _LOCK:
        if cmd == "ping":
            return {
                "ok": True,
                "db_path": _DB_PATH,
                "session": _CURRENT_SESSION,
                "tools": sorted(_ALLOWED_TOOLS),
            }
        if cmd == "schemas":
            return _PROVIDER.get_tool_schemas() or []
        if cmd == "prefetch":
            _ensure_session(args.get("session_id", ""))
            return _PROVIDER.prefetch(str(args.get("text") or ""), session_id=str(args.get("session_id") or ""))
        if cmd == "tool":
            name = str(args.get("name") or "")
            if name not in _ALLOWED_TOOLS:
                raise ValueError(f"unknown tool {name!r}; expected one of {sorted(_ALLOWED_TOOLS)}")
            _ensure_session(args.get("session_id", ""))
            payload = args.get("args") or {}
            if not isinstance(payload, dict):
                raise ValueError("tool args must be an object")
            return _PROVIDER.handle_tool_call(name, payload)
        if cmd == "status":
            status = _PROVIDER.recall_status()
            return {"count": getattr(status, "count", 0)} if status else None
        if cmd == "shutdown":
            threading.Thread(target=_stop, name="stop", daemon=True).start()
            return {"stopping": True}
        raise ValueError(f"unknown command {cmd!r}")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            raw = raw.strip()
            if not raw:
                continue
            req_id = None
            try:
                request = json.loads(raw)
                req_id = request.get("id")
                result = _handle(str(request.get("cmd") or ""), request.get("args") or {})
                response = {"id": req_id, "ok": True, "result": result}
            except Exception as e:
                logger.debug("request failed: %s\n%s", e, traceback.format_exc())
                response = {"id": req_id, "ok": False, "error": f"{type(e).__name__}: {e}"}
            try:
                self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _stop(*_args: object) -> None:
    """Stops the server. Must never run on the thread that runs serve_forever():
    server.shutdown() waits for that loop, and the loop cannot advance while the
    signal handler (or any other call) is executing on top of it."""
    global _STOPPING
    with _LOCK:
        if _STOPPING:
            return
        _STOPPING = True
        if _PROVIDER is not None:
            try:
                _PROVIDER.shutdown()
            except Exception as e:
                logger.warning("shutdown failed: %s", e)
    if _SERVER is not None:
        _SERVER.shutdown()


def _request_stop(*_args: object) -> None:
    """Signal handler entry point: delegates to a worker thread and returns."""
    threading.Thread(target=_stop, name="stop", daemon=True).start()


def _prepare_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        path.unlink(missing_ok=True)
    else:
        raise SystemExit(f"another sidecar is already listening on {path}")
    finally:
        probe.close()


def main() -> int:
    global _SERVER
    parser = argparse.ArgumentParser(description="opencode-holographic sidecar")
    parser.add_argument(
        "--socket",
        default=os.environ.get("OPENCODE_HOLOGRAPHIC_SOCKET")
        or str(Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "opencode-holographic.sock"),
    )
    parser.add_argument("--db-path", default=os.environ.get("OPENCODE_HOLOGRAPHIC_DB") or None)
    parser.add_argument("--hermes-home", default=_HERMES_HOME)
    parser.add_argument("--session-id", default="opencode")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    _load_provider(args)

    sock = Path(args.socket).expanduser()
    _prepare_socket(sock)
    _SERVER = _Server(str(sock), _Handler)
    os.chmod(sock, 0o600)
    logger.info("listening on %s", sock)

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    try:
        _SERVER.serve_forever()
    finally:
        with _LOCK:
            if _PROVIDER is not None:
                try:
                    _PROVIDER.shutdown()
                except Exception:
                    pass
        sock.unlink(missing_ok=True)
        logger.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
