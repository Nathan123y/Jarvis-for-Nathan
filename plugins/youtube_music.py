"""YouTube Music: play songs, artists, albums, your playlists, liked songs and
recent plays by voice, plus pause / skip / "what song is this".

Playback happens in Google Chrome on music.youtube.com, where the user is
already signed in, so it plays from their own account with their own
Premium/ads situation. One tab is reused: a new request replaces what that tab
is playing instead of stacking tabs that play over each other. Pause, skip and
previous are the Mac's media keys, which Chrome routes to that tab.

Searching works without connecting. The user's library (their playlists,
recent plays, adding to a playlist, liking) needs a one-time connection:
"connect YouTube Music" walks them through copying the request headers from
Chrome, which are saved only on this Mac (config/youtube_music_auth.json,
git-ignored) and then wiped from the clipboard.
"""
from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

PLUGIN = {
    "name": "youtube_music",
    "description": (
        "Play music on the user's YouTube Music account and control it. Use for 'play "
        "<song/artist/album>', 'play my <name> playlist', 'play my liked songs', 'play my "
        "recent plays', 'play a recent Arabic song' / 'my Arabic mix' (kind recent, query "
        "arabic: one of their recent songs in that language, then similar ones), 'play "
        "something chill / some focus music', 'pause', 'resume', "
        "'skip', 'next song', 'go back', 'what song is this', 'add this song to my gym "
        "playlist', 'like this song', 'what playlists do I have', 'connect YouTube Music'. "
        "Actions: play (query + kind: song, artist, album, playlist (the user's own first, "
        "then public), liked, recent, mood (a vibe like chill, focus, hype)), pause, resume, "
        "next, previous, now_playing, add_to_playlist (playlist=name; the current song), "
        "like, playlists, connect, connect_done (after the user copied the headers), status. "
        "Use this instead of youtube_video or the browser for music. Say what is playing in "
        "a few words; do not read URLs."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["play", "pause", "resume", "next", "previous",
                                                  "now_playing", "add_to_playlist", "like",
                                                  "playlists", "connect", "connect_done", "status"]},
            "query": {"type": "STRING", "description": "What to play: song, artist, album, playlist name or a mood; with kind recent, a language, artist or word to pick from the user's recent plays"},
            "kind": {"type": "STRING", "enum": ["song", "artist", "album", "playlist", "liked", "recent", "mood"]},
            "playlist": {"type": "STRING", "description": "For add_to_playlist: the user's playlist name"},
        },
        "required": ["action"],
    },
}

_ROOT = Path(__file__).resolve().parents[1]
AUTH_FILE = _ROOT / "config" / "youtube_music_auth.json"
_BASE = "https://music.youtube.com"
_RECENT_TITLE = "Jarvis · Recently played"
_OSA_SECONDS = 5
_lock = threading.Lock()
_clients: dict = {}


# ── account ──────────────────────────────────────────────────────────────────
def _session():
    import requests

    class _TimeoutSession(requests.Session):
        def request(self, *args, **kwargs):          # a stuck request must not hang a voice turn
            kwargs.setdefault("timeout", 12)
            return super().request(*args, **kwargs)

    return _TimeoutSession()


def connected() -> bool:
    return AUTH_FILE.exists()


def _client(account: bool = False):
    """A YTMusic client; signed in when connected (always, if `account`)."""
    from ytmusicapi import YTMusic
    use_auth = connected()
    if account and not use_auth:
        raise _NeedsConnect()
    key = ("auth", AUTH_FILE.stat().st_mtime) if use_auth else ("anon",)
    with _lock:
        if key not in _clients:
            _clients.clear()
            _clients[key] = YTMusic(str(AUTH_FILE), requests_session=_session()) if use_auth \
                else YTMusic(requests_session=_session())
        return _clients[key]


class _NeedsConnect(Exception):
    pass


_CONNECT_STEPS = (
    "CONNECT YOUTUBE MUSIC (one time)\n\n"
    "1. In the YouTube Music tab that just opened, make sure you're signed in.\n"
    "2. Press Cmd+Option+I to open Developer Tools, then click the Network tab.\n"
    "3. In the filter box type:  browse\n"
    "4. Click Home or Library in YouTube Music so a 'browse' request appears.\n"
    "5. Right-click that request → Copy → Copy request headers.\n"
    "6. Close Developer Tools and say \"done\".\n\n"
    "The headers are saved only on this Mac and wiped from your clipboard."
)


