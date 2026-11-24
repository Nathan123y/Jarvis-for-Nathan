"""The weekly social scorecard: every Monday morning, one text with last week's views on each platform, the best and
worst videos, and what to change. Also `python3 -m worker.social_scorecard` to print it any time.

YouTube numbers come from worker/yt_stats.py (public watch pages), TikTok from the stories' own view reader
(memory/viral.json "tiktok"). Instagram views aren't read yet, so it reports how many Reels went up.
"""
from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE = ROOT / "memory" / "social_scorecard.json"
WEEK = 7 * 86400
SEND_DAY, SEND_HOUR = 0, 9          # Monday 09:00


def _short(title: str, n: int = 48) -> str:
    title = " ".join(str(title or "").split())
    return title if len(title) <= n else title[:n - 1] + "…"


def youtube_week(stats: dict, now: float) -> dict:
    this = [v for v in stats.values() if now - v.get("made", 0) <= WEEK]
    last = [v for v in stats.values() if WEEK < now - v.get("made", 0) <= 2 * WEEK]
    kinds = {}
    for v in this:
        kinds.setdefault(v.get("kind", "?"), []).append(v.get("views", 0))
    return {"n": len(this), "views": sum(v.get("views", 0) for v in this), "last_views": sum(v.get("views", 0) for v in last),
            "median": statistics.median([v.get("views", 0) for v in this]) if this else 0,
            "best": sorted(this, key=lambda v: -v.get("views", 0))[:3],
            "worst": sorted(this, key=lambda v: v.get("views", 0))[:2],
            "kinds": {k: (len(vs), sum(vs) / len(vs)) for k, vs in kinds.items()}}


def tiktok_week(viral: dict, now: float) -> dict:
    seen = [r for r in (viral.get("tiktok") or {}).values() if now - r.get("first", 0) <= WEEK]
    views = [r.get("views", 0) for r in seen]
    return {"n": len(seen), "views": sum(views), "median": statistics.median(views) if views else 0,
            "best": sorted(seen, key=lambda r: -r.get("views", 0))[:2]}


def instagram_week(viral: dict, now: float) -> dict:
    vids = viral.get("videos") or []
    made = [v for v in vids if now - float(v.get("made") or 0) <= WEEK]
    return {"made": len(made), "posted": sum(1 for v in made if v.get("ig_posted"))}


def advice(yt: dict, tt: dict, ig: dict, cfg: dict) -> list[str]:
    out = []
    kinds = yt["kinds"]
    if len(kinds) >= 2:
        best = max(kinds, key=lambda k: kinds[k][1])
        out.append(f"YouTube: {best}s average the most views ({kinds[best][1]:,.0f} each), so lean that way.")
    if yt["n"] >= 10 and yt["median"] < 10:
        out.append("YouTube: most uploads get under 10 views; fewer, stronger Shorts beat more of them.")
    if not cfg.get("tiktok", True) or not tt["n"]:
        out.append("TikTok: paused. A fresh account needs a few days of use by hand, then 1-2 posts a day.")
    elif tt["median"] < 10:
        out.append(f"TikTok: the typical post got {tt['median']:.0f} views; the account is being held back.")
    if ig["made"] and ig["posted"] < ig["made"]:
        out.append(f"Instagram: only {ig['posted']} of {ig['made']} stories went up; check the login.")
    return out


def build(now: Optional[float] = None, yt_stats: Optional[dict] = None, viral: Optional[dict] = None,
          cfg: Optional[dict] = None) -> str:
    now = now or time.time()
    if yt_stats is None:
        try:
            from worker import yt_stats as ys
            yt_stats = ys.refresh()
        except Exception:
            yt_stats = {}
    if viral is None or cfg is None:
        from worker import viral as wv
        viral = wv.load_state() if viral is None else viral
        cfg = wv.settings() if cfg is None else cfg
    yt, tt, ig = youtube_week(yt_stats, now), tiktok_week(viral, now), instagram_week(viral, now)
    trend = ""
    if yt["last_views"]:
        pct = (yt["views"] - yt["last_views"]) / yt["last_views"] * 100
        trend = f" ({'+' if pct >= 0 else ''}{pct:.0f}% vs the week before)"
    lines = ["📊 Social scorecard, last 7 days",
             f"YouTube: {yt['views']:,} views on {yt['n']} uploads{trend}"]
    lines += [f"  🔝 {v.get('views', 0):,} {_short(v.get('title'))}" for v in yt["best"]]
    lines += [f"  🔻 {v.get('views', 0):,} {_short(v.get('title'))}" for v in yt["worst"] if yt["n"] > 3]
    lines.append(f"TikTok: {tt['views']:,} views on {tt['n']} posts" if tt["n"] else "TikTok: no posts")
    lines += [f"  🔝 {r.get('views', 0):,} {_short(r.get('desc'))}" for r in tt["best"] if r.get("views")]
    lines.append(f"Instagram: {ig['posted']} of {ig['made']} stories posted (views aren't read yet)")
    tips = advice(yt, tt, ig, cfg)
    if tips:
        lines.append("What to change:")
        lines += [f"• {t}" for t in tips]
    return "\n".join(lines)


def due(now: datetime, last_week: str) -> bool:
    week = now.strftime("%G-W%V")
    return now.weekday() == SEND_DAY and now.hour >= SEND_HOUR and week != last_week


class ScorecardService:
    PASS_SECONDS = 900

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="social-scorecard").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.PASS_SECONDS):
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"scorecard: pass failed: {exc!r}")

    def run_once(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.now()
        try:
            st = json.loads(STATE.read_text())
        except Exception:
            st = {}
        if not due(now, st.get("week", "")):
            return ""
        text = build()
        from core import phone_alerts
        phone_alerts.send_text(text)
        STATE.write_text(json.dumps({"week": now.strftime("%G-W%V"), "at": time.time(), "text": text}, indent=1))
        self.log("scorecard: sent the weekly social scorecard")
        return text


if __name__ == "__main__":
    print(build())
