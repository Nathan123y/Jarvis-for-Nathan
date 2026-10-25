"""Posting the shop's videos as Instagram Reels (Instagram API with Instagram Login).

A Meta app in development mode may publish to the accounts that have a role on it, with no app review,
so the user's own professional Instagram account (added as the app's Instagram tester) can post
publicly. Instagram fetches the video from a public address, so the video is first put on the
storefront site (worker/storefront.py, under v/).

The token (plugin_config.instagram.token, a long-lived one from the app dashboard) lasts 60 days;
it's refreshed here whenever it's more than a week old.
"""
from __future__ import annotations

import time
from typing import Callable

NS = "instagram"
GRAPH = "https://graph.instagram.com/v21.0/"


def _cfg() -> dict:
    from memory.config_manager import get_plugin_config
    return get_plugin_config(NS)


def _save(**kv) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(NS, kv)


def connected() -> bool:
    return bool(_cfg().get("token"))


def _token() -> str:
    import requests
    cfg = _cfg()
    if time.time() - float(cfg.get("refreshed_at") or 0) > 7 * 86400:
        r = requests.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": cfg["token"]}, timeout=30)
        if r.status_code == 200 and r.json().get("access_token"):
            _save(token=r.json()["access_token"], refreshed_at=time.time())
            return r.json()["access_token"]
    return cfg["token"]


def _call(method: str, path: str, **params):
    import requests
    r = requests.request(method, GRAPH + path, params={**params, "access_token": _token()}, timeout=60)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Instagram {path}: {(data.get('error') or {}).get('message', r.text[:160])}")
    return data


def post_reel(video_url: str, caption: str, sleep: Callable[[float], None] = time.sleep) -> str:
    """Publish a Reel from a public video address; returns the post id."""
    me = _cfg().get("user_id") or _call("GET", "me", fields="user_id,username")["user_id"]
    _save(user_id=me)
    container = _call("POST", f"{me}/media", media_type="REELS", video_url=video_url, caption=caption[:2200],
                      share_to_feed="true")["id"]
    for _ in range(60):                                      # Instagram processes the video first
        status = _call("GET", container, fields="status_code").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video ({status})")
        sleep(10)
    else:
        raise RuntimeError("Instagram took too long to process the video")
    return _call("POST", f"{me}/media_publish", creation_id=container)["id"]
