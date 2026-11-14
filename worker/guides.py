"""Game guides: narrated videos about the game (GTA 6) over real footage: one long YouTube video a day, and
numbered-list Shorts ("5 crazy things...") for YouTube Shorts, TikTok and Instagram.

Each video:
  1. Claude looks the topic up on the web (real sources, nothing invented) and writes the script: a hook, then
     sections with a heading, the narration and what the footage should show.
  2. Gemini's TTS (the daily videos' narrator) reads it; Whisper times every word for the captions and chapters.
  3. Footage: real gameplay. The scout's tools find recent no-commentary gameplay videos of the game on YouTube;
     Gemini watches each one by its link and says which stretches fit which section; those stretches are downloaded.
  4. ffmpeg cuts the stretches into 5-7 second shots with a slow zoom, in section order, under the narration (the
     game's own sound stays in quietly), burns in the captions and each section's heading, and makes a thumbnail.
  5. It uploads through YouTube Studio in a Chrome profile logged into the guides' channel, with chapters and the
     footage's sources in the description.

    python3 -m worker.guides make ["topic"] [--minutes 2] [--game "GTA 5"] [--no-post]     a long video now
    python3 -m worker.guides short ["topic"] [--no-post]                                  a numbered-list Short now
    python3 -m worker.guides topic "..." / shorttopic "..."                               queue a topic
    python3 -m worker.guides library                                                      catalogue new footage
    python3 -m worker.guides upload FILE.mp4      upload a finished video (its .json sits beside it)
    python3 -m worker.guides login                log in to the YouTube channel once, in a Chrome window
    python3 -m worker.guides status
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker import clip_scout as cs
from worker import clips
from worker import viral as wv

STATE = ROOT / "memory" / "guides.json"
WORK = ROOT / "memory" / "guides"                       # one folder per video: script, voice, footage, shots
OUT = Path.home() / "Movies" / "Jarvis Guides"
PROFILE = ROOT / "memory" / "youtube_profile"           # the Chrome profile logged into the guides' channel
NS = "game_guides"
W, H, FPS = 1920, 1080, 30
DEFAULTS = {"enabled": True,
            "start_date": "2026-11-13",                 # the user wants GTA posting running before launch (11-13)
            "launch_date": "2026-11-19",                # before it: official trailers + the Extended Look only
            "time": "16:00",                            # one long video a day
            "short_times": "12:00,17:00,21:00",         # numbered-list shorts (YouTube Shorts + TikTok + Instagram)
            "vertical_copy": True,                      # the long video is also cut tall for TikTok and Instagram, so
                                                        # every account gets it (user, 11-13: "every social media account")
            "official": "@RockstarGames",               # channels whose footage of the game is official
            # Rockstar's own upload of the 27-minute Extended Look is age-restricted (can't be fetched without a
            # login), so these full re-uploads of it stand in; the first one that catalogues cleanly is used
            "extra_sources": "M3qfj0_IO9E,yk7WDsjC3v4",
            "post": True,
            "game": "GTA 6",
            "game_names": "Grand Theft Auto VI,Grand Theft Auto 6,GTA VI,GTA 6,GTA6",
            "minutes": 7,                               # narration length to aim for (8+ minutes allows mid-roll ads,
                                                        # but a day's free narrator requests are shared with the shorts)
            "minutes_prelaunch": 4,                     # before launch there are only a few minutes of official footage
            "channel_id": "UCpdCqccEXBYmMkSq-xbD3Sg",   # the ONLY channel Jarvis may upload to
            "visibility": "public",
            "claude_effort": "medium",
            "sources": 2,                               # new gameplay videos added to the footage library per day (after launch)
            "footage_search": "{game} gameplay no commentary"}
WPM = 165                                               # the narrator at speed 1.08
SHOT = 2.5                                              # a new shot every 2-3 s, like the reference list videos (user, 11-14)
LIB = ROOT / "memory" / "guides_library"                # the footage catalogue (index.json) + downloaded stretches
LIB_MAX_BYTES = 2_500_000_000                           # the Mac's disk is nearly full: old stretches are dropped past this
SIZES = {"guide": (1920, 1080), "short": (1080, 1920)}


class Busy(RuntimeError):
    """Claude, the narrator or Gemini isn't free right now: try this video again later."""


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS and v is not None})
    return out


# ── 1. the script ────────────────────────────────────────────────────────────
SCRIPT_PROMPT = """You write long-form YouTube videos for a gaming channel about {game}: fast numbered-list videos
("15 Things You NEED To Do On Day 1", "10 Details You Missed") narrated by one confident, enthusiastic fan over
quick-cut footage of the game. They cut to the chase and every item is specific. Today is {today}.
{when}

{topic_line}
Videos already made (pick something different): {history}

RESEARCH FIRST. Use web search to find what is actually true about this topic right now, from several sources
(the publisher's own site, big gaming outlets, wikis, widely-upvoted player posts). Every fact, number, name,
location and price in the script must come from what you found. If sources disagree or something is unconfirmed,
say so in the narration ("players report...", "not confirmed yet"). Never invent a detail to fill space; a
shorter true script beats a longer padded one.

Then write the script as ONE JSON object and nothing else:
{{"topic": "the topic in a few words",
 "title": "the YouTube title, under 70 characters, in the form '{game} - 12 Things You NEED To Do On Day 1': the game's name, the exact count of items, what the viewer gets. One or two words in CAPS for punch, no emojis",
 "thumbnail": "2-4 words for the thumbnail, different from the title",
 "description": "2-3 plain sentences on what the video covers",
 "tags": ["8-12 search phrases"],
 "sections": [
   {{"heading": "", "say": "THE HOOK, 3-4 sentences, under 25 seconds: one bold line about the game, the problem
      the viewer has (too much to do, too much they missed), then the promise with the exact count ('these are the
      12 things...'). No greeting, no 'welcome back'.",
     "show": "what the footage should show, 3-8 words"}},
   {{"heading": "1. the item in 2-5 words", "say": "this item's narration, 3-6 sentences (20-40 seconds)", "show": "..."}}
 ],
 "sources": ["the URLs the facts came from"]}}

Rules for the narration ("say"):
- About {words} words in total: the hook, then {items} numbered items (heading "1. ...", "2. ..." in order), one
  section each. Fewer items if the facts run out; never pad the count with weak ones. Save the best item for last
  and say early that it's coming.
- Spoken English in the first person of a fan who can't wait ("the first thing I'm doing is..."): short
  sentences, contractions, numbers written the way they're said. Say each item's number out loud ("Number four").
- Items flow into each other like one plan (get a car, then tune it, then take it to...), not a shuffled list.
- Each item opens with its point and gives a concrete detail the viewer can use or repeat: a name, a place, a
  number, how it works.
- No asking for likes or subscribes until one short line at the very end.
- "show" describes ordinary gameplay that would exist in any playthrough footage (driving at night, a shootout,
  the map screen, a car garage), not a specific rare moment."""


SHORT_PROMPT = """You write viral numbered-list Shorts about {game} (YouTube Shorts, TikTok, Reels): one fast narrator over
real footage, about 60 seconds. Today is {today}.
{when}

{topic_line}
Shorts already made (pick something different): {history}

RESEARCH FIRST. Use web search. Every item must be true and checkable; say "reportedly" for anything not
official. Pick the items a fan would repeat to a friend: specific, surprising, concrete. Nothing generic
("better graphics") and nothing everybody already knows.

Then write ONE JSON object and nothing else:
{{"topic": "a few words",
 "title": "the Short's title, under 70 characters, like '5 Crazy Things In GTA 6 You Can't Do In GTA 5'",
 "headline": "the same promise in 4-8 words for the screen",
 "hook": "ONE spoken sentence that says the title's promise outright (e.g. 'Here's five crazy things you can do in GTA 6 that you could never do in GTA 5.')",
 "items": [{{"headline": "2-4 words", "say": "starts 'Number one.' then 2-3 punchy spoken sentences, 22-34 words, explaining exactly what it is and why it's wild", "show": "footage to show, 3-8 words"}}],
 "outro": "one short question to the viewer, then the words 'And in case you missed it...' so the video loops into its start",
 "caption": "the post's caption: one line that makes people comment",
 "hashtags": ["4-6 tags without #"],
 "sources": ["URLs"]}}

Exactly 5 items unless the topic gives fewer true ones (minimum 3). Spoken English, contractions, no filler, no
'in this video'. The whole narration is 150-185 words."""