_HEADER_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
# Describe the copied request rather than this one: sending them makes YouTube
# expect a gzip body and answer with an HTML error page.
_DROP_HEADERS = {"content-encoding", "content-length", "host", "accept-encoding"}


def _clean_headers(raw: str) -> str:
    """Copied request headers as plain 'name: value' lines. Chrome now copies
    each name and value on separate lines, after pseudo-headers (:path…) and
    with a multi-line 'Decoded:' block inside, which ytmusicapi misreads."""
    lines = [l.strip() for l in raw.replace("\r", "").split("\n")]
    pairs: list[tuple[str, str]] = []
    if sum(": " in l for l in lines) > len([l for l in lines if l]) / 2:
        for l in lines:                                   # classic one-line format
            name, sep, value = l.partition(": ")
            if sep and _HEADER_NAME.fullmatch(name.lower()):
                pairs.append((name.lower(), value))
    else:
        i = 0
        while i < len(lines):
            line = lines[i]
            if line == "Decoded:":                        # x-client-data, decoded
                while i < len(lines) and lines[i] != "}":
                    i += 1
                i += 1
                continue
            if (line.startswith(":") or _HEADER_NAME.fullmatch(line)) and i + 1 < len(lines):
                pairs.append((line.lower(), lines[i + 1]))
                i += 2
                continue
            i += 1
    return "\n".join(f"{n}: {v}" for n, v in pairs
                     if not n.startswith((":", "sec-")) and n not in _DROP_HEADERS)


def _connect_done() -> str:
    try:
        raw = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        raw = ""
    if "cookie" not in raw.lower():
        return ("I don't see the copied headers on the clipboard. In Developer Tools, right-click "
                "the 'browse' request, choose Copy, then Copy request headers, and say done again.")
    from ytmusicapi import setup
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    try:
        setup(filepath=str(tmp), headers_raw=_clean_headers(raw))
        from ytmusicapi import YTMusic
        YTMusic(str(tmp), requests_session=_session()).get_library_playlists(limit=1)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return (f"Those headers didn't work ({type(exc).__name__}). Make sure you're signed in to "
                "YouTube Music and copy the headers of a 'browse' request.")
    tmp.replace(AUTH_FILE)
    try:
        AUTH_FILE.chmod(0o600)
        subprocess.run(["pbcopy"], input="", text=True, timeout=5)     # cookies off the clipboard
    except Exception:
        pass
    with _lock:
        _clients.clear()
    return "YouTube Music is connected. Your playlists, liked songs and recent plays are ready."


# ── Chrome ───────────────────────────────────────────────────────────────────
def _osa(script: str) -> str | None:
    """Run AppleScript; None when it failed or macOS is still asking for permission."""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           timeout=_OSA_SECONDS)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


_FIND_TAB = '''
if application "Google Chrome" is running then
  tell application "Google Chrome"
    repeat with w in windows
      repeat with t in tabs of w
        if URL of t contains "music.youtube.com" then
          %s
        end if
      end repeat
    end repeat
  end tell
end if
return ""
'''


def _open(url: str) -> str:
    """Play `url` in the existing YouTube Music tab, or a new one."""
    safe = url.replace("\\", "").replace('"', "")
    if _osa(_FIND_TAB % f'set URL of t to "{safe}"\n          return "reused"') == "reused":
        return "reused"
    subprocess.run(["open", "-g", "-a", "Google Chrome", url], capture_output=True, timeout=10)
    return "opened"


def _tab_url() -> str:
    return _osa(_FIND_TAB % "return URL of t") or ""


def _current_video_id() -> str:
    q = parse_qs(urlsplit(_tab_url()).query)
    vid = (q.get("v") or [""])[0]
    if vid:
        return vid
    if connected():
        try:
            hist = _client(account=True).get_history()
            return hist[0]["videoId"] if hist else ""
        except Exception:
            return ""
    return ""


_MEDIA = {"play": 16, "next": 17, "previous": 18}


def _media_key(name: str) -> bool:
    """Press a hardware media key; Chrome sends it to the tab that is playing."""
    try:
        import Quartz
        from AppKit import NSEvent
        code = _MEDIA[name]
        for down in (True, False):
            flags = 0xA00 if down else 0xB00
            ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                14, (0, 0), flags, 0, 0, None, 8, (code << 16) | ((0xA if down else 0xB) << 8), -1)
            Quartz.CGEventPost(0, ev.CGEvent())
        return True
    except Exception as exc:
        print(f"[YouTubeMusic] media key failed: {exc!r}")
        return False


