"""SQLite storage. One file in the data folder; WAL mode so the web server and the design worker can share it."""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY,
  ref TEXT UNIQUE NOT NULL,
  token_hash TEXT UNIQUE NOT NULL,
  token_nonce TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  email TEXT NOT NULL,
  garment TEXT NOT NULL,
  idea TEXT NOT NULL,
  colors TEXT NOT NULL DEFAULT '',
  style TEXT NOT NULL,
  back_name TEXT NOT NULL DEFAULT '',
  back_number TEXT NOT NULL DEFAULT '',
  instructions TEXT NOT NULL DEFAULT '',
  own_brand INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  revision_limit INTEGER NOT NULL,
  revisions_used INTEGER NOT NULL DEFAULT 0,
  generations_used INTEGER NOT NULL DEFAULT 0,
  chosen_version_id INTEGER,
  branding_status TEXT NOT NULL DEFAULT 'not_needed',
  branding_notes TEXT NOT NULL DEFAULT '',
  admin_instructions TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  kind TEXT NOT NULL,          -- reference | concept | proof | production | template
  panel TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL,
  original_name TEXT NOT NULL DEFAULT '',
  mime TEXT NOT NULL,
  width INTEGER, height INTEGER,
  bytes INTEGER NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  version INTEGER NOT NULL,
  data TEXT NOT NULL,
  source TEXT NOT NULL,        -- interpreter | revision | admin
  created_at REAL NOT NULL,
  UNIQUE(request_id, version)
);

CREATE TABLE IF NOT EXISTS design_versions (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  version_no INTEGER NOT NULL,         -- 1, 2, 3 … across the whole request, never reused
  label TEXT NOT NULL,                 -- A / B for first round, A2 etc. for revisions
  round INTEGER NOT NULL,
  parent_id INTEGER REFERENCES design_versions(id),
  brief_id INTEGER REFERENCES briefs(id),
  job_id INTEGER,
  prompt TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL,              -- demo | codex | openai | manual
  is_demo INTEGER NOT NULL DEFAULT 0,
  front_asset_id INTEGER NOT NULL REFERENCES assets(id),
  back_asset_id INTEGER NOT NULL REFERENCES assets(id),
  checks TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL,                 -- draft | held | sent | chosen | replaced
  created_at REAL NOT NULL,
  sent_at REAL,
  UNIQUE(request_id, version_no)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  kind TEXT NOT NULL,                  -- initial | revision | regenerate
  status TEXT NOT NULL,                -- queued | running | succeeded | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0,
  stage TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  base_version_id INTEGER,
  revision_id INTEGER,
  not_before REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  version_id INTEGER NOT NULL REFERENCES design_versions(id),
  note TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS proofs (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  version_id INTEGER NOT NULL REFERENCES design_versions(id),
  front_asset_id INTEGER NOT NULL, back_asset_id INTEGER NOT NULL,
  source TEXT NOT NULL,                -- concept | admin_upload
  back_name TEXT NOT NULL DEFAULT '', back_number TEXT NOT NULL DEFAULT '',
  current INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  version_id INTEGER NOT NULL REFERENCES design_versions(id),
  proof_id INTEGER NOT NULL REFERENCES proofs(id),
  garment TEXT NOT NULL, back_name TEXT NOT NULL, back_number TEXT NOT NULL,
  size TEXT NOT NULL, quantity INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  valid INTEGER NOT NULL DEFAULT 1,
  invalidated_reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL UNIQUE REFERENCES requests(id),
  approval_id INTEGER REFERENCES approvals(id),
  currency TEXT NOT NULL,
  unit_price_cents INTEGER NOT NULL, quantity INTEGER NOT NULL,
  subtotal_cents INTEGER NOT NULL, shipping_cents INTEGER NOT NULL,
  tax_cents INTEGER, total_cents INTEGER NOT NULL,
  prices_are_demo INTEGER NOT NULL,
  payment_status TEXT NOT NULL DEFAULT 'unpaid',   -- unpaid | pending | paid | refunded
  paid_approval_id INTEGER,
  paid_amount_cents INTEGER,
  paid_at REAL,
  checkout_ref TEXT,
  shipping TEXT NOT NULL DEFAULT '{}',
  artwork_status TEXT NOT NULL DEFAULT 'missing',  -- missing | needs_review | passed | failed
  artwork_notes TEXT NOT NULL DEFAULT '',
  production_status TEXT NOT NULL DEFAULT 'not_submitted',  -- not_submitted | submitting | submitted | in_production | shipped
  fulfillment_provider TEXT, fulfillment_ref TEXT, submitted_at REAL,
  carrier TEXT NOT NULL DEFAULT '', tracking_number TEXT NOT NULL DEFAULT '', tracking_url TEXT NOT NULL DEFAULT '',
  manufacturing_cost_cents INTEGER,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS checkouts (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  approval_id INTEGER NOT NULL REFERENCES approvals(id),
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,               -- open | paid | expired
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_events (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  event_id TEXT NOT NULL,
  order_id INTEGER,
  kind TEXT NOT NULL,
  amount_cents INTEGER,
  outcome TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(provider, event_id)
);

CREATE TABLE IF NOT EXISTS updates (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  message TEXT NOT NULL,
  customer_visible INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS emails (
  id INTEGER PRIMARY KEY,
  request_id INTEGER,
  kind TEXT NOT NULL,
  to_addr TEXT NOT NULL,
  subject TEXT NOT NULL,
  html TEXT NOT NULL,
  text TEXT NOT NULL,
  provider TEXT NOT NULL,
  status TEXT NOT NULL,               -- previewed | sent | failed
  error TEXT NOT NULL DEFAULT '',
  dedupe_key TEXT UNIQUE,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, not_before);
CREATE INDEX IF NOT EXISTS idx_versions_req ON design_versions(request_id);
CREATE INDEX IF NOT EXISTS idx_assets_req ON assets(request_id);
"""


class DB:
    """Thin wrapper: one connection per thread, rows as dicts, a write lock for multi-statement transactions."""

    def __init__(self, path: Path):
        self.path = str(path)
        self._local = threading.local()
        self._all = []
        self.lock = threading.RLock()
        with self.lock:
            self.conn().executescript(SCHEMA)     # executescript manages its own transaction

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
            with self.lock:
                self._all.append(c)
        return c

    def close_all(self):
        with self.lock:
            for c in self._all:
                c.close()
            self._all.clear()
        self._local = threading.local()

    class _Tx:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            self.db.lock.acquire()
            self.c = self.db.conn()
            loc = self.db._local
            loc.depth = getattr(loc, "depth", 0) + 1
            if loc.depth == 1:                   # nested `with db.tx()` joins the outer transaction
                self.c.execute("BEGIN IMMEDIATE")
            return self.c

        def __exit__(self, et, ev, tb):
            loc = self.db._local
            try:
                loc.depth -= 1
                if loc.depth == 0:
                    self.c.execute("COMMIT" if et is None else "ROLLBACK")
            finally:
                self.db.lock.release()
            return False

    def tx(self):
        """`with db.tx() as c:` — an IMMEDIATE transaction (serialises writers, so check-then-write is safe)."""
        return DB._Tx(self)

    def one(self, sql, args=()):
        r = self.conn().execute(sql, args).fetchone()
        return dict(r) if r else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    def run(self, sql, args=()):
        with self.tx() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid


def now() -> float:
    return time.time()


def jloads(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default
