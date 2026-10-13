#!/usr/bin/env python3
"""opencode-holographic sidecar.

Runs the `holographic` memory provider and exposes it to OpenCode over a Unix socket
with newline-delimited JSON.

Two modes:

- **shared** — a Hermes installation is present: the provider is loaded from
  `$HERMES_HOME/plugins/holographic` and the store path comes from Hermes' own
  `config.yaml`, so both agents use the same database.
- **standalone** — no Hermes: the provider is loaded from `vendor/holographic` with
  `sidecar/hermes_shim.py` standing in for the Hermes modules, and the store lives
  wherever `--db-path` / the config file / the environment says.

Protocol (one JSON object per line, one response per request):

    -> {"id": 1, "cmd": "ping"}
    <- {"id": 1, "ok": true, "result": {...}}

Commands
    ping                                  -> sidecar status (mode, database, tools)
    schemas                               -> the provider's tool schemas
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

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
DEFAULT_CONFIG = Path("~/.config/opencode-holographic/config.json")
DEFAULT_DB = Path("~/.local/share/opencode-holographic/memory_store.db")

# Keys the provider accepts; a config file may override any of them.
_PROVIDER_KEYS = (
    "hrr_dim",
    "hrr_weight",
    "fts_weight",
    "jaccard_weight",
    "prefetch_min_coverage",
    "prefetch_repeat_turns",
    "min_trust_threshold",
    "default_trust",
    "recall_source",
    "recall_indicator",
    "auto_extract",
    "mirror_memory_writes",
    "temporal_decay_half_life",
    "scope",
)

_PROVIDER = None
_CONFIG: dict = {}
_DB_PATH = ""
_MODE = ""
_SOURCE = ""
_CURRENT_SESSION = ""
_LOCK = threading.RLock()
_ALLOWED_TOOLS: set[str] = set()
_SERVER = None
_STOPPING = False
_HERMES_HOME = str(Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser())
_TOKEN = ""


def default_endpoint() -> str:
    """Unix socket where the OS provides one, TCP loopback on Windows (Node there does
    not connect to a Python AF_UNIX socket; it uses named pipes)."""
    if sys.platform == "win32":
        return "tcp://127.0.0.1:4748"
    if sys.platform == "darwin":
        return str(Path("~/Library/Caches/opencode-holographic/sidecar.sock").expanduser())
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return str(Path(runtime) / "opencode-holographic.sock")
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return str(Path("/tmp") / f"opencode-holographic-{uid}.sock")


def parse_endpoint(value: str) -> tuple[str, object]:
    """`tcp://host:port`, `unix:/path`, `unix:///path` or a plain path (unix)."""
    text = str(value or "").strip()
    if text.startswith("tcp://"):
        host, _, port = text[len("tcp://"):].rpartition(":")
        return "tcp", (host or "127.0.0.1", int(port))
    for prefix in ("unix://", "unix:"):
        if text.startswith(prefix):
            return "unix", Path(text[len(prefix):])
    return "unix", Path(text)


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


# --------------------------------------------------------------------------- config


def _expand(value: str) -> str:
    return str(Path(value).expanduser())


def load_config_file(path: str | None) -> dict:
    candidate = Path(_expand(path)) if path else DEFAULT_CONFIG.expanduser()
    if not candidate.is_file():
        return {}
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {candidate}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{candidate}: expected a JSON object")
    logger.info("config loaded from %s", candidate)
    return data


def detect_mode(requested: str, config: dict) -> str:
    """`shared` when Hermes can provide the provider, `standalone` otherwise."""
    if requested != "auto":
        return requested
    declared = str(config.get("mode") or "").strip().lower()
    if declared in ("shared", "standalone"):
        return declared
    hermes_plugin = Path(config.get("hermesHome") or _HERMES_HOME) / "plugins" / "holographic" / "__init__.py"
    if hermes_plugin.is_file():
        sys.path.insert(0, str(hermes_plugin.parent.parent))
        try:
            import hermes_shim  # noqa: PLC0415

            if hermes_shim.hermes_importable():
                return "shared"
        finally:
            sys.path.pop(0)
    return "standalone"


def resolve_db_path(args: argparse.Namespace, config: dict, mode: str) -> str:
    explicit = (
        args.db_path
        or os.environ.get("OPENCODE_HOLOGRAPHIC_DB")
        or config.get("dbPath")
        or (config.get("provider") or {}).get("db_path")
    )
    if explicit:
        return _expand(str(explicit))
    if mode == "shared":
        # Hermes' own config.yaml decides; the provider reads it when no path is given.
        return ""
    path = _expand(str(DEFAULT_DB))
    logger.warning("no database path configured; using the default %s", path)
    return path


def build_provider_config(config: dict, db_path: str, base: dict) -> dict:
    values = dict(base)
    for key in _PROVIDER_KEYS:
        if key in config:
            values[key] = config[key]
    values.update({k: v for k, v in (config.get("provider") or {}).items() if k in _PROVIDER_KEYS})
    if db_path:
        values["db_path"] = db_path
    return values


# --------------------------------------------------------------------------- provider


def load_provider(args: argparse.Namespace, config: dict, mode: str) -> tuple[object, str, object]:
    """Imports the provider for the selected mode; returns (class, source, module)."""
    import hermes_shim  # noqa: PLC0415

    if mode == "standalone":
        vendor = PROJECT / "vendor"
        if not (vendor / "holographic" / "__init__.py").is_file():
            raise SystemExit(f"standalone mode needs the vendored provider at {vendor}/holographic")
        hermes_shim.install()
        sys.path.insert(0, str(vendor))
        source = "vendored"
    else:
        hermes_home = Path(config.get("hermesHome") or _HERMES_HOME)
        sys.path.insert(0, str(hermes_home / "plugins"))
        source = "hermes"

    import holographic as provider_module  # noqa: PLC0415

    return provider_module.HolographicMemoryProvider, source, provider_module


def _load_provider(args: argparse.Namespace, config: dict) -> None:
    global _PROVIDER, _CONFIG, _DB_PATH, _MODE, _SOURCE, _CURRENT_SESSION, _ALLOWED_TOOLS

    mode = detect_mode(args.mode, config)
    provider_class, source, provider_module = load_provider(args, config, mode)

    base: dict = {}
    if mode == "shared":
        try:  # Hermes' own db_path / hrr settings, same loader the provider uses
            base = dict(provider_module._load_plugin_config() or {})
        except Exception as exc:
            logger.warning("could not read the Hermes plugin config: %s", exc)

    db_path = resolve_db_path(args, config, mode)
    _CONFIG = build_provider_config(config, db_path, base)
    _DB_PATH = str(_CONFIG.get("db_path") or "")
    if _DB_PATH and mode != "shared":
        Path(_DB_PATH).expanduser().parent.mkdir(parents=True, exist_ok=True)

    _PROVIDER = provider_class(config=_CONFIG)
    _MODE, _SOURCE = mode, source
    _CURRENT_SESSION = args.session_id
    _PROVIDER.initialize(args.session_id, hermes_home=config.get("hermesHome") or _HERMES_HOME, platform="opencode")

    schemas = _PROVIDER.get_tool_schemas() or []
    _ALLOWED_TOOLS = {s.get("name") for s in schemas if s.get("name")}
    logger.info(
        "ready (mode=%s, provider=%s, db_path=%s, tools=%s)",
        _MODE,
        _SOURCE,
        _DB_PATH or "(from Hermes config)",
        sorted(_ALLOWED_TOOLS),
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


# --------------------------------------------------------------------------- commands


def _handle(cmd: str, args: dict) -> object:
    with _LOCK:
        if cmd == "ping":
            return {
                "ok": True,
                "mode": _MODE,
                "provider": _SOURCE,
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
                if _TOKEN and str(request.get("token") or "") != _TOKEN:
                    raise PermissionError("unauthorized")
                result = _handle(str(request.get("cmd") or ""), request.get("args") or {})
                response = {"id": req_id, "ok": True, "result": result}
            except Exception as exc:
                logger.debug("request failed: %s\n%s", exc, traceback.format_exc())
                response = {"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class _TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
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
            except Exception as exc:
                logger.warning("shutdown failed: %s", exc)
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
    global _SERVER, _TOKEN
    parser = argparse.ArgumentParser(description="opencode-holographic sidecar")
    parser.add_argument(
        "--socket",
        default=os.environ.get("OPENCODE_HOLOGRAPHIC_SOCKET") or default_endpoint(),
        help="unix:/path, a plain path, or tcp://host:port",
    )
    parser.add_argument("--token", default=os.environ.get("OPENCODE_HOLOGRAPHIC_TOKEN") or None)
    parser.add_argument("--config", default=os.environ.get("OPENCODE_HOLOGRAPHIC_CONFIG") or None)
    parser.add_argument("--db-path", default=None, help="SQLite file; wins over the config file")
    parser.add_argument("--mode", default=os.environ.get("OPENCODE_HOLOGRAPHIC_MODE") or "auto",
                        choices=["auto", "shared", "standalone"])
    parser.add_argument("--hermes-home", default=_HERMES_HOME)
    parser.add_argument("--session-id", default="opencode")
    parser.add_argument("--check", action="store_true", help="print the resolved setup as JSON and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config_file(args.config)
    if args.hermes_home:
        config.setdefault("hermesHome", args.hermes_home)
    _TOKEN = str(args.token or config.get("token") or "")

    _load_provider(args, config)

    if args.check:
        print(json.dumps(
            {
                "ok": True,
                "mode": _MODE,
                "provider": _SOURCE,
                "db_path": _DB_PATH,
                "tools": sorted(_ALLOWED_TOOLS),
                "python": sys.executable,
                "endpoint": args.socket,
                "config": str(Path(_expand(args.config)) if args.config else DEFAULT_CONFIG.expanduser()),
            },
            ensure_ascii=False,
        ))
        _PROVIDER.shutdown()
        return 0

    kind, target = parse_endpoint(args.socket)
    if kind == "tcp":
        host, port = target
        if not _is_loopback(host) and not _TOKEN:
            logger.warning("listening on %s without a token: anyone who can reach that port can read and write the store", args.socket)
        _SERVER = _TcpServer((host, port), _Handler)
    else:
        _prepare_socket(target)
        _SERVER = _Server(str(target), _Handler)
        os.chmod(target, 0o600)
    logger.info("listening on %s (%s)", args.socket, kind)
    if _TOKEN:
        logger.info("token required")

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
        if kind == "unix":
            target.unlink(missing_ok=True)
        logger.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