# ── finding things ───────────────────────────────────────────────────────────
def _words(s) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(s or "").lower())) - {"my", "the", "playlist", "a", "some"}


def _my_playlist(name: str) -> dict | None:
    if not connected():
        return None
    want = _words(name)
    if not want:
        return None
    best, score = None, 0.0
    for p in _client(account=True).get_library_playlists(limit=None) or []:
        have = _words(p.get("title"))
        if not have:
            continue
        s = len(want & have) / len(want | have)
        if s > score:
            best, score = p, s
    return best if score >= 0.5 else None


def _watch_list(playlist_id: str) -> str:
    pid = playlist_id[2:] if playlist_id.startswith("VL") else playlist_id
    return f"{_BASE}/watch?list={quote(pid)}"


def _artists(item) -> str:
    return ", ".join(a.get("name", "") for a in (item.get("artists") or []) if a.get("name"))


def _play(query: str, kind: str) -> str:
    query = (query or "").strip()
    kind = (kind or "").strip().lower()
    if kind == "liked":
        _open(f"{_BASE}/watch?list=LM")
        return "Playing your liked songs."
    if kind == "recent":
        return _play_recent_matching(query) if query else _play_recent()
    if not query:
        return "What should I play?"
    yt = _client()
    if kind in ("playlist", "") and connected():
        mine = _my_playlist(query)
        if mine:
            _open(_watch_list(mine["playlistId"]))
            return f"Playing your playlist {mine.get('title')}."
    if kind == "artist":
        found = yt.search(query, filter="artists", limit=1)
        if found:
            art = yt.get_artist(found[0]["browseId"])
            pid = art.get("shuffleId") or art.get("radioId") or \
                (art.get("songs") or {}).get("browseId")
            if pid:
                _open(_watch_list(pid))
                return f"Shuffling {art.get('name') or found[0].get('artist') or query}."
    if kind == "album":
        found = yt.search(query, filter="albums", limit=1)
        if found:
            album = yt.get_album(found[0]["browseId"])
            if album.get("audioPlaylistId"):
                _open(_watch_list(album["audioPlaylistId"]))
                return f"Playing the album {album.get('title')} by {_artists(album) or 'the artist'}."
    if kind in ("playlist", "mood"):
        found = yt.search(query if kind == "playlist" else f"{query} music", filter="playlists", limit=3)
        if found:
            _open(_watch_list(found[0]["browseId"]))
            return f"Playing the playlist {found[0].get('title')}."
    found = yt.search(query, filter="songs", limit=1)
    if not found or not found[0].get("videoId"):
        return f"I couldn't find {query} on YouTube Music."
    song = found[0]
    _open(f"{_BASE}/watch?v={quote(song['videoId'])}")
    return f"Playing {song.get('title')} by {_artists(song) or 'unknown artist'}."


def _play_recent() -> str:
    """Your recently played songs, as one private playlist that Jarvis keeps up to date."""
    from memory.config_manager import _save_flag, load_api_keys
    yt = _client(account=True)
    ids = []
    for item in yt.get_history() or []:
        vid = item.get("videoId")
        if vid and vid not in ids:
            ids.append(vid)
        if len(ids) >= 50:
            break
    if not ids:
        return "Your YouTube Music history is empty."
    old = load_api_keys().get("youtube_music_recent_playlist")
    if old:
        try:
            yt.delete_playlist(old)
        except Exception:
            pass
    pid = yt.create_playlist(_RECENT_TITLE, "Your recent plays, refreshed by Jarvis.",
                             privacy_status="PRIVATE", video_ids=ids)
    if not isinstance(pid, str):
        _open(f"{_BASE}/watch?v={quote(ids[0])}")
        return "Playing your most recent song and similar music."
    _save_flag("youtube_music_recent_playlist", pid)
    _open(_watch_list(pid))
    return f"Playing your {len(ids)} most recent songs."


# Language words the user may say, and the script their song titles are written in.
_SCRIPTS = {"arabic": "\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff"}


