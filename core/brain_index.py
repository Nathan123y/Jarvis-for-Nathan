"""The brain's recall: a local full-text index of everything in the Obsidian brain.

The notes in <vault>/Jarvis Brain are what you read; this index is how Jarvis
and Claude find things in them fast. It is a cache, rebuilt from the notes at any
time (`python3 -m core.brain_index rebuild`), and it is local on purpose: iCloud
moves notes off the Mac, and recall must not wait on a download or the internet.

What is in it (`kind`):
    log      one line of a day's log (written as it happens, by core/vault_brain.py)
    digest   a day's summary from the nightly consolidation (worker/brain_sleep.py)
    week     a week's summary
    lesson   something learned, with its area
    concept  a named thing (product, person, coin, ...) and what happened with it
    fact     one of Jarvis's facts about you (Jarvis Memory notes)
    memory   a paragraph of Claude Code's memory notes
    note     a paragraph of your own notes elsewhere in the vault

Ranking: full-text relevance (bm25), times how much a kind of thing matters
(money and your own words weigh more than a routine status line), times recency.
Shared by the app, the worker and the Claude Code hooks: WAL mode, short busy wait,
and every public function swallows its own errors.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "memory" / "brain_index.db"


def db_path() -> Path:
    """JARVIS_BRAIN_INDEX if set; a throwaway file while the test suite runs; else memory/brain_index.db."""
    env = os.environ.get("JARVIS_BRAIN_INDEX")
    if env:
        return Path(env)
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if (getattr(spec, "name", "") or "").split(".")[0] in ("unittest", "pytest"):
        import tempfile
        return Path(tempfile.gettempdir()) / f"jarvis-brain-index-test-{os.getpid()}.db"
    return DB_PATH

KIND_WEIGHT = {"digest": 3.0, "week": 3.0, "lesson": 3.0, "thread": 2.5, "concept": 2.5, "fact": 2.5,
               "memory": 2.0, "note": 1.5, "log": 1.0}
_MONEY = re.compile(r"(\$\s?\d|\bsold\b|\bsale\b|\bsales\b|p&l|\bprofit|\border(ed)?\b|\bpaid\b|\brevenue)", re.I)
_DONE = re.compile(r"\b(posted|listed|published|applied|uploaded|approved|launched|fixed|merged|bought)\b", re.I)
_BROKEN = re.compile(r"\b(fail(ed|s|ing)?|error|broke|suspended|restricted|banned|warning|crash)", re.I)
_STOP = set("""a an and are as at be been but by can could did do does doing for from had has have how i if in
into is it its just me my of on or our so than that the their them then there these they this to u up us was
we were what when where which who why will with would you your ur im ill ive dont can't cant yeah yes ok okay
please pls lets let make made get got go going want need now today yesterday week some any all also about
again still like think know see look tell show give help thing things stuff more much very really
add new feature use using used work working set build check change run running put keep take try way one
time every each first next good better best right sure cool thanks thank hey hi hello wanna gonna should
isn't aren't didn't doesn't it's that's there's what's how's can't won't i'm you're we're they're""".split())

_local = threading.local()
_LINE = re.compile(r"^- (\d{2}):(\d{2}) · \[\[([^\]]+)\]\] · (.*)$")


