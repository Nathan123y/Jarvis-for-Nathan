"""Durable, timestamped record of what happened while Jarvis was away.

One small SQLite file (config/events.db, ignored by git) shared by the Jarvis app
and the headless worker on the SAME Mac. It is never meant to be shared across
machines: a second computer gets its own file (see docs/while-you-were-away.md).

Every row is evidence, not opinion:
    source       who wrote it ("worker", "gmail", "trading", "promotion", ...)
    source_id    that source's own id for the thing (unique per source, so
                 re-importing the same thing never double counts)
    kind         what happened (see KINDS)
    task_id      the business / job the event is about (counts are distinct task_ids)
    campaign_id  the campaign it belongs to
    status       kind-specific state (for replies: interested, call_request, ...)
    evidence     references that back the row up (message id, URL, journal line)

Also kept here: when each source last synced successfully (so a missing or old
source reads "unavailable"/"stale", never "0"), the briefing cursor, and the
briefings already delivered.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

# The funnel, in order. Counting one stage never implies the next.
KINDS = (
    "biz_found", "biz_qualified", "site_built", "site_checked", "preview_published",
    "offer_sent", "reply_received", "sale_paid", "delivered",
    # paper trading (always simulated)
    "trade_result", "paper_snapshot",
    # things that need a person
    "job_failed", "job_paused", "connection_missing", "decision_needed", "campaign_stopped",
    # plumbing
    "worker_started", "worker_stopped",
)

REPLY_STATUSES = ("interested", "call_request", "question", "declined",
                  "opted_out", "bounced", "automated", "unclear")

OPEN = "open"
HANDLED = "handled"

_MIGRATIONS = [
    # 1: first schema
    """
    CREATE TABLE events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        source      TEXT NOT NULL,
        source_id   TEXT NOT NULL,
        kind        TEXT NOT NULL,
        task_id     TEXT NOT NULL DEFAULT '',
        campaign_id TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT '',
        title       TEXT NOT NULL DEFAULT '',
        detail      TEXT NOT NULL DEFAULT '{}',
        evidence    TEXT NOT NULL DEFAULT '{}',
        private     INTEGER NOT NULL DEFAULT 0,
        handled_at  REAL,
        UNIQUE (source, source_id)
    );
    CREATE INDEX events_ts ON events (ts);
    CREATE INDEX events_kind ON events (kind, ts);
    CREATE TABLE sources (
        name        TEXT PRIMARY KEY,
        last_ok     REAL,
        last_error  TEXT NOT NULL DEFAULT '',
        last_error_at REAL,
        stale_after REAL NOT NULL DEFAULT 86400
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE briefings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created     REAL NOT NULL,
        window_start REAL NOT NULL,
        window_end  REAL NOT NULL,
        through_id  INTEGER NOT NULL,
        spoken      TEXT NOT NULL,
        panel       TEXT NOT NULL,
        counts      TEXT NOT NULL DEFAULT '{}',
        delivered_at REAL,
        how         TEXT NOT NULL DEFAULT ''
    );
    """,
]


def default_path() -> Path:
    from memory.config_manager import CONFIG_DIR
    return CONFIG_DIR / "events.db"


def _j(value) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), default=str)


def _unj(text) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


class Store:
    """All access goes through short-lived connections, so the app's threads and
    the worker process can use the same file (WAL + busy timeout)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()
        self._ready = False

    # ── connection / migrations ──────────────────────────────────────────────
    @contextmanager
    def _conn(self):
        self._migrate_once()
        con = sqlite3.connect(str(self.path), timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _migrate_once(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            try:
                try:
                    con.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error:
                    pass
                con.execute("BEGIN IMMEDIATE")
                version = con.execute("PRAGMA user_version").fetchone()[0]
                for number, script in enumerate(_MIGRATIONS, 1):
                    if number > version:
                        for statement in filter(None, (s.strip() for s in script.split(";"))):
                            con.execute(statement)
                        con.execute(f"PRAGMA user_version={number}")
                con.execute("COMMIT")
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                con.close()
            self._ready = True

    # ── events ───────────────────────────────────────────────────────────────
    def record(self, kind: str, *, source: str, source_id: str, ts: Optional[float] = None,
               task_id: str = "", campaign_id: str = "", status: str = "", title: str = "",
               detail: Optional[dict] = None, evidence: Optional[dict] = None,
               private: bool = False, update: bool = False) -> bool:
        """Add one event. Returns True if it is new.

        The same (source, source_id) is stored once. With update=True a repeat
        refreshes status/detail/evidence in place (keeping its id and so its place
        in the briefing order), used for things that legitimately change, such as
        a day's trade result."""
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        row = (float(ts if ts is not None else time.time()), source, str(source_id), kind,
               str(task_id or ""), str(campaign_id or ""), str(status or ""), str(title or "")[:300],
               _j(detail), _j(evidence), 1 if private else 0)
        with self._conn() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO events (ts, source, source_id, kind, task_id, campaign_id,"
                " status, title, detail, evidence, private) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
            if cur.rowcount:
                return True
            if update:
                con.execute("UPDATE events SET status=?, title=?, detail=?, evidence=? "
                            "WHERE source=? AND source_id=?",
                            (row[6], row[7], row[8], row[9], source, str(source_id)))
            return False

    def events(self, *, after_id: int = 0, since: Optional[float] = None, until: Optional[float] = None,
               kinds: Optional[Iterable[str]] = None, source: Optional[str] = None,
               limit: int = 5000) -> list[dict]:
        sql, args = ["SELECT * FROM events WHERE id > ?"], [int(after_id)]
        if since is not None:
            sql.append("AND ts >= ?"); args.append(float(since))
        if until is not None:
            sql.append("AND ts <= ?"); args.append(float(until))
        if kinds:
            kinds = list(kinds)
            sql.append(f"AND kind IN ({','.join('?' * len(kinds))})"); args += kinds
        if source:
            sql.append("AND source = ?"); args.append(source)
        sql.append("ORDER BY id ASC LIMIT ?"); args.append(int(limit))
        with self._conn() as con:
            return [self._row(r) for r in con.execute(" ".join(sql), args)]

    def open_items(self, kinds: Iterable[str], max_age_days: float = 30.0, now: Optional[float] = None) -> list[dict]:
        """Events still needing a person (not handled), newest first."""
        kinds = list(kinds)
        cutoff = (now if now is not None else time.time()) - max_age_days * 86400
        with self._conn() as con:
            rows = con.execute(
                f"SELECT * FROM events WHERE kind IN ({','.join('?' * len(kinds))}) "
                "AND handled_at IS NULL AND ts >= ? ORDER BY ts DESC", (*kinds, cutoff)).fetchall()
        return [self._row(r) for r in rows]

    def max_id(self) -> int:
        with self._conn() as con:
            return int(con.execute("SELECT IFNULL(MAX(id), 0) FROM events").fetchone()[0])

    def mark_handled(self, ids: Iterable[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with self._conn() as con:
            cur = con.execute(f"UPDATE events SET handled_at=? WHERE handled_at IS NULL AND id IN "
                              f"({','.join('?' * len(ids))})", (time.time(), *ids))
            return cur.rowcount

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["detail"], d["evidence"] = _unj(d.get("detail")), _unj(d.get("evidence"))
        d["private"] = bool(d.get("private"))
        return d

    # ── source freshness ─────────────────────────────────────────────────────
    def sync_ok(self, source: str, stale_after: Optional[float] = None, at: Optional[float] = None) -> None:
        at = float(at if at is not None else time.time())
        with self._conn() as con:
            con.execute("INSERT OR IGNORE INTO sources (name) VALUES (?)", (source,))
            con.execute("UPDATE sources SET last_ok=?, last_error='', "
                        "stale_after=COALESCE(?, stale_after) WHERE name=?", (at, stale_after, source))

    def sync_failed(self, source: str, error: str, at: Optional[float] = None) -> None:
        with self._conn() as con:
            con.execute("INSERT OR IGNORE INTO sources (name) VALUES (?)", (source,))
            con.execute("UPDATE sources SET last_error=?, last_error_at=? WHERE name=?",
                        (str(error)[:300], float(at if at is not None else time.time()), source))

    def sync_state(self, source: str, now: Optional[float] = None) -> dict:
        """{"state": ok|stale|error|unavailable, "last_ok", "error"} — never a made-up zero."""
        now = float(now if now is not None else time.time())
        with self._conn() as con:
            r = con.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if r is None or r["last_ok"] is None:
            return {"state": "unavailable", "last_ok": None, "error": (r["last_error"] if r else "")}
        err_newer = bool(r["last_error"]) and (r["last_error_at"] or 0) > r["last_ok"]
        if err_newer:
            state = "error"
        elif now - r["last_ok"] > r["stale_after"]:
            state = "stale"
        else:
            state = "ok"
        return {"state": state, "last_ok": r["last_ok"], "error": r["last_error"] if err_newer else ""}

    # ── small key/value (cursors, heartbeat, settings the worker needs) ──────
    def get(self, key: str, default=None):
        with self._conn() as con:
            r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except ValueError:
            return default

    def set(self, key: str, value) -> None:
        with self._conn() as con:
            con.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # ── briefings ────────────────────────────────────────────────────────────
    def save_briefing(self, *, window_start: float, window_end: float, through_id: int,
                      spoken: str, panel: str, counts: dict) -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO briefings (created, window_start, window_end, through_id, spoken, panel, counts)"
                " VALUES (?,?,?,?,?,?,?)",
                (time.time(), window_start, window_end, through_id, spoken, panel, _j(counts)))
            return int(cur.lastrowid)

    def mark_delivered(self, briefing_id: int, how: str) -> None:
        """The briefing reached the user (spoken or shown): later briefings start after it."""
        with self._conn() as con:
            r = con.execute("SELECT through_id, delivered_at FROM briefings WHERE id=?", (briefing_id,)).fetchone()
            if r is None:
                return
            if r["delivered_at"] is None:
                con.execute("UPDATE briefings SET delivered_at=?, how=? WHERE id=?",
                            (time.time(), how, briefing_id))
            cur = con.execute("SELECT value FROM meta WHERE key='delivered_through_id'").fetchone()
            now_through = max(int(json.loads(cur["value"])) if cur else 0, int(r["through_id"]))
            con.execute("INSERT INTO meta (key, value) VALUES ('delivered_through_id', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(now_through),))
            con.execute("INSERT INTO meta (key, value) VALUES ('delivered_through_ts', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(time.time()),))

    def delivered_through(self) -> tuple[int, Optional[float]]:
        return int(self.get("delivered_through_id", 0) or 0), self.get("delivered_through_ts")

    def last_briefing(self, delivered_only: bool = False) -> Optional[dict]:
        sql = "SELECT * FROM briefings " + ("WHERE delivered_at IS NOT NULL " if delivered_only else "") + \
              "ORDER BY id DESC LIMIT 1"
        with self._conn() as con:
            r = con.execute(sql).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["counts"] = _unj(d.get("counts"))
        return d
