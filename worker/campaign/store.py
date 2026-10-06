"""Campaign records: campaigns + authorizations, businesses, suppressions, replies, audit trail.

One SQLite file (config/worker/campaign.db), private to this Mac. Never contains passwords or
tokens. Business contacts are stored because the outreach needs them; the file is git-ignored.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

_MIGRATIONS = [
    """
    CREATE TABLE campaigns (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'research',
        policy TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'draft', stop_reason TEXT NOT NULL DEFAULT '',
        authorized_at REAL, authorized_hash TEXT NOT NULL DEFAULT '', authorized_until REAL, authorized_by TEXT NOT NULL DEFAULT '',
        revoked_at REAL, created REAL NOT NULL, updated REAL NOT NULL
    );
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, campaign_id TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE businesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, dedupe_key TEXT NOT NULL,
        name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        website TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL DEFAULT '',
        stage TEXT NOT NULL DEFAULT 'found', status TEXT NOT NULL DEFAULT 'ok', hold_reason TEXT NOT NULL DEFAULT '',
        data TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE (campaign_id, dedupe_key)
    );
    CREATE INDEX businesses_stage ON businesses (campaign_id, stage, status);
    CREATE TABLE suppressions (key TEXT PRIMARY KEY, reason TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);
    CREATE TABLE replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, business_id INTEGER NOT NULL, campaign_id TEXT NOT NULL,
        message_id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
        classification TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0, snippet TEXT NOT NULL DEFAULT '',
        needs_review INTEGER NOT NULL DEFAULT 0, handled_at REAL
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
]

STAGES = ("found", "verified", "audited", "qualified", "researched", "built", "checked", "preview", "queued", "sent")


def _j(v) -> str:
    return json.dumps(v if v is not None else {}, separators=(",", ":"), default=str)