def _connect() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    p = db_path()
    path = str(p)
    if con is not None and getattr(_local, "path", None) == path:
        return con
    if con is not None:
        try:
            con.close()
        except Exception:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=3.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=3000")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY, key TEXT UNIQUE, ts REAL NOT NULL, day TEXT NOT NULL,
            area TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, text TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS items_day ON items (day, kind);
        CREATE INDEX IF NOT EXISTS items_kind ON items (kind, ts);
        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
            text, area, content='items', content_rowid='id', tokenize='porter unicode61');
        CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
            INSERT INTO items_fts(rowid, text, area) VALUES (new.id, new.text, new.area); END;
        CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
            INSERT INTO items_fts(items_fts, rowid, text, area) VALUES ('delete', old.id, old.text, old.area); END;
        CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
            INSERT INTO items_fts(items_fts, rowid, text, area) VALUES ('delete', old.id, old.text, old.area);
            INSERT INTO items_fts(rowid, text, area) VALUES (new.id, new.text, new.area); END;
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL NOT NULL);
    """)
    _local.con, _local.path = con, path
    return con


def weigh(kind: str, area: str, text: str) -> float:
    w = KIND_WEIGHT.get(kind, 1.0)
    if kind == "log":
        if _MONEY.search(text) or area == "Trading":
            w *= 2.5
        elif text.startswith("You:"):
            w *= 2.0                       # what you said yourself
        elif area == "Claude Code" or _DONE.search(text):
            w *= 1.6
        elif _BROKEN.search(text):
            w *= 1.3
    return round(w, 3)


def _key(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def put(kind: str, text: str, *, area: str = "", when: datetime | None = None, key: str = "",
        source: str = "") -> None:
    """Add or replace one item. `key` makes it replaceable (a digest, a fact); default: the content."""
    try:
        text = " ".join(str(text or "").split())
        if not text:
            return
        when = when or datetime.now()
        key = key or _key(kind, when.strftime("%Y-%m-%d %H:%M"), area, text)
        con = _connect()
        with con:
            con.execute(
                "INSERT INTO items (key, ts, day, area, kind, text, weight, source) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, day=excluded.day, area=excluded.area, "
                "kind=excluded.kind, text=excluded.text, weight=excluded.weight, source=excluded.source "
                "WHERE items.text != excluded.text OR items.area != excluded.area",
                (key, when.timestamp(), when.date().isoformat(), area, kind, text, weigh(kind, area, text), source))
    except Exception as e:
        print(f"[Brain index] put failed: {e!r}")


def add_log_line(area: str, text: str, when: datetime) -> None:
    put("log", text, area=area, when=when)


def drop(where: str, args=()) -> None:
    try:
        con = _connect()
        with con:
            con.execute(f"DELETE FROM items WHERE {where}", args)
    except Exception as e:
        print(f"[Brain index] drop failed: {e!r}")


# ── search ────────────────────────────────────────────────────────────────────

def terms(query: str) -> list[str]:
    words = re.findall(r"[\w$&'-]+", (query or "").lower())
    out = []
    for w in words:
        w = w.strip("'-")
        if len(w) < 2 or w in _STOP or w in out:
            continue
        out.append(w)
    return out[:12]


def _match_expr(words: list[str]) -> str:
    safe = [re.sub(r'[^\w$&-]', "", w).replace('"', "") for w in words]
    return " OR ".join(f'"{w}"*' for w in safe if w)


def search(query: str = "", *, area: str = "", kinds=None, since: str = "", until: str = "",
           limit: int = 12, exclude_since_ts: float = 0.0, exclude_area: str = "") -> list[dict]:
    """Best matches for `query` (all items in the range when it is empty), best first.
    since/until are ISO days, inclusive. exclude_area items newer than exclude_since_ts are left out."""
    try:
        con = _connect()
        where, args = [], []
        if area:
            where.append("i.area = ? COLLATE NOCASE")
            args.append(area)
        if kinds:
            kinds = list(kinds)
            where.append(f"i.kind IN ({','.join('?' * len(kinds))})")
            args += kinds
        if since:
            where.append("i.day >= ?")
            args.append(since)
        if until:
            where.append("i.day <= ?")
            args.append(until)
        if exclude_since_ts and exclude_area:
            where.append("NOT (i.area = ? AND i.ts >= ?)")
            args += [exclude_area, exclude_since_ts]
        words = terms(query)
        now = time.time()
        if words:
            sql = ("SELECT i.*, bm25(items_fts) AS rank FROM items_fts JOIN items i ON i.id = items_fts.rowid "
                   "WHERE items_fts MATCH ?" + "".join(f" AND {w}" for w in where) + " ORDER BY rank LIMIT 300")
            rows = con.execute(sql, [_match_expr(words)] + args).fetchall()
        else:
            sql = ("SELECT i.*, -1.0 AS rank FROM items i" + (" WHERE " + " AND ".join(where) if where else "")
                   + " ORDER BY i.ts DESC LIMIT 600")
            rows = con.execute(sql, args).fetchall()
        cols = [d[0] for d in con.execute("SELECT * FROM items LIMIT 0").description] + ["rank"]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            age_days = max(0.0, (now - d["ts"]) / 86400)
            recency = 1.0 / (1.0 + age_days / 21.0)
            hits = sum(1 for w in words if w in d["text"].lower()) if words else 0
            d["score"] = (-d["rank"] + 0.15 * hits) * d["weight"] * (0.35 + recency)
            out.append(d)
        out.sort(key=lambda d: d["score"], reverse=True)
        seen, best = set(), []
        for d in out:
            sig = (d["kind"], d["day"], re.sub(r"\d+", "#", d["text"].lower())[:90])
            if sig in seen:
                continue
            seen.add(sig)
            best.append(d)
            if len(best) >= limit:
                break
        return best
    except Exception as e:
        print(f"[Brain index] search failed: {e!r}")
        return []


def day_items(day: str, kinds=("log",)) -> list[dict]:
    try:
        con = _connect()
        kinds = list(kinds)
        rows = con.execute(f"SELECT ts, area, kind, text, weight FROM items WHERE day = ? AND kind IN "
                           f"({','.join('?' * len(kinds))}) ORDER BY ts", [day] + kinds).fetchall()
        return [dict(zip(("ts", "area", "kind", "text", "weight"), r)) for r in rows]
    except Exception as e:
        print(f"[Brain index] day_items failed: {e!r}")
        return []


def area_counts(day: str) -> dict:
    try:
        rows = _connect().execute("SELECT area, COUNT(*) FROM items WHERE day = ? AND kind = 'log' "
                                  "GROUP BY area ORDER BY COUNT(*) DESC", (day,)).fetchall()
        return dict(rows)
    except Exception:
        return {}


# ── rebuilding from the notes ─────────────────────────────────────────────────

def _paragraphs(text: str, limit: int = 600) -> list[str]:
    text = re.sub(r"^---\n.*?\n---\n", "", text or "", flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"(?m)\bRelated:\s*(\[\[[^\]]+\]\][,;. ]*)+", " ", text)    # link lists match everything
    text = re.sub(r"\[\[([^\]|]+)(\|[^\]]*)?\]\]", r"\1", text)
    out = []
    for block in re.split(r"\n\s*\n", text):
        b = " ".join(block.split())
        if len(b) < 12:
            continue
        while b:
            out.append(b[:limit])
            b = b[limit:]
    return out


def _file_changed(con, path: Path) -> float | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    row = con.execute("SELECT mtime FROM files WHERE path = ?", (str(path),)).fetchone()
    return None if row and abs(row[0] - mtime) < 1e-6 else mtime


def _mark(con, path: Path, mtime: float) -> None:
    with con:
        con.execute("INSERT INTO files (path, mtime) VALUES (?, ?) ON CONFLICT(path) DO UPDATE SET "
                    "mtime=excluded.mtime", (str(path), mtime))


def index_log_note(path: Path) -> int:
    day = path.stem
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        when = datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)))
        text = re.sub(r" → (\[\[[^\]]+\]\](, )?)+$", "", m.group(4))
        put("log", text, area=m.group(3), when=when)
        n += 1
    return n


def index_note(path: Path, kind: str, area: str = "") -> int:
    """Replace one note's paragraphs (memory / note kinds)."""
    src = str(path)
    drop("source = ? AND kind = ?", (src, kind))
    try:
        when = datetime.fromtimestamp(path.stat().st_mtime)
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    paras = _paragraphs(text)
    for i, p in enumerate(paras):
        put(kind, f"{path.stem}: {p}", area=area, when=when, key=_key(kind, src, i), source=src)
    return len(paras)


