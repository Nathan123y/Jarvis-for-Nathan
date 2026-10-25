"""What the internet is joking about today, for the shop's Trend Scout: Know Your Meme's newest confirmed memes (it documents
what goes viral on TikTok, X and Instagram, which have no free feeds), the day's top posts in Reddit's meme and funny-phrase
subreddits, Bluesky's trending topics, and (read only, once connected) Instagram's big meme pages and hashtags (ig_scout.py).
One request per source, once a day; a source that fails is just left out.
"""
from __future__ import annotations

import html
import json
import re
from typing import Callable, Optional

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Jarvis/1.0"}
SOURCES = {
    "Know Your Meme (new memes)": "https://knowyourmeme.com/memes.rss",
    # one combined request: Reddit refuses a second one straight after
    "Reddit today (meme and funny-phrase subreddits, r/OutOfTheLoop)":
        "https://www.reddit.com/r/OutOfTheLoop+memes+dankmemes+me_irl+teenagers+GenZ+BrandNewSentence+clevercomebacks+rareinsults"
        "+TikTokCringe/top/.rss?t=day&limit=60",
    "Bluesky trending topics": "https://api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics?limit=25",
}
PER_SOURCE = 40


def titles(xml: str) -> list:
    """Item titles from an RSS or Atom feed (the feed's own title is skipped)."""
    out = []
    for raw in re.findall(r"<title[^>]*>(.*?)</title>", xml, re.S)[1:]:
        t = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", raw)).strip()
        t = re.sub(r"^What'?s (going on|up) with\s*", "", t, flags=re.I)
        if len(t) > 3 and t.lower() not in ("me_irl", "meirl") and t not in out:
            out.append(t[:120])
    return out


def bluesky_topics(text: str) -> list:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return [str(t.get("topic") or t.get("displayName"))[:120] for t in (data.get("topics") or []) + (data.get("suggested") or [])
            if t.get("topic") or t.get("displayName")]


def fetch(get: Optional[Callable] = None) -> dict:
    """{source name: [titles]}."""
    real = get is None
    if get is None:
        import requests
        get = lambda url: requests.get(url, headers=UA, timeout=20)
    found = {}
    for name, url in SOURCES.items():
        try:
            r = get(url)
            if r.status_code == 200:
                items = (bluesky_topics(r.text) if url.startswith("https://api.bsky.app") else titles(r.text))[:PER_SOURCE]
                if items:
                    found[name] = items
        except Exception:
            pass
    if real:                                               # Instagram (read only), once it's connected
        try:
            from worker import ig_scout
            lines = ig_scout.fetch() if ig_scout.connected() else []
            if lines:
                found["Instagram (the most viral posts on big meme pages and hashtags, read from the pictures)"] = lines
        except Exception:
            pass
    return found


def brief(found: dict) -> str:
    return "\n".join(f"{name}:\n" + "\n".join(f"- {t}" for t in items) for name, items in found.items())
