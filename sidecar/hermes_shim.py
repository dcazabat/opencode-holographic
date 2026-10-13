"""Minimal stand-ins for the Hermes modules the holographic provider imports.

Only installed when Hermes is **not** importable (the provider then comes from
`vendor/holographic`). The names and shapes mirror the real modules closely enough for
the provider's own code paths:

| stub | why the provider needs it |
| --- | --- |
| `agent.memory_provider` | base class + `RecallStatus` dataclass |
| `agent.context_compressor` | compaction-summary detection used by auto-extract |
| `hermes_cli.config` | `cfg_get` / `load_config_readonly` (we pass a config dict instead) |
| `hermes_cli.sqlite_util` | `add_column_if_missing`, the idempotent `ALTER TABLE` |
| `hermes_constants` | `get_hermes_home` and its override |
| `hermes_state_wal` | `apply_wal_with_fallback` |
| `hermes_time` | `get_timezone` (None = server-local, which the provider handles) |
| `tools.registry` | `tool_error` / `tool_result` |
| `utils` | `is_truthy_value` |
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
from dataclasses import dataclass
from pathlib import Path

INDICATOR_GLYPH = "🧠"
TRUTHY_STRINGS = frozenset({"1", "true", "yes", "y", "on", "enabled"})
_MERGED_PRIOR_CONTEXT_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
_MERGED_SUMMARY_DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
_MAX_ERROR_CHARS = 2000

_HOME_OVERRIDE: dict[str, str | None] = {"value": None}

# Hermes modules this shim replaces. Missing any of them means "standalone mode".
SHIMMED_MODULES = (
    "agent.memory_provider",
    "hermes_cli.config",
    "hermes_state_wal",
    "hermes_constants",
    "hermes_time",
    "tools.registry",
    "utils",
)


def hermes_importable() -> bool:
    """True when the real Hermes modules are on the path (shared mode)."""
    for name in SHIMMED_MODULES:
        try:
            __import__(name)
        except Exception:
            return False
    return True


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def install() -> None:
    """Register the stub modules. Idempotent."""
    if getattr(sys.modules.get("hermes_state_wal"), "__standalone_shim__", False):
        return

    # utils -----------------------------------------------------------------
    utils = _module("utils")

    def is_truthy_value(value: object, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in TRUTHY_STRINGS
        return bool(value)

    utils.is_truthy_value = is_truthy_value
    utils.TRUTHY_STRINGS = TRUTHY_STRINGS

    # tools.registry --------------------------------------------------------
    tools_package = _module("tools")
    registry = _module("tools.registry")

    def tool_error(message: object, **extra: object) -> str:
        return json.dumps({"error": str(message)[:_MAX_ERROR_CHARS], **extra}, ensure_ascii=False)

    def tool_result(data: object = None, **kwargs: object) -> str:
        return json.dumps({**(data or {}), **kwargs}, ensure_ascii=False)  # type: ignore[arg-type]

    registry.tool_error = tool_error
    registry.tool_result = tool_result
    tools_package.registry = registry

    # hermes_time -----------------------------------------------------------
    hermes_time = _module("hermes_time")
    hermes_time.get_timezone = lambda: None  # None = server-local
    hermes_time.get_timezone_name = lambda: ""

    # hermes_constants ------------------------------------------------------
    hermes_constants = _module("hermes_constants")

    def get_hermes_home() -> Path:
        if _HOME_OVERRIDE["value"]:
            return Path(_HOME_OVERRIDE["value"]).expanduser()
        return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()

    def set_hermes_home_override(path: str) -> None:
        _HOME_OVERRIDE["value"] = path

    def reset_hermes_home_override(*_args: object) -> None:
        _HOME_OVERRIDE["value"] = None

    def get_hermes_home_override() -> str | None:
        return _HOME_OVERRIDE["value"]

    hermes_constants.get_hermes_home = get_hermes_home
    hermes_constants.set_hermes_home_override = set_hermes_home_override
    hermes_constants.reset_hermes_home_override = reset_hermes_home_override
    hermes_constants.get_hermes_home_override = get_hermes_home_override

    # hermes_state_wal ------------------------------------------------------
    hermes_state_wal = _module("hermes_state_wal")
    hermes_state_wal.__standalone_shim__ = True  # type: ignore[attr-defined]

    def apply_wal_with_fallback(
        conn: sqlite3.Connection, *, db_label: str = "state.db", require_wal: bool = False
    ) -> str:
        """WAL when the filesystem allows it, DELETE otherwise (NFS/SMB/FUSE)."""
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError:
            if require_wal:
                raise
            return str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        return str(row[0]).lower()

    hermes_state_wal.apply_wal_with_fallback = apply_wal_with_fallback

    # hermes_cli.{config,sqlite_util} ---------------------------------------
    hermes_cli = _module("hermes_cli")

    sqlite_util = _module("hermes_cli.sqlite_util")

    def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in columns:
            return False
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" in str(exc):  # lost the race with another migrator
                return False
            raise
        return True

    sqlite_util.add_column_if_missing = add_column_if_missing
    hermes_cli.sqlite_util = sqlite_util

    config = _module("hermes_cli.config")

    def cfg_get(cfg: object, *keys: str, default: object = None) -> object:
        current = cfg
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def load_config_readonly() -> dict:
        return {}  # standalone: the sidecar passes the config explicitly

    def save_config(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("standalone mode: there is no Hermes config to write")

    config.cfg_get = cfg_get
    config.load_config_readonly = load_config_readonly
    config.save_config = save_config
    hermes_cli.config = config

    # agent.memory_provider -------------------------------------------------
    agent = _module("agent")
    memory_provider = _module("agent.memory_provider")

    @dataclass
    class RecallStatus:
        provider_label: str
        count: int
        glyph: str = INDICATOR_GLYPH

    class MemoryProvider:
        """Permissive base: the provider overrides every method it uses."""

        def name(self) -> str:
            return "memory"

        def is_available(self) -> bool:
            return True

        def unavailable_reason(self) -> str:
            return ""

        def initialize(self, session_id: str, **kwargs: object) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            return ""

        def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
            return None

        def recall_status(self) -> RecallStatus | None:
            return None

        def get_tool_schemas(self) -> list[dict]:
            return []

        def handle_tool_call(self, tool_name: str, args: dict, **kwargs: object) -> str:
            raise NotImplementedError(tool_name)

        def system_prompt_block(self) -> str:
            return ""

        def identity_signature(self) -> dict:
            return {}

        def get_config_schema(self) -> list[dict]:
            return []

        def backup_paths(self) -> list[str]:
            return []

    memory_provider.MemoryProvider = MemoryProvider
    memory_provider.RecallStatus = RecallStatus
    memory_provider.INDICATOR_GLYPH = INDICATOR_GLYPH
    agent.memory_provider = memory_provider

    # agent.context_compressor (auto-extract only) --------------------------
    compressor = _module("agent.context_compressor")
    compressor._MERGED_PRIOR_CONTEXT_HEADER = _MERGED_PRIOR_CONTEXT_HEADER
    compressor._MERGED_SUMMARY_DELIMITER = _MERGED_SUMMARY_DELIMITER
    compressor.is_compaction_summary_message = lambda _message: False
    agent.context_compressor = compressor
