"""Durable job queue, send outbox, schedules and campaign locks (one SQLite file).

Local to ONE machine: never put this file on a shared drive or open it from two computers.
A second machine gets its own file and must be handed a different campaign (see
docs/while-you-were-away.md for the always-on plan).

Job states: queued -> running -> done | failed | cancelled, plus paused (needs a person or
a setup step). A job that was running when the worker died is found by its expired lease
and re-queued (counting an attempt) or failed once its attempts are used up.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

QUEUED, RUNNING, DONE, FAILED, CANCELLED, PAUSED = "queued", "running", "done", "failed", "cancelled", "paused"
TERMINAL = (DONE, FAILED, CANCELLED)

# Outbox (external sends)
O_QUEUED, O_SENDING, O_SENT, O_UNCERTAIN, O_FAILED, O_HELD = "queued", "sending", "sent", "uncertain", "failed", "held"

BACKOFF_BASE, BACKOFF_CAP = 30.0, 3600.0

_MIGRATIONS = [
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, campaign_id TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'queued',
        priority INTEGER NOT NULL DEFAULT 5, attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3, timeout_s REAL NOT NULL DEFAULT 600,
        run_after REAL NOT NULL DEFAULT 0, checkpoint TEXT NOT NULL DEFAULT '{}',
        result TEXT NOT NULL DEFAULT '{}', last_error TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL, started_at REAL, finished_at REAL,
        lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
        unique_key TEXT UNIQUE
    );
    CREATE INDEX jobs_ready ON jobs (state, run_after, priority);
    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idem_key TEXT NOT NULL UNIQUE, campaign_id TEXT NOT NULL DEFAULT '',
        recipient_key TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        provider_message_id TEXT NOT NULL DEFAULT '', provider_thread_id TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL, sent_at REAL
    );
    CREATE INDEX outbox_state ON outbox (state, campaign_id);
    CREATE TABLE schedules (
        name TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
        campaign_id TEXT NOT NULL DEFAULT '', every_s REAL NOT NULL, next_run REAL NOT NULL,
        max_lateness_s REAL NOT NULL DEFAULT 0, catchup TEXT NOT NULL DEFAULT 'run_once',
        enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE locks (name TEXT PRIMARY KEY, owner TEXT NOT NULL, lease_until REAL NOT NULL);
    """,
    # 2: jobs that must not run late (expiry) and when a send was last attempted
    """
    ALTER TABLE jobs ADD COLUMN expires_at REAL;
    ALTER TABLE outbox ADD COLUMN attempted_at REAL;
    """,
]


def _j(v) -> str:
    return json.dumps(v if v is not None else {}, separators=(",", ":"), default=str)


def _unj(t) -> dict:
    try:
        v = json.loads(t or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def backoff(attempts: int) -> float:
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, attempts - 1)))


class JobDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ready = False

    @contextmanager
    def _conn(self, immediate: bool = False):
        self._migrate_once()
        con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
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

    @staticmethod
    def _job(r) -> dict:
        d = dict(r)
        for k in ("payload", "checkpoint", "result"):
            d[k] = _unj(d.get(k))
        return d

    # ── jobs ─────────────────────────────────────────────────────────────────
    def enqueue(self, kind: str, payload: Optional[dict] = None, *, campaign_id: str = "",
                run_after: float = 0.0, max_attempts: int = 3, timeout_s: float = 600.0,
                unique_key: Optional[str] = None, priority: int = 5, expires_at: Optional[float] = None,
                now: Optional[float] = None) -> int:
        """Add a job; returns its id. A repeated unique_key returns the existing job's id.
        A job with expires_at is cancelled instead of run (or retried) once that time has passed."""
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            if unique_key:
                r = con.execute("SELECT id FROM jobs WHERE unique_key=?", (unique_key,)).fetchone()
                if r:
                    return int(r["id"])
            cur = con.execute(
                "INSERT INTO jobs (kind, campaign_id, payload, priority, max_attempts, timeout_s, run_after,"
                " created, updated, unique_key, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (kind, campaign_id, _j(payload), priority, max_attempts, timeout_s, run_after, now, now, unique_key,
                 expires_at))
            return int(cur.lastrowid)

    def _expire(self, con, now: float) -> None:
        con.execute("UPDATE jobs SET state='cancelled', finished_at=?, updated=?, lease_until=NULL,"
                    " last_error='expired before it could run (not replayed)' "
                    "WHERE state IN ('queued','paused') AND expires_at IS NOT NULL AND expires_at<?", (now, now, now))

    def claim(self, owner: str, *, now: Optional[float] = None, lease_s: float = 0.0,
              exclusive_kinds: Iterable[str] = (), only_kinds: Optional[Iterable[str]] = None) -> Optional[dict]:
        """Take the next ready job (lease expires after its timeout + slack). Kinds in
        `exclusive_kinds` run one at a time: none is handed out while one is running."""
        now = now if now is not None else time.time()
        exclusive = tuple(exclusive_kinds)
        only = tuple(only_kinds) if only_kinds is not None else None
        with self._conn(immediate=True) as con:
            self._expire(con, now)
            skip: list = []
            if exclusive and con.execute(
                    f"SELECT 1 FROM jobs WHERE state='running' AND kind IN ({','.join('?' * len(exclusive))}) LIMIT 1",
                    exclusive).fetchone():
                skip = list(exclusive)                # one running generation job blocks all of them
            sql, args = "SELECT * FROM jobs WHERE state='queued' AND run_after<=?", [now]
            if skip:
                sql += f" AND kind NOT IN ({','.join('?' * len(skip))})"; args += skip
            if only is not None:
                if not only:
                    return None
                sql += f" AND kind IN ({','.join('?' * len(only))})"; args += list(only)
            r = con.execute(sql + " ORDER BY priority ASC, run_after ASC, id ASC LIMIT 1", args).fetchone()
            if r is None:
                return None
            lease = now + (lease_s or r["timeout_s"]) + 30
            con.execute("UPDATE jobs SET state='running', attempts=attempts+1, started_at=?, updated=?,"
                        " lease_until=? WHERE id=?", (now, now, lease, r["id"]))
            return self._job(con.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone())

    # The `attempt` argument fences a stale handler thread: once a job has been re-claimed (a newer
    # attempt), the old attempt can no longer checkpoint, complete, fail, defer or pause it.
    @staticmethod
    def _fence(attempt: Optional[int]) -> tuple[str, list]:
        return (" AND attempts=?", [attempt]) if attempt is not None else ("", [])

    def checkpoint(self, job_id: int, data: dict, now: Optional[float] = None, extend_lease_s: float = 0.0,
                   attempt: Optional[int] = None) -> bool:
        now = now if now is not None else time.time()
        f, fa = self._fence(attempt)
        with self._conn(immediate=True) as con:
            return con.execute("UPDATE jobs SET checkpoint=?, updated=?, lease_until=MAX(lease_until, ?) "
                               f"WHERE id=? AND state='running'{f}",
                               [_j(data), now, now + extend_lease_s, job_id, *fa]).rowcount > 0

    def complete(self, job_id: int, result: Optional[dict] = None, now: Optional[float] = None,
                 attempt: Optional[int] = None) -> bool:
        now = now if now is not None else time.time()
        f, fa = self._fence(attempt)
        with self._conn(immediate=True) as con:
            return con.execute("UPDATE jobs SET state='done', result=?, finished_at=?, updated=?, lease_until=NULL,"
                               f" last_error='' WHERE id=? AND state='running'{f}",
                               [_j(result), now, now, job_id, *fa]).rowcount > 0

    def fail(self, job_id: int, error: str, *, retry: bool = True, now: Optional[float] = None,
             attempt: Optional[int] = None) -> str:
        """Record a failure. Returns the new state: queued (will retry after a backoff), failed or cancelled."""
        now = now if now is not None else time.time()
        f, fa = self._fence(attempt)
        with self._conn(immediate=True) as con:
            r = con.execute(f"SELECT attempts, max_attempts, cancel_requested, expires_at FROM jobs WHERE id=?{f}",
                            [job_id, *fa]).fetchone()
            if r is None:
                return "stale"
            if r["cancel_requested"]:
                con.execute("UPDATE jobs SET state='cancelled', finished_at=?, updated=?, lease_until=NULL,"
                            " last_error=? WHERE id=?", (now, now, str(error)[:300], job_id))
                return CANCELLED
            retry_at = now + backoff(r["attempts"])
            if retry and r["attempts"] < r["max_attempts"] and not (r["expires_at"] is not None and retry_at > r["expires_at"]):
                con.execute("UPDATE jobs SET state='queued', run_after=?, updated=?, lease_until=NULL, last_error=? "
                            "WHERE id=?", (retry_at, now, str(error)[:300], job_id))
                return QUEUED
            con.execute("UPDATE jobs SET state='failed', finished_at=?, updated=?, lease_until=NULL, last_error=? "
                        "WHERE id=?", (now, now, str(error)[:300], job_id))
            return FAILED

    def defer(self, job_id: int, run_after: float, reason: str, now: Optional[float] = None,
              attempt: Optional[int] = None) -> str:
        """Put a job back for later WITHOUT using up an attempt (quota reached, kill switch, shutdown).
        A pending cancel wins: the job is cancelled instead."""
        now = now if now is not None else time.time()
        f, fa = self._fence(attempt)
        with self._conn(immediate=True) as con:
            r = con.execute(f"SELECT cancel_requested FROM jobs WHERE id=? AND state='running'{f}", [job_id, *fa]).fetchone()
            if r is None:
                return "stale"
            if r["cancel_requested"]:
                con.execute("UPDATE jobs SET state='cancelled', finished_at=?, updated=?, lease_until=NULL WHERE id=?",
                            (now, now, job_id))
                return CANCELLED
            con.execute("UPDATE jobs SET state='queued', run_after=?, attempts=MAX(0, attempts-1), updated=?,"
                        " lease_until=NULL, last_error=? WHERE id=?", (run_after, now, str(reason)[:300], job_id))
            return QUEUED

    def pause(self, job_id: int, reason: str, now: Optional[float] = None, attempt: Optional[int] = None) -> str:
        now = now if now is not None else time.time()
        f, fa = self._fence(attempt)
        with self._conn(immediate=True) as con:
            r = con.execute(f"SELECT state, cancel_requested FROM jobs WHERE id=? AND state IN ('running','queued'){f}",
                            [job_id, *fa]).fetchone()
            if r is None:
                return "stale"
            if r["cancel_requested"]:
                con.execute("UPDATE jobs SET state='cancelled', finished_at=?, updated=?, lease_until=NULL WHERE id=?",
                            (now, now, job_id))
                return CANCELLED
            con.execute("UPDATE jobs SET state='paused', updated=?, lease_until=NULL, last_error=? WHERE id=?",
                        (now, str(reason)[:300], job_id))
            return PAUSED

    def resume(self, job_id: int, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            return con.execute("UPDATE jobs SET state='queued', run_after=?, updated=?, last_error='' "
                               "WHERE id=? AND state='paused'", (now, now, job_id)).rowcount > 0

    def request_cancel(self, job_id: int, now: Optional[float] = None) -> str:
        """Queued/paused jobs are cancelled at once; a running one is asked to stop (it checks)."""
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            r = con.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if r is None:
                return "missing"
            if r["state"] in (QUEUED, PAUSED):
                con.execute("UPDATE jobs SET state='cancelled', finished_at=?, updated=? WHERE id=?", (now, now, job_id))
                return CANCELLED
            if r["state"] == RUNNING:
                con.execute("UPDATE jobs SET cancel_requested=1, updated=? WHERE id=?", (now, job_id))
                return "cancelling"
            return r["state"]

    def cancel_requested(self, job_id: int) -> bool:
        with self._conn() as con:
            r = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(r and r["cancel_requested"])

    def mark_cancelled(self, job_id: int, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            con.execute("UPDATE jobs SET state='cancelled', finished_at=?, updated=?, lease_until=NULL "
                        "WHERE id=? AND state='running'", (now, now, job_id))

    def recover(self, now: Optional[float] = None, force: bool = False) -> list[dict]:
        """Jobs whose worker died (lease ran out). Re-queued with backoff, or failed when out of attempts.
        force=True treats every running job as orphaned: used once at startup, when the process lock
        proves the previous worker is gone."""
        now = now if now is not None else time.time()
        out = []
        with self._conn(immediate=True) as con:
            self._expire(con, now)
            lease_cut = float("inf") if force else now
            for r in con.execute("SELECT * FROM jobs WHERE state='running' AND lease_until<?", (lease_cut,)).fetchall():
                if r["cancel_requested"]:
                    state = CANCELLED
                elif r["attempts"] < r["max_attempts"]:
                    state = QUEUED
                else:
                    state = FAILED
                if state == QUEUED and r["expires_at"] is not None and now + backoff(r["attempts"]) > r["expires_at"]:
                    state = CANCELLED                # time-sensitive work is dropped, never run late
                con.execute("UPDATE jobs SET state=?, run_after=?, updated=?, lease_until=NULL, last_error=?,"
                            " finished_at=CASE WHEN ? IN ('failed','cancelled') THEN ? ELSE finished_at END "
                            "WHERE id=?", (state, now + (0 if force else backoff(r["attempts"])) if state == QUEUED else 0, now,
                                           "worker stopped or timed out while this job was running", state, now, r["id"]))
                out.append({"id": r["id"], "kind": r["kind"], "state": state, "attempts": r["attempts"],
                            "campaign_id": r["campaign_id"]})
        return out

    def get(self, job_id: int) -> Optional[dict]:
        with self._conn() as con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(r) if r else None

    def list(self, states: Optional[Iterable[str]] = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM jobs", []
        if states:
            states = list(states)
            sql += f" WHERE state IN ({','.join('?' * len(states))})"
            args += states
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._conn() as con:
            return [self._job(r) for r in con.execute(sql, args)]

    def counts(self) -> dict:
        with self._conn() as con:
            return {r["state"]: r["n"] for r in con.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state")}

    def purge_finished(self, older_than_s: float = 30 * 86400, now: Optional[float] = None) -> int:
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            return con.execute("DELETE FROM jobs WHERE state IN ('done','cancelled') AND finished_at<?",
                               (now - older_than_s,)).rowcount

    # ── outbox: sends that must never be duplicated ──────────────────────────
    def outbox_add(self, idem_key: str, payload: dict, *, campaign_id: str = "", recipient_key: str = "",
                   now: Optional[float] = None) -> tuple[int, bool]:
        """Queue one external message. The same idem_key is accepted once: (id, created)."""
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            r = con.execute("SELECT id FROM outbox WHERE idem_key=?", (idem_key,)).fetchone()
            if r:
                return int(r["id"]), False
            cur = con.execute("INSERT INTO outbox (idem_key, campaign_id, recipient_key, payload, created, updated)"
                              " VALUES (?,?,?,?,?,?)", (idem_key, campaign_id, recipient_key, _j(payload), now, now))
            return int(cur.lastrowid), True

    def outbox_claim(self, *, campaign_id: Optional[str] = None, now: Optional[float] = None,
                     allow=None) -> Optional[dict]:
        """Move the oldest queued message to 'sending' BEFORE the provider is called, so a crash
        mid-send leaves a row that says "might have been sent" instead of "never sent".
        `allow` (e.g. "kill switch is off") is checked first: when it says no, nothing is claimed."""
        if allow is not None and not allow():
            return None
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            sql = "SELECT * FROM outbox WHERE state='queued'" + (" AND campaign_id=?" if campaign_id else "") + \
                  " ORDER BY id ASC LIMIT 1"
            r = con.execute(sql, (campaign_id,) if campaign_id else ()).fetchone()
            if r is None:
                return None
            con.execute("UPDATE outbox SET state='sending', attempts=attempts+1, updated=?, attempted_at=? WHERE id=?",
                        (now, now, r["id"]))
            row = dict(con.execute("SELECT * FROM outbox WHERE id=?", (r["id"],)).fetchone())
        row["payload"] = _unj(row["payload"])
        return row

    def outbox_sent(self, oid: int, message_id: str, thread_id: str = "", now: Optional[float] = None) -> bool:
        """Record a provider-confirmed send. Needs the provider's message id: without it the row is
        not "sent". Only a message being sent (or reconciled) can become sent."""
        if not str(message_id or "").strip():
            raise ValueError("a send is only 'sent' with the provider's message id")
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            return con.execute("UPDATE outbox SET state='sent', provider_message_id=?, provider_thread_id=?, sent_at=?,"
                               " updated=?, last_error='' WHERE id=? AND state IN ('sending','uncertain')",
                               (message_id, thread_id, now, now, oid)).rowcount > 0

    def _outbox_move(self, oid: int, frm: tuple, to: str, error: str, now: Optional[float]) -> bool:
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            return con.execute(f"UPDATE outbox SET state=?, last_error=?, updated=? WHERE id=? "
                               f"AND state IN ({','.join('?' * len(frm))})", (to, error[:300], now, oid, *frm)).rowcount > 0

    def outbox_fail(self, oid: int, error: str, now: Optional[float] = None) -> bool:
        """The provider definitely rejected it (nothing was sent): sending -> failed."""
        return self._outbox_move(oid, (O_SENDING,), O_FAILED, error, now)

    def outbox_requeue(self, oid: int, reason: str, now: Optional[float] = None) -> bool:
        """The provider refused before sending anything (limit, block): sending -> queued, try later."""
        return self._outbox_move(oid, (O_SENDING,), O_QUEUED, reason, now)

    def outbox_hold(self, oid: int, reason: str, now: Optional[float] = None) -> bool:
        """Park a message for a person to look at: sending/uncertain -> held. Never resent by itself."""
        return self._outbox_move(oid, (O_SENDING, O_UNCERTAIN), O_HELD, reason, now)

    def outbox_mark_uncertain(self, oid: int, reason: str, now: Optional[float] = None) -> bool:
        """The call timed out or crashed: it may have been delivered. sending -> uncertain."""
        return self._outbox_move(oid, (O_SENDING,), O_UNCERTAIN, reason, now)

    def outbox_recover(self, now: Optional[float] = None, grace_s: float = 0.0) -> int:
        """After a crash: anything still 'sending' may or may not have gone out. Never resend it
        blindly: it becomes 'uncertain' and is reconciled with the provider or held for review.
        Call once at startup, under the process lock (so no live sender can be in flight); with a
        grace period only rows older than that are touched."""
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            return con.execute("UPDATE outbox SET state='uncertain', updated=?, last_error=? "
                               "WHERE state='sending' AND updated<=?",
                               (now, "worker stopped during send; outcome unknown", now - grace_s)).rowcount

    def outbox_list(self, states: Optional[Iterable[str]] = None, campaign_id: Optional[str] = None,
                    limit: int = 200) -> list[dict]:
        sql, args, where = "SELECT * FROM outbox", [], []
        if states:
            states = list(states)
            where.append(f"state IN ({','.join('?' * len(states))})"); args += states
        if campaign_id:
            where.append("campaign_id=?"); args.append(campaign_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(int(limit))
        with self._conn() as con:
            rows = [dict(r) for r in con.execute(sql, args)]
        for r in rows:
            r["payload"] = _unj(r["payload"])
        return rows

    def outbox_count_sent_since(self, since: float, campaign_id: Optional[str] = None) -> int:
        """Messages that went out (or may have: sending/uncertain) since `since`: used for daily caps."""
        sql = ("SELECT COUNT(*) FROM outbox WHERE state IN ('sent','sending','uncertain','held') "
               "AND COALESCE(attempted_at, created)>=?")
        args: list = [since]
        if campaign_id:
            sql += " AND campaign_id=?"; args.append(campaign_id)
        with self._conn() as con:
            return int(con.execute(sql, args).fetchone()[0])

    def reconcile(self, oid: int, lookup, now: Optional[float] = None) -> str:
        """Settle an uncertain send. `lookup(row)` asks the provider and returns
        {"found": True, "message_id":..., "thread_id":...}, {"found": False, "authoritative": True}
        (the provider can prove it was not sent), or anything else/raises (cannot tell).
        Not provably unsent means held for review, never resent."""
        with self._conn() as con:
            r = con.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
        if r is None or r["state"] != O_UNCERTAIN:
            return r["state"] if r else "missing"
        row = dict(r)
        row["payload"] = _unj(row["payload"])
        try:
            answer = lookup(row)
        except Exception as exc:
            self.outbox_hold(oid, f"could not check with provider: {type(exc).__name__}", now)
            return O_HELD
        if isinstance(answer, dict) and answer.get("found"):
            if not str(answer.get("message_id") or "").strip():
                self.outbox_hold(oid, "provider reported a match without a message id; held for review", now)
                return O_HELD
            ok = self.outbox_sent(oid, str(answer["message_id"]), str(answer.get("thread_id") or ""), now)
            return O_SENT if ok else self._state_of(oid)
        if isinstance(answer, dict) and answer.get("found") is False and answer.get("authoritative"):
            ok = self._outbox_move(oid, (O_UNCERTAIN,), O_QUEUED, "provider confirmed it was not sent", now)
            return O_QUEUED if ok else self._state_of(oid)
        self.outbox_hold(oid, "provider could not confirm; held for review", now)
        return O_HELD

    def outbox_get(self, oid: int) -> Optional[dict]:
        with self._conn() as con:
            r = con.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["payload"] = _unj(d["payload"])
        return d

    def _state_of(self, oid: int) -> str:
        with self._conn() as con:
            r = con.execute("SELECT state FROM outbox WHERE id=?", (oid,)).fetchone()
        return r["state"] if r else "missing"

    # ── schedules: persistent next-run with a catch-up policy ────────────────
    def schedule(self, name: str, kind: str, every_s: float, *, payload: Optional[dict] = None,
                 campaign_id: str = "", max_lateness_s: float = 0.0, catchup: str = "run_once",
                 start_at: Optional[float] = None, now: Optional[float] = None) -> None:
        """Create or update a schedule. catchup: "run_once" (after downtime, run once and move on) or
        "skip_expired" (time-sensitive: a slot later than max_lateness_s is dropped, never replayed)."""
        if catchup not in ("run_once", "skip_expired"):
            raise ValueError("catchup must be run_once or skip_expired")
        if catchup == "skip_expired" and max_lateness_s <= 0:
            raise ValueError("skip_expired needs max_lateness_s > 0 (how late a slot may still run)")
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            r = con.execute("SELECT next_run FROM schedules WHERE name=?", (name,)).fetchone()
            nxt = r["next_run"] if r else (start_at if start_at is not None else now)
            con.execute("INSERT INTO schedules (name, kind, payload, campaign_id, every_s, next_run, max_lateness_s, catchup)"
                        " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,"
                        " payload=excluded.payload, campaign_id=excluded.campaign_id, every_s=excluded.every_s,"
                        " max_lateness_s=excluded.max_lateness_s, catchup=excluded.catchup",
                        (name, kind, _j(payload), campaign_id, every_s, nxt, max_lateness_s, catchup))

    def set_schedule_enabled(self, name: str, enabled: bool) -> None:
        with self._conn(immediate=True) as con:
            con.execute("UPDATE schedules SET enabled=? WHERE name=?", (1 if enabled else 0, name))

    def tick_schedules(self, now: Optional[float] = None) -> dict:
        """Enqueue what is due. After downtime a schedule fires AT MOST once (never one per missed
        slot), a skip_expired slot that is too late is dropped, and next_run always moves into the future."""
        now = now if now is not None else time.time()
        made, skipped = [], []
        with self._conn(immediate=True) as con:
            for s in con.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (now,)).fetchall():
                every = max(1.0, s["every_s"])
                steps = int(max(0.0, now - s["next_run"]) // every) + 1
                slot_time = s["next_run"] + (steps - 1) * every          # the latest slot that is due
                new_next = slot_time + every
                lateness = now - slot_time
                if s["catchup"] == "skip_expired" and lateness > s["max_lateness_s"]:
                    skipped.append(s["name"])
                else:
                    slot = f"{s['name']}@{int(slot_time)}"
                    expires = slot_time + s["max_lateness_s"] if s["catchup"] == "skip_expired" else None
                    if not con.execute("SELECT 1 FROM jobs WHERE unique_key=?", (slot,)).fetchone():
                        cur = con.execute(
                            "INSERT INTO jobs (kind, campaign_id, payload, run_after, created, updated, unique_key,"
                            " expires_at) VALUES (?,?,?,?,?,?,?,?)",
                            (s["kind"], s["campaign_id"], s["payload"], 0, now, now, slot, expires))
                        made.append(int(cur.lastrowid))
                con.execute("UPDATE schedules SET next_run=? WHERE name=?", (new_next, s["name"]))
        return {"enqueued": made, "skipped": skipped}

    def schedules(self) -> list[dict]:
        with self._conn() as con:
            return [dict(r) for r in con.execute("SELECT * FROM schedules ORDER BY name")]

    # ── campaign locks: one executor per campaign ────────────────────────────
    def acquire_lock(self, name: str, owner: str, lease_s: float, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        with self._conn(immediate=True) as con:
            r = con.execute("SELECT owner, lease_until FROM locks WHERE name=?", (name,)).fetchone()
            if r and r["owner"] != owner and r["lease_until"] > now:
                return False
            con.execute("INSERT INTO locks (name, owner, lease_until) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE "
                        "SET owner=excluded.owner, lease_until=excluded.lease_until", (name, owner, now + lease_s))
            return True

    def release_lock(self, name: str, owner: str) -> None:
        with self._conn(immediate=True) as con:
            con.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))
