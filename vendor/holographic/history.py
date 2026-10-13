"""Fact history (0.8.0): versions with valid time and provenance, the five ways a fact stops being current, and
forgetting for good. Mixed into MemoryStore; docs/history.md is the contract.

``facts`` stays the projection of current versions (no new column, so older versions of the plugin and tools that
read it keep working); ``fact_versions`` holds every version, the current one included, with its own FTS index that
only ``as_of`` reads."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import unicodedata
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_versions (
    version_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id        INTEGER NOT NULL,
    seq            INTEGER NOT NULL,
    content        TEXT NOT NULL,
    category       TEXT DEFAULT 'general',
    tags           TEXT DEFAULT '',
    valid_from     TEXT,
    valid_to       TEXT,
    valid_to_exact INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'current',
    recorded_at    TEXT NOT NULL,
    recorded_on    TEXT NOT NULL,
    closed_at      TEXT,
    supersedes     INTEGER,
    superseded_by  INTEGER,
    trust_score    REAL,
    src_via        TEXT,
    src_platform   TEXT,
    src_chat_type  TEXT,
    src_chat_id    TEXT,
    src_thread_id  TEXT,
    src_session_id TEXT,
    src_user_id    TEXT,
    src_user_name  TEXT,
    UNIQUE (fact_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_versions_fact  ON fact_versions(fact_id);
CREATE INDEX IF NOT EXISTS idx_versions_valid ON fact_versions(status, valid_from, valid_to);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_current ON fact_versions(fact_id) WHERE status = 'current';

CREATE VIRTUAL TABLE IF NOT EXISTS fact_versions_fts
    USING fts5(content, tags, content=fact_versions, content_rowid=version_id);
CREATE TRIGGER IF NOT EXISTS fact_versions_ai AFTER INSERT ON fact_versions BEGIN
    INSERT INTO fact_versions_fts(rowid, content, tags) VALUES (new.version_id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS fact_versions_ad AFTER DELETE ON fact_versions BEGIN
    INSERT INTO fact_versions_fts(fact_versions_fts, rowid, content, tags)
        VALUES ('delete', old.version_id, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS fact_versions_au AFTER UPDATE OF content, tags ON fact_versions BEGIN
    INSERT INTO fact_versions_fts(fact_versions_fts, rowid, content, tags)
        VALUES ('delete', old.version_id, old.content, old.tags);
    INSERT INTO fact_versions_fts(rowid, content, tags) VALUES (new.version_id, new.content, new.tags);
END;

-- Tombstones: the id and keyed hashes of every version's text, never the text.
CREATE TABLE IF NOT EXISTS forgotten (
    fact_id      INTEGER PRIMARY KEY,
    forgotten_at TEXT NOT NULL,
    digests      TEXT NOT NULL
);
"""

HISTORY_SCHEMA_VERSION = 1
SOURCE_FIELDS = ("via", "platform", "chat_type", "chat_id", "thread_id", "session_id", "user_id", "user_name")
_SOURCE_COLUMNS = tuple(f"src_{f}" for f in SOURCE_FIELDS)
CLOSED = ("changed", "ended")  # past versions that really held; 'corrected' ones never did
_TS = "%Y-%m-%d %H:%M:%S"  # SQLite CURRENT_TIMESTAMP format, UTC


class HistoryUnavailable(RuntimeError):
    """History is read in a context that may not migrate (a subagent or cron job) before any writer set it up."""


class HistoryTooNew(RuntimeError):
    """The file carries a history schema this version does not know: a newer release wrote it. A writer refuses
    to open it (its migration would stamp the schema back and could mis-read new columns); readers still read."""


def normalize(text: str) -> str:
    """What two texts must share to count as the same fact for tombstones: NFKC, casefold, whitespace collapsed,
    end punctuation dropped."""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split()).strip(" .!?;,")


