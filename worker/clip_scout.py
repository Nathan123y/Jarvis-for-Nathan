"""Clip scout: finds the best moments of the game (GTA 6 at launch) on Twitch and YouTube and drops them into the
clip bot's inbox (worker/clips.py), which cuts and posts them.

  Twitch   For each streamer on the list, the clips their own viewers made in the last day, most-watched first.
           A clip is taken only when Twitch files it under the game, so nothing is fetched before launch day.
  YouTube  New videos from the channels on the list, plus this week's most-watched videos about the game. The best
           moments are the peaks of YouTube's own "most replayed" graph; a minute around each peak is downloaded.
           A video too new to have that graph is watched by Gemini through its link (up to 40 minutes long),
           which names the best moments; longer ones are looked at again on later passes.

Each download gets a "<clip>.json" beside it with who to credit and where it came from. Nothing is fetched twice,
and the scout stops once enough clips are waiting.

    python3 -m worker.clip_scout run [--game "Grand Theft Auto V"]    one pass now (the --game is for testing)
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker import clips
from worker import viral as wv

NS = "clip_scout"
DEFAULTS = {"enabled": True,
            "start_date": "2026-11-19",      # launch day: before it, "GTA 6 gameplay" online is fakes and mods
            # ...so until then only the listed streamers' own videos about the game are clipped (their reactions
            # to the trailers and the Extended Look, like the user's reference, Fanum's "REACTING TO GTA 6..")
            "prelaunch": True,
            "prelaunch_skip": "leak,rumor,news,compilation,meme,gta 5,gta v ",
            "every_min": 30,
            "stock": 6,                      # stop fetching once this many clips wait in the inbox
            # Twitch files clips under the game's exact name; YouTube titles are matched on any of these
            "game_names": "Grand Theft Auto VI,Grand Theft Auto 6,GTA VI,GTA 6,GTA6",
            "twitch": ("kaicenat,caseoh_,xqc,jynxzi,summit1g,lirik,shroud,zackrawrr,nmplol,sodapoppin,hasanabi,"
                       "plaqueboymax,agent00,fanum,stableronaldo,lacy,sketch,tarik,forsen,moistcr1tikal,"
                       "timthetatman,nickmercs,cloakzy,ludwig,pokimane"),
            # streamers' YouTube channels (their stream uploads); each handle checked to exist on 11-14
            "youtube": ("@FanumLive,@KaiCenatLive,@IShowSpeed,@xQcOW,@Agent00,@CaseOh_,@TypicalGamer,@SilkyLive,@jynxzi,"
                        "@TimTheTatman,@jacksepticeye,@Markiplier"),
            "youtube_search": "gta 6 gameplay",   # also this week's most-watched videos for this search ("" = off)
            "twitch_min_views": 300,              # a day-old clip with a few hundred views is already a hit
            "youtube_min_views": 50000,
            "per_video": 2,                  # moments taken from one YouTube video
            "watch_max_min": 40,             # Gemini watches a video itself up to this long (free-tier token limits)
            "watches_per_pass": 3,
            "skip_titles": "meme,compilation,trailer,reaction,leak,rumor,news,review,before gta"}
SEEN_KEEP = 4000


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS and v is not None})
    return out


def _list(text) -> list[str]:
    return [x.strip() for x in str(text or "").split(",") if x.strip()]


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def is_game(text, names: list[str]) -> bool:
    """True when the text (a Twitch category or a video title) names the game, as whole words: "GTA 6" must not
    match "GTA 67" and "GTA V" must not match "GTA VI"."""
    hay = f" {_norm(text)} "
    return any(f" {_norm(n)} " in hay for n in names if _norm(n))


def ffmpeg_dir() -> str:
    """A folder holding the bundled ffmpeg under the plain name "ffmpeg" (yt-dlp only looks for that name)."""
    import tempfile
    d = Path(tempfile.gettempdir()) / "jarvis-ffmpeg"
    link, real = d / "ffmpeg", Path(wv.ffmpeg())
    if not link.exists() or link.resolve() != real.resolve():
        d.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(real)
    return str(d)


def _ydl(**opts):
    import yt_dlp
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
    FFmpegPostProcessor._ffmpeg_location.set(ffmpeg_dir())    # the partial-download check reads this, not the option
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 30,
                             "ffmpeg_location": ffmpeg_dir(), **opts})


def listing(url: str, n: int) -> list[dict]:
    """The entries of a channel / search page, without opening each video."""
    with _ydl(extract_flat=True, playlistend=n) as y:
        return [e for e in ((y.extract_info(url, download=False) or {}).get("entries") or []) if e]


def details(url: str) -> dict:
    with _ydl() as y:
        return y.extract_info(url, download=False) or {}


def download(url: str, dest: Path, span: Optional[tuple] = None) -> Path:
    """The video (or just `span` = (start, end) seconds of it) as an mp4 at up to 1080p."""
    import yt_dlp
    opts = {"outtmpl": str(dest.with_suffix(".%(ext)s")), "merge_output_format": "mp4",
            "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[height<=1080]/b"}
    if span:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [span])
    with _ydl(**opts) as y:
        y.download([url])
    if not dest.exists():
        raise RuntimeError(f"the download left no file at {dest.name}")
    return dest


# ── picking ──────────────────────────────────────────────────────────────────
def twitch_picks(entries: list[dict], seen: set, min_views: int) -> list[dict]:
    """Viewer-made clips worth a look, most-watched first."""
    good = [e for e in entries if e.get("url") and e.get("id") and f"tw:{e['id']}" not in seen
            and (e.get("view_count") or 0) >= min_views and 8 <= (e.get("duration") or 0) <= 90]
    return sorted(good, key=lambda e: -(e.get("view_count") or 0))


def moments(heatmap: list[dict], duration: float, count: int = 2, pad: float = 24, skip_start: float = 75,
            apart: float = 150) -> list[tuple]:
    """The most replayed stretches of a video as (start, end) spans about a minute long, best first. The opening
    is left out (everyone replays an intro) and two moments are never close together."""
    peaks = sorted((h for h in heatmap or [] if h.get("start_time", 0) >= skip_start and h.get("value", 0) >= .25),
                   key=lambda h: -h["value"])
    out = []
    for h in peaks:
        mid = (h["start_time"] + h["end_time"]) / 2
        if any(abs(mid - (a + b) / 2) < apart for a, b in out):
            continue
        start = max(0.0, h["start_time"] - pad)
        end = min(float(duration or h["end_time"] + pad), max(h["end_time"] + pad, start + 45))
        if end - start < 45:                                    # a peak at the very end: reach back instead
            start = max(0.0, end - 45)
        out.append((round(start), round(end)))
        if len(out) >= count:
            break
    return out


WATCH_PROMPT = """Watch this whole video. It should be gameplay of {game}.
Find the {count} moments most likely to go viral as a short vertical clip: a crash, a glitch, a stunt, a chase, a
funny failure, a big reaction. Each moment is 30-60 seconds and starts a few seconds BEFORE it builds.
Answer with JSON only:
{{"is_game": true or false,     // false when the video is not mostly real gameplay of {game} (a trailer, news,
                               // a talk video, a different game, a compilation of other people's clips)
 "moments": [{{"start": seconds, "end": seconds, "what": "one line"}}]}}"""


def clean_moments(raw, duration: float, count: int) -> list[tuple]:
    """Gemini's moments as (start, end) spans inside the video, 20-75 s long, not overlapping."""
    out = []
    if not isinstance(raw, dict) or not raw.get("is_game"):
        return out
    for m in raw.get("moments") or []:
        if not isinstance(m, dict):
            continue
        a, b = wv._num(m.get("start"), -1), wv._num(m.get("end"), -1)
        if a < 0 or b <= a or (duration and a >= duration):
            continue
        b = min(b, a + 75, duration or b)
        if b - a < 20:
            b = min(a + 45, duration or a + 45)
        if b - a >= 15 and not any(a < y and x < b for x, y in out):
            out.append((round(a), round(b)))
    return out[:count]


