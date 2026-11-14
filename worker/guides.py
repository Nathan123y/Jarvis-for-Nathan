"""Game guides: long narrated YouTube videos about the game (GTA 6 at launch), one a day, on the guides' own channel.

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

    python3 -m worker.guides make ["topic"] [--minutes 2] [--game "GTA 5"] [--no-post]
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
            "start_date": "2026-11-19",                 # launch day: nothing true can be said about the game before it
            "time": "16:00",                            # one video a day
            "post": True,
            "game": "GTA 6",
            "game_names": "Grand Theft Auto VI,Grand Theft Auto 6,GTA VI,GTA 6,GTA6",
            "minutes": 7,                               # narration length to aim for (8+ minutes allows mid-roll ads,
                                                        # but a day's free narrator requests are shared with the shorts)
            "channel_id": "UCpdCqccEXBYmMkSq-xbD3Sg",   # the ONLY channel Jarvis may upload to
            "visibility": "public",
            "claude_effort": "medium",
            "sources": 3,                               # gameplay videos Gemini watches for footage
            "footage_search": "{game} gameplay no commentary"}
WPM = 165                                               # the narrator at speed 1.08
SHOT = 6.0


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
SCRIPT_PROMPT = """You write long-form YouTube videos for a gaming channel about {game}: clear, fast, useful guides
and explainers narrated over real gameplay. Today is {today}.

{topic_line}
Videos already made (pick something different): {history}

RESEARCH FIRST. Use web search to find what is actually true about this topic right now, from several sources
(the publisher's own site, big gaming outlets, wikis, widely-upvoted player posts). Every fact, number, name,
location and price in the script must come from what you found. If sources disagree or something is unconfirmed,
say so in the narration ("players report...", "not confirmed yet"). Never invent a detail to fill space; a
shorter true script beats a longer padded one.

Then write the script as ONE JSON object and nothing else:
{{"topic": "the topic in a few words",
 "title": "the YouTube title, under 70 characters: specific, searchable, says what the viewer gets. No ALL CAPS words, no emojis",
 "thumbnail": "2-4 words for the thumbnail, different from the title",
 "description": "2-3 plain sentences on what the video covers",
 "tags": ["8-12 search phrases"],
 "sections": [
   {{"heading": "", "say": "THE HOOK, 2-4 sentences: the single most useful or surprising thing in the video, stated
      straight away, then what the rest covers. No greeting, no 'in this video', no 'welcome back'.",
     "show": "what the footage should show, 3-8 words"}},
   {{"heading": "a short chapter name, 2-5 words", "say": "this section's narration, 4-9 sentences", "show": "..."}}
 ],
 "sources": ["the URLs the facts came from"]}}

Rules for the narration ("say"):
- About {words} words in total across 5-9 sections. Spoken English: short sentences, contractions, no lists read
  aloud, no "firstly/secondly", numbers written the way they're said.
- Each section delivers something the viewer can use or repeat; open each with its point, not a wind-up.
- No asking for likes or subscribes until one short line at the very end.
- "show" describes ordinary gameplay that would exist in any playthrough footage (driving at night, a shootout,
  the map screen, a car garage), not a specific rare moment."""


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


def write_script(topic: str, cfg: dict, history: list, ask: Callable = _claude_web) -> dict:
    prompt = SCRIPT_PROMPT.format(game=cfg["game"], today=datetime.now().strftime("%B %d, %Y"),
                                  topic_line=(f"The topic: {topic}" if topic else
                                              "Pick the topic yourself: search for what players are asking about the game "
                                              "this week and choose the one question the most people need answered."),
                                  history="; ".join(history[-25:]) or "none yet", words=int(float(cfg["minutes"]) * WPM))
    text = ask(prompt, effort=cfg["claude_effort"])
    if not text:
        raise Busy("Claude isn't available to write the script")
    return clean_script(wv._json_from(text), float(cfg["minutes"]))


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
FOOTAGE_PROMPT = """Watch this whole video. It should be clean gameplay footage of {game}.
I'm editing a narrated guide and need stretches of this footage to put under each section of the narration.

The sections (number: what the footage should show):
{sections}

Answer with JSON only:
{{"is_game": true or false,     // false if this is not mostly real {game} gameplay
 "clean": true or false,       // false if a facecam, a streamer's overlay, big burned-in text or a watermark covers
                               // the picture for most of the video
 "spans": [{{"section": number, "start": seconds, "end": seconds}}]}}

Rules: 2-4 spans per section where the video has something fitting, each 12-40 seconds. Also add 6-10 spans with
"section": -1 of the best-looking general gameplay (driving, action, scenery) to use anywhere. No menus, loading
screens, cutscenes with subtitles, or stretches where nothing moves. Spans must not overlap."""


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


def clean_spans(raw, duration: float, n_sections: int) -> list[dict]:
    out = []
    if not isinstance(raw, dict) or not raw.get("is_game") or raw.get("clean") is False:
        return out
    for s in raw.get("spans") or []:
        if not isinstance(s, dict):
            continue
        a, b, sec = wv._num(s.get("start"), -1), wv._num(s.get("end"), -1), int(wv._num(s.get("section"), -1))
        if a < 0 or b - a < 6 or (duration and b > duration + 1):
            continue
        b = min(b, a + 40)
        if not any(a < o["end"] and o["start"] < b for o in out):
            out.append({"section": sec if 0 <= sec < n_sections else -1, "start": round(a), "end": round(b)})
    return out


def watch_source(url: str, duration: float, sections: list, game: str, log: Callable = print) -> Optional[list]:
    """Gemini watches one gameplay video and matches stretches to sections. None = couldn't be reached."""
    from core import gemini
    from google.genai import types
    listing = "\n".join(f"{i}: {s['show']}" + (f" ({s['heading']})" if s["heading"] else "") for i, s in enumerate(sections))
    c = gemini.client(timeout_ms=600000)
    for model in ("gemini-3.5-flash", "gemini-3.5-flash-lite"):
        try:
            r = c.models.generate_content(
                model=model, contents=[types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*")),
                                       FOOTAGE_PROMPT.format(game=game, sections=listing)],
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW))
            return clean_spans(wv._json_from(r.text), duration, len(sections))
        except Exception as exc:
            log(f"guides: {model} couldn't watch {url[-11:]} ({str(exc)[:80]})")
    return None


