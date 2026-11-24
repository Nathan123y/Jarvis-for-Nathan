"""YouTube uploads through the official YouTube Data API (Google Cloud project "gta-channel-uploads", owned by the
channel's Google account), instead of driving YouTube Studio in a browser.

Until Google's YouTube API compliance audit passes, videos uploaded through an unaudited project are locked to
private, so worker/guides.py keeps using the browser and only switches when plugin_config game_guides.youtube_api
is turned on (after the audit). `python3 -m worker.youtube_api auth` connects the channel once; `check` confirms it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "config" / "youtube_api_client.json"      # the OAuth "Desktop app" client (git-ignored)
TOKEN = ROOT / "config" / "youtube_api_token.json"        # the channel's refresh token (git-ignored)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def authorize(show_url: Callable[[str], None] = print) -> str:
    """One-time: the channel's owner approves the app; the token is saved. Returns the channel's name."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=False, prompt="consent", access_type="offline",
                                  authorization_prompt_message="AUTH_URL {url}", timeout_seconds=1800,
                                  success_message="Connected. You can close this tab.")
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    return channel()["title"]


def _creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    return creds


def service():
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_creds(), cache_discovery=False)


def connected() -> bool:
    return TOKEN.exists()


def channel() -> dict:
    items = service().channels().list(part="snippet", mine=True).execute().get("items") or []
    if not items:
        raise RuntimeError("the token has no YouTube channel")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def upload(path: str, title: str, description: str, tags: list, want_channel: str, privacy: str = "public",
           short: bool = True, log: Callable = print) -> str:
    """Uploads one video to the channel (never any other: the token's channel must be `want_channel`). Returns its
    link. Category 20 = Gaming. Made-for-kids is always False, AI disclosure set (altered/synthetic content)."""
    from googleapiclient.http import MediaFileUpload
    ch = channel()
    if want_channel and ch["id"] != want_channel:
        raise RuntimeError(f"the API token is for channel {ch['id']}, not {want_channel}; not uploading")
    body = {"snippet": {"title": title[:100], "description": description[:5000], "tags": tags[:30], "categoryId": "20"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}}
    req = service().videos().insert(part="snippet,status", body=body,
                                    media_body=MediaFileUpload(path, chunksize=8 << 20, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    log(f"youtube api: uploaded {vid} ({resp.get('status', {}).get('privacyStatus')})")
    return f"https://youtube.com/shorts/{vid}" if short else f"https://youtu.be/{vid}"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "auth":
        print("connected to", authorize(lambda u: print(u, flush=True)), flush=True)
    else:
        print(json.dumps(channel()) if connected() else "not connected (run: python3 -m worker.youtube_api auth)")
