"""View counts of the GTA channel's own uploads, so the clip picker and the Shorts writer can lean toward what works.

Read from each video's public watch page (the project has no YouTube API key), at most twice a day, cached in
memory/yt_stats.json. Any failure just means no "what worked" line in the prompt: never blocks a video.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "memory" / "yt_stats.json"
SOURCES = {"clips": ROOT / "memory" / "clips.json", "guides": ROOT / "memory" / "guides.json"}
REFRESH_S = 12 * 3600
MAX_AGE_S = 21 * 86400                  # older uploads keep their last count (views have settled by then)
MIN_AGE_S = 36 * 3600                   # a video needs a day and a half before its views say anything


def video_id(link: str) -> str:
    m = re.search(r"(?:shorts/|youtu\.be/|v=)([\w-]{11})", link or "")
    return m.group(1) if m else ""


def _uploads() -> list[dict]:
    out = []
    for src, path in SOURCES.items():
        try:
            vids = json.loads(path.read_text()).get("videos") or []
        except Exception:
            continue
        for v in vids if isinstance(vids, list) else list(vids.values()):
            vid = video_id(str(v.get("youtube") or v.get("link") or ""))
            if vid:
                kind = "clip" if src == "clips" else str(v.get("kind") or "guide")
                out.append({"id": vid, "title": str(v.get("title", "")), "kind": kind, "made": float(v.get("made") or 0)})
    return out


def _views(vid: str) -> int | None:
    req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"})
    try:
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf8", "ignore")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:                             # rate-limited: stop this round, keep what we have
            raise
        return None
    except Exception:
        return None
    m = re.search(r'"viewCount":"(\d+)"', html)
    return int(m.group(1)) if m else None


def refresh(force: bool = False, fetch=_views) -> dict:
    """{video id: {title, kind, made, views}}; refetched when the cache is older than REFRESH_S."""
    try:
        cache = json.loads(CACHE.read_text())
    except Exception:
        cache = {"at": 0, "videos": {}}
    if not force and time.time() - cache.get("at", 0) < REFRESH_S:
        return cache["videos"]
    ups = [u for u in _uploads() if time.time() - u["made"] < MAX_AGE_S]
    counts = []
    for u in ups:                                       # one at a time, gently: YouTube answers bursts with 429
        try:
            counts.append(fetch(u["id"]))
        except urllib.error.HTTPError:
            break
        time.sleep(0.7)
    ups = ups[:len(counts)]
    vids = dict(cache.get("videos") or {})
    for u, n in zip(ups, counts):
        if n is not None:
            vids[u["id"]] = dict(u, views=n)
    if not any(n is not None for n in counts):          # offline (e.g. just woke): keep the old numbers, retry next time
        return vids
    cache = {"at": time.time(), "videos": vids}
    try:
        CACHE.write_text(json.dumps(cache, indent=1))
    except OSError:
        pass
    return vids


def winners(kinds: tuple[str, ...], n: int = 5) -> tuple[list[dict], list[dict]]:
    """(the n most-watched, the n least-watched) of these kinds that are old enough to judge."""
    try:
        vids = [v for v in refresh().values() if v.get("kind") in kinds and time.time() - v.get("made", 0) > MIN_AGE_S]
    except Exception:
        return [], []
    vids.sort(key=lambda v: -v.get("views", 0))
    if len(vids) < 2 * n:
        return [], []
    return vids[:n], vids[-n:]


def prompt_line(kinds: tuple[str, ...], what: str) -> str:
    """A few lines for a prompt: what did best and worst on the channel. Empty when there isn't enough to go on."""
    best, worst = winners(kinds)
    if not best or best[0].get("views", 0) < 100:
        return ""
    fmt = lambda vs: "; ".join(f'"{v["title"]}" ({v["views"]:,} views)' for v in vs)
    return (f"WHAT WORKS ON OUR CHANNEL. Our most-watched {what}: {fmt(best)}. Our least-watched: {fmt(worst)}. "
            f"Lean toward what the top ones have in common (the kind of moment or fact, and how the title is worded); "
            f"never copy a title.")


if __name__ == "__main__":
    vs = sorted(refresh(force=True).values(), key=lambda v: -v.get("views", 0))
    print(f"{len(vs)} videos, {sum(v['views'] for v in vs):,} views")
    for v in vs[:15]:
        print(f"{v['views']:>7,}  {v['kind']:<6} {v['title'][:70]}")
