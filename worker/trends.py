"""What the internet is joking about today, for the shop's designs: Know Your Meme's newest confirmed memes,
r/OutOfTheLoop's "what's up with ..." questions and the day's top meme posts on Reddit. Fetched once a
day (one request per source) and cached in the shop's state; a source that fails is just left out.
"""
from __future__ import annotations

import html
import re
from typing import Callable, Optional

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Jarvis/1.0"}
SOURCES = {
    "Know Your Meme (new memes)": "https://knowyourmeme.com/memes.rss",
    # one combined request: Reddit refuses a second one straight after
    "Reddit today (r/OutOfTheLoop questions and top meme posts)":
        "https://www.reddit.com/r/OutOfTheLoop+memes+dankmemes+teenagers+GenZ/top/.rss?t=day&limit=40",
}
PER_SOURCE = 25


def titles(xml: str) -> list:
    """Item titles from an RSS or Atom feed (the feed's own title is skipped)."""
    out = []
    for raw in re.findall(r"<title[^>]*>(.*?)</title>", xml, re.S)[1:]:
        t = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", raw)).strip()
        t = re.sub(r"^What'?s (going on|up) with\s*", "", t, flags=re.I)
        if len(t) > 3 and t.lower() not in ("me_irl", "meirl") and t not in out:
            out.append(t[:120])
    return out


def fetch(get: Optional[Callable] = None) -> dict:
    """{source name: [titles]}."""
    if get is None:
        import requests
        get = lambda url: requests.get(url, headers=UA, timeout=20)
    found = {}
    for name, url in SOURCES.items():
        try:
            r = get(url)
            if r.status_code == 200:
                items = titles(r.text)[:PER_SOURCE]
                if items:
                    found[name] = items
        except Exception:
            pass
    return found


def brief(found: dict) -> str:
    return "\n".join(f"{name}:\n" + "\n".join(f"- {t}" for t in items) for name, items in found.items())
