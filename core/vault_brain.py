"""Jarvis Brain: what Jarvis, its crews and Claude Code do, kept as linked notes
in the connected Obsidian vault around one hub note, [[Nucleus]].

    <vault>/Jarvis Brain/
        Nucleus.md               the hub: every area, the latest days, both memories, your notes
        Areas/<Area>.md          one hub per area (Shop, Viral Videos, Trading, ...)
        Log/YYYY-MM-DD.md        one line per thing that happened, each linking its area
        Jarvis Memory/Me - *.md  what Jarvis knows about you. Two-way: edit or delete a
                                 line in Obsidian and Jarvis goes by it (memory_manager.py)
        Claude Memory/           Claude Code's memory notes (its autoMemoryDirectory)

The vault is the one Jarvis is connected to (actions/obsidian_notes.py). The app,
the worker and the Claude Code hook (scripts/claude_vault_log.py) all write here,
so every append holds a file lock.

Nothing here may break or slow its caller: every public function swallows its own
errors, and log() hands lines to a background writer, because an iCloud vault can
stall on a write.
"""
from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import platform
import queue
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BRAIN = "Jarvis Brain"
NUCLEUS = "Nucleus"
LINE_MAX = 420
REPEAT_QUIET_S = 3 * 3600          # a repeated worker line (same failure every few minutes) is written once per 3 h
VAULT_RECHECK_S = 300.0

# Area hub notes: title -> (what it is, Claude memory notes about it).
AREAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Conversations":   ("What you said to Jarvis, what it answered, and every tool it used.", ()),
    "Jarvis Thoughts": ("Heads-ups and ideas Jarvis came up with on its own between conversations.",
                        ("jarvis-brain",)),
    "Jarvis Memory":   ("Facts Jarvis saved about you. The notes linked below ARE its memory: "
                        "edit or delete a line and Jarvis goes by it.", ()),
    "Claude Code":     ("Every request you gave Claude Code and what it changed.",
                        ("auto-push-after-changes", "parallel-sessions", "claude-for-services")),
    "Shop":            ("Print-on-demand shop: designs made daily, listed through Printify on Etsy, eBay, "
                        "TikTok Shop and the storefront.", ("etsy-shop",)),
    "Jersey Line":     ("Original crossover jerseys drawn and listed on every store.", ("jersey-line",)),
    "Viral Videos":    ("Narrated what-if sports shorts posted to TikTok and Instagram Reels.", ("viral-videos",)),
    "GTA Clips":       ("Streamer reaction and live-moment clips posted to YouTube Shorts.", ("game-clips",)),
    "GTA Guides":      ("GTA 6 list Shorts, fed by what got views.", ("game-guides",)),
    "Internships":     ("Summer 2027 internship applications and company replies.",
                        ("internship-crew", "internship-answers")),
    "Trading":         ("Paper-trading bots, memecoin bot and their results.",
                        ("trading-live-money-later", "trading-risk-appetite", "trading-agents",
                         "memecoin-research")),
    "Website Pitcher": ("Finds local businesses, builds them a site and pitches it.", ("website-pitcher",)),
    "AI Phone Line":   ("The website-sales phone line.", ("ai-phone-line",)),
    "Phone Alerts":    ("Texts sent to your phone: sales, reminders, news.", ("phone-alerts",)),
    "Home Dashboard":  ("The Home page at localhost:8460 showing money and every crew.", ("agents-hq",)),
    "School":          ("Classes, Canvas and homework.", ("canvas-login",)),
    "Lights & Home":   ("Lights and the rest of the room.", ("govee-lights-pending",)),
    "Worker":          ("The background worker itself: starts, stops and anything not filed elsewhere.", ()),
}

MEMORY_NOTES = {
    "identity":      "Me - Identity",
    "preferences":   "Me - Preferences",
    "projects":      "Me - Projects",
    "relationships": "Me - People",
    "wishes":        "Me - Wishes",
    "notes":         "Me - Notes",
}
MEMORY_MARK = "<!-- jarvis-memory: one fact per line, `- **key**: value` -->"
_FACT = re.compile(r"^\s*[-*]\s+\*\*(?P<key>[^*\n]+?)\*\*\s*:\s*(?P<value>.*?)\s*"
                   r"(?:`(?P<date>\d{4}-\d{2}-\d{2})`)?\s*$")

