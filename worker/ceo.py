"""The CEO morning page: one text that covers every business, from data the crews already keep.

Read-only and model-free (costs nothing to run). Each section is built independently, so a
missing file or a half-written crew only drops that line, never the whole page.

  Shop        listings made in the last day, sales (7 days / total, by channel), what is waiting
  Videos      posted yesterday, TikTok views on recent ones, anything held back
  Internships applications by status, replies, ones that need applying by hand
  Money       day-trading and memecoin paper accounts (simulated)
  Needs you   open decisions, paused/failed jobs, connections that need a sign-in
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent.parent
MAX_TEXT = 1100


def _read(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _short(value, limit=60) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def shop(now: float, base: Path = BASE) -> Optional[str]:
    s = _read(base / "memory" / "etsy_shop.json", {}) or {}
    if not s:
        return None
    day_ago = now - 86400
    made = sum(1 for d in s.get("designs") or [] if float(d.get("made") or 0) >= day_ago)
    sales = s.get("sales_log") or []
    week = [x for x in sales if float(x.get("at") or 0) >= now - 7 * 86400]
    rev = sum(float(x.get("price") or 0) for x in week)
    line = f"Shop: {made} new designs in 24h, {len(s.get('designs') or [])} total. "
    if sales:
        by = Counter(x.get("channel") for x in week)
        line += f"Sales 7d: {len(week)} (${rev:,.0f})"
        if by:
            line += " " + ", ".join(f"{c} {n}" for c, n in by.most_common(3))
        line += f"; {len(sales)} all-time"
        if not week:
            days = (now - float(sales[-1].get("at") or now)) / 86400
            line += f", none for {days:.0f} days"
    else:
        line += "No sales yet"
    return line + "."


def videos(now: float, base: Path = BASE) -> Optional[str]:
    v = _read(base / "memory" / "viral.json", {}) or {}
    if not v:
        return None
    yday = (date.fromtimestamp(now) - timedelta(days=1)).isoformat()
    slots = (v.get("slots") or {}).get(yday)
    stats = [x for x in (v.get("tiktok") or {}).values() if isinstance(x, dict) and x.get("views") is not None]
    stats.sort(key=lambda x: x.get("first") or 0)
    parts = []
    if slots is not None:
        parts.append(f"{len(slots)} slots posted yesterday")
    if stats:
        recent = stats[-6:]
        parts.append(f"TikTok views last {len(recent)}: {[int(x.get('views') or 0) for x in recent]}")
    held = v.get("held") or []
    if held:
        parts.append(f"{len(held)} held back")
    return ("Videos: " + "; ".join(parts) + ".") if parts else None


def internships(base: Path = BASE) -> Optional[str]:
    d = _read(base / "memory" / "internships.json", {}) or {}
    jobs = list((d.get("jobs") or {}).values())
    if not jobs:
        return None
    c = Counter(j.get("status") for j in jobs)
    keep = ("applied", "replied", "interview", "manual", "failed", "new")
    shown = ", ".join(f"{k} {c[k]}" for k in keep if c.get(k))
    return f"Internships: {shown or dict(c)}."


def money(base: Path = BASE) -> Optional[str]:
    out = []
    st = _read(base / "config" / "trading_day" / "state.json", {}) or {}
    seen = st.get("last_seen") or {}
    if seen.get("equity") and st.get("start_equity"):
        pct = (float(seen["equity"]) / float(st["start_equity"]) - 1) * 100
        out.append(f"day-trading ${float(seen['equity']):,.0f} ({pct:+.1f}%)")
    try:
        from worker import sources
        m = sources.meme_status()
    except Exception:
        m = None
    if m and m.get("equity"):
        txt = f"memecoin ${float(m['equity']):,.0f}"
        if m.get("first_equity"):
            txt += f" ({float(m['equity']) - float(m['first_equity']):+,.0f})"
        out.append(txt)
    return ("Paper money: " + ", ".join(out) + " (simulated).") if out else None


def needs_you(now: float) -> list[str]:
    try:
        from core.events import Store
        items = Store().open_items(("job_failed", "job_paused", "connection_missing",
                                    "decision_needed", "campaign_stopped"), max_age_days=3, now=now)
    except Exception:
        return []
    out, seen = [], set()
    for e in items:
        detail = e.get("detail") or {}
        what = detail.get("text") or detail.get("error") or detail.get("why") or e.get("status") or ""
        line = f"{e.get('source')}: {e.get('kind')} {_short(what, 70)}".strip()
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out[:4]


def page(now: Optional[float] = None, base: Path = BASE, attention: Optional[list] = None) -> str:
    now = time.time() if now is None else now
    when = datetime.fromtimestamp(now).strftime("%a %b %-d")
    lines = [f"📊 CEO page, {when}"]
    for fn in (lambda: shop(now, base), lambda: videos(now, base), lambda: internships(base), lambda: money(base)):
        try:
            line = fn()
        except Exception:
            line = None
        if line:
            lines.append(line)
    ask = needs_you(now) if attention is None else attention
    if ask:
        lines.append("Needs you: " + " | ".join(ask))
    else:
        lines.append("Needs you: nothing.")
    return "\n".join(lines)[:MAX_TEXT]