def _play_recent_matching(query: str) -> str:
    """One of the user's recent songs that fits `query` (a language like Arabic, an
    artist, or a word in the title), played as that song's radio so similar songs
    follow. A song by an artist the user has played in that script counts too,
    which catches romanised titles."""
    import random
    history = [i for i in (_client(account=True).get_history() or []) if i.get("videoId")]
    want = _words(query) - {"song", "songs", "music", "mix", "recent", "recently", "played"}
    script = next((_SCRIPTS[w] for w in want if w in _SCRIPTS), None)

    def text(item):
        return " ".join([str(item.get("title") or ""), _artists(item),
                         str((item.get("album") or {}).get("name") or "")])

    if script:
        pattern = re.compile(f"[{script}]")
        artists = {a for i in history if pattern.search(text(i))
                   for a in _artists(i).split(", ") if a}
        matches = [i for i in history if pattern.search(text(i))
                   or set(_artists(i).split(", ")) & artists]
    else:
        matches = [i for i in history if want and want <= _words(text(i))]
    if not matches:
        found = _client().search(f"{query} music mix", filter="playlists", limit=1)
        if found:
            _open(_watch_list(found[0]["browseId"]))
            return (f"I didn't find any {query} songs in your recent plays, so I'm playing "
                    f"the playlist {found[0].get('title')}.")
        return f"I didn't find any {query} songs in your recent plays."
    # Pick among the latest few so the same song doesn't open every time.
    song = random.choice(matches[:10])
    vid = quote(song["videoId"])
    _open(f"{_BASE}/watch?v={vid}&list=RDAMVM{vid}")
    return f"Playing {song.get('title')} by {_artists(song) or 'unknown artist'}, then similar songs."


def _now_playing() -> str:
    vid = _current_video_id()
    if not vid:
        return "Nothing seems to be playing on YouTube Music."
    try:
        d = _client().get_song(vid).get("videoDetails") or {}
        return f"This is {d.get('title', 'unknown')} by {d.get('author', 'unknown')}."
    except Exception:
        return "I couldn't tell which song this is."


def _add_to_playlist(name: str) -> str:
    vid = _current_video_id()
    if not vid:
        return "Nothing is playing to add."
    target = _my_playlist(name)
    if not target:
        names = [p.get("title") for p in _client(account=True).get_library_playlists(limit=25) or []]
        return f"I couldn't find a playlist called {name}. Yours are: {', '.join(n for n in names if n)}."
    _client(account=True).add_playlist_items(target["playlistId"], [vid], duplicates=False)
    return f"Added it to {target.get('title')}."


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "play").strip().lower()
    try:
        if action == "play":
            return _play(str(args.get("query") or ""), str(args.get("kind") or ""))
        if action in ("pause", "resume"):
            return ("Done." if _media_key("play")
                    else "I couldn't press play/pause; Jarvis may need Accessibility permission.")
        if action in ("next", "previous"):
            if not _media_key(action):
                return "I couldn't control playback; Jarvis may need Accessibility permission."
            return "Skipped." if action == "next" else "Going back."
        if action == "now_playing":
            return _now_playing()
        if action == "add_to_playlist":
            return _add_to_playlist(str(args.get("playlist") or args.get("query") or ""))
        if action == "like":
            vid = _current_video_id()
            if not vid:
                return "Nothing is playing to like."
            _client(account=True).rate_song(vid, "LIKE")
            return "Liked."
        if action == "playlists":
            names = [p.get("title") for p in _client(account=True).get_library_playlists(limit=50) or []]
            if player and names:
                try:
                    player.show_content("YOUR PLAYLISTS", "\n".join(names))
                except Exception:
                    pass
            return "Your playlists: " + ", ".join(n for n in names if n) if names else "You have no playlists."
        if action == "connect":
            subprocess.run(["open", "-a", "Google Chrome", _BASE], capture_output=True, timeout=10)
            if player:
                try:
                    player.show_content("YOUTUBE MUSIC", _CONNECT_STEPS)
                except Exception:
                    pass
            return ("I opened YouTube Music and put the steps on screen: open Developer Tools, "
                    "copy the request headers of a 'browse' request, then say done.")
        if action == "connect_done":
            return _connect_done()
        if action == "status":
            return ("YouTube Music is connected to your account." if connected() else
                    "Searching and playing work; say 'connect YouTube Music' to use your own playlists and history.")
        return "Actions: play, pause, resume, next, previous, now_playing, add_to_playlist, like, playlists, connect."
    except _NeedsConnect:
        return ("That needs your YouTube Music account. Say 'connect YouTube Music' and I'll walk "
                "you through it; it takes a minute, once.")
    except Exception as exc:
        print(f"[YouTubeMusic] {action} failed: {exc!r}")
        return f"YouTube Music didn't respond ({type(exc).__name__}). Try again in a moment."