_DAYS_START, _DAYS_END = "<!-- recent-days -->", "<!-- /recent-days -->"
_NOTES_START, _NOTES_END = "<!-- your-notes -->", "<!-- /your-notes -->"

# Which area a worker log line belongs to: by its "word:" prefix first, then by logger.
_PREFIX_AREAS = {
    "jerseys": "Jersey Line", "jersey": "Jersey Line", "storefront": "Shop", "shop": "Shop",
    "campaign": "Website Pitcher", "scout": "GTA Clips", "clips": "GTA Clips", "guides": "GTA Guides",
    "viral": "Viral Videos", "internships": "Internships", "trading": "Trading", "memebot": "Trading",
}
_LOGGER_AREAS = {
    "jarvis.shop": "Shop", "jarvis.viral": "Viral Videos", "jarvis.clips": "GTA Clips",
    "jarvis.guides": "GTA Guides", "jarvis.internships": "Internships", "jarvis.alerts": "Phone Alerts",
    "jarvis.phone": "AI Phone Line", "jarvis.campaign": "Website Pitcher", "jarvis.trading": "Trading",
    "jarvis.worker": "Worker",
}
# Jarvis tools (and plugins) by name fragment.
_TOOL_AREAS = (
    ("trad", "Trading"), ("money", "Trading"), ("etsy", "Shop"), ("shop", "Shop"), ("product_sales", "Shop"),
    ("intern", "Internships"), ("viral", "Viral Videos"), ("clip", "GTA Clips"), ("guide", "GTA Guides"),
    ("website_campaign", "Website Pitcher"), ("phone_line", "AI Phone Line"), ("phone_alert", "Phone Alerts"),
    ("light", "Lights & Home"), ("speaker", "Lights & Home"), ("canvas", "School"), ("ee_tutor", "School"),
    ("mission_control", "Home Dashboard"), ("memory", "Jarvis Memory"),
)
# Repo paths Claude Code edits, by fragment, for linking a Claude turn to the areas it touched.
_PATH_AREAS = (
    ("jersey", "Jersey Line"), ("shop", "Shop"), ("etsy", "Shop"), ("storefront", "Shop"), ("lettering", "Shop"),
    ("sweater", "Shop"), ("viral", "Viral Videos"), ("clip", "GTA Clips"), ("guide", "GTA Guides"),
    ("yt_stats", "GTA Guides"), ("intern", "Internships"), ("trading", "Trading"), ("optionsbots", "Trading"),
    ("campaign", "Website Pitcher"), ("worker/phone", "AI Phone Line"), ("alerts", "Phone Alerts"),
    ("agents_hq", "Home Dashboard"), ("canvas", "School"), ("core/brain.py", "Jarvis Thoughts"),
    ("memory_manager", "Jarvis Memory"), ("lights", "Lights & Home"),
)

