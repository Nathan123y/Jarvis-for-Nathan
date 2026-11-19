#!/usr/bin/env python3
"""Per-day posting counts per platform from Jarvis's video logs. Read-only."""
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
GOAL = 3

# (log file, label, {platform: predicate on a record})
SOURCES = [
    ("viral.json", "stories", {"TikTok": lambda v: v.get("posted"), "Instagram": lambda v: v.get("ig_posted")}),
    ("clips.json", "clips", {"YouTube": lambda v: v.get("youtube")}),
    ("guides.json", "guides", {"YouTube": lambda v: v.get("link") or v.get("posted")}),
]


def load(name):
    try:
        return json.loads((ROOT / "memory" / name).read_text()).get("videos", [])
    except (OSError, ValueError) as e:
        print(f"! cannot read {name}: {e}")
        return []


cut = datetime.now().date() - timedelta(days=DAYS - 1)
made = defaultdict(int)                      # (day, platform) -> videos made
posted = defaultdict(int)                    # (day, platform) -> videos posted
stuck = []
for fname, label, plats in SOURCES:
    for v in load(fname):
        t = datetime.fromtimestamp(v.get("made", 0))
        if t.date() < cut:
            continue
        miss = []
        for p, ok in plats.items():
            made[(t.date(), p)] += 1
            if ok(v):
                posted[(t.date(), p)] += 1
            else:
                miss.append(p)
        if miss:
            stuck.append((t, label, v.get("title", "?"), miss))

print(f"Posting audit, last {DAYS} day(s). Goal: >= {GOAL}/day on each platform.\n")
for i in range(DAYS):
    d = datetime.now().date() - timedelta(days=i)
    tag = " (today, partial)" if i == 0 else ""
    print(d.strftime("%a %b %d") + tag)
    for p in ("TikTok", "Instagram", "YouTube"):
        n = posted[(d, p)]
        print(f"  {p:<10} {n} posted / {made[(d, p)]} made   {'OK' if n >= GOAL else 'BELOW GOAL'}")
print("\nMade but not posted everywhere:")
for t, label, title, miss in sorted(stuck):
    print(f"  {t:%m-%d %H:%M} [{label}] {title[:70]}  -> missing: {', '.join(miss)}")
if not stuck:
    print("  none")