def index_facts(memory: dict, notes: dict) -> None:
    """Jarvis's facts, one item each, replacing the previous set (the notes are the truth)."""
    drop("kind = 'fact'")
    for cat, title in notes.items():
        for key, entry in (memory.get(cat) or {}).items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            if not value:
                continue
            updated = entry.get("updated") if isinstance(entry, dict) else ""
            try:
                when = datetime.fromisoformat(updated) if updated else datetime.now()
            except ValueError:
                when = datetime.now()
            put("fact", f"{key.replace('_', ' ')}: {value}", area=title, when=when, key=f"fact:{title}:{key}")


def _index_memory_notes(con, brain: Path) -> int:
    """Re-index Jarvis's facts from its memory notes when any of them changed."""
    from core import vault_brain
    paths = [brain / "Jarvis Memory" / f"{t}.md" for t in vault_brain.MEMORY_NOTES.values()]
    changed = [(p, m) for p in paths if (m := _file_changed(con, p)) is not None]
    if not changed:
        return 0
    memory = {}
    for cat, title in vault_brain.MEMORY_NOTES.items():
        try:
            memory[cat] = vault_brain.parse_memory_note(
                (brain / "Jarvis Memory" / f"{title}.md").read_text(encoding="utf-8")) or {}
        except OSError:
            memory[cat] = {}
    index_facts(memory, vault_brain.MEMORY_NOTES)
    for p, m in changed:
        _mark(con, p, m)
    return sum(len(v) for v in memory.values())