_SECRETS = (
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b(\s*[=:]\s*)([^\s,;&'\"]+)"),
     r"\1\2[hidden]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [hidden]"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[hidden key]"),
    (re.compile(r"\b(ghp|gho|github_pat|xox[abp])_[A-Za-z0-9_]{12,}"), "[hidden key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[hidden key]"),
)


# ── where the brain lives ─────────────────────────────────────────────────────

def _state_file() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Jarvis" / "obsidian-vault.json"
    return Path.home() / ".config" / "jarvis" / "obsidian-vault.json"


_vault_cache: tuple[float, Path | None] = (0.0, None)
_skeleton_done: set[str] = set()


def _under_test() -> bool:
    """Running the test suite (python3 -m unittest / pytest): never touch the real vault."""
    import sys
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return (getattr(spec, "name", "") or "").split(".")[0] in ("unittest", "pytest")


def vault_root() -> Path | None:
    """The connected Obsidian vault, or None. JARVIS_BRAIN_VAULT overrides it (tests)."""
    global _vault_cache
    override = os.environ.get("JARVIS_BRAIN_VAULT")
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    if _under_test():
        return None
    now = time.monotonic()
    if _vault_cache[0] and now - _vault_cache[0] < VAULT_RECHECK_S:
        return _vault_cache[1]
    vault = None
    try:
        p = Path(json.loads(_state_file().read_text(encoding="utf-8"))["vault"]).expanduser()
        if (p / ".obsidian").is_dir():
            vault = p
    except Exception:
        vault = None
    _vault_cache = (now, vault)
    return vault


def brain_dir(create: bool = True) -> Path | None:
    """<vault>/Jarvis Brain, with its hub notes made the first time. None when no vault is connected."""
    vault = vault_root()
    if vault is None:
        return None
    brain = vault / BRAIN
    if create and str(brain) not in _skeleton_done:
        try:
            ensure_skeleton(brain)
            _skeleton_done.add(str(brain))
        except Exception as e:
            print(f"[Brain vault] could not set up {brain}: {e!r}")
    return brain


# ── note text ─────────────────────────────────────────────────────────────────

def clean(text, limit: int = LINE_MAX) -> str:
    """One safe line for a note: whitespace collapsed, secrets hidden, [[ ]] defused, capped."""
    s = " ".join(str(text or "").split())
    for rx, rep in _SECRETS:
        s = rx.sub(rep, s)
    s = s.replace("[[", "[").replace("]]", "]")
    if len(s) > limit:
        s = s[:limit - 1].rstrip() + "…"
    return s


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def _replace_block(text: str, start: str, end: str, body: str) -> str:
    i, j = text.find(start), text.find(end)
    if i < 0 or j < i:
        return text
    return text[:i + len(start)] + "\n" + body.rstrip("\n") + "\n" + text[j:]


def _area_note(title: str, brain: Path) -> str:
    blurb, mems = AREAS[title]
    lines = ["---", "tags: [jarvis-area]", "---", f"# {title}", f"Part of [[{NUCLEUS}]].", "", blurb, ""]
    if title == "Jarvis Memory":
        lines.append("Memory notes: " + " · ".join(f"[[{n}]]" for n in MEMORY_NOTES.values()))
        lines.append("")
    if title == "Claude Code":
        lines.append("Claude's memory: [[MEMORY]] (the index of every note in Claude Memory)")
        lines.append("")
    if mems:
        lines.append("What Claude knows about it: " + " · ".join(f"[[{m}]]" for m in mems))
        lines.append("")
    lines += [
        "## Activity",
        "Every day's log links here, so **Backlinks** lists each day this area did something. "
        "The lines themselves:",
        "",
        "```query",
        f'path:"{BRAIN}/Log" "[[{title}]]"',
        "```",
        "",
    ]
    return "\n".join(lines)


def _nucleus_note() -> str:
    areas = "\n".join(f"- [[{t}]] — {AREAS[t][0]}" for t in AREAS)
    mem = " · ".join(f"[[{n}]]" for n in MEMORY_NOTES.values())
    return "\n".join([
        "---",
        "tags: [jarvis-nucleus]",
        "---",
        f"# {NUCLEUS}",
        "The centre of the brain. Everything Jarvis, its crews and Claude Code do is written into "
        f"the *{BRAIN}* folder, and every note there links back here.",
        "",
        "## Areas",
        areas,
        "",
        "## Memory",
        f"- Jarvis (edit these and Jarvis goes by them): {mem}",
        "- Claude Code: [[MEMORY]] — the index of everything in *Claude Memory*",
        "",
        "## Days",
        _DAYS_START,
        _DAYS_END,
        "",
        "## Your notes",
        _NOTES_START,
        _NOTES_END,
        "",
    ])


def _recent_days(brain: Path, n: int = 14) -> str:
    log = brain / "Log"
    days = sorted((p.stem for p in log.glob("????-??-??.md")), reverse=True)[:n] if log.is_dir() else []
    return "\n".join(f"- [[{d}]]" for d in days) or "- (nothing yet)"


def _your_notes(brain: Path) -> str:
    """Links to the vault's own notes outside the brain, so the nucleus ties them in too."""
    vault = brain.parent
    out = []
    try:
        entries = sorted(vault.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return "- (could not read the vault)"
    for p in entries:
        if p.name.startswith(".") or p.name == BRAIN:
            continue
        if p.is_file() and p.suffix == ".md":
            out.append(f"- [[{p.stem}]]")
        elif p.is_dir():
            notes = sorted(q.stem for q in p.rglob("*.md") if not any(x.startswith(".") for x in q.parts))
            if not notes:
                continue
            same = [n for n in notes if n.lower() == p.name.lower()]
            shown = same or notes[:12]
            more = "" if same or len(notes) <= 12 else f" (+{len(notes) - 12} more)"
            out.append(f"- **{p.name}**: " + " · ".join(f"[[{n}]]" for n in shown) + more)
        if len(out) >= 40:
            break
    return "\n".join(out) or "- (none yet)"


def refresh_nucleus(brain: Path) -> None:
    """Rewrite the two generated blocks of Nucleus.md (days, your notes); the rest is yours to edit."""
    path = brain / f"{NUCLEUS}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    new = _replace_block(text, _DAYS_START, _DAYS_END, _recent_days(brain))
    new = _replace_block(new, _NOTES_START, _NOTES_END, _your_notes(brain))
    if new != text:
        _write_if_changed(path, new)


def ensure_skeleton(brain: Path) -> None:
    """Make the folders, the nucleus and the area hubs if missing. Never overwrites a note."""
    for sub in ("Areas", "Log", "Jarvis Memory", "Claude Memory"):
        (brain / sub).mkdir(parents=True, exist_ok=True)
    nucleus = brain / f"{NUCLEUS}.md"
    if not nucleus.exists():
        _write_if_changed(nucleus, _nucleus_note())
        refresh_nucleus(brain)
    for title in AREAS:
        p = brain / "Areas" / f"{_file_name(title)}.md"
        if not p.exists():
            _write_if_changed(p, _area_note(title, brain))


def _file_name(title: str) -> str:
    return title.replace("/", "-").replace(":", "-")


# ── the daily log ─────────────────────────────────────────────────────────────

def _day_title(day: date) -> str:
    return f"{day.strftime('%A')}, {day.strftime('%B')} {day.day}, {day.year}"


def _day_header(brain: Path, day: date) -> str:
    log = brain / "Log"
    earlier = sorted(p.stem for p in log.glob("????-??-??.md") if p.stem < day.isoformat())
    prev = f" · day before: [[{earlier[-1]}]]" if earlier else ""
    return ("---\ntags: [jarvis-log]\n---\n"
            f"# {_day_title(day)}\nPart of [[{NUCLEUS}]]{prev}. Everything Jarvis, its crews and Claude Code "
            "did today, oldest first.\n\n")


def format_line(area: str, text: str, when: datetime, links=(), limit: int = LINE_MAX) -> str:
    extra = [a for a in dict.fromkeys(links) if a and a != area]
    tail = (" → " + ", ".join(f"[[{a}]]" for a in extra)) if extra else ""
    return f"- {when.strftime('%H:%M')} · [[{area}]] · {clean(text, limit)}{tail}\n"


def append_line(brain: Path, area: str, text: str, when: datetime | None = None, links=(),
                limit: int = LINE_MAX) -> None:
    """Append one line to that day's log, under a lock shared by every process writing here."""
    when = when or datetime.now()
    day = when.date()
    path = brain / "Log" / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    new_day = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(_day_header(brain, day))
            f.write(format_line(area, text, when, links, limit))
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    if new_day:
        refresh_nucleus(brain)


def allow_icloud_downloads(thread_only: bool = True) -> bool:
    """Let this thread (or process) download iCloud notes that "Optimize Mac Storage" moved
    to the cloud. macOS refuses that to background processes such as the launchd worker:
    every open() of such a note fails with EDEADLK ("Resource deadlock avoided")."""
    if platform.system() != "Darwin":
        return True
    try:
        import ctypes
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        # IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_THREAD/PROCESS, IOPOL_MATERIALIZE_DATALESS_FILES_ON
        return libc.setiopolicy_np(3, 1 if thread_only else 0, 2) == 0
    except Exception:
        return False


class _Writer:
    """One background thread per process that does the vault writes in order."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue(maxsize=5000)
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def submit(self, fn) -> threading.Event:
        done = threading.Event()
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="vault-brain", daemon=True)
                self.thread.start()
        try:
            self.q.put_nowait((fn, done))
        except queue.Full:
            done.set()
        return done

    def _run(self):
        allow_icloud_downloads()
        while True:
            fn, done = self.q.get()
            try:
                fn()
            except Exception as e:
                print(f"[Brain vault] write failed: {e!r}")
            finally:
                done.set()

    def flush(self, timeout: float = 3.0) -> None:
        if self.thread is None or not self.thread.is_alive():
            return
        self.submit(lambda: None).wait(timeout)


_writer = _Writer()
atexit.register(lambda: _writer.flush(3.0))
_seen: dict[tuple[str, str], float] = {}
_seen_lock = threading.Lock()


def _repeat(area: str, text: str) -> bool:
    key = (area, re.sub(r"\d+", "#", text.lower())[:140])
    now = time.monotonic()
    with _seen_lock:
        last = _seen.get(key)
        if last is not None and now - last < REPEAT_QUIET_S:
            return True
        _seen[key] = now
        if len(_seen) > 4000:
            for k, t in list(_seen.items()):
                if now - t >= REPEAT_QUIET_S:
                    del _seen[k]
    return False


def log(area: str, text, *, links=(), dedupe: bool = False, when: datetime | None = None,
        wait: float = 0.0, limit: int = LINE_MAX) -> None:
    """Note that something happened. Never raises, never blocks (unless `wait` seconds are given)."""
    try:
        text = str(text or "").strip()
        if not text:
            return
        if area not in AREAS:
            area = "Worker" if area is None else str(area)
        if dedupe and _repeat(area, text):
            return
        when = when or datetime.now()
        links = tuple(links or ())

        def write():
            brain = brain_dir()
            if brain is not None:
                append_line(brain, area, text, when, links, limit)

        done = _writer.submit(write)
        if wait:
            done.wait(wait)
    except Exception as e:
        print(f"[Brain vault] log failed: {e!r}")


def flush(timeout: float = 3.0) -> None:
    try:
        _writer.flush(timeout)
    except Exception:
        pass


# ── mapping things to areas ───────────────────────────────────────────────────

def area_for_logger(name: str, message: str) -> str:
    m = re.match(r"\s*([a-z][a-z_ ]{1,20}):", message or "", re.I)
    if m and m.group(1).strip().lower() in _PREFIX_AREAS:
        return _PREFIX_AREAS[m.group(1).strip().lower()]
    best = ""
    for prefix in _LOGGER_AREAS:
        if (name == prefix or name.startswith(prefix + ".")) and len(prefix) > len(best):
            best = prefix
    return _LOGGER_AREAS.get(best, "Worker")


def area_for_tool(name: str) -> str:
    n = (name or "").lower()
    for frag, area in _TOOL_AREAS:
        if frag in n:
            return area
    return "Conversations"


def areas_for_paths(paths) -> list[str]:
    out: list[str] = []
    for p in paths or ():
        s = str(p).lower()
        for frag, area in _PATH_AREAS:
            if frag in s and area not in out:
                out.append(area)
                break
    return out


class VaultLogHandler(logging.Handler):
    """Puts the worker's log lines (INFO and up) into the day's log, filed under their area.
    The same line repeating (a check failing every few minutes) is written once per 3 hours."""

    def __init__(self, level=logging.INFO):
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and not msg:
                msg = repr(record.exc_info[1])
            area = area_for_logger(record.name, msg)
            if record.levelno >= logging.WARNING:
                msg = f"{record.levelname.lower()}: {msg}"
            log(area, msg, dedupe=True, when=datetime.fromtimestamp(record.created))
        except Exception:
            pass


# ── Jarvis's memory as notes (two-way) ────────────────────────────────────────

def _memory_note(category: str, items: dict) -> str:
    title = MEMORY_NOTES[category]
    lines = ["---", "tags: [jarvis-memory]", "---", f"# {title}", MEMORY_MARK,
             f"Part of [[{NUCLEUS}]] · [[Jarvis Memory]]. This note *is* what Jarvis remembers here: edit a "
             "line, add one as `- **key**: value`, or delete one, and Jarvis goes by it from then on.", ""]
    rows = []
    for key, entry in (items or {}).items():
        if isinstance(entry, dict):
            value, updated = entry.get("value", ""), entry.get("updated", "")
        else:
            value, updated = entry, ""
        value = " ".join(str(value or "").split())
        if not value:
            continue
        key = " ".join(str(key).replace("*", "").split())
        rows.append((str(updated or ""), key, value))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    for updated, key, value in rows:
        lines.append(f"- **{key}**: {value}" + (f" `{updated}`" if updated else ""))
    if not rows:
        lines.append("_(nothing yet)_")
    return "\n".join(lines) + "\n"


def parse_memory_note(text: str) -> dict | None:
    """{key: {'value', 'updated'}} from a memory note, or None if it is not one (no marker)."""
    if MEMORY_MARK not in (text or ""):
        return None
    out = {}
    for line in text.splitlines():
        m = _FACT.match(line)
        if not m:
            continue
        key, value = m.group("key").strip(), m.group("value").strip()
        if key and value:
            out[key] = {"value": value, "updated": m.group("date") or date.today().isoformat()}
    return out


def _stamp(path: Path):
    try:
        st = path.stat()
        return [st.st_mtime, st.st_size]
    except OSError:
        return None


def _load_sync(sync_path: Path) -> dict:
    try:
        d = json.loads(sync_path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_memory_notes(memory: dict, sync_path: Path) -> None:
    """Render each memory category as its note and remember the notes' stamps, so an edit made
    in Obsidian afterwards can be told apart from Jarvis's own write."""
    brain = brain_dir()
    if brain is None:
        return
    sync = _load_sync(sync_path)
    for cat, title in MEMORY_NOTES.items():
        path = brain / "Jarvis Memory" / f"{title}.md"
        _write_if_changed(path, _memory_note(cat, memory.get(cat) or {}))
        sync[cat] = _stamp(path)
    tmp = sync_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sync), encoding="utf-8")
    tmp.replace(sync_path)


def save_memory_notes(memory: dict, sync_path: Path) -> None:
    """write_memory_notes on the background writer, from a copy, so the caller never waits."""
    try:
        snapshot = json.loads(json.dumps(memory, ensure_ascii=False, default=str))
        _writer.submit(lambda: write_memory_notes(snapshot, sync_path))
    except Exception as e:
        print(f"[Brain vault] memory notes not written: {e!r}")


def adopt_memory_edits(memory: dict, sync_path: Path) -> list[str]:
    """Take in categories whose note was edited in Obsidian since Jarvis last wrote it.
    A missing note, or one without the marker, changes nothing. Returns the categories taken."""
    brain = brain_dir(create=False)
    if brain is None:
        return []
    allow_icloud_downloads()
    sync = _load_sync(sync_path)
    taken = []
    for cat, title in MEMORY_NOTES.items():
        path = brain / "Jarvis Memory" / f"{title}.md"
        stamp = _stamp(path)
        if stamp is None or stamp == sync.get(cat):
            continue
        try:
            parsed = parse_memory_note(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if parsed is None:
            continue
        old = memory.get(cat) if isinstance(memory.get(cat), dict) else {}
        for key, entry in parsed.items():          # unchanged values keep their old date
            prev = old.get(key)
            if isinstance(prev, dict) and str(prev.get("value", "")).strip() == entry["value"]:
                entry["updated"] = prev.get("updated") or entry["updated"]
        if parsed != old:
            memory[cat] = parsed
            taken.append(cat)
        sync[cat] = stamp
    if sync != _load_sync(sync_path):
        try:
            tmp = sync_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sync), encoding="utf-8")
            tmp.replace(sync_path)
        except OSError:
            pass
    return taken


def setup(memory: dict | None = None, sync_path: Path | None = None) -> Path | None:
    """Make the brain now and write the memory notes. Returns the brain folder."""
    brain = brain_dir()
    if brain is None:
        return None
    if memory is not None and sync_path is not None:
        write_memory_notes(memory, sync_path)
    refresh_nucleus(brain)
    return brain


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from memory import memory_manager as mm
    b = setup(mm.load_memory(), mm.VAULT_SYNC_PATH)
    print(b or "No Obsidian vault is connected (say \"Jarvis, connect my Obsidian vault\").")
