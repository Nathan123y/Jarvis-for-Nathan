"""The brain's sleep: each night, turn yesterday's raw log into understanding.

At 03:30 (or the first chance after, catching up to 3 missed days) the worker
reads one finished day from the index (core/brain_index.py, local: no iCloud
download) and asks the SMART ladder (Claude on the membership first, Gemini
behind it) — ONE call per day, plus one per week on Mondays — for:

    summary        3-5 sentences, put at the top of that day's note
    by area        one line per area that did something
    highlights     the few things that mattered most (money, wins, breakages, decisions)
    lessons        cause → effect patterns seen in the lines   → Lessons.md
    open threads   unfinished work, carried forward each night until resolved
    concepts       named things that matter beyond the day     → Concepts/<Name>.md

Concept notes are where the brain links up the way a person's memory does: a
design, a coin, a streamer or a bug gets its own note, and every day it comes up
adds a dated line, so its whole history is one click away and the graph shows
which days and areas it ties together.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from core import brain_index as bi
from core import vault_brain as vb

RUN_AT = (3, 30)
CATCH_UP_DAYS = 3
MAX_PROMPT_CHARS = 90_000
MAX_CONCEPTS = 10

SYSTEM = (
    "You are the memory consolidation of Jarvis, a personal assistant system that runs its owner's "
    "businesses and life: a print-on-demand shop, jerseys, viral videos, GTA clips and guides, internship "
    "applications, paper-trading bots, a website pitcher, phone alerts, and conversations with Jarvis and "
    "with Claude Code. You turn one day's raw event log into lasting memory. Facts only: every statement "
    "must rest on the lines given. Reply with JSON only."
)

DAY_PROMPT = """DAY: {day}

OPEN THREADS CARRIED IN FROM BEFORE (keep the ones still unresolved at the end of this day, drop the
resolved ones, add new ones):
{threads}

EXISTING CONCEPT NOTES (reuse these exact names when the same thing comes up):
{concepts}

THE DAY'S EVENTS, oldest first ("HH:MM [Area] text"; "×N" = the same line N times):
{lines}

Return this JSON:
{{
  "summary": "3-5 plain sentences. Lead with what matters most: money (sales, trades, P&L), things that broke or got fixed, decisions made in conversations or Claude Code sessions, then routine output.",
  "areas": {{"<Area>": "one line on what it did", "...": "..."}},
  "highlights": ["at most 6, the most important things of the day"],
  "lessons": [{{"area": "<Area>", "lesson": "a cause -> effect pattern visible in the lines (e.g. a failure that repeats at a time, a fix that worked, a format that got views). No generic advice. At most 4, often none."}}],
  "open_threads": ["unfinished work, unresolved problems, follow-ups promised. At most 6."],
  "concepts": [{{"name": "specific named thing", "kind": "product|design|person|company|coin|stock|streamer|video|bug|project|place|idea", "area": "<Area>", "note": "what happened with it today, one sentence"}}]
}}
Concepts: at most {max_concepts}; only specific named things that matter beyond today (a design title, a coin,
a streamer, a person, a company, a recurring bug) — never an area name and never a generic word."""

WEEK_PROMPT = """WEEK: {start} to {end}

DAILY SUMMARIES:
{digests}

LESSONS LEARNED THIS WEEK:
{lessons}

EVENT COUNTS BY AREA:
{counts}