def refresh(brain: Path, *, full: bool = False, with_logs: bool = True, with_vault_notes: bool = True) -> dict:
    """Index notes that changed since last time (all of them with full=True)."""
    stats = {"log": 0, "memory": 0, "note": 0, "fact": 0}
    try:
        con = _connect()
        if full:
            with con:
                con.execute("DELETE FROM files")
            drop("kind IN ('memory', 'note')")             # paragraphs of notes since deleted or excluded
        for p in (sorted((brain / "Log").glob("????-??-??.md")) if with_logs else ()):
            m = _file_changed(con, p)
            if m is not None:
                stats["log"] += index_log_note(p)
                _mark(con, p, m)
        stats["fact"] = _index_memory_notes(con, brain)
        for p in sorted(q for q in (brain / "Claude Memory").glob("*.md") if q.stem != "MEMORY"):
            m = _file_changed(con, p)
            if m is not None:
                stats["memory"] += index_note(p, "memory", "Claude Memory")
                _mark(con, p, m)
        if with_vault_notes:
            vault = brain.parent
            for p in vault.rglob("*.md"):
                if brain in p.parents or any(x.startswith(".") for x in p.relative_to(vault).parts):
                    continue
                m = _file_changed(con, p)
                if m is not None:
                    stats["note"] += index_note(p, "note", p.parent.name if p.parent != vault else "")
                    _mark(con, p, m)
    except Exception as e:
        print(f"[Brain index] refresh failed: {e!r}")
    return stats


def count() -> int:
    try:
        return _connect().execute("SELECT COUNT(*) FROM items").fetchone()[0]
    except Exception:
        return 0


if __name__ == "__main__":
    sys.path.insert(0, str(BASE))
    from core import vault_brain
    vault_brain.allow_icloud_downloads(thread_only=False)
    b = vault_brain.brain_dir()
    if b is None:
        print("No Obsidian vault is connected.")
        sys.exit(1)
    full = len(sys.argv) > 1 and sys.argv[1] == "rebuild"
    print(refresh(b, full=full), "items:", count())