def _claude_web(prompt: str, timeout_s: float = 1800, effort: str = "medium") -> str:
    """One `claude -p` turn on the membership login that may search the web. "" on failure."""
    import tempfile
    from core import gemini
    exe = gemini._claude_bin()
    if gemini._claude_off() or not exe or gemini._cooling(gemini.CLAUDE):
        return ""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    model = wv.settings().get("claude_model")
    with tempfile.TemporaryDirectory(prefix="jarvis-guide-") as tmp:
        try:
            r = subprocess.run([exe, "-p", "--output-format", "json", "--tools", "WebSearch,WebFetch",
                                "--allowedTools", "WebSearch,WebFetch", "--strict-mcp-config", "--setting-sources", "",
                                "--no-session-persistence", "--disable-slash-commands", "--effort", effort]
                               + (["--model", model] if model else []),
                               input=prompt, capture_output=True, text=True, cwd=tmp, env=env, timeout=timeout_s)
            out = json.loads(r.stdout)
        except Exception as exc:
            print(f"[guides] Claude: {type(exc).__name__}: {str(exc)[:120]}")
            return ""
    result = str(out.get("result") or "").strip()
    if out.get("is_error"):
        if "limit" in result.lower():
            gemini._cool(gemini.CLAUDE, gemini._CLAUDE_LIMIT_COOLDOWN_S)
        print(f"[guides] Claude: {result[:120]}")
        return ""
    return result


def clean_script(raw, minutes: float) -> dict:
    """Claude's script made safe to build from. Raises ValueError when there isn't a usable script in it."""
    if not isinstance(raw, dict):
        raise ValueError("no script")
    sections = []
    for s in raw.get("sections") or []:
        say = re.sub(r"\s+", " ", str((s or {}).get("say") or "")).strip() if isinstance(s, dict) else ""
        if say:
            sections.append({"heading": re.sub(r"\s+", " ", str(s.get("heading") or "")).strip()[:40],
                             "say": say, "show": re.sub(r"\s+", " ", str(s.get("show") or "gameplay")).strip()[:80]})
    words = sum(len(s["say"].split()) for s in sections)
    if len(sections) < 3 or words < max(150, minutes * WPM * .4):
        raise ValueError(f"the script is too thin ({len(sections)} sections, {words} words)")
    sections[0]["heading"] = ""
    title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()
    if not title:
        raise ValueError("no title")
    tags, seen = [], set()
    for t in raw.get("tags") or []:
        t = re.sub(r"[^\w\s'-]", "", str(t)).strip().lower()[:40]
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    return {"topic": str(raw.get("topic") or title)[:120], "title": title[:95],
            "thumbnail": re.sub(r"\s+", " ", str(raw.get("thumbnail") or "")).strip()[:28] or " ".join(title.split()[:3]),
            "description": re.sub(r"\s+", " ", str(raw.get("description") or "")).strip()[:600], "tags": tags[:14],
            "sections": sections, "sources": [str(u)[:300] for u in raw.get("sources") or [] if str(u).startswith("http")][:12]}


def clean_short(raw) -> dict:
    """A list Short's script in the same shape as a guide's: hook, one section per item, the outro."""
    if not isinstance(raw, dict):
        raise ValueError("no script")
    tidy = lambda v: re.sub(r"\s+", " ", str(v or "")).strip()
    items = [i for i in raw.get("items") or [] if isinstance(i, dict) and tidy(i.get("say"))]
    title, hook = tidy(raw.get("title")), tidy(raw.get("hook"))
    if len(items) < 3 or not title or not hook:
        raise ValueError(f"the short's script is too thin ({len(items)} items)")
    sections = [{"heading": (tidy(raw.get("headline")) or title).upper()[:60], "say": hook, "show": tidy(items[0].get("show")) or "gameplay"}]
    for n, it in enumerate(items[:7], 1):
        sections.append({"heading": f"{n}. {tidy(it.get('headline')).upper()[:28]}", "say": tidy(it["say"]),
                         "show": tidy(it.get("show"))[:80] or "gameplay"})
    if tidy(raw.get("outro")):
        sections.append({"heading": "", "say": tidy(raw["outro"]), "show": "the most exciting action"})
    words = sum(len(x["say"].split()) for x in sections)
    if not 90 <= words <= 260:
        raise ValueError(f"the short's narration is {words} words, not about a minute")
    tags, seen = [], set()
    for t in list(raw.get("hashtags") or []) + ["gta6", "gtavi", "gta", "gaming", "fyp"]:
        t = re.sub(r"[^a-z0-9]", "", str(t).lower())
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    return {"topic": tidy(raw.get("topic")) or title, "title": title[:90], "thumbnail": "", "description": tidy(raw.get("caption")) or title,
            "caption": tidy(raw.get("caption")) or title, "hashtags": tags[:9], "tags": tags[:9], "sections": sections,
            "sources": [str(u)[:300] for u in raw.get("sources") or [] if str(u).startswith("http")][:8]}


def when_line(cfg: dict, today: Optional[str] = None) -> str:
    launch = str(cfg.get("launch_date") or "")
    if launch and (today or datetime.now().date().isoformat()) < launch:
        have = sorted({v.get("title", "") for v in load_library()["sources"].values()} - {""})
        footage = ("The only footage we can show is: " + "; ".join(have) + ". Every \"show\" must describe something visible in those."
                   if have else "The only footage we can show is the official trailers, so every \"show\" must describe something visible in them.")
        return (f"IMPORTANT: the game is NOT out yet (it releases {launch}). What exists is official material only: Rockstar's "
                "trailers, the 27-minute 'Extended Look' gameplay presentation, Rockstar's own site, and previews by major outlets. "
                "State confirmed things as confirmed; anything from leaks or speculation must be called unconfirmed, or left out. "
                + footage)
    return f"The game released on {launch}; write from what players have actually found in it." if launch else ""


def minutes_for(cfg: dict, today: Optional[str] = None) -> float:
    before = (today or datetime.now().date().isoformat()) < str(cfg.get("launch_date") or "")
    return float(cfg["minutes_prelaunch"] if before and cfg.get("minutes_prelaunch") else cfg["minutes"])


def write_script(topic: str, cfg: dict, history: list, ask: Callable = _claude_web, kind: str = "guide") -> dict:
    if kind == "short":
        prompt = SHORT_PROMPT.format(game=cfg["game"], today=datetime.now().strftime("%B %d, %Y"), when=when_line(cfg),
                                     topic_line=(f"The topic: {topic}" if topic else "Pick the topic yourself: the list fans would most want to see this week."),
                                     history="; ".join(history[-40:]) or "none yet")
        text = ask(prompt, effort=cfg["claude_effort"])
        if not text:
            raise Busy("Claude isn't available to write the script")
        return clean_short(wv._json_from(text))
    prompt = SCRIPT_PROMPT.format(game=cfg["game"], today=datetime.now().strftime("%B %d, %Y"), when=when_line(cfg),
                                  topic_line=(f"The topic: {topic}" if topic else
                                              "Pick the topic yourself: search for what players are asking about and excited "
                                              "for in the game this week and choose the list the most people would click."),
                                  history="; ".join(history[-25:]) or "none yet", words=int(minutes_for(cfg) * WPM),
                                  items=max(5, round(minutes_for(cfg) * 60 / 32)))
    text = ask(prompt, effort=cfg["claude_effort"])
    if not text:
        raise Busy("Claude isn't available to write the script")
    return clean_script(wv._json_from(text), minutes_for(cfg))


# ── 2. the narration ─────────────────────────────────────────────────────────
def voice_groups(sections: list, max_words: int = 330) -> list[list[int]]:
    """Sections grouped into as few narrator requests as fit (the free tier allows about ten a day)."""
    groups, cur, n = [], [], 0
    for i, s in enumerate(sections):
        w = len(s["say"].split())
        if cur and n + w > max_words:
            groups.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += w
    return groups + ([cur] if cur else [])