def gemini_moments(url: str, duration: float, game: str, count: int, log: Callable = print) -> Optional[list]:
    """Gemini watches the YouTube video by its link (nothing is downloaded) and names the best moments.
    [] = not the game / nothing good; None = Gemini couldn't be reached."""
    from core import gemini
    from google.genai import types
    c = gemini.client(timeout_ms=600000)
    for model in ("gemini-3.5-flash", "gemini-3.5-flash-lite"):
        try:
            r = c.models.generate_content(
                model=model, contents=[types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*")),
                                       WATCH_PROMPT.format(game=game, count=count)],
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW))
            return clean_moments(wv._json_from(r.text), duration, count)
        except Exception as exc:
            log(f"scout: {model} couldn't watch {url[-11:]} ({str(exc)[:80]})")
    return None


def video_ok(e: dict, names: list[str], skip: list[str], min_views: int) -> bool:
    title = e.get("title") or ""
    return (is_game(title, names) and not any(s in title.lower() for s in skip)
            and 180 <= (e.get("duration") or 0) <= 4 * 3600 and (e.get("view_count") or 0) >= min_views
            and e.get("live_status") not in ("is_live", "is_upcoming"))


def write_note(clip: Path, credit: str, site: str, url: str, title: str = "") -> None:
    clip.with_suffix(".json").write_text(json.dumps({"credit": credit, "site": site, "url": url, "title": title}))