Return this JSON:
{{
  "summary": "4-6 plain sentences on the week: money first, then what moved forward, what kept breaking.",
  "areas": {{"<Area>": "one line on its week"}},
  "wins": ["at most 5"],
  "problems": ["at most 5, the ones that cost the most"],
  "next": ["at most 5 concrete things worth doing next week, each tied to something in the week"]
}}"""


def _default_think(prompt: str) -> Optional[dict]:
    from core import gemini
    return gemini.as_json(prompt, tier=gemini.SMART,
                          config={"system_instruction": SYSTEM, "response_mime_type": "application/json"},
                          timeout_ms=300_000, default=None)


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower())[:160]


def compress(items: list[dict], max_chars: int = MAX_PROMPT_CHARS) -> str:
    """The day's lines for the prompt: repeats folded into one line with a count, capped by size
    (when too big, the weightiest lines are kept, still in time order)."""
    counts = Counter((i["area"], _norm(i["text"])) for i in items)
    seen, rows = set(), []
    for i in items:
        k = (i["area"], _norm(i["text"]))
        if k in seen:
            continue
        seen.add(k)
        n = counts[k]
        hm = datetime.fromtimestamp(i["ts"]).strftime("%H:%M")
        rows.append((i["weight"], i["ts"], f"{hm} [{i['area']}] {i['text'][:500]}" + (f" ×{n}" if n > 1 else "")))
    total = sum(len(r[2]) + 1 for r in rows)
    if total > max_chars:
        keep, used = [], 0
        for r in sorted(rows, key=lambda r: (-r[0], r[1])):
            if used + len(r[2]) + 1 > max_chars:
                continue
            keep.append(r)
            used += len(r[2]) + 1
        rows = keep
    return "\n".join(r[2] for r in sorted(rows, key=lambda r: r[1]))


def safe_name(name: str) -> str:
    n = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", str(name or ""))
    n = " ".join(n.split()).strip(" .")
    if len(n) > 60:
        n = n[:60].rsplit(" ", 1)[0] if " " in n[:60] else n[:60]
    return n.strip(" .,-")


def _area(a) -> str:
    a = str(a or "").strip()
    if a in vb.AREAS:
        return a
    from core.brain_recall import area_from
    return area_from(a) or "Worker"


def _existing_concepts(brain: Path) -> dict[str, str]:
    d = brain / "Concepts"
    return {p.stem.lower(): p.stem for p in d.glob("*.md")} if d.is_dir() else {}


def _summary_block(day: str, r: dict, concept_titles: list[str]) -> str:
    out = [vb.SUMMARY_START, "## Summary", vb.clean(r.get("summary"), 1600), ""]
    hl = [h for h in (r.get("highlights") or []) if str(h).strip()][:6]
    if hl:
        out += ["**Highlights**"] + [f"- {vb.clean(h, 300)}" for h in hl] + [""]
    areas = r.get("areas") if isinstance(r.get("areas"), dict) else {}
    if areas:
        out += ["**By area**"] + [f"- [[{_area(a)}]]: {vb.clean(t, 300)}" for a, t in areas.items() if str(t).strip()] + [""]
    lessons = [l for l in (r.get("lessons") or []) if isinstance(l, dict) and l.get("lesson")][:4]
    if lessons:
        out += ["**Lessons**"] + [f"- [[{_area(l.get('area'))}]]: {vb.clean(l['lesson'], 300)}" for l in lessons] + [""]
    threads = [t for t in (r.get("open_threads") or []) if str(t).strip()][:6]
    if threads:
        out += ["**Still open**"] + [f"- {vb.clean(t, 300)}" for t in threads] + [""]
    if concept_titles:
        out += ["**Concepts**: " + " · ".join(f"[[{c}]]" for c in concept_titles), ""]
    out += ["## Everything, in order", vb.SUMMARY_END, ""]
    return "\n".join(out)


def write_summary(brain: Path, day: str, block: str) -> None:
    """Put the summary at the top of the day's note (replacing an earlier one), under the log lock."""
    path = brain / "Log" / f"{day}.md"
    if not path.exists():
        return
    with open(path, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            text = f.read()
            i, j = text.find(vb.SUMMARY_START), text.find(vb.SUMMARY_END)
            if i >= 0 and j > i:
                new = text[:i] + block.rstrip("\n") + text[j + len(vb.SUMMARY_END):]
            else:
                m = re.search(r"^- \d{2}:\d{2} · ", text, re.M)
                at = m.start() if m else len(text)
                new = text[:at] + block + "\n" + text[at:]
            if new != text:
                f.seek(0)
                f.write(new)
                f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _append(path: Path, header: str, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(header)
            f.write(line)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write_concepts(brain: Path, day: str, concepts: list) -> list[str]:
    existing = _existing_concepts(brain)
    titles = []
    noon = datetime.fromisoformat(day + "T12:00")
    for c in (concepts or [])[:MAX_CONCEPTS]:
        if not isinstance(c, dict):
            continue
        name = safe_name(c.get("name"))
        if len(name) < 2 or name in vb.AREAS or name.lower() in ("nucleus", "lessons"):
            continue
        title = existing.get(name.lower(), name)
        existing[title.lower()] = title
        area = _area(c.get("area"))
        kind = re.sub(r"[^a-z]", "", str(c.get("kind") or "idea").lower())[:20] or "idea"
        note = vb.clean(c.get("note"), 400)
        header = (f"---\ntags: [jarvis-concept, {kind}]\n---\n# {title}\n"
                  f"A {kind} the brain keeps track of. Part of [[{vb.NUCLEUS}]] · [[{area}]].\n\n## Timeline\n")
        _append(brain / "Concepts" / f"{title}.md", header, f"- [[{day}]] · [[{area}]] · {note}\n")
        bi.put("concept", f"{title} ({kind}): {note}", area=area, when=noon, key=f"concept:{title}:{day}")
        if title not in titles:
            titles.append(title)
    return titles


def write_lessons(brain: Path, day: str, lessons: list) -> int:
    header = (f"---\ntags: [jarvis-lessons]\n---\n# Lessons\nWhat the brain has learned, one line per lesson, "
              f"newest last. Part of [[{vb.NUCLEUS}]].\n\n")
    n = 0
    when = datetime.fromisoformat(day + "T23:00")
    for i, l in enumerate((lessons or [])[:4]):
        if not isinstance(l, dict) or not str(l.get("lesson") or "").strip():
            continue
        area = _area(l.get("area"))
        text = vb.clean(l["lesson"], 400)
        _append(brain / "Lessons.md", header, f"- [[{day}]] · [[{area}]] · {text}\n")
        bi.put("lesson", text, area=area, when=when, key=f"lesson:{day}:{i}")
        n += 1
    return n


def consolidate_day(day: str, think: Callable[[str], Optional[dict]] = _default_think,
                    brain: Path | None = None) -> dict | None:
    """Consolidate one finished day. None when there was nothing to do or the model did not answer."""
    brain = brain or vb.brain_dir()
    if brain is None:
        return None
    items = bi.day_items(day)
    if not items:
        return {"day": day, "events": 0}
    prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    threads = [t["text"] for t in bi.search("", kinds=("thread",), since=prev, until=prev, limit=8)]
    concepts = sorted(_existing_concepts(brain).values())[:200]
    prompt = DAY_PROMPT.format(day=day, threads="\n".join(f"- {t}" for t in threads) or "(none)",
                               concepts=", ".join(concepts) or "(none yet)", lines=compress(items),
                               max_concepts=MAX_CONCEPTS)
    r = think(prompt)
    if not isinstance(r, dict) or not str(r.get("summary") or "").strip():
        return None
    titles = write_concepts(brain, day, r.get("concepts"))
    n_lessons = write_lessons(brain, day, r.get("lessons"))
    write_summary(brain, day, _summary_block(day, r, titles))
    end = datetime.fromisoformat(day + "T23:59")
    hl = [vb.clean(h, 300) for h in (r.get("highlights") or [])[:6] if str(h).strip()]
    bi.put("digest", vb.clean(r["summary"], 1600) + (" Highlights: " + "; ".join(hl) if hl else ""),
           when=end, key=f"digest:{day}")
    bi.drop("kind = 'thread' AND day = ?", (day,))
    for i, t in enumerate([t for t in (r.get("open_threads") or []) if str(t).strip()][:6]):
        bi.put("thread", vb.clean(t, 300), when=end, key=f"thread:{day}:{i}")
    return {"day": day, "events": len(items), "concepts": len(titles), "lessons": n_lessons}


def consolidate_week(start: str, think: Callable[[str], Optional[dict]] = _default_think,
                     brain: Path | None = None) -> dict | None:
    brain = brain or vb.brain_dir()
    if brain is None:
        return None
    s = date.fromisoformat(start)
    e = s + timedelta(days=6)
    digests = sorted(bi.search("", kinds=("digest",), since=s.isoformat(), until=e.isoformat(), limit=7),
                     key=lambda d: d["day"])
    if len(digests) < 2:
        return {"week": start, "days": len(digests)}
    lessons = bi.search("", kinds=("lesson",), since=s.isoformat(), until=e.isoformat(), limit=20)
    counts = Counter()
    for d in range(7):
        counts.update(bi.area_counts((s + timedelta(days=d)).isoformat()))
    prompt = WEEK_PROMPT.format(
        start=s.isoformat(), end=e.isoformat(),
        digests="\n".join(f"{d['day']}: {d['text']}" for d in digests),
        lessons="\n".join(f"- ({l['area']}) {l['text']}" for l in lessons) or "(none)",
        counts=", ".join(f"{a} {n}" for a, n in counts.most_common()))
    r = think(prompt)
    if not isinstance(r, dict) or not str(r.get("summary") or "").strip():
        return None
    iso = s.isocalendar()
    title = f"Week {iso[0]}-W{iso[1]:02d}"
    out = ["---", "tags: [jarvis-week]", "---", f"# {title}",
           f"{s.strftime('%b %d')} – {e.strftime('%b %d, %Y')}. Part of [[{vb.NUCLEUS}]]. Days: "
           + " · ".join(f"[[{d['day']}]]" for d in digests), "", "## Summary", vb.clean(r["summary"], 2000), ""]
    for key, label in (("wins", "Wins"), ("problems", "Problems"), ("next", "Next week")):
        vals = [v for v in (r.get(key) or []) if str(v).strip()][:5]
        if vals:
            out += [f"## {label}"] + [f"- {vb.clean(v, 300)}" for v in vals] + [""]
    areas = r.get("areas") if isinstance(r.get("areas"), dict) else {}
    if areas:
        out += ["## By area"] + [f"- [[{_area(a)}]]: {vb.clean(t, 300)}" for a, t in areas.items()] + [""]
    path = brain / "Digests" / f"{title}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    bi.put("week", vb.clean(r["summary"], 1600), when=datetime.combine(e, datetime.min.time()),
           key=f"week:{title}")
    return {"week": title, "days": len(digests)}


class SleepService:
    """Runs the nightly consolidation from the worker (worker/__main__.py)."""

    def __init__(self, state_path: Path, log: Callable[[str], None] = print,
                 think: Callable[[str], Optional[dict]] = _default_think):
        self.state_path = state_path
        self.log = log
        self.think = think
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _state(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save(self, st: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        tmp.replace(self.state_path)

    def due_days(self, now: datetime) -> list[str]:
        if (now.hour, now.minute) < RUN_AT:
            return []
        done = set(self._state().get("days", []))
        return [d for d in ((now.date() - timedelta(days=k)).isoformat() for k in range(CATCH_UP_DAYS, 0, -1))
                if d not in done]

    def run_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        brain = vb.brain_dir()
        if brain is None:
            return
        did = False
        for day in self.due_days(now):
            res = consolidate_day(day, self.think, brain)
            if res is None:
                self.log(f"brain: could not consolidate {day} (no model answered); trying again later")
                return
            st = self._state()
            st["days"] = sorted(set(st.get("days", [])) | {day})[-60:]
            self._save(st)
            if res.get("events"):
                did = True
                self.log(f"brain: slept on {day} — {res['events']} events → a summary, "
                         f"{res.get('concepts', 0)} concepts, {res.get('lessons', 0)} lessons")
        week_start = (now.date() - timedelta(days=now.weekday() + 7)).isoformat()
        st = self._state()
        if (now.hour, now.minute) >= RUN_AT and week_start not in st.get("weeks", []):
            res = consolidate_week(week_start, self.think, brain)
            if res is not None:
                st = self._state()
                st["weeks"] = sorted(set(st.get("weeks", [])) | {week_start})[-20:]
                self._save(st)
                if res.get("days", 0) >= 2:
                    did = True
                    self.log(f"brain: wrote {res['week']}")
        if did or not st.get("indexed"):
            stats = bi.refresh(brain)
            st = self._state()
            st["indexed"] = datetime.now().isoformat(timespec="minutes")
            self._save(st)
            if any(stats.values()):
                self.log(f"brain: re-indexed notes {stats}")
            vb.refresh_nucleus(brain)

    def start(self) -> None:
        def run():
            vb.allow_icloud_downloads()
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as e:
                    self.log(f"brain: sleep failed: {e!r}")
                self._stop.wait(600)
        self._thread = threading.Thread(target=run, name="brain-sleep", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