def parse_day(value, *, field: str, today: str) -> str | None:
    """A date argument: YYYY-MM-DD, not after *today*. None / "" -> None (unknown)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} must be a date as YYYY-MM-DD (got {text!r})") from None
    if len(text) != 10:
        raise ValueError(f"{field} must be a date as YYYY-MM-DD (got {text!r})")
    if day.isoformat() > today:
        raise ValueError(f"{field} {day.isoformat()} is in the future; only dates up to today ({today}) are accepted")
    return day.isoformat()


def version_state(row, day: str) -> tuple[str, str]:
    """('holds' | 'undetermined' | 'no', reason) of a version on *day* (YYYY-MM-DD), by the rules of docs/history.md:
    an unknown start is known only from the day the version was recorded; an inexact end is an upper bound."""
    if row["status"] == "corrected":
        return "no", "corrected: it never held"
    start, end = row["valid_from"], row["valid_to"]
    if start and day < start:
        return "no", f"held from {start}"
    if end and day >= end:
        return "no", f"held until {end}" if row["valid_to_exact"] else f"no longer held by {end}"
    if not start and day < row["recorded_on"]:
        return "undetermined", f"start unknown: recorded on {row['recorded_on']}"
    if end and not row["valid_to_exact"] and day > row["recorded_on"]:
        return "undetermined", f"held on {row['recorded_on']}; changed at some point before {end}"
    return "holds", ""


def source_of(row, prefix: str = "src_") -> dict | None:
    """The provenance of a version row as a dict (empty fields left out); None for facts stored before 0.8.0."""
    src = {f: row[prefix + f] for f in SOURCE_FIELDS if row[prefix + f] not in (None, "")}
    return None if not src or src.get("via") == "legacy" else src


class HistoryMixin:
    """The history half of MemoryStore. Expects _conn, _tx, _one, _write, _meta, _entry, default_trust, tz and the
    fact-row helpers of MemoryStore."""

    tz = None  # tzinfo of Hermes's configured timezone; None = UTC

    # -- clocks ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime(_TS)

    def _today(self) -> str:
        return datetime.now(self.tz or timezone.utc).date().isoformat()

    def _local_day(self, timestamp) -> str:
        """Local date of a UTC timestamp written by SQLite; today when it is missing or unreadable."""
        try:
            ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return self._today()
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(self.tz or timezone.utc).date().isoformat()

    # -- set-up and reconciliation ---------------------------------------------------------------------------------

    def history_ready(self) -> bool:
        return self._meta("history_schema") is not None

    def _require_history(self) -> None:
        if not self.history_ready():
            raise HistoryUnavailable("fact history is not set up in this database yet: the main agent sets it up "
                                     "the first time it opens the store (subagents and cron jobs only read)")

    def _tombstone_key(self) -> bytes:
        key = self._meta("tombstone_key")
        if key is None:
            key = secrets.token_hex(16)
            self._write("INSERT OR IGNORE INTO meta (key, value) VALUES ('tombstone_key', ?)", (key,))
            key = self._meta("tombstone_key")
        return bytes.fromhex(key)

    def digest(self, text: str) -> str:
        return hmac.new(self._tombstone_key(), normalize(text).encode("utf-8"), hashlib.sha256).hexdigest()

    def _migrate_history(self) -> None:
        """Inside the migration transaction: tombstone key, versions for every fact, reconciliation, schema mark."""
        found = self._meta("history_schema")
        if found is not None and found.strip().isdigit() and int(found) > HISTORY_SCHEMA_VERSION:
            raise HistoryTooNew(f"this database was written by a newer holographic (history schema {found}, this "
                                f"version knows {HISTORY_SCHEMA_VERSION}): update the plugin; not opened for writing")
        self._tombstone_key()
        self._reconcile_history()
        self._write("INSERT INTO meta (key, value) VALUES ('history_schema', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(HISTORY_SCHEMA_VERSION),))

    def _reconcile_history(self) -> None:
        """Bring the versions in line with ``facts`` after an older version of the plugin wrote the file (see
        docs/history.md, Migration). Three indexed queries; nothing to do is the normal case."""
        missing = self._conn.execute(
            "SELECT f.fact_id, f.content, f.category, f.tags, COALESCE(f.updated_at, f.created_at) AS ts FROM facts f "
            "WHERE NOT EXISTS (SELECT 1 FROM fact_versions v WHERE v.fact_id = f.fact_id AND v.status = 'current')").fetchall()
        for f in missing:
            self._insert_version(f["fact_id"], f["content"], f["category"], f["tags"], valid_from=None,
                                 source={"via": "legacy"}, recorded_at=f["ts"])
        drifted = self._conn.execute(
            "SELECT f.fact_id, f.content, f.category, f.tags, COALESCE(f.updated_at, f.created_at) AS ts, f.trust_score, "
            "v.version_id, v.recorded_on FROM facts f JOIN fact_versions v ON v.fact_id = f.fact_id AND v.status = 'current' "
            "WHERE f.content != v.content").fetchall()
        for f in drifted:  # an older `update content=`: the old text held until some point before the update
            bound = max(f["recorded_on"], self._local_day(f["ts"]))
            self._close_version(f["version_id"], "changed", valid_to=bound, exact=False, trust=f["trust_score"])
            new = self._insert_version(f["fact_id"], f["content"], f["category"], f["tags"], valid_from=None,
                                       source={"via": "legacy"}, recorded_at=f["ts"], supersedes=f["version_id"])
            self._write("UPDATE fact_versions SET superseded_by = ? WHERE version_id = ?", (new, f["version_id"]))
        self._write("UPDATE fact_versions SET tags = (SELECT tags FROM facts WHERE fact_id = fact_versions.fact_id), "
                    "category = (SELECT category FROM facts WHERE fact_id = fact_versions.fact_id) "
                    "WHERE status = 'current' AND EXISTS (SELECT 1 FROM facts f WHERE f.fact_id = fact_versions.fact_id "
                    "AND (f.tags IS NOT fact_versions.tags OR f.category IS NOT fact_versions.category))")
        gone = [r[0] for r in self._conn.execute(
            "SELECT fact_id FROM fact_versions v WHERE status = 'current' "
            "AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.fact_id = v.fact_id)")]
        for fact_id in gone:  # an older `remove` or `supersedes`: the user wanted it gone, history included
            self._erase(fact_id)
        if gone:
            self._write("INSERT INTO meta (key, value) VALUES ('scrub_pending', '1') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value")

    # -- version rows ----------------------------------------------------------------------------------------------

    def _insert_version(self, fact_id: int, content: str, category, tags, *, valid_from: str | None,
                        source: dict | None = None, recorded_at: str | None = None, supersedes: int | None = None,
                        status: str = "current") -> int:
        seq = (self._one("SELECT MAX(seq) FROM fact_versions WHERE fact_id = ?", (fact_id,))[0] or 0) + 1
        recorded_at = str(recorded_at or self._now())
        src = source or {}
        values = [src.get(f) if src.get(f) not in ("", None) else None for f in SOURCE_FIELDS]
        values = [str(v) if v is not None else None for v in values]
        cur = self._write(
            f"INSERT INTO fact_versions (fact_id, seq, content, category, tags, valid_from, status, recorded_at, recorded_on, "
            f"supersedes, {', '.join(_SOURCE_COLUMNS)}) VALUES ({', '.join('?' * (10 + len(_SOURCE_COLUMNS)))})",
            (fact_id, seq, content, category or "general", tags or "", valid_from, status, recorded_at,
             self._local_day(recorded_at), supersedes, *values))
        return int(cur.lastrowid)

    def _close_version(self, version_id: int, status: str, *, valid_to: str | None = None, exact: bool = True,
                       trust: float | None = None) -> None:
        self._write("UPDATE fact_versions SET status = ?, valid_to = COALESCE(?, valid_to), valid_to_exact = ?, "
                    "closed_at = ?, trust_score = COALESCE(?, trust_score) WHERE version_id = ?",
                    (status, valid_to, 1 if exact else 0, self._now(), trust, version_id))

    def _current_version(self, fact_id: int):
        return self._one("SELECT * FROM fact_versions WHERE fact_id = ? AND status = 'current'", (fact_id,))

    def _head_version(self, fact_id: int):
        """The newest version that is not a correction: current, or ended for a fact that left the present. A revert
        leaves a corrected version above it, so the highest seq is not the head."""
        return self._one("SELECT * FROM fact_versions WHERE fact_id = ? AND status != 'corrected' ORDER BY seq DESC "
                         "LIMIT 1", (fact_id,))

    def _version(self, version_id):
        return self._one("SELECT * FROM fact_versions WHERE version_id = ?", (version_id,)) if version_id else None

    def _replace_current(self, fact_id: int, closing: str, content: str, category, tags, *, valid_from: str | None,
                         source: dict | None, valid_to: str | None = None, exact: bool = True,
                         trust: float | None = None) -> int:
        """Close the current version as *closing* and make a new current one; returns the new version's seq."""
        cur = self._current_version(fact_id)
        if cur is None:  # a writer of 0.8.0 keeps every fact versioned; an older one is reconciled before writes
            raise KeyError(f"fact_id {fact_id} has no current version")
        self._close_version(cur["version_id"], closing, valid_to=valid_to, exact=exact, trust=trust)
        new = self._insert_version(fact_id, content, category, tags, valid_from=valid_from, source=source,
                                   supersedes=cur["version_id"])
        self._write("UPDATE fact_versions SET superseded_by = ? WHERE version_id = ?", (new, cur["version_id"]))
        self._fix_predecessor(new)
        return int(self._one("SELECT seq FROM fact_versions WHERE version_id = ?", (new,))[0])

    def _fix_predecessor(self, version_id: int) -> None:
        """Keep the end of the version that *version_id* (now current) replaced in step with its start: the version
        it replaced, skipping corrected ones, ends where it begins (exactly), or at the day the change was recorded
        (an upper bound) when its start is unknown."""
        current = self._version(version_id)
        link, change_point = current, None
        while link is not None and link["supersedes"]:
            prev = self._version(link["supersedes"])
            if prev is None:
                return
            if prev["status"] != "corrected":
                if prev["status"] != "changed":
                    return
                change_point = self._version(prev["superseded_by"]) or link
                break
            link = prev
        else:
            return
        if current["valid_from"]:
            end, exact = current["valid_from"], True
        else:
            end, exact = change_point["recorded_on"], False
        if exact and prev["valid_from"] and end <= prev["valid_from"]:  # an upper bound may equal the start
            raise ValueError(f"the previous version (#{prev['seq']}) started on {prev['valid_from']}: the date would leave "
                             "it no time at all; correct that version's date first, or revert")
        self._write("UPDATE fact_versions SET valid_to = ?, valid_to_exact = ? WHERE version_id = ?",
                    (end, 1 if exact else 0, prev["version_id"]))

    # -- what happened to a fact -----------------------------------------------------------------------------------

    def change_fact(self, fact_id: int, content: str, *, valid_from=None, category: str | None = None,
                    tags: str | None = None, entities=(), source: dict | None = None) -> dict:
        """The fact changed in the world: the current version is closed as `changed` and *content* becomes the
        current version of the same fact. An ended fact holds again in this new form. If another current fact
        already says exactly *content*, this one is ended and that one returned."""
        content = (content or "").strip()
        if not content:
            raise ValueError("content must not be empty")
        self._require_history()
        today = self._today()
        start = parse_day(valid_from, field="valid_from", today=today)
        with self._tx():
            self._reconcile_history()
            fact = self._one("SELECT * FROM facts WHERE fact_id = ?", (fact_id,))
            if fact is None:
                return self._resume(fact_id, content, start, category, tags, entities, source)
            cur = self._current_version(fact_id)
            if content == fact["content"]:
                raise ValueError("this is already what the fact says: nothing changed. A wrong start date is fixed "
                                 "with correct {fact_id, valid_from}")
            if start and cur["valid_from"] and start <= cur["valid_from"]:
                raise ValueError(f"valid_from {start} is not after the current version's start ({cur['valid_from']}): the "
                                 "current text never held then. If it was simply wrong, use correct")
            end, exact = (start, True) if start else (today, False)
            other = self._one("SELECT fact_id FROM facts WHERE content = ? AND fact_id != ?", (content, fact_id))
            if other is not None:  # what it changed into is already a fact of its own
                self._end(fact, cur, end, exact)
                return {"fact_id": int(other["fact_id"]), "status": "exists", "ended": fact_id}
            new_category = category or fact["category"]
            new_tags = fact["tags"] if tags is None else tags
            seq = self._replace_current(fact_id, "changed", content, new_category, new_tags, valid_from=start,
                                        source=source, valid_to=end, exact=exact, trust=fact["trust_score"])
            # A changed fact has not earned the old text's feedback (as `supersedes` did by making a new fact).
            self._rewrite_fact(fact_id, content, new_category, new_tags, entities=entities, old_category=fact["category"],
                               reset_trust=True)
            return {"fact_id": fact_id, "status": "changed", "version": seq,
                    "previous_until": end if exact else f"before {end}"}

    def _resume(self, fact_id, content, start, category, tags, entities, source) -> dict:
        last = self._head_version(fact_id)
        if last is None or last["status"] != "ended":
            raise KeyError(f"fact_id {fact_id} not found")
        if start and start < last["valid_to"] and last["valid_to_exact"]:
            raise ValueError(f"valid_from {start} is before the fact ended ({last['valid_to']}); use correct or revert "
                             "to change how it ended")
        other = self._one("SELECT fact_id FROM facts WHERE content = ?", (content,))
        if other is not None:  # what it holds as again is already a fact of its own
            return {"fact_id": int(other["fact_id"]), "status": "exists"}
        new = self._insert_version(fact_id, content, category or last["category"], last["tags"] if tags is None else tags,
                                   valid_from=start, source=source, supersedes=last["version_id"])
        self._write("UPDATE fact_versions SET superseded_by = ? WHERE version_id = ?", (new, last["version_id"]))
        row = self._version(new)
        self._insert_fact_row(fact_id, row, trust=self.default_trust, entities=entities)
        return {"fact_id": fact_id, "status": "resumed", "version": int(row["seq"])}

    def end_fact(self, fact_id: int, *, valid_to=None) -> dict:
        """The fact stopped holding and nothing replaces it: the current version is closed as `ended` and the fact
        leaves the current store; its history stays."""
        self._require_history()
        today = self._today()
        end = parse_day(valid_to, field="valid_to", today=today)
        with self._tx():
            self._reconcile_history()
            fact = self._one("SELECT * FROM facts WHERE fact_id = ?", (fact_id,))
            if fact is None:
                last = self._head_version(fact_id)
                if last is not None and last["status"] == "ended":
                    raise ValueError(f"fact #{fact_id} already ended ({last['valid_to']})")
                raise KeyError(f"fact_id {fact_id} not found")
            cur = self._current_version(fact_id)
            if end and cur["valid_from"] and end <= cur["valid_from"]:
                raise ValueError(f"valid_to {end} is not after the fact's start ({cur['valid_from']})")
            self._end(fact, cur, end or today, end is not None)
            return {"fact_id": fact_id, "status": "ended", "valid_to": end or today, "exact": end is not None}

    def _end(self, fact, cur, end: str, exact: bool) -> None:
        self._close_version(cur["version_id"], "ended", valid_to=end, exact=exact, trust=fact["trust_score"])
        self._delete_fact_row(fact["fact_id"], fact["category"])

    def correct_fact(self, fact_id: int, content: str | None = None, *, valid_from=None, entities=(),
                     source: dict | None = None) -> dict:
        """It was wrong: the current version is closed as `corrected` (never returned as the past) and the corrected
        text and/or start date become the current version."""
        today = self._today()
        start = parse_day(valid_from, field="valid_from", today=today) if valid_from not in (None, "") else None
        text = (content or "").strip() if content is not None else None
        if text is not None and not text:
            raise ValueError("content must not be empty")
        self._require_history()
        with self._tx():
            self._reconcile_history()
            fact = self._one("SELECT * FROM facts WHERE fact_id = ?", (fact_id,))
            if fact is None:
                last = self._head_version(fact_id)
                if last is not None and last["status"] == "ended":
                    raise ValueError(f"fact #{fact_id} has ended; revert the end first, then correct it")
                raise KeyError(f"fact_id {fact_id} not found")
            cur = self._current_version(fact_id)
            new_text = text if text is not None else fact["content"]
            new_start = start if start is not None else cur["valid_from"]
            if new_text == fact["content"] and new_start == cur["valid_from"]:
                return {"fact_id": fact_id, "status": "unchanged"}
            seq = self._replace_current(fact_id, "corrected", new_text, fact["category"], fact["tags"],
                                        valid_from=new_start, source=source, trust=fact["trust_score"])
            if new_text != fact["content"]:
                self._rewrite_fact(fact_id, new_text, fact["category"], fact["tags"], entities=entities,
                                   old_category=fact["category"])
            return {"fact_id": fact_id, "status": "corrected", "version": seq}

    def revert_fact(self, fact_id: int) -> dict:
        """Undo the last change, correction or end: the last version is closed as `corrected` and the one before it
        is current again (an ended fact is current again)."""
        self._require_history()
        with self._tx():
            self._reconcile_history()
            fact = self._one("SELECT * FROM facts WHERE fact_id = ?", (fact_id,))
            last = self._head_version(fact_id)
            if last is None:
                raise KeyError(f"fact_id {fact_id} not found")
            if fact is None:  # undo an end
                if last["status"] != "ended":
                    raise KeyError(f"fact_id {fact_id} not found")
                if self._one("SELECT 1 FROM facts WHERE content = ?", (last["content"],)):
                    raise ValueError("another current fact already says this; forget one of them instead")
                self._write("UPDATE fact_versions SET status = 'current', valid_to = NULL, valid_to_exact = 1, "
                            "closed_at = NULL WHERE version_id = ?", (last["version_id"],))
                self._insert_fact_row(fact_id, self._version(last["version_id"]),
                                      trust=last["trust_score"] if last["trust_score"] is not None else self.default_trust)
                return {"fact_id": fact_id, "status": "reverted", "current_version": int(last["seq"])}
            cur = self._current_version(fact_id)
            prev = self._version(cur["supersedes"])
            if prev is None:
                if self._one("SELECT 1 FROM fact_versions WHERE fact_id = ? AND version_id != ?",
                             (fact_id, cur["version_id"])):  # the other versions were reverted away (corrected)
                    raise ValueError(f"fact #{fact_id}: nothing to revert; version {cur['seq']} is the one before "
                                     "the reverted ones, and a revert is not undone by another revert (change it "
                                     "again instead)")
                raise ValueError(f"fact #{fact_id} has a single version: nothing to revert (forget deletes it)")
            self._close_version(cur["version_id"], "corrected", trust=fact["trust_score"])
            if prev["status"] == "ended":  # undo a resume: the fact is ended again
                self._delete_fact_row(fact_id, fact["category"])
                return {"fact_id": fact_id, "status": "reverted", "ended": prev["valid_to"]}
            if self._one("SELECT 1 FROM facts WHERE content = ? AND fact_id != ?", (prev["content"], fact_id)):
                raise ValueError("another current fact already says what the previous version said; forget one of them")
            self._write("UPDATE fact_versions SET status = 'current', valid_to = NULL, valid_to_exact = 1, closed_at = NULL, "
                        "superseded_by = NULL WHERE version_id = ?", (prev["version_id"],))
            self._fix_predecessor(prev["version_id"])
            trust = prev["trust_score"] if prev["trust_score"] is not None else fact["trust_score"]
            self._rewrite_fact(fact_id, prev["content"], prev["category"], prev["tags"], old_category=fact["category"],
                               trust=trust)
            return {"fact_id": fact_id, "status": "reverted", "current_version": int(prev["seq"])}

    def forget_fact(self, fact_id: int) -> dict:
        """The user asked to forget it: the fact and every version are deleted for good (see docs/history.md,
        Forget means gone) and a tombstone without text is kept. Forgetting it again retries a file rewrite that
        another process blocked last time."""
        self._require_history()
        with self._tx():
            self._reconcile_history()
            known = self._one("SELECT 1 FROM facts WHERE fact_id = ?", (fact_id,)) or \
                self._one("SELECT 1 FROM fact_versions WHERE fact_id = ?", (fact_id,))
            already = not known and self._one("SELECT 1 FROM forgotten WHERE fact_id = ?", (fact_id,)) is not None
            if not known and not already:
                raise KeyError(f"fact_id {fact_id} not found")
            versions = 0
            if not already:
                versions = self._erase(fact_id)
                self._write("INSERT INTO meta (key, value) VALUES ('scrub_pending', '1') "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        result = {"fact_id": fact_id, "forgotten": True, "versions": versions,
                  "scrub": "done" if self.scrub() else "pending"}
        if already:
            result["already"] = True
        return result

    def _erase(self, fact_id: int) -> int:
        """Delete the fact, its versions and links, rebuild both indexes, write the tombstone (in a transaction)."""
        texts = [r[0] for r in self._conn.execute("SELECT content FROM fact_versions WHERE fact_id = ?", (fact_id,))]
        row = self._one("SELECT content, category FROM facts WHERE fact_id = ?", (fact_id,))
        if row is not None:
            texts.append(row["content"])
            self._delete_fact_row(fact_id, row["category"])
        versions = self._write("DELETE FROM fact_versions WHERE fact_id = ?", (fact_id,)).rowcount
        digests = sorted({self.digest(t) for t in texts})
        old = self._one("SELECT digests FROM forgotten WHERE fact_id = ?", (fact_id,))
        if old is not None:
            digests = sorted(set(digests) | set(json.loads(old["digests"] or "[]")))
        self._write("INSERT INTO forgotten (fact_id, forgotten_at, digests) VALUES (?, ?, ?) ON CONFLICT(fact_id) DO "
                    "UPDATE SET forgotten_at = excluded.forgotten_at, digests = excluded.digests",
                    (fact_id, self._now(), json.dumps(digests)))
        self._write("DELETE FROM memory_banks")  # superpositions that included the fact
        # An FTS5 delete only marks the row; the words stay in older index segments until the index is rebuilt.
        self._write("INSERT INTO facts_fts(facts_fts) VALUES ('rebuild')")
        self._write("INSERT INTO fact_versions_fts(fact_versions_fts) VALUES ('rebuild')")
        return max(versions, 0)

    def scrub(self) -> bool:
        """Rewrite the file and empty the WAL so freed pages keep no copy of forgotten text. Best effort: another
        process holding the database can block it; then the mark stays and the next open or forget retries."""
        with self._lock:
            if self._conn.in_transaction or self._meta("scrub_pending") is None:
                return self._meta("scrub_pending") is None
            # Without waiting on another process (busy timeout 0 for these two statements only, as the recall
            # counts do): a reader blocks the checkpoint for as long as it reads, and every provider operation
            # waits behind a forget, so "pending" must come back at once rather than after the 10 s timeout.
            timeout = self._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self._conn.execute("PRAGMA busy_timeout = 0")
            try:
                self._conn.execute("VACUUM")
                busy = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                done = busy is None or not busy[0]
            except sqlite3.Error as exc:
                logger.warning("holographic: scrub after forget deferred (%s)", exc)
                return False
            finally:
                self._conn.execute(f"PRAGMA busy_timeout = {int(timeout)}")
            if done:
                self._conn.execute("DELETE FROM meta WHERE key = 'scrub_pending'")
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return done

    # -- reads -----------------------------------------------------------------------------------------------------

    def forgotten_match(self, text: str) -> dict | None:
        """The tombstone whose versions include *text* (normalised), or None."""
        if not self.history_ready():
            return None
        row = self.read("SELECT fact_id, forgotten_at FROM forgotten WHERE digests LIKE ?", (f'%"{self.digest(text)}"%',))
        return dict(row[0]) if row else None

    def past_version(self, text: str, statuses=CLOSED) -> dict | None:
        """A closed version with exactly *text* (the newest), with whether its fact is still current."""
        if not self.history_ready():
            return None
        marks = ",".join("?" * len(statuses))
        row = self.read(f"SELECT v.fact_id, v.seq, v.status, v.valid_to, v.valid_to_exact, "
                        f"EXISTS (SELECT 1 FROM facts f WHERE f.fact_id = v.fact_id) AS is_current FROM fact_versions v "
                        f"WHERE v.content = ? AND v.status IN ({marks}) ORDER BY v.version_id DESC LIMIT 1",
                        ((text or "").strip(), *statuses))
        return dict(row[0]) if row else None

    def history(self, fact_id: int) -> dict:
        self._require_history()
        rows = self.read("SELECT * FROM fact_versions WHERE fact_id = ? ORDER BY seq", (fact_id,))
        if not rows:
            gone = self.read("SELECT forgotten_at FROM forgotten WHERE fact_id = ?", (fact_id,))
            if gone:
                return {"fact_id": fact_id, "forgotten": True, "forgotten_at": gone[0][0], "versions": []}
            raise KeyError(f"fact_id {fact_id} not found")
        seqs = {r["version_id"]: r["seq"] for r in rows}
        versions = []
        for r in rows:
            entry = {"version": r["seq"], "content": r["content"], "status": r["status"],
                     "valid_from": r["valid_from"] or "unknown", "valid_to": r["valid_to"],
                     "recorded_at": r["recorded_at"], "source": source_of(r) or "unknown"}
            if r["valid_to"] and not r["valid_to_exact"]:
                entry["valid_to"] = f"before {r['valid_to']}"
            if r["closed_at"]:
                entry["closed_at"] = r["closed_at"]
            if r["supersedes"] in seqs:
                entry["replaces_version"] = seqs[r["supersedes"]]
            if r["superseded_by"] in seqs:
                entry["replaced_by_version"] = seqs[r["superseded_by"]]
            versions.append(entry)
        current = any(r["status"] == "current" for r in rows)
        return {"fact_id": fact_id, "current": current, "versions": versions}

    def versions_on(self, day: str, category: str | None = None, min_trust: float = 0.0, limit: int = 50) -> dict:
        """What held on *day* (`list as_of`): versions known to hold, and those that may have (undetermined)."""
        self._require_history()
        rows = self.read(
            "SELECT v.*, COALESCE(f.trust_score, v.trust_score, ?) AS trust FROM fact_versions v "
            "LEFT JOIN facts f ON f.fact_id = v.fact_id AND v.status = 'current' "
            "WHERE v.status != 'corrected' AND (v.valid_from IS NULL OR v.valid_from <= ?) "
            "AND (v.valid_to IS NULL OR v.valid_to > ?)" + (" AND v.category = ?" if category else "")
            + " ORDER BY v.fact_id, v.seq", (self.default_trust, day, day, *([category] if category else [])))
        held, maybe = {}, {}
        for r in rows:
            if r["trust"] < min_trust:
                continue
            state, why = version_state(r, day)
            entry = {"fact_id": r["fact_id"], "version": r["seq"], "content": r["content"], "category": r["category"],
                     "valid_from": r["valid_from"] or "unknown", "valid_to": r["valid_to"], "status": r["status"]}
            if state == "holds":
                held[r["fact_id"]] = entry
            elif state == "undetermined":
                maybe.setdefault(r["fact_id"], {**entry, "reason": why})
        undetermined = [e for fid, e in maybe.items() if fid not in held]
        return {"as_of": day, "facts": list(held.values())[:limit], "undetermined": undetermined[:limit]}

    def forgotten_ids(self) -> list[dict]:
        return [dict(r) for r in self.read("SELECT fact_id, forgotten_at FROM forgotten ORDER BY fact_id")]

    def history_counts(self) -> dict:
        if not self.history_ready():
            return {}
        counts = {r[0]: r[1] for r in self.read("SELECT status, COUNT(*) FROM fact_versions GROUP BY status")}
        ended = self.read("SELECT COUNT(DISTINCT fact_id) FROM fact_versions v WHERE status = 'ended' AND NOT EXISTS "
                          "(SELECT 1 FROM fact_versions w WHERE w.fact_id = v.fact_id AND w.status = 'current')")[0][0]
        return {"past_versions": counts.get("changed", 0) + counts.get("ended", 0), "corrected": counts.get("corrected", 0),
                "ended_facts": ended, "forgotten": self.read("SELECT COUNT(*) FROM forgotten")[0][0]}

    def current_sources(self, fact_ids) -> dict[int, dict | None]:
        """{fact_id: source of its current version} (None: unknown, stored before 0.8.0)."""
        ids = [int(i) for i in fact_ids]
        if not ids or not self.history_ready():
            return {}
        cols = ", ".join(_SOURCE_COLUMNS)
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in self.read(f"SELECT fact_id, recorded_on, {cols} FROM fact_versions WHERE status = 'current' "
                               f"AND fact_id IN ({','.join('?' * len(chunk))})", chunk):
                src = source_of(r)
                if src is not None:
                    src["recorded_on"] = r["recorded_on"]
                out[r["fact_id"]] = src
        return out


__all__ = ["HISTORY_SCHEMA", "HistoryMixin", "HistoryUnavailable", "HistoryTooNew", "CLOSED", "SOURCE_FIELDS", "normalize",
           "parse_day", "version_state", "source_of"]