# ── one pass ─────────────────────────────────────────────────────────────────
class Scout:
    def __init__(self, log: Callable[[str], None] = print):
        self.log = log

    def due(self, cfg: dict, st: dict, now: float) -> bool:
        if self.before_launch(cfg, now) and not cfg.get("prelaunch"):
            return False
        return bool(cfg["enabled"]) and now - st.get("scout_at", 0) >= float(cfg["every_min"]) * 60

    @staticmethod
    def before_launch(cfg: dict, now: Optional[float] = None) -> bool:
        return time.strftime("%Y-%m-%d", time.localtime(now or time.time())) < str(cfg.get("start_date") or "")

    def run(self, cfg: Optional[dict] = None, now: Optional[float] = None) -> int:
        """Tops the inbox up to `stock` clips. Returns how many it fetched."""
        cfg = cfg or settings()
        st = wv.load_state(clips.STATE)
        st["scout_at"] = now or time.time()
        wv.save_state(st, clips.STATE)
        room = int(cfg["stock"]) - len(clips.inbox(settle_s=0))
        if room <= 0:
            return 0
        clips.INBOX.mkdir(parents=True, exist_ok=True)
        seen = set(st.get("scout_seen") or [])
        names = _list(cfg["game_names"])
        got = 0
        early = self.before_launch(cfg, now)
        try:
            if not early:
                got += self.twitch(cfg, names, seen, (room + 1) // 2)   # half each, so one site can't crowd out the other
            got += self.youtube(cfg, names, seen, room - got, early)
            if got < room and not early:
                got += self.twitch(cfg, names, seen, room - got)
        finally:
            st = wv.load_state(clips.STATE)
            st["scout_seen"] = sorted(seen)[-SEEN_KEEP:] if len(seen) > SEEN_KEEP else sorted(seen)
            wv.save_state(st, clips.STATE)
        if got:
            self.log(f"scout: fetched {got} clip(s) into the inbox")
        return got

    def twitch(self, cfg: dict, names: list[str], seen: set, room: int) -> int:
        if room <= 0:
            return 0
        picks = []
        for ch in _list(cfg["twitch"]):
            try:
                entries = listing(f"https://www.twitch.tv/{ch}/clips?filter=clips&range=24hr", 12)
            except Exception as exc:
                self.log(f"scout: couldn't list {ch}'s clips ({str(exc)[:70]})")
                continue
            picks += [dict(e, _channel=ch) for e in twitch_picks(entries, seen, int(cfg["twitch_min_views"]))]
        got = 0
        for e in sorted(picks, key=lambda e: -(e.get("view_count") or 0)):
            if got >= room:
                break
            key = f"tw:{e['id']}"
            if key in seen:
                continue
            seen.add(key)
            try:
                info = details(e["url"])
                if not any(is_game(c, names) for c in info.get("categories") or []):
                    continue                                    # some other game (or just chatting)
                ch = info.get("channel") or info.get("creator") or e["_channel"]
                dest = clips.INBOX / f"@{ch} tw-{e['id']}.mp4"
                download(e["url"], dest)
                write_note(dest, ch, "twitch", e["url"], info.get("title") or "")
                self.log(f"scout: Twitch clip from {ch}: \"{(info.get('title') or '')[:60]}\" ({e.get('view_count')} views)")
                got += 1
            except Exception as exc:
                self.log(f"scout: couldn't fetch the Twitch clip {e['id']} ({str(exc)[:80]})")
        return got

    def youtube(self, cfg: dict, names: list[str], seen: set, room: int, early: bool = False) -> int:
        """early = before launch: only the listed streamers' own videos about the game (found with each
        channel's search), reactions included; no site-wide search, where pre-launch "gameplay" is fake."""
        if room <= 0:
            return 0
        skip = [s.lower() for s in _list(cfg["prelaunch_skip" if early else "skip_titles"])]
        found = {}
        from urllib.parse import quote_plus
        if early:
            pages = [(f"https://www.youtube.com/{ch}/search?query={quote_plus(_list(cfg['game_names'])[-2])}", 12, int(cfg["youtube_min_views"]))
                     for ch in _list(cfg["youtube"])]
        else:
            pages = [(f"https://www.youtube.com/{ch}/videos", 6, 0) for ch in _list(cfg["youtube"])]
        if cfg["youtube_search"] and not early:                               # uploaded this week, most-watched first, any channel
            pages.append((f"https://www.youtube.com/results?search_query={quote_plus(str(cfg['youtube_search']))}&sp=CAMSBAgDEAE%253D",
                          25, int(cfg["youtube_min_views"])))
        for url, n, min_views in pages:
            try:
                rank = 0
                for e in sorted(listing(url, n), key=lambda e: -(e.get("view_count") or 0)):
                    if e.get("id") and f"yt:{e['id']}" not in seen and video_ok(e, names, skip, min_views):
                        found.setdefault(e["id"], dict(e, _rank=rank if early else 0))
                        rank += 1
            except Exception as exc:
                self.log(f"scout: couldn't list {url[24:70]} ({str(exc)[:70]})")
        got, watches = 0, int(cfg["watches_per_pass"])
        game = clips.settings()["game"]
        limit = float(cfg["watch_max_min"]) * 60                # videos Gemini can watch go first: the long ones
        order = sorted(found.values(),                          # only work once YouTube has a replay graph for them
                       key=lambda e: ((e.get("duration") or 0) > limit, e.get("_rank", 0), -(e.get("view_count") or 0)))
        # (before launch _rank is the video's place on its own channel, so every streamer's best goes first
        # rather than one channel's whole back catalogue)
        for e in order[:12]:
            if got >= room:
                break
            url = f"https://www.youtube.com/watch?v={e['id']}"
            took = 0
            try:
                info = details(url)
                if (info.get("height") or 1080) < 720:           # too blurry to repost
                    seen.add(f"yt:{e['id']}")
                    continue
                length = info.get("duration") or 0
                spans = moments(info.get("heatmap") or [], length, int(cfg["per_video"]))
                if not spans:                                   # no "most replayed" graph (new videos have none)
                    if length > float(cfg["watch_max_min"]) * 60 or watches <= 0:
                        continue                                # too long to watch: look again next pass for a graph
                    watches -= 1
                    spans = gemini_moments(url, length, cfg.get("_game") or game, int(cfg["per_video"]), self.log)
                    if spans is None:
                        continue                                # Gemini was busy: try this video again next pass
                handle = str(info.get("uploader_id") or info.get("channel") or "").lstrip("@")
                for a, b in spans[:max(1, room - got)]:
                    dest = clips.INBOX / f"@{re.sub(r'[^A-Za-z0-9_.]', '', handle) or 'youtube'} yt-{e['id']}-{a}.mp4"
                    download(url, dest, (a, b))
                    write_note(dest, handle, "youtube", f"{url}&t={a}s", info.get("title") or "")
                    self.log(f"scout: YouTube moment from {handle} at {a // 60}:{a % 60:02d} of \"{(info.get('title') or '')[:50]}\"")
                    got += 1
                    took += 1
                seen.add(f"yt:{e['id']}")                       # only once it's fetched (or has nothing): a failed
            except Exception as exc:                            # download is tried again next pass
                if took:                                        # ...unless one moment already came down
                    seen.add(f"yt:{e['id']}")
                self.log(f"scout: couldn't fetch from the YouTube video {e['id']} ({str(exc)[:80]})")
        return got


def main(argv: list[str]) -> int:
    cfg = settings()
    if "--game" in argv:
        cfg["game_names"] = cfg["_game"] = argv[argv.index("--game") + 1]
    if "--stock" in argv:
        cfg["stock"] = int(argv[argv.index("--stock") + 1])
    print("fetched:", Scout().run(cfg))
    for f in clips.inbox(settle_s=0):
        print(" ", f.name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