def narrate(sections: list, job: Path, log: Callable = print) -> Path:
    """The whole narration as one wav (48 kHz mono)."""
    import numpy as np
    v = wv.settings()
    models = [v["tts_model"]] + [m for m in wv.TTS_MODELS if m != v["tts_model"]]
    parts = []
    for gi, group in enumerate(voice_groups(sections)):
        text = "\n\n".join(sections[i]["say"] for i in group)
        piece = job / f"voice_{gi:02d}.wav"
        for model in [m for m in models if m != "none"]:
            try:
                piece.write_bytes(wv.gemini_tts(text, model, v["voice"]))
                wv._tempo(piece, float(v.get("voice_speed") or 1))
                break
            except Exception as exc:
                log(f"guides: {model} voice failed ({str(exc)[:90]})")
        else:
            raise Busy("the narrator voice (Gemini TTS) isn't available right now")
        raw = subprocess.run([wv.ffmpeg(), "-loglevel", "error", "-i", str(piece), "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                             capture_output=True, check=True).stdout
        x = wv._trim(np.frombuffer(raw, dtype=np.float32), 48000)
        parts += [x, np.zeros(int(48000 * .45), dtype=np.float32)]
    whole = np.concatenate(parts)
    whole = whole / max(1e-6, float(np.abs(whole).max())) * .89
    out = job / "voice.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes((whole * 32767).astype("<i2").tobytes())
    return out


def section_times(sections: list, words: list[tuple], total: float) -> list[float]:
    """When each section starts in the narration: the script's word counts mapped onto the words Whisper heard."""
    counts = [len(s["say"].split()) for s in sections]
    all_words = max(1, sum(counts))
    starts, done = [], 0
    for c in counts:
        if not words:
            starts.append(total * done / all_words)
        else:
            k = min(len(words) - 1, round(done / all_words * len(words)))
            starts.append(0.0 if not done else max(0.0, words[k][0] - .15))
        done += c
    return starts


# ── 3. the footage ───────────────────────────────────────────────────────────
INDEX_WAIT_S = 70                                       # Gemini's free tier counts tokens per minute

INDEX_PROMPT = """Watch this whole video: footage of {game}. List every visually distinct stretch of it so an
editor can find footage by description later.

JSON only: {{"is_game": true or false,    // false if this is not real {game} footage (fan concept, mod, AI, another game)
 "clean": true or false,                 // false if a facecam, streamer overlay or big watermark covers most of it
 "shots": [{{"start": seconds, "end": seconds, "what": "8-16 words: who or what is on screen, where, doing what"}}]}}

Rules: each stretch 3-25 seconds, in order, no overlaps, covering the whole video. Leave out logos, title cards,
rating screens, black frames and frames that are mostly text. If a menu, map, phone or weapon wheel is on screen,
say so in "what"."""

MATCH_PROMPT = """You are the editor of a narrated {game} video. Pick footage for each section from the catalogue.

SECTIONS (number | seconds of footage needed | what the narrator is saying):
{sections}

CATALOGUE (id | seconds | what it shows):
{catalogue}

JSON only: {{"sections": [[ids for section 0], [ids for section 1], ...]}}
Rules: for each section pick the shots that SHOW what is being said, best match first, adding up to at least the
seconds needed and no more than twice that. Use an id at most once in the whole answer. If nothing in the
catalogue fits a section, pick good-looking general shots (driving, the city, action) rather than leaving it empty."""


def find_sources(cfg: dict, n: int, log: Callable = print) -> list[dict]:
    """Recent gameplay videos of the game that Gemini can watch (8-40 minutes, 1080p-looking titles first)."""
    from urllib.parse import quote_plus
    names = cs._list(cfg["game_names"])
    query = str(cfg["footage_search"]).format(game=cfg["game"])
    skip = ["trailer", "reaction", "review", "news", "leak", "meme", "compilation", "podcast", "explained", "ranking"]
    found = {}
    for url in (f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=CAMSBAgEEAE%253D",   # this month, most viewed
                f"https://www.youtube.com/results?search_query={quote_plus(query)}"):
        try:
            for e in cs.listing(url, 30):
                title = (e.get("title") or "").lower()
                if (e.get("id") and cs.is_game(title, names) and not any(s in title for s in skip)
                        and 8 * 60 <= (e.get("duration") or 0) <= 40 * 60 and e.get("live_status") not in ("is_live", "is_upcoming")):
                    found.setdefault(e["id"], e)
        except Exception as exc:
            log(f"guides: couldn't search for footage ({str(exc)[:70]})")
    plain = lambda e: ("no commentary" in (e.get("title") or "").lower(), e.get("view_count") or 0)
    return sorted(found.values(), key=plain, reverse=True)[:n * 3]


NOT_FOOTAGE = re.compile(r"\b(logos?|title cards?|rating|esrb|pegi|black screen|credits|release date|pre-?order|text (on|over) (a )?(black|screen))\b", re.I)


def clean_shots(raw, duration: float) -> list[dict]:
    """Gemini's catalogue of one video: [{"start", "end", "what"}], inside the video, 2-40 s each, not overlapping."""
    out = []
    if not isinstance(raw, dict):
        return out
    for s in raw.get("shots") or []:
        if not isinstance(s, dict):
            continue
        a, b = wv._num(s.get("start"), -1), wv._num(s.get("end"), -1)
        what = re.sub(r"\s+", " ", str(s.get("what") or "")).strip()[:160]
        if a < 0 or b - a < 2 or not what or (duration and b > duration + 1) or NOT_FOOTAGE.search(what):
            continue
        b = min(b, a + 40)
        if not any(a < o["end"] and o["start"] < b for o in out):
            out.append({"start": round(a, 1), "end": round(b, 1), "what": what})
    return out


def index_source(url: str, duration: float, game: str, log: Callable = print, trusted: bool = False) -> Optional[list]:
    """Gemini watches one video by its link and catalogues its shots. None = couldn't be reached."""
    from core import gemini
    from google.genai import types
    c = gemini.client(timeout_ms=900000)
    for model in ("gemini-3.5-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"):   # the better model twice: its
        try:                                                    # per-minute limit clears after a short wait
            r = c.models.generate_content(
                model=model, contents=[types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*")),
                                       INDEX_PROMPT.format(game=game)],
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW))
            raw = wv._json_from(r.text)
            if not trusted and isinstance(raw, dict) and (raw.get("is_game") is False or raw.get("clean") is False):
                return []                                       # not the game, or covered by an overlay: never use it
            shots = clean_shots(raw, duration)
            if len(shots) >= 3:
                return shots
            log(f"guides: {model} gave no usable catalogue of {url[-11:]}")
        except Exception as exc:
            log(f"guides: {model} couldn't catalogue {url[-11:]} ({str(exc)[:80]})")
            if "429" in str(exc):
                time.sleep(INDEX_WAIT_S)
    return None


def load_library() -> dict:
    try:
        lib = json.loads((LIB / "index.json").read_text())
    except (OSError, ValueError):
        lib = {}
    lib.setdefault("sources", {})
    return lib


def save_library(lib: dict) -> None:
    LIB.mkdir(parents=True, exist_ok=True)
    tmp = LIB / "index.tmp"
    tmp.write_text(json.dumps(lib, indent=1))
    tmp.replace(LIB / "index.json")


def official_sources(cfg: dict, log: Callable = print) -> list[dict]:
    """The publisher's own videos of the game, a minute or longer (trailers, the Extended Look), longest first."""
    from urllib.parse import quote_plus
    names = cs._list(cfg["game_names"])
    found = {}
    for ch in cs._list(cfg["official"]):
        try:
            for e in cs.listing(f"https://www.youtube.com/{ch}/search?query={quote_plus(names[0])}", 40):
                if e.get("id") and cs.is_game(e.get("title"), names) and (e.get("duration") or 0) >= 60:
                    found.setdefault(e["id"], dict(e, _official=True))
        except Exception as exc:
            log(f"guides: couldn't list {ch}'s videos ({str(exc)[:70]})")
    return sorted(found.values(), key=lambda e: -(e.get("duration") or 0))


def refresh_library(cfg: dict, log: Callable = print, today: Optional[str] = None, max_new: int = 4) -> dict:
    """Adds videos not catalogued yet: the official ones always; after launch, today's best gameplay videos too."""
    lib = load_library()
    today = today or datetime.now().date().isoformat()
    wanted = official_sources(cfg, log)
    if not any(lib["sources"].get(x) for x in cs._list(cfg.get("extra_sources"))):     # one stand-in is enough
        wanted += [{"id": x, "_official": True, "_standin": True} for x in cs._list(cfg.get("extra_sources"))]
    if today >= str(cfg.get("launch_date") or "") and lib.get("gameplay_day") != today:
        wanted += find_sources(cfg, int(cfg["sources"]), log)[:int(cfg["sources"]) * 3]
    added = added_gameplay = 0
    for e in wanted:
        if e["id"] in lib["sources"] or e["id"] in lib.get("rejected", []) or added >= max_new:
            continue
        if e.get("_official") and e.get("title") and any(v.get("title") == e["title"] for v in lib["sources"].values()):
            continue                                            # the same trailer uploaded twice
        if not e.get("_official") and added_gameplay >= int(cfg["sources"]):
            continue
        url = f"https://www.youtube.com/watch?v={e['id']}"
        try:
            info = cs.details(url)
        except Exception as exc:
            log(f"guides: couldn't open {e['id']} ({str(exc)[:70]})")
            continue
        if (info.get("height") or 0) < 1080:
            lib.setdefault("rejected", []).append(e["id"])
            continue
        if e.get("_standin") and any(lib["sources"].get(x) for x in cs._list(cfg.get("extra_sources"))):
            continue
        shots = index_source(url, info.get("duration") or 0, cfg["game"], log, trusted=bool(e.get("_official")) and not e.get("_standin"))
        if shots is None:
            continue                                            # Gemini is busy: next time
        if not shots:
            lib.setdefault("rejected", []).append(e["id"])
            continue
        lib["sources"][e["id"]] = {"url": url, "title": info.get("title") or "", "channel": "Rockstar Games" if e.get("_standin") else info.get("channel") or info.get("uploader") or "",
                                   "official": bool(e.get("_official")), "added": time.time(), "shots": shots}
        added += 1
        added_gameplay += 0 if e.get("_official") else 1
        log(f"guides: catalogued {len(shots)} shots of \"{(info.get('title') or '')[:50]}\"")
        save_library(lib)
    if today >= str(cfg.get("launch_date") or ""):
        lib["gameplay_day"] = today
    extra = sorted((k for k, v in lib["sources"].items() if not v.get("official")), key=lambda k: lib["sources"][k].get("added", 0))
    for k in extra[:-24] if len(extra) > 24 else []:            # keep the catalogue to recent gameplay
        lib["sources"].pop(k, None)
    lib["rejected"] = lib.get("rejected", [])[-200:]
    save_library(lib)
    return lib


def catalogue(lib: dict) -> list[dict]:
    """Every shot in the library as one flat list: [{"id", "source", "start", "end", "what"}]."""
    out = []
    for sid, src in lib["sources"].items():
        for sh in src["shots"]:
            out.append({"id": len(out), "source": sid, "start": sh["start"], "end": sh["end"], "what": sh["what"]})
    return out


def fallback_match(needs: list[float], shots: list[dict]) -> list[list[int]]:
    """No editor available: deal the shots out in order until each section has enough."""
    out, k = [], 0
    for need in needs:
        row, have = [], 0.0
        while have < need and shots:
            sh = shots[k % len(shots)]
            row.append(sh["id"])
            have += sh["end"] - sh["start"]
            k += 1
        out.append(row)
    return out


def clean_match(raw, needs: list[float], shots: list[dict]) -> list[list[int]]:
    """Gemini's picks: known ids, each used once; a section left short is topped up from the unused shots."""
    rows = raw.get("sections") if isinstance(raw, dict) else None
    by_id = {s["id"]: s for s in shots}
    used, out = set(), []
    for i, need in enumerate(needs):
        row = []
        for x in (rows[i] if isinstance(rows, list) and i < len(rows) and isinstance(rows[i], list) else []):
            try:
                x = int(x)
            except (TypeError, ValueError):
                continue
            if x in by_id and x not in used:
                used.add(x)
                row.append(x)
        out.append(row)
    spare = [s["id"] for s in shots if s["id"] not in used]
    for i, need in enumerate(needs):
        have = sum(by_id[x]["end"] - by_id[x]["start"] for x in out[i])
        while have < need and spare:
            x = spare.pop(0)
            out[i].append(x)
            have += by_id[x]["end"] - by_id[x]["start"]
    return out


def match_shots(sections: list, needs: list[float], shots: list[dict], game: str, log: Callable = print) -> list[list[int]]:
    """Which catalogue shots go under which section: Claude (the same model that wrote the script) edits,
    Gemini only if Claude is unavailable."""
    from core import gemini
    from google.genai import types
    prompt = MATCH_PROMPT.format(
        game=game, sections="\n".join(f"{i} | {n:.0f} | {s['say'][:400]} [show: {s['show']}]" for i, (s, n) in enumerate(zip(sections, needs))),
        catalogue="\n".join(f"{s['id']} | {s['end'] - s['start']:.0f} | {s['what']}" for s in shots))
    try:
        answer = wv.claude_run(prompt + "\n\nReply with the JSON only.", timeout_s=600)
        if answer:
            return clean_match(wv._json_from(answer), needs, shots)
        log("guides: Claude couldn't pick the footage, asking Gemini")
    except Exception as exc:
        log(f"guides: Claude's footage picks unreadable ({str(exc)[:80]}), asking Gemini")
    try:
        c = gemini.client(timeout_ms=180000)
        for model in ("gemini-3.5-flash", "gemini-3.5-flash-lite"):
            try:
                r = c.models.generate_content(model=model, contents=prompt,
                                              config=types.GenerateContentConfig(response_mime_type="application/json"))
                return clean_match(wv._json_from(r.text), needs, shots)
            except Exception as exc:
                log(f"guides: {model} couldn't pick the footage ({str(exc)[:80]})")
    except Exception as exc:
        log(f"guides: couldn't ask for footage picks ({str(exc)[:80]})")
    return clean_match({}, needs, shots)


def fetch_shot(lib: dict, shot: dict) -> Path:
    """One catalogue shot as a file, downloaded once and kept for the next videos."""
    dest = LIB / "clips" / f"{shot['source']}-{shot['start']:.0f}-{shot['end']:.0f}.mp4"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        cs.download(lib["sources"][shot["source"]]["url"], dest, (shot["start"], shot["end"]))
    os.utime(dest)
    return dest


def prune_library(max_bytes: int = LIB_MAX_BYTES) -> None:
    files = sorted((LIB / "clips").glob("*.mp4"), key=lambda f: f.stat().st_mtime) if (LIB / "clips").exists() else []
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if total <= max_bytes:
            break
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def gather_footage(sections: list, needs: list[float], cfg: dict, log: Callable = print) -> tuple[list[dict], list[dict]]:
    """Footage for every section from the library. Returns (pieces, credits): pieces = [{"path", "section",
    "seconds"}], credits = [{"channel", "url"}] for the videos actually used."""
    lib = refresh_library(cfg, log)
    shots = catalogue(lib)
    if not shots:
        raise Busy("the footage library is empty (Gemini couldn't catalogue any video yet)")
    by_id = {s["id"]: s for s in shots}
    pieces, used_sources = [], []
    for sec, (row, need) in enumerate(zip(match_shots(sections, needs, shots, cfg["game"], log), needs)):
        have = 0.0
        for x in row:
            if have >= need * 1.1:
                break
            try:
                f = fetch_shot(lib, by_id[x])
                secs = float(clips.probe(f)["duration"])
            except Exception as exc:
                log(f"guides: couldn't fetch a shot of {by_id[x]['source']} ({str(exc)[:70]})")
                continue
            pieces.append({"path": str(f), "section": sec, "seconds": secs})
            have += secs
            if by_id[x]["source"] not in used_sources:
                used_sources.append(by_id[x]["source"])
    if not pieces:
        raise RuntimeError("no footage could be downloaded")
    return pieces, [{"channel": lib["sources"][k]["channel"], "url": lib["sources"][k]["url"]} for k in used_sources]


def plan_shots(pieces: list[dict], starts: list[float], total: float, shot: float = SHOT) -> list[dict]:
    """The cut list: for each section, shots from its own footage first, then general footage, never the same
    stretch twice until everything has been used. [{"path", "at", "seconds", "zoom_in"}] covering `total` seconds."""
    pools = {}
    for p in pieces:
        offs = [round(o * shot, 2) for o in range(int(max(0, p["seconds"] - .3) // shot))] or [0.0]
        for at in offs:
            pools.setdefault(p["section"], []).append({"path": p["path"], "at": at, "max": max(1.5, min(shot, p["seconds"] - at))})
    everything = [s for k in sorted(pools) for s in pools[k]]
    used, out, n = set(), [], 0
    ends = starts[1:] + [total]

    def take(sec: int) -> dict:
        for pool in (pools.get(sec, []), pools.get(-1, []), everything):
            for s in pool:
                key = (s["path"], s["at"])
                if key not in used:
                    used.add(key)
                    return s
        used.clear()                                        # all of it has been shown once: go round again
        return take(sec)

    for sec, (a, b) in enumerate(zip(starts, ends)):
        left = b - a
        while left > .05:
            s = take(sec)
            d = min(s["max"], left)
            if left - d < 1.5:                              # don't leave a sliver for the next shot
                d = min(s["max"], left) if left <= s["max"] else left - 1.5
            out.append({"path": s["path"], "at": s["at"], "seconds": round(d, 2), "zoom_in": n % 2 == 0})
            left -= d
            n += 1
    return out


# ── 4. the edit ──────────────────────────────────────────────────────────────
def detect_bars(path: Path) -> str:
    """A crop ("w:h:x:y") that removes black cinema bars baked into the footage, or "" when there are none."""
    out = subprocess.run([wv.ffmpeg(), "-hide_banner", "-ss", "0.5", "-i", str(path), "-vf", "cropdetect=28:2:0", "-frames:v", "45",
                          "-f", "null", "-"], capture_output=True, text=True, timeout=120).stderr
    size = re.search(r"Stream #.*Video:.*?\b(\d{3,5})x(\d{3,5})\b", out)
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", out)
    if not size or not found:
        return ""
    w, h, x, y = (int(v) for v in found[-1])
    iw, ih = int(size.group(1)), int(size.group(2))
    if w < iw * .5 or h < ih * .5 or (w >= iw * .97 and h >= ih * .97):       # a dark scene, or nothing to remove
        return ""
    return f"{w}:{h}:{x}:{y}"


def black_seconds(path: Path) -> float:
    """How long the picture is (nearly) black in this file."""
    out = subprocess.run([wv.ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-vf", "blackdetect=d=0.15:pix_th=0.08", "-an",
                          "-f", "null", "-"], capture_output=True, text=True, timeout=120).stderr
    return sum(float(d) for d in re.findall(r"black_duration:([\d.]+)", out))


def shot_command(s: dict, out: Path, has_audio: bool, size: tuple = (W, H)) -> list[str]:
    w, h = size
    d = s["seconds"]
    z = f"(1+0.05*t/{d:.2f})" if s["zoom_in"] else f"(1.05-0.05*t/{d:.2f})"
    bars = f"crop={s['crop']}," if s.get("crop") else ""
    vf = (f"fps={FPS},{bars}scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
          f"scale=w='2*trunc({w // 2}*{z})':h='2*trunc({h // 2}*{z})':eval=frame,crop={w}:{h},setsar=1,format=yuv420p")
    cmd = [wv.ffmpeg(), "-loglevel", "error", "-y", "-ss", f"{s['at']:.2f}", "-t", f"{d:.2f}", "-i", s["path"]]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{d:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    return cmd + ["-vf", vf, "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-t", f"{d:.2f}",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                  "-video_track_timescale", "15360", str(out)]


ASS_HEAD = ("[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
            "MarginV, Encoding\n{styles}\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def _safe(t: str) -> str:
    return re.sub(r"[{}\\\n]", "", t)


def write_ass(chunks: list[tuple], headings: list[tuple], out: Path) -> Path:
    """A long video's text: captions low in the frame, and each section's heading top-left for its first seconds."""
    styles = ("Style: Cap,Impact,66,&H0021DEFF,&H0021DEFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,5,2,2,60,60,70,1\n"
              "Style: Head,Impact,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,0,0,0,0,100,100,1,0,3,14,0,7,60,60,56,1")
    lines = [f"Dialogue: 0,{clips._ass_time(a)},{clips._ass_time(b)},Cap,,0,0,0,,{_safe(t)}" for a, b, t in chunks]
    lines += [f"Dialogue: 1,{clips._ass_time(a)},{clips._ass_time(a + 4.5)},Head,,0,0,0,,{{\\fad(250,400)}}{_safe(t.upper())}"
              for a, t in headings if t]
    out.write_text(ASS_HEAD.format(w=1920, h=1080, styles=styles) + "\n".join(lines) + "\n")
    return out


def write_ass_short(chunks: list[tuple], headings: list[tuple], out: Path) -> Path:
    """A Short's text: each item's numbered headline big in the top third for as long as the item lasts (its number
    yellow, the words white), and the word-by-word captions in the middle. headings = [(from, to, text)]."""
    styles = ("Style: Cap,Impact,92,&H0021DEFF,&H0021DEFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,7,3,5,60,60,40,1\n"
              "Style: Head,Impact,104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,8,3,8,70,70,300,1")
    lines = [f"Dialogue: 0,{clips._ass_time(a)},{clips._ass_time(b)},Cap,,0,0,0,,{{\\pos(540,1200)\\fscx82\\fscy82\\t(0,70,\\fscx100\\fscy100)}}{_safe(t)}"
             for a, b, t in chunks]
    for a, b, t in headings:
        if not t:
            continue
        m = re.match(r"(\d+\.)\s*(.*)", t)
        text = f"{{\\c&H0021DEFF&}}{m.group(1)} {{\\c&H00FFFFFF&}}{_safe(m.group(2))}" if m else _safe(t)
        lines.append(f"Dialogue: 1,{clips._ass_time(a)},{clips._ass_time(b)},Head,,0,0,0,,{{\\fad(120,0)}}{text}")
    out.write_text(ASS_HEAD.format(w=1080, h=1920, styles=styles) + "\n".join(lines) + "\n")
    return out


def thumbnail(frame: Path, text: str, out: Path) -> Path:
    """1280x720: the still, punched up, with 2-4 huge words stacked on the left (yellow and white, heavy black
    edge) over a dark fade, and a yellow bar down the left side."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    img = Image.open(frame).convert("RGB")
    w, h = img.size
    side = min(w, int(h * 16 / 9))
    img = img.crop(((w - side) // 2, 0, (w - side) // 2 + side, h)).resize((1280, 720), Image.LANCZOS)
    img = ImageEnhance.Color(ImageEnhance.Contrast(img).enhance(1.18)).enhance(1.22)
    img = ImageEnhance.Sharpness(img).enhance(1.6)
    shade = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shade)
    for x in range(860):                                    # dark on the left, under the words
        ds.line([(x, 0), (x, 720)], fill=(0, 0, 0, int(225 * (1 - x / 860) ** 1.2)))
    img = Image.alpha_composite(img.convert("RGBA"), shade)
    words = text.upper().split()[:4] or ["GTA 6"]
    probe = ImageDraw.Draw(img)
    size = 250
    while size > 80:
        f = clips._font(clips.TITLE_FONT, size)
        if all(probe.textlength(wd, font=f) <= 760 for wd in words) and len(words) * size * 1.0 <= 650:
            break
        size -= 8
    edge = max(10, size // 9)
    layer = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    y = (720 - len(words) * size) / 2 - size * .06
    for i, wd in enumerate(words):
        d.text((64, y), wd, font=f, fill=clips.YELLOW if i % 2 == 0 else clips.WHITE, stroke_width=edge, stroke_fill=(0, 0, 0, 255))
        y += size
    glow = layer.filter(ImageFilter.GaussianBlur(14))       # a soft dark halo so the words lift off any background
    dark = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    dark.putalpha(glow.getchannel("A").point(lambda v: min(255, int(v * .8))))
    img = Image.alpha_composite(Image.alpha_composite(img, dark), layer)
    ImageDraw.Draw(img).rectangle((0, 0, 22, 720), fill=clips.YELLOW)
    img.convert("RGB").save(out, quality=93)
    return out


def chapters(sections: list, starts: list[float]) -> str:
    """YouTube chapter lines ("0:00 Intro"); YouTube needs at least three, the first at 0:00, 10+ seconds apart."""
    rows, last = [], -99.0
    for s, t in zip(sections, starts):
        t = 0.0 if not rows else t
        if t - last >= 10:
            rows.append(f"{int(t) // 60}:{int(t) % 60:02d} {s['heading'] or 'Intro'}")
            last = t
    return "\n".join(rows) if len(rows) >= 3 else ""


def site_names(urls: list) -> list[str]:
    """kotaku.com, forbes.com ... : where the facts came from, without the long links."""
    from urllib.parse import urlparse
    out = []
    for u in urls:
        host = re.sub(r"^www\.", "", urlparse(str(u)).netloc.lower())
        if host and host not in out:
            out.append(host)
    return out


def describe(script: dict, starts: list[float], credits: list[dict]) -> str:
    """The YouTube description: what the video covers, the chapters, then one short line each for the footage and
    the sources (no walls of links), and three hashtags."""
    parts = [script["description"], chapters(script["sections"], starts)]
    who = sorted({c["channel"] for c in credits if c.get("channel")})
    tail = []
    if who:
        tail.append("Footage: " + ", ".join(who))
    names = site_names(script.get("sources") or [])
    if names:
        tail.append("Sources: " + ", ".join(names[:8]))
    parts.append("\n".join(tail))
    tags = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in script.get("tags") or []]
    parts.append(" ".join("#" + t for t in list(dict.fromkeys(["gta6"] + [t for t in tags if t and len(t) <= 18]))[:3]))
    return "\n\n".join(p for p in parts if p)[:4900]


PEOPLE = re.compile(r"\b(jason|lucia|man|woman|men|women|character|face|close-?up|couple|person|people)\b", re.I)


def people_shots(files: list[Path]) -> list[Path]:
    """The library clips whose catalogue line says a person is on screen: faces make the best thumbnails."""
    what = {f"{c['source']}-{c['start']:.0f}-{c['end']:.0f}.mp4": c["what"] for c in catalogue(load_library())}
    return [f for f in files if PEOPLE.search(what.get(Path(f).name, ""))]


def best_frame(shot_files: list[Path], out: Path) -> Path:
    """The most eye-catching still for the thumbnail: a shot with a person in it when there is one, then bright,
    colourful and sharp. Taken from the footage itself, so there are no captions on it."""
    import numpy as np
    from PIL import Image
    best, best_score = None, -1.0
    shot_files = people_shots(shot_files) or shot_files
    step = max(1, len(shot_files) // 14)
    for k, f in enumerate(shot_files[::step][:14]):
        tmp = out.with_name(f"cand-{k}.png")
        bars = detect_bars(Path(f))
        r = subprocess.run([wv.ffmpeg(), "-loglevel", "error", "-y", "-ss", "1.2", "-i", str(f)] + (["-vf", f"crop={bars}"] if bars else [])
                           + ["-frames:v", "1", str(tmp)], capture_output=True, timeout=120)
        if r.returncode or not tmp.exists():
            continue
        x = np.asarray(Image.open(tmp).convert("RGB").resize((320, 180)), dtype=np.float32) / 255
        light = float(x.mean())
        colour = float((x.max(axis=2) - x.min(axis=2)).mean())
        g = x.mean(axis=2)
        sharp = float(np.abs(np.diff(g, axis=0)).mean() + np.abs(np.diff(g, axis=1)).mean())
        score = colour * 2 + sharp * 6 + (1 - abs(light - .5) * 2) * .6
        if score > best_score:
            if best:
                best.unlink(missing_ok=True)
            best, best_score = tmp, score
        else:
            tmp.unlink(missing_ok=True)
    if not best:
        raise RuntimeError("no frame for the thumbnail")
    best.replace(out)
    return out


def make(topic: str = "", cfg: Optional[dict] = None, log: Callable = print, resume: Optional[Path] = None,
         kind: str = "guide") -> dict:
    """One video, from the topic to the finished mp4 (+ thumbnail for a long one) and its .json. kind = "guide"
    (long, 16:9) or "short" (a numbered list, 9:16, about a minute)."""
    import tempfile
    import numpy as np
    cfg = cfg or settings()
    st = wv.load_state(STATE)
    short = kind == "short"
    job = resume or WORK / (datetime.now().strftime("%Y%m%d-%H%M%S") + ("-short" if short else ""))
    job.mkdir(parents=True, exist_ok=True)
    job = job.resolve()                                         # ffmpeg's shot list needs full paths
    sfile = job / "script.json"
    if sfile.exists():
        script = json.loads(sfile.read_text())
    else:
        script = write_script(topic, cfg, [v["title"] for v in st["videos"]], kind=kind)
        sfile.write_text(json.dumps(script, indent=1))
    sections = script["sections"]
    log(f"guides: script \"{script['title']}\" ({sum(len(s['say'].split()) for s in sections)} words, {len(sections)} sections)")

    voice = job / "voice.wav"
    if not voice.exists():
        narrate(sections, job, log)
    total = clips.wav_seconds(voice)
    words = clips.hear_words(voice, 0, total)
    starts = section_times(sections, words, total)
    ends = starts[1:] + [total]
    chunks = clips.caption_chunks(words, total)
    log(f"guides: narration {total:.0f}s, {len(chunks)} captions")

    pfile = job / "pieces.json"
    saved = json.loads(pfile.read_text()) if pfile.exists() else None
    if saved and all(Path(x["path"]).exists() for x in saved["pieces"]):
        pieces, credits = saved["pieces"], saved["credits"]
    else:
        pieces, credits = gather_footage(sections, [b - a for a, b in zip(starts, ends)], cfg, log)
        pfile.write_text(json.dumps({"pieces": pieces, "credits": credits}, indent=1))
    size = SIZES[kind]
    shots = plan_shots(pieces, starts, total, shot=3.0 if short else SHOT)
    shots_dir = job / "shots"
    shots_dir.mkdir(exist_ok=True)
    audio_of, bars_of, listing = {}, {}, []
    for i, s in enumerate(shots):
        if s["path"] not in audio_of:
            audio_of[s["path"]] = clips.probe(Path(s["path"]))["audio"]
            try:
                bars_of[s["path"]] = detect_bars(Path(s["path"]))
            except Exception:
                bars_of[s["path"]] = ""
        s["crop"] = bars_of[s["path"]]
        f = shots_dir / f"{i:04d}.mp4"
        spares = [x for x in pieces if x["path"] != s["path"] and x["seconds"] >= s["seconds"] + .2]
        for attempt in range(4):                                # a shot that fades to black is swapped for another
            done = subprocess.run(shot_command(s, f, audio_of.get(s["path"], True), size), capture_output=True, text=True, timeout=600)
            if done.returncode or not f.exists():
                raise RuntimeError(f"ffmpeg couldn't cut shot {i} ({done.stderr.strip()[-160:]})")
            if black_seconds(f) < .3 or not spares or attempt == 3:
                break
            alt = spares[(i + attempt) % len(spares)]
            if alt["path"] not in audio_of:
                audio_of[alt["path"]] = clips.probe(Path(alt["path"]))["audio"]
                bars_of[alt["path"]] = detect_bars(Path(alt["path"]))
            s = dict(s, path=alt["path"], at=0.0, crop=bars_of.get(alt["path"], ""))
            shots[i] = s
        listing.append(f"file '{f}'")
    (job / "shots.txt").write_text("\n".join(listing) + "\n")

    OUT.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", script["title"].lower()).strip("-")[:60]
    out = OUT / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.mp4"
    subs = Path(tempfile.gettempdir()) / f"jarvis-guide-{int(time.time() * 1000)}.ass"
    inputs = ["-f", "concat", "-safe", "0", "-i", str(job / "shots.txt"), "-i", str(voice)]
    bed = job / "music.wav"                                     # a driving bed under the voice, quieter in long videos
    if True:
        x = wv.music_drive(total, seed=int(time.time()) % 97)
        with wave.open(str(bed), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(wv.SR)
            w.writeframes((np.clip(x / max(1e-6, float(np.abs(x).max())), -1, 1) * 32767 * .9).astype("<i2").tobytes())
        inputs += ["-i", str(bed)]
    if short:
        write_ass_short(chunks, [(a, b, s["heading"]) for s, a, b in zip(sections, starts, ends)], subs)
        audio = ("[0:a]volume=0.05[g];[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0[n];[2:a]aresample=48000,pan=stereo|c0=c0|c1=c0,volume=0.20[m];"
                 "[g][n][m]amix=inputs=3:duration=shortest:normalize=0,alimiter=limit=0.8[a]")
    else:
        write_ass(chunks, [(t, s["heading"]) for s, t in zip(sections, starts)], subs)
        audio = ("[0:a]volume=0.08[g];[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0[n];[2:a]aresample=48000,pan=stereo|c0=c0|c1=c0,volume=0.12[m];"
                 "[g][n][m]amix=inputs=3:duration=shortest:normalize=0,alimiter=limit=0.8[a]")
    try:
        done = subprocess.run(
            [wv.ffmpeg(), "-loglevel", "error", "-y"] + inputs +
            ["-filter_complex", f"[0:v]subtitles=f={subs}:fontsdir=/System/Library/Fonts/Supplemental,format=yuv420p[v];" + audio,
             "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20" if short else "22",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], capture_output=True, text=True, timeout=7200)
    finally:
        subs.unlink(missing_ok=True)
    if done.returncode or not out.exists():
        raise RuntimeError(f"ffmpeg couldn't build the video ({done.stderr.strip()[-200:]})")
    prune_library()

    rec = {"kind": kind, "path": str(out), "job": str(job), "title": script["title"], "topic": script["topic"],
           "tags": script["tags"], "seconds": round(total), "made": time.time()}
    footage = ("Footage: " + ", ".join(sorted({c["channel"] for c in credits if c["channel"]}))) if credits else ""
    if short:
        tags = " ".join("#" + t for t in script["hashtags"])
        rec["caption"] = f"{script['caption']} {tags}".strip()[:2000]
        rec["description"] = "\n\n".join(x for x in (script["caption"], footage, tags if "#shorts" in tags else "#shorts " + tags) if x)[:4900]
    else:
        thumb = out.with_suffix(".jpg")
        frame = best_frame(list(dict.fromkeys(Path(x["path"]) for x in pieces)), job / "frame.png")
        thumbnail(frame, script["thumbnail"], thumb)
        rec["thumb"] = str(thumb)
        rec["description"] = describe(script, starts, credits)
    if not short and cfg.get("vertical_copy"):
        try:
            rec["vertical"] = str(vertical_copy(job, shots, audio_of, sections, starts, ends, chunks, voice, total, out))
            tags = " ".join("#" + t for t in dict.fromkeys(["gta6", "gtavi"] + [re.sub(r"[^a-z0-9]", "", t.lower()) for t in script["tags"][:3]] + ["gaming", "fyp"]) if t)
            rec["caption"] = f"{script['title']} {tags}"[:2000]
        except Exception as exc:
            log(f"guides: couldn't make the tall copy ({str(exc)[:120]})")
    out.with_suffix(".json").write_text(json.dumps(rec, indent=1))
    log(f"guides: made \"{script['title']}\" ({total / 60:.1f} min, {len(shots)} shots)")
    return rec


def vertical_copy(job: Path, shots: list, audio_of: dict, sections: list, starts: list, ends: list, chunks: list, voice: Path,
                  total: float, out: Path) -> Path:
    """The same long video cut tall (1080x1920) for TikTok and Instagram: the same shots and narration, the
    section headings big at the top, the captions in the middle."""
    import tempfile
    vdir = job / "shots_v"
    vdir.mkdir(exist_ok=True)
    listing = []
    for i, s in enumerate(shots):
        f = vdir / f"{i:04d}.mp4"
        done = subprocess.run(shot_command(s, f, audio_of.get(s["path"], True), SIZES["short"]), capture_output=True, text=True, timeout=600)
        if done.returncode or not f.exists():
            raise RuntimeError(f"ffmpeg couldn't cut tall shot {i} ({done.stderr.strip()[-120:]})")
        listing.append(f"file '{f}'")
    (job / "shots_v.txt").write_text("\n".join(listing) + "\n")
    tall = out.with_name(out.stem + "-tall.mp4")
    subs = write_ass_short(chunks, [(a, min(b, a + 5), s["heading"]) for s, a, b in zip(sections, starts, ends)],
                           Path(tempfile.gettempdir()) / f"jarvis-guide-tall-{int(time.time() * 1000)}.ass")
    try:
        done = subprocess.run(
            [wv.ffmpeg(), "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(job / "shots_v.txt"), "-i", str(out),
             "-filter_complex", f"[0:v]subtitles=f={subs}:fontsdir=/System/Library/Fonts/Supplemental,format=yuv420p[v]",
             "-map", "[v]", "-map", "1:a", "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
             "-c:a", "copy",                                # the long video's finished sound (voice, game, music)
             "-movflags", "+faststart", str(tall)], capture_output=True, text=True, timeout=7200)
    finally:
        subs.unlink(missing_ok=True)
    if done.returncode or not tall.exists():
        raise RuntimeError(f"ffmpeg couldn't build the tall copy ({done.stderr.strip()[-160:]})")
    return tall


# ── 5. YouTube ───────────────────────────────────────────────────────────────
class WrongChannel(RuntimeError):
    """Studio is on some channel other than the guides' own: never upload there."""


def _context(p, visible: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = ["--disable-blink-features=AutomationControlled"]
    if not visible:
        args.append("--window-position=-2400,0")
    return p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False, args=args,
                                                viewport={"width": 1400, "height": 950}, ignore_default_args=["--enable-automation"])


def logged_in(ctx) -> bool:
    return any(c["name"] == "LOGIN_INFO" and c.get("value") for c in ctx.cookies("https://www.youtube.com"))


def youtube_login(wait_s: int = 900) -> bool:
    from playwright.sync_api import sync_playwright
    with wv.BROWSER, sync_playwright() as p:
        ctx = _context(p, visible=True)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto("https://accounts.google.com/ServiceLogin?service=youtube&continue=https://studio.youtube.com/")
        end = time.time() + wait_s
        while time.time() < end:
            if logged_in(ctx) and "studio.youtube.com" in pg.url:
                time.sleep(5)
                ctx.close()
                return True
            time.sleep(2)
        ctx.close()
    return False


def youtube_upload(rec: dict, cfg: Optional[dict] = None, log: Callable = print, dry_run: bool = False) -> str:
    """Uploads through YouTube Studio. Returns the video's link (or a dry-run note); raises on failure (screenshot kept)."""
    from playwright.sync_api import sync_playwright
    cfg = cfg or settings()
    want = str(cfg["channel_id"]).strip()
    with wv.BROWSER, sync_playwright() as p:
        ctx = _context(p, visible=False)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        shot = WORK / f"youtube-{int(time.time())}.png"
        try:
            if not logged_in(ctx):
                raise wv.NotLoggedIn("YouTube isn't logged in (run: python3 -m worker.guides login)")
            pg.goto("https://studio.youtube.com/", wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_url(re.compile(r"studio\.youtube\.com/channel/"), timeout=60000)
            if not want or f"/channel/{want}" not in pg.url:    # never upload to any other channel
                raise WrongChannel(f"Studio opened {pg.url[:80]}, not the guides' channel {want or '?'}")
            pg.goto(f"https://studio.youtube.com/channel/{want}/videos/upload?d=ud", wait_until="domcontentloaded", timeout=60000)
            pg.locator("input[type=file]").first.set_input_files(rec["path"], timeout=60000)
            title = pg.locator("#title-textarea #textbox").first
            title.wait_for(state="visible", timeout=120000)
            pg.wait_for_timeout(2500)
            title.click()
            pg.keyboard.press("Meta+A")
            pg.keyboard.type(rec["title"][:100], delay=6)
            desc = pg.locator("#description-textarea #textbox").first
            desc.click()
            pg.keyboard.insert_text(rec["description"][:4900])
            if rec.get("thumb") and Path(rec["thumb"]).exists():
                try:                                            # needs a phone-verified channel; skipped if not offered
                    pg.locator("input#file-loader[type=file], ytcp-thumbnail-uploader input[type=file]").first.set_input_files(rec["thumb"], timeout=8000)
                except Exception:
                    log("guides: YouTube didn't take a custom thumbnail (the channel may need phone verification)")
            pg.locator("tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']").first.click(timeout=30000)
            try:                                                # tags and the altered-content answer sit under "Show more"
                pg.locator("#toggle-button").first.click(timeout=8000)
                pg.wait_for_timeout(1200)
                tags = pg.locator("#tags-container input#text-input, ytcp-form-input-container#tags-container input").first
                tags.click(timeout=8000)
                pg.keyboard.insert_text(",".join(rec.get("tags") or [])[:450] + ",")
            except Exception:
                log("guides: couldn't add the tags")
            for _ in range(3):                                  # details -> video elements -> checks -> visibility
                pg.locator("#next-button").first.click(timeout=30000)
                pg.wait_for_timeout(2500)
            vis = str(cfg["visibility"]).upper()
            pg.locator(f"tp-yt-paper-radio-button[name='{vis}']").first.click(timeout=30000)
            link = ""
            try:
                link = pg.locator("a[href*='youtu.be/'], a[href*='youtube.com/shorts/']").first.get_attribute("href", timeout=8000) or ""
            except Exception:
                pass
            end = time.time() + 3600                            # wait for the file itself to finish uploading
            while time.time() < end:
                text = (pg.locator("span.progress-label, ytcp-video-upload-progress").first.inner_text(timeout=10000) or "").lower()
                if "uploading" not in text and "%" not in text:
                    break
                pg.wait_for_timeout(4000)
            if dry_run:
                pg.screenshot(path=str(shot))
                return f"dry run: ready to publish as {vis.lower()} on channel {want} ({link or 'no link yet'}; screenshot {shot})"
            pg.locator("#done-button").first.click(timeout=30000)
            pg.wait_for_timeout(8000)
            pg.screenshot(path=str(shot))
            return link or "posted"
        except Exception:
            try:
                pg.screenshot(path=str(shot))
            except Exception:
                pass
            raise
        finally:
            ctx.close()


def youtube_edit(video_id: str, description: str = "", thumb: str = "", cfg: Optional[dict] = None, log: Callable = print) -> str:
    """Changes a posted video's description and/or thumbnail in YouTube Studio. Returns what happened."""
    from playwright.sync_api import sync_playwright
    cfg = cfg or settings()
    notes = []
    with wv.BROWSER, sync_playwright() as p:
        ctx = _context(p, visible=False)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        shot = WORK / f"youtube-edit-{int(time.time())}.png"
        try:
            if not logged_in(ctx):
                raise wv.NotLoggedIn("YouTube isn't logged in (run: python3 -m worker.guides login)")
            pg.goto(f"https://studio.youtube.com/video/{video_id}/edit", wait_until="domcontentloaded", timeout=60000)
            box = pg.locator("#description-textarea #textbox").first
            box.wait_for(state="visible", timeout=60000)
            pg.wait_for_timeout(2500)
            if description:
                box.click()
                pg.keyboard.press("Meta+A")
                pg.keyboard.press("Backspace")
                pg.keyboard.insert_text(description[:4900])
                notes.append("description set")
            if thumb and Path(thumb).exists():
                try:
                    pg.locator("input#file-loader[type=file], ytcp-thumbnail-uploader input[type=file], ytcp-thumbnails-compact-editor-uploader input[type=file]").first.set_input_files(thumb, timeout=8000)
                    pg.wait_for_timeout(5000)
                    notes.append("thumbnail sent")
                except Exception:
                    notes.append("no thumbnail upload offered (the channel needs phone verification)")
            pg.wait_for_timeout(1500)
            save = pg.locator("#save").first
            if save.get_attribute("disabled") is None and save.get_attribute("aria-disabled") != "true":
                save.click(timeout=15000)
                pg.wait_for_timeout(6000)
                notes.append("saved")
            pg.screenshot(path=str(shot), full_page=False)
            return "; ".join(notes) + f" (screenshot {shot})"
        except Exception:
            try:
                pg.screenshot(path=str(shot))
            except Exception:
                pass
            raise
        finally:
            ctx.close()


# ── the daily slots ──────────────────────────────────────────────────────────
KINDS = (("guide", "time", "slots"), ("short", "short_times", "short_slots"))


def remove_files(rec: dict) -> None:
    """A posted video's files go (the disk is nearly full); its record stays."""
    video = Path(rec["path"])
    if video.parent == OUT:
        for f in (video, video.with_suffix(".json"), video.with_suffix(".jpg"), video.with_name(video.stem + "-tall.mp4")):
            f.unlink(missing_ok=True)
    job = Path(rec.get("job") or "")
    if job.parent == WORK:
        shutil.rmtree(job, ignore_errors=True)


class GuideService:
    PASS_SECONDS = 120
    MAX_TRIES = 2

    def __init__(self, log: Callable[[str], None] = print, notify: Optional[Callable[[str, str], None]] = None):
        self.log, self.notify = log, notify or (lambda kind, text: None)
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._poster = wv.ViralService(log, notify)             # TikTok + Instagram posting for the shorts

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="game-guides").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.PASS_SECONDS):
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"guides: pass failed: {exc!r}")

    def run_once(self, now: Optional[datetime] = None) -> str:
        cfg = settings()
        now = now or datetime.now()
        st = wv.load_state(STATE)
        if (not cfg["enabled"] or now.date().isoformat() < str(cfg["start_date"] or "") or time.time() < st.get("busy_until", 0)
                or not self._busy.acquire(blocking=False)):
            return ""
        try:
            today = now.date().isoformat()
            for kind, times_key, slots_key in KINDS:
                done = (st.get(slots_key) or {}).get(today, [])
                due = wv.due_slots(cfg[times_key], done, now, late_ok_h=6 if kind == "guide" else 3)
                if due:
                    return self._produce(kind, slots_key, today, done + due, cfg)
            return ""
        finally:
            self._busy.release()

    def _produce(self, kind: str, slots_key: str, today: str, done: list, cfg: dict) -> str:
        st = wv.load_state(STATE)
        queue_key = "topic_queue" if kind == "guide" else "short_queue"
        topic = (st.get(queue_key) or [""])[0]
        try:
            rec = make(topic, cfg, self.log, kind=kind)
        except Busy as exc:
            st = wv.load_state(STATE)
            st["tries"] = st.get("tries", 0) + 1
            if st["tries"] >= self.MAX_TRIES:
                st["tries"], st[slots_key] = 0, {today: done}
                self.log(f"guides: {exc}; gave up on this {kind}")
            else:
                st["busy_until"] = time.time() + 30 * 60
                self.log(f"guides: {exc}; trying again in 30 minutes")
            wv.save_state(st, STATE)
            return "waiting"
        except Exception as exc:
            st = wv.load_state(STATE)
            st[slots_key] = {today: done}
            wv.save_state(st, STATE)
            self.log(f"guides: couldn't make the {kind}: {exc}")
            self.notify("guides", f"🎮 Couldn't make a GTA {kind} video: {str(exc)[:160]}")
            return "failed"
        st = wv.load_state(STATE)
        st[slots_key], st["tries"] = {today: done}, 0
        if topic:
            st[queue_key] = (st.get(queue_key) or [])[1:]
        st["videos"] = (st["videos"] + [rec])[-300:]
        wv.save_state(st, STATE)
        return self.deliver(rec, cfg)

    def deliver(self, rec: dict, cfg: dict) -> str:
        from core import phone_alerts
        short = rec.get("kind") == "short"
        if not cfg["post"]:
            phone_alerts.send_text(f"🎮 Made a GTA video (not posted, posting is off): \"{rec['title']}\"\n{rec['path']}")
            return "off"
        lines, ok = [], True
        try:
            link = youtube_upload(rec, cfg, self.log)
            lines.append(f"YouTube: ✅ {link}")
        except Exception as exc:
            self.log(f"guides: YouTube upload failed: {exc}")
            link, ok = "", False
            lines.append(f"YouTube: ❌ {str(exc)[:110]}")
        tall = rec if short else dict(rec, path=rec["vertical"]) if rec.get("vertical") and Path(rec["vertical"]).exists() else None
        if tall:                                                # the Short (or the long video's tall copy) on TikTok and Instagram
            pcfg = clips.post_settings(dict(cfg, ai_label=False))
            tt = self._poster._deliver_tiktok(tall, pcfg)
            ig = self._poster.post_instagram(tall, pcfg) if pcfg.get("instagram") else "off"
            rec.setdefault("_why", {}).update(tall.get("_why", {}))
            why = rec.get("_why", {})
            lines.append("TikTok: ✅" if tt == "posted" else f"TikTok: ❌ {why.get('tiktok', tt)}")
            if ig != "off":
                lines.append("Instagram: ✅" if ig == "posted" else f"Instagram: ❌ {why.get('instagram', ig)}")
            ok = ok and tt == "posted" and ig in ("posted", "off")
        st = wv.load_state(STATE)
        for v in st["videos"]:
            if v.get("path") == rec["path"]:
                v.update(posted=bool(link), link=link)
                v.pop("_why", None)
        wv.save_state(st, STATE)
        self.log(f"guides: {rec['title']}: " + "; ".join(lines))
        try:
            length = f"{rec['seconds']} s" if short else f"{rec['seconds'] // 60} min"
            phone_alerts.send_text(f"🎮 GTA {'short' if short else 'video'}: \"{rec['title']}\" ({length})\n" + "\n".join(lines)
                                   + ("" if ok else f"\nThe file is kept: {rec['path']}"))
        except Exception as exc:
            self.log(f"guides: couldn't text the post note: {exc}")
        if ok:
            remove_files(rec)
        return "posted" if ok else "partly" if link else "failed"


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    cfg = settings()
    opt = lambda name: argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None
    if cmd == "login":
        print("A Chrome window is opening: log in to the guides' YouTube channel there (it waits up to 15 minutes).")
        print("Logged in." if youtube_login() else "Timed out before you logged in.")
        return 0
    if cmd in ("topic", "shorttopic"):
        st = wv.load_state(STATE)
        key = "topic_queue" if cmd == "topic" else "short_queue"
        st.setdefault(key, []).append(" ".join(argv[1:]))
        wv.save_state(st, STATE)
        print("next topics:", st[key])
        return 0
    if cmd == "library":
        lib = refresh_library(cfg)
        for sid, src in lib["sources"].items():
            print(f"{len(src['shots']):4d} shots  {'official' if src.get('official') else 'gameplay'}  {src['title'][:70]}")
        return 0
    if cmd in ("make", "short"):
        if opt("--minutes"):
            cfg["minutes"] = float(opt("--minutes"))
        if opt("--game"):
            cfg["game"] = cfg["game_names"] = opt("--game")
        skip = {opt("--minutes"), opt("--game"), opt("--resume")}
        topic = " ".join(a for a in argv[1:] if not a.startswith("--") and a not in skip)
        rec = make(topic, cfg, resume=Path(opt("--resume")) if opt("--resume") else None, kind="short" if cmd == "short" else "guide")
        print(json.dumps(rec, indent=1))
        if cfg["post"] and "--no-post" not in argv:
            st = wv.load_state(STATE)
            st["videos"].append(rec)
            wv.save_state(st, STATE)
            print(GuideService().deliver(rec, cfg))
        return 0
    if cmd == "upload" and len(argv) >= 2:
        rec = json.loads(Path(argv[1]).with_suffix(".json").read_text())
        print(youtube_upload(rec, cfg, dry_run="--dry-run" in argv))
        return 0
    for v in wv.load_state(STATE)["videos"][-10:]:
        print(datetime.fromtimestamp(v["made"]).strftime("%m-%d %H:%M"), v.get("link") or "not posted", v["title"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
