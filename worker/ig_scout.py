"""Instagram, READ ONLY, for the shop's Trend Scout: what's going viral on the big meme pages and hashtags right now.

The user's rule: nothing is ever posted to their Instagram. This module only makes GET requests, and the token it is given only has
read permissions (instagram_basic, pages_show_list, pages_read_engagement, business_management; no instagram_content_publish, which
posting would need). There is no posting code in Jarvis at all.

Once a day (with the other trend feeds) it
  1. reads the latest posts of each watched page (Business Discovery: public business/creator accounts) and the top posts of a few
     meme hashtags (Hashtag Search: at most 30 different hashtags a week, so the list stays short and their ids are cached),
  2. ranks posts from the last 48 hours by likes + comments per hour, relative to the page's followers,
  3. has Gemini read the most viral images (memes put the joke in the picture, not the caption) and say what the joke is,
and hands the Scout lines like `"<the joke>" (12.4k likes in 6h on @page)`. The shop makes ORIGINAL designs riffing on the joke;
it never copies a post's picture.

Setup: plugin_config.instagram_scout.token, a user token from the Graph API Explorer for the user's Meta app ("Instagram API with
Facebook Login"), with their professional Instagram linked to a Facebook Page. The Instagram account id is found from the token.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional

NS = "instagram_scout"
GRAPH = "https://graph.facebook.com/v21.0/"
PAGES = ["9gag", "pubity", "daquan", "memezar", "sarcasm_only", "betches", "ladbible", "girlswithnojob", "middleclassfancy",
         "kalesalad", "memes", "4gag", "funnymemes", "bestofmemes", "dank.memes"]
HASHTAGS = ["memes", "relatable", "funnymemes", "dankmemes", "memesdaily"]
FIELDS = "caption,like_count,comments_count,timestamp,media_type,media_url,thumbnail_url,permalink"
WINDOW_H = 48
READ_IMAGES = 8                     # how many of the most viral posts Gemini looks at each day
VISION_PROMPT = ("This is a viral Instagram meme post. Say in one short line what the joke or catchphrase is (quote any text in it), "
                 "so a shirt designer who can't see it gets it. If it's about a real person, news or politics, or it's an ad, "
                 "reply SKIP.")


def _cfg() -> dict:
    from memory.config_manager import get_plugin_config
    return get_plugin_config(NS)


def _save(**kv) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(NS, kv)


def connected() -> bool:
    return bool(_cfg().get("token"))


class Reader:
    """GET-only Graph API client."""

    def __init__(self, token: str, get: Optional[Callable] = None):
        self.token = token
        if get is None:
            import requests
            get = lambda url, params: requests.get(url, params=params, timeout=30)
        self._get = get

    def get(self, path: str, **params) -> dict:
        r = self._get(GRAPH + path, {**params, "access_token": self.token})
        data = r.json() if getattr(r, "content", b"") else {}
        if r.status_code >= 400 or "error" in data:
            raise RuntimeError(f"Instagram {path}: {(data.get('error') or {}).get('message', str(r.status_code))}")
        return data

    def my_account(self) -> str:
        """The user's Instagram account id, via the Facebook Page it's linked to."""
        for page in self.get("me/accounts", fields="instagram_business_account").get("data") or []:
            acct = (page.get("instagram_business_account") or {}).get("id")
            if acct:
                return acct
        raise RuntimeError("no Instagram professional account is linked to a Facebook Page on this token")


def score(post: dict, now: datetime, followers: Optional[int]) -> float:
    """Likes + 3x comments per hour since posting, per million followers when known (a small page's hit counts too)."""
    try:
        posted = datetime.fromisoformat(str(post["timestamp"]).replace("+0000", "+00:00"))
    except (KeyError, ValueError):
        return 0.0
    hours = max(1.0, (now - posted).total_seconds() / 3600)
    if hours > WINDOW_H:
        return 0.0
    eng = float(post.get("like_count") or 0) + 3 * float(post.get("comments_count") or 0)
    per_hour = eng / hours
    return per_hour / max(0.05, (followers or 1_000_000) / 1_000_000) ** 0.5


def collect(reader: Reader, me: str, now: datetime, pages: list, hashtags: list, tag_ids: dict, log=print) -> list:
    """[{"post", "where", "score", "hours"}] from every watched page and hashtag that answered."""
    found = []
    for name in pages:
        try:
            bd = reader.get(me, fields=f"business_discovery.username({name}){{followers_count,media.limit(12){{{FIELDS}}}}}")
            info = bd.get("business_discovery") or {}
            for p in (info.get("media") or {}).get("data") or []:
                found.append({"post": p, "where": f"@{name}", "score": score(p, now, info.get("followers_count"))})
        except Exception as exc:
            log(f"ig scout: @{name} skipped: {str(exc)[:120]}")
    for tag in hashtags:
        try:
            if tag not in tag_ids:
                hits = reader.get("ig_hashtag_search", user_id=me, q=tag).get("data") or []
                if not hits:
                    continue
                tag_ids[tag] = hits[0]["id"]
            for p in reader.get(f"{tag_ids[tag]}/top_media", user_id=me, fields=FIELDS, limit=25).get("data") or []:
                found.append({"post": p, "where": f"#{tag}", "score": score(p, now, None)})
        except Exception as exc:
            log(f"ig scout: #{tag} skipped: {str(exc)[:120]}")
    seen, out = set(), []
    for f in sorted(found, key=lambda f: -f["score"]):
        key = f["post"].get("permalink") or f["post"].get("id")
        if f["score"] > 0 and key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _likes(n) -> str:
    n = float(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def read_image(url: str, get_bytes: Optional[Callable] = None, look: Optional[Callable] = None) -> Optional[str]:
    """Gemini's one-line reading of a meme image, or None (SKIP, no answer, or the image didn't download)."""
    try:
        if get_bytes is None:
            import requests
            get_bytes = lambda u: requests.get(u, timeout=30).content
        img = get_bytes(url)
        if look is None:
            from google.genai import types
            from core import gemini
            look = lambda b: gemini.text([types.Part.from_bytes(data=b, mime_type="image/jpeg"), VISION_PROMPT], tier=gemini.SMART)
        line = (look(img) or "").strip().splitlines()[0][:200] if img else ""
    except Exception:
        return None
    return None if not line or line.upper().startswith("SKIP") else line


def fetch(log=print, reader: Optional[Reader] = None, now: Optional[datetime] = None, read=read_image) -> list:
    """Lines for the Trend Scout; [] when Instagram isn't connected or nothing answered."""
    cfg = _cfg()
    if reader is None:
        if not cfg.get("token"):
            return []
        reader = Reader(cfg["token"])
    now = now or datetime.now(timezone.utc)
    try:
        me = cfg.get("ig_user_id") or reader.my_account()
    except Exception as exc:
        log(f"ig scout: {exc}")
        return []
    tag_ids = dict(cfg.get("hashtag_ids") or {})
    posts = collect(reader, me, now, list(cfg.get("pages") or PAGES), list(cfg.get("hashtags") or HASHTAGS), tag_ids, log)
    try:
        _save(ig_user_id=me, hashtag_ids=tag_ids, last_run=time.time(), last_found=len(posts))
    except Exception:
        pass
    lines = []
    for f in posts[:READ_IMAGES * 2]:
        if len(lines) >= READ_IMAGES:
            break
        p = f["post"]
        joke = None
        url = p.get("thumbnail_url") if p.get("media_type") == "VIDEO" else p.get("media_url")
        if url:
            joke = read(url)
        if not joke:
            cap = " ".join(str(p.get("caption") or "").split())
            if not cap:
                continue
            joke = f"caption: {cap[:140]}"
        try:
            hours = max(1, int((now - datetime.fromisoformat(str(p['timestamp']).replace('+0000', '+00:00'))).total_seconds() // 3600))
        except (KeyError, ValueError):
            hours = 0
        lines.append(f"\"{joke}\" ({_likes(p.get('like_count'))} likes, {p.get('comments_count') or 0} comments in {hours}h on {f['where']})")
    return lines
