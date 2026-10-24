"""Jarvis's own connection to the user's Etsy shop (Etsy Open API v3), for what Printify can't do: adding
more photos to each listing.

Connecting is OAuth with PKCE, done once. connect_url() gives the approval link. Etsy then sends the
browser to the app's callback (https://localhost:3003/callback), which doesn't load, but the address bar
holds the code; finish(that address) trades it for tokens. Tokens live in plugin_config.etsy and refresh
themselves.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit

NS = "etsy"
REDIRECT = "https://localhost:3003/callback"
SCOPES = "listings_r listings_w shops_r"
API = "https://api.etsy.com/v3/application/"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
MAX_PHOTOS = 10


def _cfg() -> dict:
    from memory.config_manager import get_plugin_config
    return get_plugin_config(NS)


def _save(**kv) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(NS, kv)


def connected() -> bool:
    return bool(_cfg().get("refresh_token"))


def connect_url() -> str:
    cfg = _cfg()
    if not cfg.get("keystring"):
        raise RuntimeError("the Etsy app's keystring isn't saved")
    verifier = secrets.token_urlsafe(64)[:96]
    state = secrets.token_urlsafe(16)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _save(pkce_verifier=verifier, pkce_state=state)
    return "https://www.etsy.com/oauth/connect?" + urlencode({
        "response_type": "code", "client_id": cfg["keystring"], "redirect_uri": REDIRECT, "scope": SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def _api_key(cfg: dict) -> str:
    return f"{cfg['keystring']}:{cfg['shared_secret']}" if cfg.get("shared_secret") else cfg["keystring"]


def finish(redirected: str) -> str:
    """Trade the code in the address Etsy redirected to for tokens; returns the shop id."""
    import requests
    cfg = _cfg()
    q = parse_qs(urlsplit(redirected.strip()).query)
    if q.get("error"):
        raise RuntimeError(f"Etsy said: {q.get('error_description', q['error'])[0]}")
    code, state = (q.get("code") or [""])[0], (q.get("state") or [""])[0]
    if not code:
        raise RuntimeError("that address has no code in it; copy the whole address after approving")
    if state != cfg.get("pkce_state"):
        raise RuntimeError("that approval is from an older link; ask for a new one")
    r = requests.post(TOKEN_URL, data={"grant_type": "authorization_code", "client_id": cfg["keystring"],
                                       "redirect_uri": REDIRECT, "code": code, "code_verifier": cfg["pkce_verifier"]},
                      timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Etsy refused the code ({r.status_code}): {r.text[:160]}")
    _store_tokens(r.json())
    me = call("GET", "users/me")
    _save(shop_id=me.get("shop_id"), user_id=me.get("user_id"), pkce_verifier="", pkce_state="")
    return str(me.get("shop_id"))


def _store_tokens(t: dict) -> None:
    _save(access_token=t["access_token"], refresh_token=t["refresh_token"],
          expires_at=time.time() + float(t.get("expires_in", 3600)) - 120)


def _token() -> str:
    import requests
    cfg = _cfg()
    if not cfg.get("refresh_token"):
        raise RuntimeError("Etsy isn't connected")
    if time.time() < float(cfg.get("expires_at") or 0):
        return cfg["access_token"]
    r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": cfg["keystring"],
                                       "refresh_token": cfg["refresh_token"]}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Etsy wouldn't refresh the login ({r.status_code}); connect again")
    _store_tokens(r.json())
    return _cfg()["access_token"]


def call(method: str, path: str, **kw):
    import requests
    cfg = _cfg()
    r = requests.request(method, API + path, headers={"x-api-key": _api_key(cfg), "Authorization": f"Bearer {_token()}"},
                         timeout=120, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Etsy {method} {path} failed ({r.status_code}): {r.text[:200]}")
    return r.json() if r.content else {}


def listing_id(url: str) -> Optional[str]:
    m = re.search(r"/listing/(\d+)", url or "")
    return m.group(1) if m else None


def add_photos(listing: str, paths: list) -> int:
    """Upload photos after the ones already on the listing, up to Etsy's 10. Returns how many were added."""
    shop = _cfg().get("shop_id")
    have = call("GET", f"listings/{listing}/images").get("count", 0)
    added = 0
    for path in paths:
        if have + added >= MAX_PHOTOS:
            break
        p = Path(path)
        if not p.is_file():
            continue
        with open(p, "rb") as f:
            call("POST", f"shops/{shop}/listings/{listing}/images",
                 files={"image": (p.name, f, "image/jpeg")}, data={"rank": str(have + added + 1)})
        added += 1
    return added


def ping() -> tuple:
    """(ok, message): whether Etsy accepts the app's key yet (new apps wait for Etsy's approval)."""
    import requests
    cfg = _cfg()
    r = requests.get(API + "openapi-ping", headers={"x-api-key": _api_key(cfg)}, timeout=20)
    return r.status_code == 200, r.text[:160]