def _unj(t) -> dict:
    try:
        v = json.loads(t or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def default_path() -> Path:
    from worker.runtime import default_dir
    return default_dir() / "campaign.db"


class CampaignStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()
        self._ready = False

    @contextmanager
    def _conn(self):
        self._migrate_once()
        con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
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
            con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
            try:
                try:
                    con.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error:
                    pass
                con.execute("BEGIN IMMEDIATE")
                version = con.execute("PRAGMA user_version").fetchone()[0]
                for n, script in enumerate(_MIGRATIONS, 1):
                    if n > version:
                        for stmt in filter(None, (x.strip() for x in script.split(";"))):
                            con.execute(stmt)
                        con.execute(f"PRAGMA user_version={n}")
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

    # ── campaigns ────────────────────────────────────────────────────────────
    @staticmethod
    def _campaign(r) -> dict:
        d = dict(r)
        d["policy"] = _unj(d["policy"])
        return d

    def save_campaign(self, cid: str, name: str, policy: dict, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._conn() as con:
            con.execute("INSERT INTO campaigns (id, name, policy, created, updated) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, policy=excluded.policy, updated=excluded.updated",
                        (cid, name, _j(policy), now, now))

    def campaign(self, cid: str) -> Optional[dict]:
        with self._conn() as con:
            r = con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        return self._campaign(r) if r else None

    def campaigns(self) -> list[dict]:
        with self._conn() as con:
            return [self._campaign(r) for r in con.execute("SELECT * FROM campaigns ORDER BY created")]

    def set_campaign(self, cid: str, now: Optional[float] = None, **fields) -> None:
        now = now if now is not None else time.time()
        allowed = {"mode", "status", "stop_reason", "authorized_at", "authorized_hash", "authorized_until",
                   "authorized_by", "revoked_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown campaign fields {bad}")
        sets = ", ".join(f"{k}=?" for k in fields) + (", " if fields else "") + "updated=?"
        with self._conn() as con:
            con.execute(f"UPDATE campaigns SET {sets} WHERE id=?", (*fields.values(), now, cid))

    def audit(self, campaign_id: str, actor: str, action: str, detail: Optional[dict] = None,
              now: Optional[float] = None) -> None:
        with self._conn() as con:
            con.execute("INSERT INTO audit (ts, campaign_id, actor, action, detail) VALUES (?,?,?,?,?)",
                        (now if now is not None else time.time(), campaign_id, actor, action, _j(detail)))

    def audit_log(self, campaign_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM audit", []
        if campaign_id:
            sql += " WHERE campaign_id=?"; args.append(campaign_id)
        with self._conn() as con:
            rows = [dict(r) for r in con.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]
        for r in rows:
            r["detail"] = _unj(r["detail"])
        return rows

    # ── businesses ───────────────────────────────────────────────────────────
    @staticmethod
    def _biz(r) -> dict:
        d = dict(r)
        d["data"] = _unj(d["data"])
        return d

    def add_business(self, campaign_id: str, dedupe_key: str, fields: dict, now: Optional[float] = None) -> tuple[int, bool]:
        """(id, created). A business with the same dedupe key in a campaign is stored once."""
        now = now if now is not None else time.time()
        with self._conn() as con:
            r = con.execute("SELECT id FROM businesses WHERE campaign_id=? AND dedupe_key=?",
                            (campaign_id, dedupe_key)).fetchone()
            if r:
                return int(r["id"]), False
            cols = ("name", "category", "address", "city", "phone", "email", "website", "source", "source_id")
            cur = con.execute(
                f"INSERT INTO businesses (campaign_id, dedupe_key, {','.join(cols)}, data, created, updated) "
                f"VALUES (?,?,{','.join('?' * len(cols))},?,?,?)",
                (campaign_id, dedupe_key, *[str(fields.get(c) or "") for c in cols],
                 _j(fields.get("data")), now, now))
            return int(cur.lastrowid), True

    def business(self, bid: int) -> Optional[dict]:
        with self._conn() as con:
            r = con.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
        return self._biz(r) if r else None

    def businesses(self, campaign_id: str, *, stage: Optional[str] = None, status: Optional[str] = None,
                   limit: int = 1000) -> list[dict]:
        sql, args = "SELECT * FROM businesses WHERE campaign_id=?", [campaign_id]
        if stage:
            sql += " AND stage=?"; args.append(stage)
        if status:
            sql += " AND status=?"; args.append(status)
        with self._conn() as con:
            return [self._biz(r) for r in con.execute(sql + " ORDER BY id LIMIT ?", (*args, limit))]

    def update_business(self, bid: int, *, now: Optional[float] = None, data: Optional[dict] = None, **fields) -> None:
        """Set columns (stage, status, hold_reason, email, website, phone...) and merge keys into `data`."""
        now = now if now is not None else time.time()
        allowed = {"name", "category", "address", "city", "phone", "email", "website", "stage", "status", "hold_reason"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown business fields {bad}")
        with self._conn() as con:
            if data:
                cur = _unj(con.execute("SELECT data FROM businesses WHERE id=?", (bid,)).fetchone()["data"])
                cur.update(data)
                fields["data"] = _j(cur)
            sets = ", ".join(f"{k}=?" for k in fields) + (", " if fields else "") + "updated=?"
            con.execute(f"UPDATE businesses SET {sets} WHERE id=?", (*fields.values(), now, bid))

    def email_in_use(self, email: str, exclude_id: int = 0, stages: tuple = ("qualified", "researched", "built", "checked", "preview", "queued", "sent")) -> bool:
        """Is another live business already being pitched at this contact address?"""
        email = (email or "").strip().lower()
        if not email:
            return False
        marks = ",".join("?" * len(stages))
        with self._conn() as con:
            return con.execute(f"SELECT 1 FROM businesses WHERE lower(email)=? AND id!=? AND status IN ('ok','held') AND stage IN ({marks}) LIMIT 1",
                               (email, exclude_id, *stages)).fetchone() is not None

    def count_by_stage(self, campaign_id: str) -> dict:
        with self._conn() as con:
            return {f"{r['stage']}/{r['status']}": r["n"] for r in con.execute(
                "SELECT stage, status, COUNT(*) n FROM businesses WHERE campaign_id=? GROUP BY stage, status",
                (campaign_id,))}

    def known_keys(self, campaign_id: Optional[str] = None) -> set:
        """dedupe keys already seen (all campaigns by default: a business is never pitched twice)."""
        with self._conn() as con:
            if campaign_id:
                rows = con.execute("SELECT dedupe_key FROM businesses WHERE campaign_id=?", (campaign_id,))
            else:
                rows = con.execute("SELECT dedupe_key FROM businesses")
            return {r[0] for r in rows}

    # ── suppressions (opt-outs, bounces, complaints): permanent, checked before any send ──
    def suppress(self, key: str, reason: str, source: str = "", now: Optional[float] = None) -> None:
        key = str(key).strip().lower()
        if not key:
            return
        with self._conn() as con:
            con.execute("INSERT OR IGNORE INTO suppressions (key, reason, source, ts) VALUES (?,?,?,?)",
                        (key, reason, source, now if now is not None else time.time()))

    def is_suppressed(self, *keys: str) -> Optional[str]:
        keys = [str(k).strip().lower() for k in keys if k]
        if not keys:
            return None
        with self._conn() as con:
            r = con.execute(f"SELECT reason FROM suppressions WHERE key IN ({','.join('?' * len(keys))})", keys).fetchone()
        return r["reason"] if r else None

    # ── replies ──────────────────────────────────────────────────────────────
    def add_reply(self, business_id: int, campaign_id: str, message_id: str, thread_id: str, ts: float,
                  classification: str, confidence: float, snippet: str, needs_review: bool) -> bool:
        with self._conn() as con:
            cur = con.execute("INSERT OR IGNORE INTO replies (business_id, campaign_id, message_id, thread_id, ts,"
                              " classification, confidence, snippet, needs_review) VALUES (?,?,?,?,?,?,?,?,?)",
                              (business_id, campaign_id, message_id, thread_id, ts, classification, confidence,
                               snippet[:300], 1 if needs_review else 0))
            return cur.rowcount > 0

    def replies(self, campaign_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM replies", []
        if campaign_id:
            sql += " WHERE campaign_id=?"; args.append(campaign_id)
        with self._conn() as con:
            return [dict(r) for r in con.execute(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit))]

    def get(self, key: str, default=None):
        with self._conn() as con:
            r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        try:
            return json.loads(r["value"]) if r else default
        except ValueError:
            return default

    def set(self, key: str, value) -> None:
        with self._conn() as con:
            con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))