def gather_footage(sections: list, need_s: float, job: Path, cfg: dict, log: Callable = print) -> tuple[list[dict], list[dict]]:
    """Downloads matched stretches until there's enough to cover the narration. Returns (pieces, credits):
    pieces = [{"path", "section", "seconds"}], credits = [{"channel", "url"}]."""
    foot = job / "footage"
    foot.mkdir(parents=True, exist_ok=True)
    pieces, credits, watched, reached = [], [], 0, False
    for e in find_sources(cfg, int(cfg["sources"]), log):
        have = sum(p["seconds"] for p in pieces)
        if watched >= int(cfg["sources"]) or have >= need_s * 1.15:
            break
        url = f"https://www.youtube.com/watch?v={e['id']}"
        try:
            info = cs.details(url)
            if (info.get("height") or 0) < 1080:
                continue
        except Exception as exc:
            log(f"guides: couldn't open {e['id']} ({str(exc)[:70]})")
            continue
        spans = watch_source(url, info.get("duration") or 0, sections, cfg["game"], log)
        if spans is None:
            continue
        reached = True
        watched += 1
        got = 0
        spans.sort(key=lambda sp: sp["section"] < 0)            # section-matched stretches first, general ones after
        for sp in spans:
            if sum(p["seconds"] for p in pieces) >= need_s * 1.3:   # enough: every download costs time
                break
            dest = foot / f"{e['id']}-{sp['start']}.mp4"
            try:
                cs.download(url, dest, (sp["start"], sp["end"]))
                pieces.append({"path": str(dest), "section": sp["section"], "seconds": float(clips.probe(dest)["duration"])})
                got += 1
            except Exception as exc:
                log(f"guides: couldn't fetch {e['id']} at {sp['start']}s ({str(exc)[:70]})")
        if got:
            credits.append({"channel": info.get("channel") or info.get("uploader") or "", "url": url})
            log(f"guides: {got} stretches of footage from \"{(info.get('title') or '')[:50]}\"")
    if not pieces:
        raise Busy("Gemini couldn't watch any footage") if not reached else RuntimeError("no usable gameplay footage was found")
    return pieces, credits


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
def shot_command(s: dict, out: Path, has_audio: bool) -> list[str]:
    d = s["seconds"]
    z = f"(1+0.05*t/{d:.2f})" if s["zoom_in"] else f"(1.05-0.05*t/{d:.2f})"
    vf = (f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"scale=w='2*trunc({W // 2}*{z})':h='2*trunc({H // 2}*{z})':eval=frame,crop={W}:{H},setsar=1,format=yuv420p")
    cmd = [wv.ffmpeg(), "-loglevel", "error", "-y", "-ss", f"{s['at']:.2f}", "-t", f"{d:.2f}", "-i", s["path"]]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{d:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    return cmd + ["-vf", vf, "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-t", f"{d:.2f}",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                  "-video_track_timescale", "15360", str(out)]


def write_ass(chunks: list[tuple], headings: list[tuple], out: Path) -> Path:
    """Captions low in the frame, and each section's heading top-left for its first seconds."""
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
            "MarginV, Encoding\n"
            "Style: Cap,Impact,66,&H0021DEFF,&H0021DEFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,5,2,2,60,60,70,1\n"
            "Style: Head,Impact,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,0,0,0,0,100,100,1,0,3,14,0,7,60,60,56,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    safe = lambda t: re.sub(r"[{}\\\n]", "", t)
    lines = [f"Dialogue: 0,{clips._ass_time(a)},{clips._ass_time(b)},Cap,,0,0,0,,{safe(t)}" for a, b, t in chunks]
    lines += [f"Dialogue: 1,{clips._ass_time(a)},{clips._ass_time(a + 4.5)},Head,,0,0,0,,{{\\fad(250,400)}}{safe(t.upper())}"
              for a, t in headings if t]
    out.write_text(head + "\n".join(lines) + "\n")
    return out


def thumbnail(frame: Path, text: str, out: Path) -> Path:
    from PIL import Image, ImageDraw, ImageEnhance
    img = ImageEnhance.Contrast(Image.open(frame).convert("RGB").resize((1280, 720))).enhance(1.15)
    img = ImageEnhance.Color(img).enhance(1.25)
    shade = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shade)
    for x in range(760):                                    # darker on the left, under the words
        ds.line([(x, 0), (x, 720)], fill=(0, 0, 0, int(190 * (1 - x / 760))))
    img = Image.alpha_composite(img.convert("RGBA"), shade)
    d = ImageDraw.Draw(img)
    words = text.upper().split()
    size = 190
    while size > 70:
        f = clips._font(clips.TITLE_FONT, size)
        if all(d.textlength(w, font=f) <= 700 for w in words) and len(words) * size * 1.05 <= 640:
            break
        size -= 8
    y = (720 - len(words) * size * 1.05) / 2
    for i, w in enumerate(words):
        d.text((50, y), w, font=f, fill=clips.YELLOW if i % 2 == 0 else clips.WHITE, stroke_width=max(6, size // 14), stroke_fill=(0, 0, 0, 255))
        y += size * 1.05
    img.convert("RGB").save(out, quality=92)
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


def describe(script: dict, starts: list[float], credits: list[dict]) -> str:
    parts = [script["description"], chapters(script["sections"], starts)]
    if credits:
        parts.append("Gameplay footage from:\n" + "\n".join(f"{c['channel']} - {c['url']}" for c in credits))
    if script["sources"]:
        parts.append("Sources:\n" + "\n".join(script["sources"][:8]))
    return "\n\n".join(p for p in parts if p)[:4900]


def make(topic: str = "", cfg: Optional[dict] = None, log: Callable = print, resume: Optional[Path] = None) -> dict:
    """One guide, from the topic to the finished mp4 + thumbnail + its .json (title, description, tags)."""
    import tempfile
    cfg = cfg or settings()
    st = wv.load_state(STATE)
    job = resume or WORK / datetime.now().strftime("%Y%m%d-%H%M%S")
    job.mkdir(parents=True, exist_ok=True)
    sfile = job / "script.json"
    if sfile.exists():
        script = json.loads(sfile.read_text())
    else:
        script = write_script(topic, cfg, [v["title"] for v in st["videos"]])
        sfile.write_text(json.dumps(script, indent=1))
    sections = script["sections"]
    log(f"guides: script \"{script['title']}\" ({sum(len(s['say'].split()) for s in sections)} words, {len(sections)} sections)")

    voice = job / "voice.wav"
    if not voice.exists():
        narrate(sections, job, log)
    total = clips.wav_seconds(voice)
    words = clips.hear_words(voice, 0, total)
    starts = section_times(sections, words, total)
    chunks = clips.caption_chunks(words, total)
    log(f"guides: narration {total:.0f}s, {len(chunks)} captions")

    pfile = job / "pieces.json"
    if pfile.exists():
        saved = json.loads(pfile.read_text())
        pieces, credits = saved["pieces"], saved["credits"]
    else:
        pieces, credits = gather_footage(sections, total, job, cfg, log)
        pfile.write_text(json.dumps({"pieces": pieces, "credits": credits}, indent=1))
    shots = plan_shots(pieces, starts, total)
    shots_dir = job / "shots"
    shots_dir.mkdir(exist_ok=True)
    audio_of = {}
    listing = []
    for i, s in enumerate(shots):
        if s["path"] not in audio_of:
            audio_of[s["path"]] = clips.probe(Path(s["path"]))["audio"]
        f = shots_dir / f"{i:04d}.mp4"
        done = subprocess.run(shot_command(s, f, audio_of[s["path"]]), capture_output=True, text=True, timeout=600)
        if done.returncode or not f.exists():
            raise RuntimeError(f"ffmpeg couldn't cut shot {i} ({done.stderr.strip()[-160:]})")
        listing.append(f"file '{f}'")
    (job / "shots.txt").write_text("\n".join(listing) + "\n")

    OUT.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", script["title"].lower()).strip("-")[:60]
    out = OUT / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.mp4"
    subs = write_ass(chunks, [(t, s["heading"]) for s, t in zip(sections, starts)],
                     Path(tempfile.gettempdir()) / f"jarvis-guide-{int(time.time() * 1000)}.ass")
    try:
        done = subprocess.run(
            [wv.ffmpeg(), "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(job / "shots.txt"), "-i", str(voice),
             "-filter_complex",
             f"[0:v]subtitles=f={subs}:fontsdir=/System/Library/Fonts/Supplemental,format=yuv420p[v];"
             f"[0:a]volume=0.10[g];[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0[n];"
             f"[g][n]amix=inputs=2:duration=shortest:normalize=0,alimiter=limit=0.89[a]",
             "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], capture_output=True, text=True, timeout=7200)
    finally:
        subs.unlink(missing_ok=True)
    if done.returncode or not out.exists():
        raise RuntimeError(f"ffmpeg couldn't build the video ({done.stderr.strip()[-200:]})")

    frame, thumb = job / "frame.png", out.with_suffix(".jpg")
    at = starts[1] + 2 if len(starts) > 1 else total / 3   # a frame from the first real section, without captions on it
    k = next((i for i, x in enumerate(shots) if sum(y["seconds"] for y in shots[:i + 1]) > at), 0)
    subprocess.run([wv.ffmpeg(), "-loglevel", "error", "-y", "-ss", "1", "-i", str(shots_dir / f"{k:04d}.mp4"), "-frames:v", "1", str(frame)],
                   check=True, timeout=120)
    thumbnail(frame, script["thumbnail"], thumb)
    rec = {"path": str(out), "thumb": str(thumb), "job": str(job), "title": script["title"], "topic": script["topic"],
           "description": describe(script, starts, credits), "tags": script["tags"], "seconds": round(total), "made": time.time()}
    out.with_suffix(".json").write_text(json.dumps(rec, indent=1))
    log(f"guides: made \"{script['title']}\" ({total / 60:.1f} min, {len(shots)} shots)")
    return rec


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


# ── the daily slot ───────────────────────────────────────────────────────────
class GuideService:
    PASS_SECONDS = 120
    MAX_TRIES = 2

    def __init__(self, log: Callable[[str], None] = print, notify: Optional[Callable[[str, str], None]] = None):
        self.log, self.notify = log, notify or (lambda kind, text: None)
        self._stop = threading.Event()
        self._busy = threading.Lock()

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
            done = st["slots"].get(today, [])
            due = wv.due_slots(cfg["time"], done, now, late_ok_h=6)
            if not due:
                return ""
            topic = (st.get("topic_queue") or [""])[0]
            try:
                rec = make(topic, cfg, self.log, resume=Path(st["resume"]) if st.get("resume") and Path(st["resume"]).exists() else None)
            except Busy as exc:
                st = wv.load_state(STATE)
                st["tries"] = st.get("tries", 0) + 1
                if st["tries"] >= self.MAX_TRIES:
                    st["tries"], st["slots"] = 0, {today: done + due}
                    self.log(f"guides: {exc}; gave up on today's guide")
                else:
                    st["busy_until"] = time.time() + 45 * 60
                    self.log(f"guides: {exc}; trying again in 45 minutes")
                wv.save_state(st, STATE)
                return "waiting"
            except Exception as exc:
                st = wv.load_state(STATE)
                st["slots"] = {today: done + due}
                wv.save_state(st, STATE)
                self.log(f"guides: couldn't make today's guide: {exc}")
                self.notify("guides", f"🎮 Couldn't make today's GTA guide video: {str(exc)[:160]}")
                return "failed"
            st = wv.load_state(STATE)
            st["slots"], st["tries"] = {today: done + due}, 0
            st["topic_queue"] = (st.get("topic_queue") or [])[1:] if topic else st.get("topic_queue") or []
            st["videos"] = (st["videos"] + [rec])[-200:]
            wv.save_state(st, STATE)
            return self.deliver(rec, cfg)
        finally:
            self._busy.release()

    def deliver(self, rec: dict, cfg: dict) -> str:
        from core import phone_alerts
        if not cfg["post"]:
            phone_alerts.send_text(f"🎮 Made a guide (not posted, posting is off): \"{rec['title']}\"\n{rec['path']}")
            return "off"
        try:
            link = youtube_upload(rec, cfg, self.log)
        except Exception as exc:
            self.log(f"guides: upload failed: {exc}")
            try:
                phone_alerts.send_text(f"🎮 Made \"{rec['title']}\" but YouTube didn't take it: {str(exc)[:140]}\nThe file: {rec['path']}")
            except Exception:
                pass
            return "failed"
        st = wv.load_state(STATE)
        for v in st["videos"]:
            if v.get("path") == rec["path"]:
                v.update(posted=True, link=link)
        wv.save_state(st, STATE)
        self.log(f"guides: posted to YouTube: {rec['title']} {link}")
        try:
            phone_alerts.send_text(f"🎮 Just posted on YouTube: \"{rec['title']}\" ({rec['seconds'] // 60} min)\n{link}")
        except Exception as exc:
            self.log(f"guides: couldn't text the post note: {exc}")
        job = Path(rec.get("job") or "")
        if job.parent == WORK:                                  # the big work folder goes; the finished video stays
            shutil.rmtree(job, ignore_errors=True)
        return "posted"


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    cfg = settings()
    opt = lambda name: argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None
    if cmd == "login":
        print("A Chrome window is opening: log in to the guides' YouTube channel there (it waits up to 15 minutes).")
        print("Logged in." if youtube_login() else "Timed out before you logged in.")
        return 0
    if cmd == "topic":
        st = wv.load_state(STATE)
        st.setdefault("topic_queue", []).append(" ".join(argv[1:]))
        wv.save_state(st, STATE)
        print("next topics:", st["topic_queue"])
        return 0
    if cmd == "make":
        if opt("--minutes"):
            cfg["minutes"] = float(opt("--minutes"))
        if opt("--game"):
            cfg["game"] = cfg["game_names"] = opt("--game")
        skip = {opt("--minutes"), opt("--game"), opt("--resume")}
        topic = " ".join(a for a in argv[1:] if not a.startswith("--") and a not in skip)
        rec = make(topic, cfg, resume=Path(opt("--resume")) if opt("--resume") else None)
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
