"""Viral videos: 3D "what if" shorts, made start to finish on this Mac and posted to TikTok.

Each video:
  1. Claude writes the script: a hook question, 10-16 short narrated beats with real numbers, an on-screen
     counter (HUD), a twist, and a line that loops back into the hook.
  2. Gemini's TTS voices each beat; the beat timings come from the real speech lengths.
  3. Claude writes the 3D scene (a JS module) against the kit in assets/viral/kit.js, timed to those beats.
  4. A test render catches errors (fed back to Claude to fix), then Claude looks at sample frames and fixes
     what looks wrong.
  5. Headless Chrome renders every frame on the GPU; numpy mixes voice + music + sound effects; ffmpeg encodes.
  6. It posts to TikTok through a Chrome profile that the user logged into once (`python3 -m worker.viral login`).

    python3 -m worker.viral make ["topic"]     make one video now (and post it, if posting is on)
    python3 -m worker.viral queue ["topic"]    make the next slot's video now, text it to you, post it at the slot
    python3 -m worker.viral login              log in to TikTok once, in a Chrome window
    python3 -m worker.viral post FILE "caption" post a finished video
    python3 -m worker.viral status             the last videos
"""
from __future__ import annotations

import base64
import json
import math
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

ASSETS = ROOT / "assets" / "viral"
STATE = ROOT / "memory" / "viral.json"
WORK = ROOT / "memory" / "viral"                    # one folder per video: script, voice, scene, frames
OUT = Path.home() / "Movies" / "Jarvis Viral"       # the finished mp4s
PROFILE = ROOT / "memory" / "tiktok_profile"        # the Chrome profile logged into TikTok
NS = "viral_videos"
FPS = 30
SR = 48000
DEFAULTS = {"enabled": True,
            "times": "11:00,16:00,20:30",           # one video at each (TikTok's busy hours)
            "post": True,                           # post to TikTok; False = text the video to the user instead
            "voice_mode": "none",                   # none (captions + sound, like the reference videos) | gemini | edge
            "voice": "Charon",                      # Gemini TTS voice, when voice_mode is gemini
            "tts_model": "gemini-3.8-flash-tts",
            "ai_label": True,
            "handle": "whatifishowedu"}                # the TikTok account, for reading view counts                       # TikTok requires the AI-generated label on AI-made videos
CHROME_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"]


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS and v is not None})
    return out


def load_state(path: Path = STATE) -> dict:
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("videos", [])
    st.setdefault("slots", {})
    return st


def save_state(st: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ── 1. the script ────────────────────────────────────────────────────────────
SCRIPT_PROMPT = """You write viral short-form videos for a faceless TikTok account. Each is a 3D simulation shown
mostly first-person, with NO narrator: the story is told by short on-screen captions, a big counter (HUD), and real
sound (ambience + sound effects). Two reference videos that went viral:

1) "What if a spoonful of neutron star landed in your backyard?" - a countdown, then captions "A teaspoon of neutron
star just landed." "It weighs about a billion tons." (mass counter racing up) "The ground can't hold it up." "It rips
the hose out of your hand." "Everything nearby starts sliding in." Twist: "Viral videos say it falls to the core."
"But that's not what would happen." "Rewind. Here's the real version." Ends on an end card that loops to the hook.
2) "What if humans evolved 1,000 years every 3 seconds?" (33 s). COLD OPEN: it starts on the wildest moment
(+10,000 years, a crowd of strange people), then snaps back to 0 years on a normal city sidewalk, coffee in hand.
Captions are tiny and punchy: "1,000 years every 3 seconds." "37 generations. Just like that." "Everyone changes.
Except you." "Wisdom teeth? Gone." "New artery in every arm." "The pigeons didn't change." "They start staring."
"You're a living fossil." "Every single one of them..." "...descends from you." Only city ambience, footsteps, and a
scream at the end. No music, no voice.

Video formats (pick the one that fits):
- WHAT IF: an impossible scenario played out with real science ("What if the Moon was 10x closer?").
- VERSUS: a hypothetical matchup or survival test ("Could prime LeBron James beat Satan in a 1v1?", "Could 100 men
  beat a gorilla?", "How long would you last on Venus?"). Real public figures are fine in harmless, fun matchups
  (never insulting, sexual or defamatory); play it like a real broadcast with stats on the HUD and a twist ending.
  The title IS the question, worded the way a viewer would type it: "Can Prime LeBron Beat Satan in a 1v1?" (never a
  statement like "Prime LeBron vs Satan. Game to 11."). Right after the hook, introduce each side with REAL stats on
  the HUD: a real person gets their best season, named ("PRIME LEBRON - 2012-13 MIAMI", "26.8 PPG - 8.0 REB - 7.3 AST",
  "56.5% FG - MVP - CHAMPION"; one stat line per beat, numbers must be correct); the other side gets its
  legendary/scientific numbers ("SATAN - AGE: ETERNAL", "HEIGHT: ABOUT 9 FT"). Then play the matchup out.
WHAT PEOPLE WATCH: fantasy, absurd, funny and unrealistic beats realistic every time. Never a boring science lesson
or a dry "what happens to your body" video. The idea alone must make someone stop scrolling and laugh or go "wait,
what?". Mix famous people, mythical creatures, gods, monsters, aliens, animals with powers and everyday life, and
play it completely straight like a real broadcast or documentary (that's the joke). Real-feeling numbers on the
HUD, escalating chaos, a funny or shocking twist, an ending that makes people comment who'd win / argue / tag a
friend. Examples of the energy: "Can Prime LeBron Beat Satan in a 1v1?", "Could 100 Men Beat Prime Mike Tyson?",
"What if Zeus joined the NBA for one season?", "Can Shaq survive one round against a T-rex?", "What if your cat
got 1,000x stronger every day?", "Could Gordon Ramsay survive a dinner shift in Hell?", "What if pigeons started
paying rent?", "Could Usain Bolt outrun a meteor?", "What if gravity turned off for 10 seconds at a Lakers game?".
{topic_line}
Don't repeat these recent videos:
{history}

TikTok rules so the account isn't banned: edgy, dark, creepy, gross and shocking are all fine; no sexual content,
no gore close-ups, nothing that teaches self-harm or dangerous stunts, no hate.

What the 3D kit can show: places - suburban backyard, busy daytime city sidewalk (crowds, pigeons, cafe tables,
first-person hand holding a coffee or phone), city street with towers, open ocean, flat plain (desert, beach, snow,
Mars, Moon), deep space (planets, stars, sun, black holes), small bedroom, indoor basketball arena with fans,
boxing ring with crowd. Things - realistic 3D humans (men and women, skin tones, hair, beards, shirts/jerseys with
numbers, shorts, boxing gloves, devil skin with horns and glowing eyes; motion-captured walk, sprint, jump, punch,
jab, get hit, fall/die, roll, dance, sit, cast a spell, swim, drive, cheer, dunk, shoot), cheering crowds, cars,
houses, trees, a basketball, glowing objects, beams, meteors. Effects - explosions, fire, smoke, sparks, debris,
camera shake, flashes. Not possible: talking faces, realistic animals besides pigeons, liquids splashing, signs.

Rules for the script:
- 30-50 seconds total: 12-20 beats. Beat 0 is the hook: the title, at most 12 words, shown over a COLD OPEN of
  the most dramatic moment when that works; beat 1 then snaps back to the start ("Rewind." / "0 years." / "Day 1.").
- Every "say" is the on-screen caption: 2-9 words, punchy, concrete, real numbers. Facts must be correct; rough
  estimates say "about". Escalate every beat. Around 60% through, a twist ("Most videos get this wrong." / "But
  that's not what happens."). The last caption lands the payoff and loops back into the hook.
- About half the beats set a "hud": {{"label": "HUMAN EVOLUTION", "kind": "count", "from": 0, "to": 1000,
  "unit": " years", "decimals": 0, "sub": "GENERATION 37"}}, or kind "timer" (counts from "from" seconds to 0 during
  the beat; good for a countdown), or kind "text" with "text": "ROUND 1". A HUD number must match its caption.
- "emph": 1-2 words of the caption shown in blue.
- Sound, per beat: "ambience" = the id of a background sound from the list below that starts at this beat and keeps
  playing until another beat changes it ("none" = silence, "keep" = carry on); "level" = its loudness from this beat
  (0.3 hushed .. 1 normal .. 2.5 roaring), e.g. swell the crowd on the big play. "sfx" = one sound effect id from the
  list (a one-shot, landing on the beat) or a synth effect: boom, whoosh, riser, hit, tick, rumble, bounce (ball on
  hardwood), dribble (three bounces), swish (net), buzzer (arena horn), squeak (sneaker), punch, whistle (referee),
  bell (boxing bell); or "none".
  "sfx2" = up to 3 more hits later in the same beat: [{{"id": "...", "at": 1.2}}] (seconds after the beat starts).
  SOUND MUST MATCH THE PICTURE LITERALLY: every sound is what you would really hear at that moment in that place, at
  that intensity. A dunk in a packed arena = a roaring crowd, never polite or golf clapping; a quiet library = near
  silence; fire = crackling flames. Read each sound's description, energy, fits and avoid notes, and never use a
  sound whose "avoid" matches the moment. If nothing in the list really fits, use "none" or a synth effect: a missing
  sound is better than a wrong one. Sound effects only on real on-screen events (impacts, reveals, the ending). "music": "tension" for a quiet dark drone under the whole video,
  or "none" (the references have none; use it only for space or empty places).
{sounds}
- "scene": one sentence on what we see during that beat (camera and action).
- "end_fact": one line of small print for the end card (a real fact backing it up).
- "caption": the TikTok caption, at most 150 characters, ending in a question that baits comments.
- "hashtags": 4-6, lowercase, without the # (mix big ones like fyp with topic ones).

Reply with only this JSON:
{{"format": "whatif|versus", "topic": "...", "title": "...", "highlight": ["..."],
  "set": "backyard|street|city|ocean|plain|space|room|court|ring", "music": "none|tension",
  "beats": [{{"say": "...", "emph": ["..."], "hud": null, "ambience": "keep", "level": 1, "sfx": "none", "sfx2": [],
             "scene": "..."}}],
  "end_fact": "...", "caption": "...", "hashtags": ["..."]}}"""


def _json_from(text: str):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a:b + 1])


def claude_run(prompt: str, images: Optional[list] = None, timeout_s: float = 900, effort: str = "medium") -> str:
    """One `claude -p` turn on the membership login (never an API key), with time for long code. "" on failure."""
    import os
    import tempfile
    from core import gemini
    exe = gemini._claude_bin()
    if gemini._claude_off() or not exe or gemini._cooling(gemini.CLAUDE):
        return ""
    with tempfile.TemporaryDirectory(prefix="jarvis-viral-") as tmp:
        names = []
        for i, img in enumerate(images or []):
            name = f"frames-{i + 1}{Path(img).suffix}"
            shutil.copy(img, Path(tmp) / name)
            names.append(name)
        if names:
            prompt = "First read these image files in the current folder: " + ", ".join(names) + "\n\n" + prompt
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        try:
            r = subprocess.run([exe, "-p", "--output-format", "json", "--system-prompt", gemini._ONE_SHOT_SYSTEM,
                                "--tools", "Read" if names else "", "--strict-mcp-config", "--setting-sources", "",
                                "--no-session-persistence", "--disable-slash-commands", "--effort", effort],
                               input=prompt, capture_output=True, text=True, cwd=tmp, env=env, timeout=timeout_s)
            out = json.loads(r.stdout)
        except Exception as exc:
            print(f"[viral] Claude: {type(exc).__name__}: {str(exc)[:120]}")
            return ""
    result = str(out.get("result") or "").strip()
    if out.get("is_error"):
        if "limit" in result.lower():
            gemini._cool(gemini.CLAUDE, gemini._CLAUDE_LIMIT_COOLDOWN_S)
        print(f"[viral] Claude: {result[:120]}")
        return ""
    return result


def ask_claude(prompt: str, timeout_s: float = 900, images: Optional[list] = None) -> str:
    """Claude on the membership; Gemini (3.5 flash first, not Claude again) if Claude is out."""
    out = claude_run(prompt, images, timeout_s)
    if out:
        return out
    from core import gemini
    contents = prompt
    if images:
        from google.genai import types
        contents = [types.Part.from_bytes(data=Path(p).read_bytes(), mime_type="image/jpeg") for p in images] + [prompt]
    return gemini.text(contents, tier="gemini-3.5-flash", timeout_ms=int(timeout_s * 1000)) or ""


SETS = ("backyard", "street", "city", "ocean", "plain", "space", "room", "court", "ring")


def clean_script(s: dict, library: Optional[dict] = None) -> dict:
    library = library if library is not None else {x["id"]: x for x in sound_library()}
    if not isinstance(s, dict) or not s.get("title") or not isinstance(s.get("beats"), list) or len(s["beats"]) < 5:
        raise ValueError("the script came back incomplete")

    def amb(v):
        v = str(v or "keep")
        return v if v in ("keep", "none") or library.get(v, {}).get("kind") == "ambient" else "keep"

    def fx(v):
        v = str(v or "none")
        return v if v in SFX or v in library else "none"

    def more(v):              # extra hits inside the beat: [{"id": ..., "at": seconds after the beat starts}]
        out = []
        for x in (v if isinstance(v, list) else []):
            sid, at = (x.get("id"), x.get("at", 0)) if isinstance(x, dict) else (x, 0)
            try:
                at = max(0.0, min(4.0, float(at or 0)))
            except (TypeError, ValueError):
                at = 0.0
            if fx(sid) != "none":
                out.append({"id": fx(sid), "at": at})
        return out[:3]

    def level(v):
        try:
            return max(.3, min(2.5, float(v)))
        except (TypeError, ValueError):
            return 1.0

    beats = [{"say": str(b.get("say") or "").strip(), "emph": [str(x) for x in (b.get("emph") or [])][:3],
              "hud": b.get("hud") if isinstance(b.get("hud"), dict) else None,
              "ambience": amb(b.get("ambience")), "level": level(b.get("level", 1)), "sfx": fx(b.get("sfx")),
              "sfx2": more(b.get("sfx2")), "scene": str(b.get("scene") or "")}
             for b in s["beats"] if isinstance(b, dict) and str(b.get("say") or "").strip()]
    beats = beats[:22]
    if beats[0]["say"].strip().lower() != str(s["title"]).strip().lower():
        beats.insert(0, {"say": s["title"], "emph": [], "hud": None, "ambience": "keep", "level": 1.0, "sfx": "none", "sfx2": [],
                         "scene": "opening"})
    for b in beats:
        h = b["hud"]
        if h:
            h["kind"] = h.get("kind") if h.get("kind") in ("count", "timer", "text") else "text"
            for k in ("from", "to"):
                try:
                    h[k] = float(h.get(k) or 0)
                except (TypeError, ValueError):
                    h[k] = 0.0
            if h["kind"] != "text" and max(abs(h["from"]), abs(h["to"])) >= 1e15:
                h["kind"], h["text"] = "text", h.get("sub") or ""
    tags = [re.sub(r"[^a-z0-9_]", "", str(t).lower().lstrip("#")) for t in s.get("hashtags") or []]
    return {"format": "versus" if s.get("format") == "versus" else "whatif",
            "topic": str(s.get("topic") or s["title"]), "title": str(s["title"]).strip(),
            "highlight": [str(x) for x in (s.get("highlight") or [])][:2],
            "set": s.get("set") if s.get("set") in SETS else "backyard",
            "music": "tension" if s.get("music") == "tension" else "none",
            "beats": beats, "end_fact": str(s.get("end_fact") or "")[:160],
            "caption": str(s.get("caption") or s["title"])[:150], "hashtags": [t for t in tags if t][:6]}


def write_script(history: list, topic: str = "", ask: Callable = ask_claude) -> dict:
    topic_line = f"This video's topic: {topic}" if topic else "Choose the topic yourself."
    hist = "\n".join(f"- {h}" for h in history[-40:]) or "- (none yet)"
    last = None
    for _ in range(2):
        try:
            return clean_script(_json_from(ask(SCRIPT_PROMPT.format(topic_line=topic_line, history=hist,
                                                                    sounds=sounds_text()), timeout_s=300)))
        except Exception as exc:                 # one more try on a malformed reply
            last = exc
    raise RuntimeError(f"couldn't get a script: {last}")


# ── 2. the voice ─────────────────────────────────────────────────────────────
TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")   # 10 free requests a day each
EDGE_VOICE = "en-US-ChristopherNeural"


def gemini_tts(text: str, model: str, voice: str) -> bytes:
    """Text to wav bytes with Gemini's TTS. The text goes in bare: any style note in the prompt gets read out
    loud, and these models refuse a system instruction."""
    from core import gemini
    from google.genai import types
    client = gemini.client(timeout_ms=180000)                # keep a reference: a dropped client closes mid-request
    r = client.models.generate_content(
        model=model, contents=text,
        config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
    data = r.candidates[0].content.parts[0].inline_data.data
    if data[:4] == b"RIFF":
        return data
    import io
    buf = io.BytesIO()                                    # raw 24 kHz PCM
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(data)
    return buf.getvalue()


def split_by_pauses(x, rate: int, words: list[int], min_gap: float = .18):
    """Cuts one recording of n lines into n pieces at its n-1 longest pauses. None when the pieces don't
    match the lines (a line's share of the speech must be close to its share of the words)."""
    import numpy as np
    n = len(words)
    if n == 1:
        return [x]
    hop = int(rate * .02)
    rms = np.array([np.sqrt(np.mean(x[i:i + hop] ** 2)) for i in range(0, len(x) - hop, hop)])
    if not len(rms):
        return None
    quiet = rms < max(.004, np.percentile(rms, 90) * .06)
    runs, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * .02 >= min_gap and i > 0 and j < len(quiet):
                runs.append((j - i, i, j))
            i = j
        else:
            i += 1
    if len(runs) < n - 1:
        return None
    cuts = sorted(((i + j) // 2) * hop for _, i, j in sorted(runs, reverse=True)[:n - 1])
    pieces = [x[a:b] for a, b in zip([0] + cuts, cuts + [len(x)])]
    total_w, total_t = sum(words), sum(len(p) for p in pieces)
    for p, w in zip(pieces, words):
        share_t, share_w = len(p) / total_t, w / total_w
        if not (share_w * .45 <= share_t <= share_w * 2.2 + .02):
            return None
    return pieces


def edge_tts(text: str, out: Path) -> None:
    import asyncio
    import edge_tts as et
    mp3 = out.with_suffix(".mp3")
    asyncio.run(et.Communicate(text, EDGE_VOICE, rate="-6%").save(str(mp3)))
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-i", str(mp3), "-ac", "1", "-ar", "24000", str(out)], check=True)
    mp3.unlink(missing_ok=True)


def say_tts(text: str, out: Path) -> None:
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-v", "Daniel", "-r", "165", "-o", str(aiff), text], check=True, timeout=60)
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "24000", str(out)], check=True)
    aiff.unlink(missing_ok=True)


def _write_wav(path: Path, x, rate: int) -> None:
    import numpy as np
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def voice_lines(lines: list[str], job: Path, cfg: dict, log: Callable = print) -> list[Path]:
    """Every line in ONE voice. Gemini reads the whole narration in one request (its free tier allows 10 a
    day), cut into lines at the pauses; else Microsoft's Edge voice line by line; else the Mac's `say`."""
    paths = [job / f"voice_{i:02d}.wav" for i in range(len(lines))]
    words = [max(1, len(l.split())) for l in lines]
    models = [] if cfg["tts_model"] == "none" else [cfg["tts_model"]] + [m for m in TTS_MODELS if m != cfg["tts_model"]]
    for model in models:
        try:
            whole = job / "voice_all.wav"
            whole.write_bytes(gemini_tts("\n\n".join(lines), model, cfg["voice"]))
            with wave.open(str(whole), "rb") as w:
                rate = w.getframerate()
            import numpy as np
            with wave.open(str(whole), "rb") as w:
                x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
            pieces = split_by_pauses(x, rate, words)
            if pieces is None:
                log(f"viral: couldn't cut {model}'s narration into lines cleanly; trying the next voice")
                continue
            for p, piece in zip(paths, pieces):
                _write_wav(p, piece, rate)
            return paths
        except Exception as exc:
            log(f"viral: {model} voice failed ({str(exc)[:90]})")
    try:
        for text, p in zip(lines, paths):
            edge_tts(text, p)
        log("viral: voiced with the Edge narrator")
        return paths
    except Exception as exc:
        log(f"viral: the Edge voice failed ({str(exc)[:90]}); using the Mac's voice")
    for text, p in zip(lines, paths):
        say_tts(text, p)
    return paths


def read_wav(path: Path):
    """mono float32 at SR"""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        n, rate, ch, width = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype={1: np.int8, 2: np.int16, 4: np.int32}[width]).astype(np.float32)
    x /= float(2 ** (8 * width - 1))
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    x = _trim(x, rate)
    if rate != SR:
        t_new = np.arange(int(len(x) * SR / rate)) / SR
        x = np.interp(t_new, np.arange(len(x)) / rate, x).astype(np.float32)
    return x


def _trim(x, rate: int, thresh: float = 0.01):
    import numpy as np
    loud = np.where(np.abs(x) > thresh)[0]
    if not len(loud):
        return x
    a, b = max(0, loud[0] - int(.03 * rate)), min(len(x), loud[-1] + int(.08 * rate))
    return x[a:b]


def timeline(lengths: list, hook_min: float = 3.2, gap: float = 0.38, end_card: float = 2.6) -> tuple[list, float]:
    """Beat start/end times from the spoken lengths: each beat lasts its line plus a breath; the hook at least
    `hook_min` (the countdown needs a moment). Returns (beats [(start, end)], total duration)."""
    t, out = 0.25, []
    for i, n in enumerate(lengths):
        d = max(n + gap, hook_min if i == 0 else 1.6)
        out.append((round(t, 3), round(t + d, 3)))
        t += d
    return out, round(t + 0.4 + end_card, 3)


# ── the sound library (public-domain clips from Wikimedia Commons, labelled by Gemini) ─────────────────
SFX_DIR = ROOT / "memory" / "viral_sfx"


def sound_library() -> list[dict]:
    try:
        return json.loads((SFX_DIR / "index.json").read_text())
    except (OSError, ValueError):
        return []


def sounds_text() -> str:
    lib = sound_library()

    def line(x):
        extra = "".join(f"; {k}: {', '.join(x[k]) if isinstance(x[k], list) else x[k]}" for k in ("fits", "avoid") if x.get(k))
        e = f", energy {x['energy']}/5" if x.get("energy") else ""
        return f"  {x['id']}: {x['what']} ({x['dur']:.0f}s{e}{extra})"
    amb = [line(x) for x in lib if x["kind"] == "ambient"]
    one = [line(x) for x in lib if x["kind"] == "oneshot"]
    return ("  Background sounds (ambience ids):\n" + ("\n".join(amb) or "  (none yet)") +
            "\n  One-shot sound effects (sfx ids):\n" + ("\n".join(one) or "  (none yet)"))


def load_sound(sid: str):
    """a library clip as mono float32 at SR"""
    import numpy as np
    f = SFX_DIR / f"{sid}.mp3"
    raw = subprocess.run([ffmpeg(), "-loglevel", "quiet", "-i", str(f), "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def caption_lengths(beats: list) -> list[float]:
    """Seconds each caption stays up without a narrator: enough to read it, never dragging."""
    out = []
    for i, b in enumerate(beats):
        words = len(str(b["say"]).split())
        out.append(min(3.6, max(2.6 if i == 0 else 1.5, 0.9 + words * 0.3)))
    return out


# ── 3. music and sound effects ───────────────────────────────────────────────
SFX = ("none", "boom", "whoosh", "riser", "hit", "tick", "rumble", "bounce", "dribble", "swish", "buzzer", "squeak", "punch", "whistle", "bell")


def sfx(kind: str, seed: int = 0):
    import numpy as np
    rng = np.random.default_rng(seed)
    if kind == "boom":
        n = int(SR * 3.2); t = np.arange(n) / SR
        f = 32 + 60 * np.exp(-t * 4)
        body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 1.6)
        crack = rng.standard_normal(n) * np.exp(-t * 9) * .5
        return (body * .7 + _lowpass(crack, 0.15)).astype(np.float32)
    if kind == "hit":
        n = int(SR * 1.2); t = np.arange(n) / SR
        return (np.sin(2 * np.pi * 55 * t) * np.exp(-t * 5) + _lowpass(rng.standard_normal(n), .3) * np.exp(-t * 14) * .6).astype(np.float32)
    if kind in ("whoosh", "riser"):
        dur = 1.1 if kind == "whoosh" else 2.4
        n = int(SR * dur); t = np.arange(n) / SR
        env = np.sin(np.pi * t / dur) ** 2 if kind == "whoosh" else (t / dur) ** 2.5
        noise = rng.standard_normal(n)
        out = np.zeros(n, np.float32)
        for k in range(8):                               # a sweeping band of noise
            a, b = k * n // 8, (k + 1) * n // 8
            cut = .012 + .07 * (k / 7 if kind == "riser" else math.sin(math.pi * k / 7))   # dark, airy, not static
            out[a:b] = _lowpass(noise[a:b], cut)
        out = _lowpass(out, .12)
        return (out * env * (1.4 if kind == "whoosh" else 1.0)).astype(np.float32)
    if kind == "tick":
        n = int(SR * .25); t = np.arange(n) / SR
        return (np.sin(2 * np.pi * 1800 * t) * np.exp(-t * 60) * .35).astype(np.float32)
    if kind == "rumble":
        n = int(SR * 4); t = np.arange(n) / SR
        return (_lowpass(rng.standard_normal(n), .01) * 6 * np.sin(np.pi * t / 4) ** 2).astype(np.float32)
    if kind in ("bounce", "dribble"):                    # a basketball on hardwood: a ringing low thump (x3 for dribble)
        one_n = int(SR * .35); t = np.arange(one_n) / SR
        one = (np.sin(2 * np.pi * (95 + 40 * np.exp(-t * 30)) * t) * np.exp(-t * 18)
               + np.sin(2 * np.pi * 340 * t) * np.exp(-t * 40) * .25
               + _lowpass(rng.standard_normal(one_n), .25) * np.exp(-t * 90) * .5)
        if kind == "bounce":
            return one.astype(np.float32)
        out = np.zeros(int(SR * 1.5), np.float32)
        for k, at in enumerate((0, .45, .9)):
            a = int(at * SR); out[a:a + one_n] += one * (1 - .1 * k)
        return out
    if kind == "swish":                                  # ball through the net: a soft airy brush
        n = int(SR * .45); t = np.arange(n) / SR
        x = rng.standard_normal(n); hp = x - _lowpass(x, .2)
        return (_lowpass(hp, .6) * np.sin(np.pi * t / .45) ** 1.5 * .9).astype(np.float32)
    if kind == "buzzer":                                 # arena horn
        n = int(SR * 1.3); t = np.arange(n) / SR
        w = sum(np.sign(np.sin(2 * np.pi * f * t)) * a for f, a in ((233, 1), (466, .5), (349, .4)))
        env = np.minimum(1, t * 60) * np.minimum(1, (1.3 - t) * 12)
        return (_lowpass(w, .35) * env * .35).astype(np.float32)
    if kind == "squeak":                                 # sneaker on the court
        n = int(SR * .22); t = np.arange(n) / SR
        f = 2100 + 900 * np.sin(np.pi * t / .22)
        return (np.sin(2 * np.pi * np.cumsum(f) / SR) * np.sin(np.pi * t / .22) * .3).astype(np.float32)
    if kind == "punch":                                  # glove on body: thud + slap
        n = int(SR * .5); t = np.arange(n) / SR
        return (np.sin(2 * np.pi * 70 * t) * np.exp(-t * 22) + _lowpass(rng.standard_normal(n), .5) * np.exp(-t * 45) * .8).astype(np.float32)
    if kind == "whistle":                                # referee whistle
        n = int(SR * .7); t = np.arange(n) / SR
        f = 2900 + 120 * np.sign(np.sin(2 * np.pi * 32 * t))
        env = np.minimum(1, t * 40) * np.minimum(1, (.7 - t) * 15)
        return (np.sin(2 * np.pi * np.cumsum(f) / SR) * env * .25).astype(np.float32)
    if kind == "bell":                                   # boxing ring bell
        n = int(SR * 2.2); t = np.arange(n) / SR
        return (sum(np.sin(2 * np.pi * f * t) * a for f, a in ((880, 1), (2210, .5), (3300, .3))) * np.exp(-t * 2.2) * .3).astype(np.float32)
    return np.zeros(1, np.float32)


def _lowpass(x, cut: float):
    """one-pole low-pass; cut 0..1 of Nyquist-ish"""
    import numpy as np
    from scipy.signal import lfilter
    a = max(1e-4, min(.99, cut))
    return lfilter([a], [1, a - 1], x).astype(np.float32)


def music(seconds: float, seed: int = 0):
    """Dark ambient bed: a slow minor chord pad over a sub drone, with a soft pulse that builds."""
    import numpy as np
    rng = np.random.default_rng(seed)
    n = int(seconds * SR); t = np.arange(n) / SR
    roots = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]       # Am F C G
    if seed % 2:
        roots = [[50, 53, 57], [46, 50, 53], [53, 57, 60], [48, 52, 55]]   # Dm Bb F C
    hz = lambda m: 440 * 2 ** ((m - 69) / 12)
    out = np.zeros(n, np.float32)
    bar = 6.0
    for i in range(int(seconds / bar) + 1):
        chord = roots[i % 4]
        a, b = int(i * bar * SR), min(n, int((i + 1) * bar * SR + SR * 1.5))
        if a >= n:
            break
        tt = t[a:b] - i * bar
        env = np.minimum(1, tt / 1.8) * np.minimum(1, np.maximum(0, (bar + 1.5 - tt) / 1.8))
        for m in chord:
            for det in (-0.12, 0.12):
                f = hz(m - 12) * 2 ** (det / 12)
                out[a:b] += (np.sin(2 * np.pi * f * tt) + .3 * np.sin(4 * np.pi * f * tt)) * env * .05
        out[a:b] += np.sin(2 * np.pi * hz(chord[0] - 24) * tt) * env * .06
    build = .4 + .6 * (t / max(seconds, 1))
    pulse = (np.sin(2 * np.pi * 1.0 * t) > .96).astype(np.float32)
    pulse = _lowpass(pulse * rng.uniform(.8, 1, n).astype(np.float32), .05) * 1.2
    out = out * build + pulse * build * .5
    air = _lowpass(_lowpass(rng.standard_normal(n).astype(np.float32), .01), .05) * .25
    return (out + air).astype(np.float32)


def mix(voice: Optional[list], beats: list, script: dict, duration: float, out: Path, seed: int = 0) -> Path:
    """voice lines (or None: captions only) + ambience beds + sound effects + optional drone, to one wav."""
    import numpy as np
    n = int(duration * SR) + SR
    vtrack = np.zeros(n, np.float32)
    fx = np.zeros(n, np.float32)
    amb = np.zeros(n, np.float32)
    lib = {x["id"]: x for x in sound_library()}
    cache: dict = {}

    def clip(sid):
        if sid not in cache:
            try:
                x = load_sound(sid)
                cache[sid] = x / (float(np.sqrt(np.mean(x ** 2))) + 1e-6)         # loudness-matched
            except Exception:
                cache[sid] = None
        return cache[sid]

    # ambience: each one plays from its beat until the next change, looped, with half-second crossfades
    spans, cur = [], ("none", 0.0)
    gain = np.ones(n, np.float32)                       # per-beat ambience loudness ("level"), glided over 0.4 s
    lv = 1.0
    for (start, _), b in zip(beats, script["beats"]):
        a_id = b.get("ambience", "keep")
        if a_id != "keep" and a_id != cur[0]:
            spans.append((cur[0], cur[1], start))
            cur = (a_id, start)
        lv = float(b.get("level", lv) or lv)
        gain[int(start * SR):] = lv
    spans.append((cur[0], cur[1], duration))
    k = int(SR * .4)
    gain = np.convolve(np.pad(gain, (k, k), mode="edge"), np.ones(k) / k, mode="same")[k:-k].astype(np.float32)
    xf = int(SR * .5)
    for sid, t0, t1 in spans:
        x = clip(sid) if sid not in ("none", "keep") else None
        if x is None or t1 <= t0:
            continue
        a, b_ = int(t0 * SR), min(n, int(t1 * SR) + xf)
        reps = int(np.ceil((b_ - a) / len(x))) + 1
        seg = np.tile(x, reps)[:b_ - a].copy()
        ramp = min(xf, len(seg) // 2)
        seg[:ramp] *= np.linspace(0, 1, ramp); seg[-ramp:] *= np.linspace(1, 0, ramp)
        amb[a:b_] += seg * .07
    amb *= gain[:len(amb)]
    # sound effects: a library one-shot lands its hit on the beat; synth ones as before
    hits = []
    for (start, _), b in zip(beats, script["beats"]):
        hits.append((b.get("sfx", "none"), start))
        hits += [(h["id"], start + h.get("at", 0)) for h in b.get("sfx2") or []]
    for sid, start in hits:
        if sid == "none":
            continue
        if sid in lib:
            x = clip(sid)
            if x is None:
                continue
            x = x[:int(SR * 6)] * .16
            a = max(0, int((start - lib[sid].get("hit_at", 0)) * SR))
        else:
            x = sfx(sid, seed) * .9
            a = int(start * SR)
            if sid == "riser":
                a = max(0, a - len(x))
        if a >= n:
            continue
        fx[a:a + len(x)] += x[:max(0, n - a)]
    if voice:
        for (start, _), v in zip(beats, voice):
            v = v * (.12 / (float(np.sqrt(np.mean(v ** 2))) + 1e-6))
            v = np.tanh(v * 1.6) / 1.6
            a = int((start + .08) * SR)
            vtrack[a:a + len(v)] += v[:max(0, n - a)]
    env = np.convolve(np.abs(vtrack), np.ones(int(SR * .25)) / (SR * .25), mode="same")
    duck = 1 - .55 * np.clip(env / (env.max() + 1e-6) * 4, 0, 1)
    bed = music(n / SR, seed)[:n] * (.3 if script.get("music") == "tension" or voice else 0)
    air = _lowpass(_lowpass(np.random.default_rng(seed).standard_normal(n).astype(np.float32), .01), .05) * .02
    mixd = (vtrack / (np.abs(vtrack).max() or 1)) * .9 + (bed + amb) * duck + fx * (.35 + .65 * duck) + air
    fade = int(SR * 2.0)                                  # everything fades out over the end card
    mixd[-fade:] *= np.linspace(1, 0, fade)
    mixd = mixd[:int(duration * SR)]
    mixd = np.tanh(mixd / (np.abs(mixd).max() + 1e-6) * 1.4) / np.tanh(1.4) * .89    # soft-limit, then -1 dB
    pcm = (mixd * 32767).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
    return out


# ── 4. the scene ─────────────────────────────────────────────────────────────
KIT_API = """You write ONE JavaScript ES module for a 3D renderer (three.js r170). The module is:

export default function (K) {
  const { THREE, scene, camera } = K;
  // build the world once here ...
  return { update(t, dt) { /* move things each frame; t = seconds since the video started */ } };
}

No imports, no DOM, no fetch, no timers, no requestAnimationFrame: the kit calls update(t) for every frame in
order (30 fps). Math.random is seeded (fine to use while building). The frame is 1080x1920 portrait. Units
are metres; y is up; the ground is y=0. The kit draws the hook title, captions and HUD counter itself (don't
draw text). Keep the subject in the upper-middle of the frame: captions sit at 59-68% of the height, and
TikTok's buttons cover the right edge and the bottom 20%.

K: THREE, scene, camera, renderer, bloom (UnrealBloomPass: bloom.strength), noise (ImprovedNoise .noise(x,y,z)),
  beats ([{start, end, say, scene}] in seconds), duration,
  K.at(i) / K.end(i): start / end of beat i (i may be negative: -1 = last beat)
  K.p(t, a, b, ease?) -> 0..1 progress of t between a and b; eases: linear in out inOut expoIn expoOut back
  K.ease.out(x) etc, K.lerp(a,b,x), K.rand(a,b), K.v3([x,y,z]) -> Vector3
  K.onUpdate(fn(t, dt))  extra per-frame callback
  K.mat(color, opts) -> MeshStandardMaterial;  K.glowMat(color, intensity) -> unlit glowing (blooms)
  K.add(object3d, [x,y,z]?, shadows=true) adds to the scene and returns it
Places (each one sets the sky and lighting too; call exactly one, first):
  K.sets.backyard({env})  house at z=-22 behind a fence (z=-14, x=-14..14), trees, chairs, dense grass r=16 around 0,0,0
  K.sets.city({night})    street along -z, towers both sides (x=±16..20), cars, centre line
  K.sets.ocean({env, waves})  K.sets.plain({env, ground: 'sand'|'snow'|'mars'|'moon'|'rock'})
  K.sets.space()          black sky + stars (add planets/suns yourself)
  K.sets.room({env})      bedroom x,z in -4..4, y 0..3: bed at (-2.2,-2.6), desk at (2.6,-3.3), window on the back wall
  K.sets.street({people}) busy daytime sidewalk along -z: walk the camera down x=6 at eye height 1.75 (y=.16 pavement);
                          road x<3, shopfronts x=10; returns {people (walking crowd), birds (pigeons near 5.5,-7)}
  K.sets.court({fans})    basketball arena: court x -7.5..7.5, z -14..14, hoops at z=±13 (rim y 3.05); returns {ball, fans}
                          fans.cheer(true/false)
  K.sets.ring()           boxing ring in a dark arena: ring floor y=1.25, x,z -3.5..3.5; returns {fans, spot}
  env names: day golden dusk overcast night space underwater hell. K.envInfo = {sun, hemi, sky?, sunDir}
Pieces (all return the object; pos is [x,y,z]):
  K.ground(type, size)  K.grass(center, radius, count)  K.tree(pos, scale)  K.house(pos, {w,d,h,color,rot,lit})
  K.fence(a, b)  K.chair(pos, color, rot)  K.car(pos, color, rot)  K.box(pos, [w,h,d], color)  K.ball(pos, r, color)
  K.person(pos, opts) -> a REALISTIC skinned, textured 3D human (1.8 m at s=1, faces +z; turn with .rotation.y or
      .lookAt(x, 0, z)). opts: sex 'm'|'f'; skin 'light'|'pale'|'tan'|'brown'|'dark' (or 'red'/'devil'/'green'/'blue'/
      'grey' for creatures); hair 'buzzed'|'parted'|'long'|'buns'|'bald', hairColor, beard: true; shirt colour (null =
      shirtless), sleeves 'none' (tank/jersey)|'short'|'long'; pants colour, legs 'shorts'|'long'; shoes colour;
      gloves colour (boxing gloves); number (printed on the shirt front+back, e.g. 23 or '666'), numberColor '#fff';
      horns: true; eyes: glow colour (0xff3300); s: size (1.1 = a 2 m athlete, 1.3 = a giant).
      Animate it EVERY frame (it holds the last pose otherwise), with time in seconds:
      .play(clip, time, weight=1) - clips: idle talk walk strut jog sprint jump_start jump_air jump_land punch jab
        hit_chest hit_head death roll dance sit sit_talk sit_down stand_up crouch sneak cast cast_idle swing guard
        push swim tread drive pickup interact kneel torch. Loops repeat; one-shots run 0..their length
        (punch 1 s, jab .87, death 2.4, jump_start 1.33, jump_land 1.27, hit_chest .33, roll 1.47, cast .5).
        Blend two by calling .play(a, t) then .play(b, t, 0.5).
      .walk(t, speed) .run(t, speed) shortcuts (move .position yourself: walk ~1.4 m/s, run ~6 m/s at speed 1)
      .pose(name, x 0..1): idle punch jab fight fall hit jump (start->air->land over x) arms_up cheer dunk shoot
        dribble point sit dance cast
      .aimArm('l'|'r', [x,y,z] in its own space: +x its left, +y up, +z forward, amount) after .play to point an arm
      .bone('hand_r'|'Head'|'spine_03'|'foot_l'...) to attach things (bone.add(mesh)); .handPos('r') -> world Vector3
  K.crowd([x0,z0], [x1,z1], count, {walk, speed, face:[x,z], s, jerseys}) -> array of realistic people (walkers stroll
      along z and wrap; walk:false + face = spectators). K.cheer(true|false|0..1, t0) makes ALL spectators jump and
      wave from t0 (call it when the big moment lands, K.cheer(false) to calm them).
  K.birds([x,y,z], count, radius) -> pigeons; .scatter(t0) makes them fly off
  K.hand({item: 'coffee'|'phone'|'none'}) -> first-person forearm in the lower right; call hand.bob(t) each frame
  K.glow(pos, {color, size, light, range}) -> glowing point with halo + light; .setPower(x) scales it (0 hides)
  K.beam(from, to, {color, width})   K.streak({color, size, tail}) -> meteor; call .fly(from, to, progress) each frame
  K.planet(pos, {r, kind: 'earth'|'mars'|'moon'|'gas', spin})  K.sun(pos, {r, color})  K.blackHole(pos, {r})
  K.stars()  K.ocean({size, color, waves})
Effects, all scheduled by time (they animate themselves):
  K.explosion(pos, {t0, size, color, debris, smoke, shake})  K.sparks(pos, {t0, count, speed, color, life, gravity, dir, spread})
  K.debris(pos, {t0, count, speed, color, size, dir, spread})  K.smoke(pos, {t0, t1, rate, rise, size, color, life})
  K.fire(pos, {t0, t1, size})  K.shake(t0, dur, amp)  K.handheld(amount 0..1, default .6)
  K.flash(t0, dur, '#fff')  K.fadeOut(t0, dur)  K.fadeIn(t0, dur)  K.vignette(on)  K.hideText = true/false
Camera: K.camPath([{t, pos:[x,y,z], look:[x,y,z], fov?, ease?}, ...]) glides between keyframes every frame
  (eye height 1.6 for first-person). Or set camera.position / camera.lookAt yourself inside update().
  Default fov 62. Shake and a gentle handheld sway are added on top automatically.

Make it look like a polished viral simulation video:
- There is no narrator: the captions carry the story, so every beat needs its own clear visual moment.
- If beat 0 is a cold open (the most dramatic moment), show that state first, then reset everything at K.at(1)
  with a quick K.flash or whip of the camera, and play the story forward from the beginning.
- First-person shots (eye height 1.6-1.75, K.hand for a held item, people walking past) feel the most real.
- Something visibly changes every 2-3 seconds; each beat's "scene" note must actually be shown at that beat's time.
- Move the camera with purpose: slow push-ins, reveals, a dramatic pull-back to show scale near the end.
- Impacts land exactly on the beat that has sfx boom/hit: K.explosion or K.flash + K.shake at K.at(i).
- Glowing things (toneMapped false / glowMat) bloom nicely; use light and contrast, avoid an all-dark frame
  and an all-white one (no camera inside a bright fog or a pale sprite filling the screen; keep fog dense
  enough to be moody but with shapes readable through it).
- Huge things (planets, waves, blasts) need the camera far enough back; check every object is in front of the
  camera at its moment. Keep geometry counts sane (< 200k instances, < 300 meshes).
- The end card covers the last ~2.6 s, so the final shot can simply hold."""

SCENE_PROMPT = """{api}

The video, beat by beat (times in seconds; total {duration}s):
Title (hook, beat 0): {title}
Place: {set}
{beats}

Write the module now. Reply with only the code in one ```js block."""

FIX_PROMPT = """{api}

Your module for this video failed when rendering:
{error}

The video, beat by beat (total {duration}s):
{beats}

Here is the module:
```js
{code}
```
Fix it. Reply with only the full corrected module in one ```js block."""

REVIEW_PROMPT = """{api}

These are 8 sample frames (left to right, top to bottom) of a short 3D video, at these times: {times}.
The video, beat by beat:
{beats}

Here is the module that drew them:
```js
{code}
```
Judge it like a viewer scrolling TikTok: is the action readable and on time? Look for empty or black frames,
the subject off-screen, behind the camera or hidden under the captions, objects at absurd scale, the camera
inside geometry, nothing happening, or anything that looks broken. If it's good, reply with exactly: OK
Otherwise reply with the full improved module in one ```js block (keep what works)."""


def review_times(times: list, duration: float, n: int = 8) -> list[float]:
    """n moments spread over the whole video (the middle of evenly picked beats) for the visual check."""
    mids = [round(min(duration - .1, (s + e) / 2), 2) for s, e in times]
    if len(mids) <= n:
        return mids
    return sorted({mids[round(i * (len(mids) - 1) / (n - 1))] for i in range(n)})


def _code_from(text: str) -> str:
    m = re.search(r"```(?:js|javascript)?\s*(.*?)```", text or "", re.S)
    code = (m.group(1) if m else text or "").strip()
    if "export default" not in code:
        raise ValueError("no scene module in the reply")
    return code


def beats_text(script: dict, times: list) -> str:
    rows = []
    for i, (b, (s, e)) in enumerate(zip(script["beats"], times)):
        hud = f" | HUD: {json.dumps(b['hud'])}" if b.get("hud") else ""
        rows.append(f"beat {i} [{s:.2f}-{e:.2f}s] sfx={b['sfx']} | caption: \"{b['say']}\" | show: {b['scene']}{hud}")
    return "\n".join(rows)


# ── 5. rendering ─────────────────────────────────────────────────────────────
class Stage:
    """Headless Chrome with the kit loaded; frames come back as JPEG bytes."""

    def __init__(self, job: Path):
        self.job = job

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True, args=CHROME_ARGS)
        self.page = self.browser.new_page(viewport={"width": 1080, "height": 1920})
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        self.page.route("http://stage.local/**", self._route)
        return self

    def _route(self, r):
        rel = r.request.url.split("stage.local/", 1)[1].split("?")[0]
        f = (self.job / rel[4:]) if rel.startswith("job/") else (ASSETS / rel)
        if not f.is_file() or ".." in rel:
            return r.fulfill(status=404, body="")
        ctype = {".js": "text/javascript", ".html": "text/html", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg",
                 ".gltf": "model/gltf+json", ".glb": "model/gltf-binary"}.get(f.suffix, "application/octet-stream")
        r.fulfill(path=str(f), headers={"Content-Type": ctype, "Cache-Control": "no-store"})

    def boot(self, data: dict, scene_file: str = "scene.js") -> None:
        self.errors.clear()
        self.page.goto("http://stage.local/stage.html")
        self.page.wait_for_function("window.__kitLoaded === true", timeout=30000)
        self.page.evaluate("([d, u]) => window.__boot(d, u)", [data, f"http://stage.local/job/{scene_file}?v={time.time()}"])
        self._check()

    def _check(self):
        if self.errors:
            raise RuntimeError("; ".join(self.errors[:3])[:900])

    def frame(self, t: float, grab: bool = True, q: float = .92) -> Optional[bytes]:
        self.page.evaluate(f"window.__frame({t:.5f})")
        self._check()
        if not grab:
            return None
        return base64.b64decode(self.page.evaluate(f"window.__grab({q})").split(",", 1)[1])

    def __exit__(self, *a):
        try:
            self.browser.close()
        finally:
            self._pw.stop()


def preview(job: Path, data: dict, times: list, fps: int = 10) -> list[Path]:
    """Steps the scene up to each time (at a coarse frame rate) and saves those frames."""
    out, t, step = [], 0.0, 1 / fps
    with Stage(job) as st:
        st.boot(data)
        for target in sorted(times):
            while t < target - 1e-6:
                st.frame(t, grab=False)
                t = round(t + step, 5)
            p = job / f"preview_{target:06.2f}.jpg"
            p.write_bytes(st.frame(target, q=.8))
            out.append(p)
            t = round(t + step, 5)
    return out


def contact_sheet(frames: list[Path], out: Path) -> Path:
    from PIL import Image
    ims = [Image.open(f).convert("RGB").resize((270, 480)) for f in frames]
    sheet = Image.new("RGB", (270 * 4, 480 * ((len(ims) + 3) // 4)))
    for i, im in enumerate(ims):
        sheet.paste(im, ((i % 4) * 270, (i // 4) * 480))
    sheet.save(out, quality=85)
    return out


def render(job: Path, data: dict, audio: Path, out: Path, log: Callable = print) -> Path:
    n = int(math.ceil(data["duration"] * FPS))
    cmd = [ffmpeg(), "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
           "-i", str(audio), "-vf", "scale=in_range=pc:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
           "-crf", "19", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
           "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    started = time.time()
    try:
        with Stage(job) as st:
            st.boot(data)
            for i in range(n):
                proc.stdin.write(st.frame(i / FPS))
                if i and i % (FPS * 20) == 0:
                    log(f"viral: rendered {i}/{n} frames ({time.time() - started:.0f}s)")
    finally:
        proc.stdin.close()
        proc.wait(timeout=600)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError("ffmpeg couldn't encode the video")
    return out


# ── the whole video ──────────────────────────────────────────────────────────
def make(topic: str = "", log: Callable = print, ask: Callable = ask_claude, history: Optional[list] = None,
         resume: Optional[Path] = None, keep_scene: bool = False) -> dict:
    """One finished video. `resume` = an earlier work folder: its script (and voice, if complete) are reused."""
    cfg = settings()
    st = load_state()
    history = history if history is not None else [v["title"] for v in st["videos"]]
    job = Path(resume) if resume else WORK / datetime.now().strftime("%Y%m%d-%H%M%S")
    stamp = job.name
    job.mkdir(parents=True, exist_ok=True)

    if (job / "script.json").exists():
        script = json.loads((job / "script.json").read_text())
    else:
        log("viral: writing the script")
        script = write_script(history, topic, ask)
        (job / "script.json").write_text(json.dumps(script, indent=1))
    log(f"viral: \"{script['title']}\" ({len(script['beats'])} beats)")

    voice = None
    if cfg["voice_mode"] in ("gemini", "edge"):
        paths = [job / f"voice_{i:02d}.wav" for i in range(len(script["beats"]))]
        if not all(p.exists() for p in paths):
            paths = voice_lines([b["say"] for b in script["beats"]], job, dict(cfg, tts_model=cfg["tts_model"]
                                if cfg["voice_mode"] == "gemini" else "none"), log)
        voice = [read_wav(p) for p in paths]
        times, duration = timeline([len(v) / SR for v in voice])
    else:
        times, duration = timeline(caption_lengths(script["beats"]), hook_min=2.6, gap=0)
    seed = int(time.time()) % 100000
    data = {"seed": seed, "fps": FPS, "duration": duration, "title": script["title"], "highlight": script["highlight"],
            "end_fact": script["end_fact"], "end_card": 2.6,
            "beats": [{"start": s, "end": e, "say": b["say"], "emph": b["emph"], "hud": b["hud"], "scene": b["scene"]}
                      for b, (s, e) in zip(script["beats"], times)]}
    (job / "data.json").write_text(json.dumps(data, indent=1))
    btxt = beats_text(script, times)

    if keep_scene and (job / "scene.js").exists():
        log("viral: keeping the scene that's already written")
    else:
        log(f"viral: {duration:.0f}s long; building the 3D scene")
        code = _code_from(ask(SCENE_PROMPT.format(api=KIT_API, duration=duration, title=script["title"],
                                                  set=script["set"], beats=btxt), timeout_s=900))
        sample = review_times(times, duration)
        frames = None
        for attempt in range(3):
            (job / "scene.js").write_text(code)
            try:
                frames = preview(job, data, sample)
                break
            except Exception as exc:
                log(f"viral: scene error ({exc}); asking for a fix")
                if attempt == 2:
                    raise RuntimeError(f"the 3D scene kept failing: {exc}")
                code = _code_from(ask(FIX_PROMPT.format(api=KIT_API, error=str(exc)[:900], duration=duration, beats=btxt,
                                                        code=code), timeout_s=900))
        # one look at the frames, and a revision if something's off
        sheet = contact_sheet(frames, job / "review.jpg")
        try:
            reply = ask(REVIEW_PROMPT.format(api=KIT_API, times=", ".join(f"{x:.1f}s" for x in sample), beats=btxt, code=code),
                        timeout_s=900, images=[sheet])
            if reply.strip().upper() != "OK" and "```" in reply:
                revised = _code_from(reply)
                (job / "scene_v1.js").write_text(code)
                (job / "scene.js").write_text(revised)
                try:
                    preview(job, data, sample[:3])
                    code = revised
                    log("viral: revised the scene after looking at it")
                except Exception as exc:
                    log(f"viral: the revision broke ({exc}); keeping the first version")
                    (job / "scene.js").write_text(code)
        except Exception as exc:
            log(f"viral: skipped the visual review: {exc}")

    log("viral: mixing the sound")
    audio = mix(voice, times, script, duration, job / "audio.wav", seed)
    for _ in range(2):                    # listen to it; swap any sound that doesn't fit what's on screen
        fixed = sound_check(audio, times, script, log)
        if not fixed:
            break
        script = fixed
        (job / "script.json").write_text(json.dumps(script, indent=1))
        audio = mix(voice, times, script, duration, job / "audio.wav", seed)
    OUT.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", script["title"].lower()).strip("-")[:50]
    out = OUT / f"{stamp}-{slug}.mp4"
    log("viral: rendering")
    t0 = time.time()
    render(job, data, audio, out, log)
    log(f"viral: done in {time.time() - t0:.0f}s -> {out}")
    rec = {"title": script["title"], "topic": script["topic"], "path": str(out), "job": str(job), "made": time.time(),
           "duration": duration, "caption": caption_for(script), "posted": False}
    st = load_state()
    st["videos"].append(rec)
    save_state(st)
    return rec


SOUND_CHECK = """You are the sound editor of a short viral 3D video (no narrator; captions + sound only). Listen to the
attached soundtrack and compare it, beat by beat, with what is on screen:
{beats}

The sound library (only these ids may be used; "none" = silence, synth effects: boom, whoosh, riser, hit, tick, rumble, bounce, dribble, swish, buzzer, squeak,
punch, whistle, bell):
{sounds}

Every sound must be what you would really hear at that moment, at the right intensity (a dunk in a packed arena =
roaring crowd, never polite or golf clapping). Flag ONLY beats that sound clearly wrong or clearly missing a key sound.
Reply ONLY JSON: {{"ok": true|false, "fixes": [{{"beat": 3, "problem": "golf claps under a dunk",
"ambience": "<id>|keep|none|unchanged", "level": 0.3-2.5 or null, "sfx": "<id>|none|unchanged",
"sfx2": [{{"id": "<id>", "at": 0.8}}] or null}}]}}"""


def sound_check(audio: Path, times: list, script: dict, log: Callable = print) -> Optional[dict]:
    """Gemini listens to the mix next to the beat list. Returns the script with the bad sounds swapped, or None."""
    try:
        from core import gemini
        from google.genai import types
        mp3 = audio.with_suffix(".check.mp3")
        subprocess.run([ffmpeg(), "-loglevel", "quiet", "-y", "-i", str(audio), "-ac", "1", "-b:a", "64k", str(mp3)], check=True)
        lib = {x["id"]: x for x in sound_library()}

        def name(sid):
            return f"{sid} ({lib[sid]['what']})" if sid in lib else sid
        rows = []
        for i, (b, (t0, t1)) in enumerate(zip(script["beats"], times)):
            extra = ", ".join(f"{name(h['id'])} at +{h['at']:.1f}s" for h in b.get("sfx2") or [])
            rows.append(f"beat {i} [{t0:.1f}-{t1:.1f}s] caption \"{b['say']}\" | on screen: {b.get('scene', '')} | "
                        f"ambience {name(b.get('ambience', 'keep'))} level {b.get('level', 1)} | sfx {name(b.get('sfx', 'none'))}"
                        + (f" | more: {extra}" if extra else ""))
        prompt = SOUND_CHECK.format(beats="\n".join(rows), sounds=sounds_text())
        c = gemini.client(timeout_ms=240000)
        r = None
        for model in ("gemini-3.5-flash", "gemini-3.5-flash-lite"):
            try:
                r = c.models.generate_content(model=model, contents=[types.Part.from_bytes(data=mp3.read_bytes(), mime_type="audio/mpeg"), prompt],
                                              config=types.GenerateContentConfig(response_mime_type="application/json"))
                break
            except Exception as exc:
                log(f"viral: sound check on {model} failed ({str(exc)[:80]})")
        if r is None:
            return None
        verdict = _json_from(r.text)
    except Exception as exc:
        log(f"viral: skipped the sound check: {exc}")
        return None
    fixes = [f for f in verdict.get("fixes") or [] if isinstance(f, dict)]
    if verdict.get("ok") or not fixes:
        log("viral: sound check passed")
        return None
    new = json.loads(json.dumps(script))
    for f in fixes:
        try:
            b = new["beats"][int(f.get("beat"))]
        except (TypeError, ValueError, IndexError):
            continue
        log(f"viral: sound fix, beat {f.get('beat')}: {str(f.get('problem', ''))[:90]}")
        for k in ("ambience", "sfx"):
            if f.get(k) not in (None, "", "unchanged"):
                b[k] = f[k]
        if f.get("level") is not None:
            b["level"] = f["level"]
        if isinstance(f.get("sfx2"), list):
            b["sfx2"] = f["sfx2"]
    try:
        return clean_script(new, lib)
    except Exception:
        return None


def caption_for(script: dict) -> str:
    tags = " ".join("#" + t for t in script.get("hashtags") or [])
    return f"{script['caption']} {tags}".strip()[:2000]


# ── 6. TikTok ────────────────────────────────────────────────────────────────
class NotLoggedIn(RuntimeError):
    pass


def _tiktok_context(p, visible: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = ["--disable-blink-features=AutomationControlled"]
    if not visible:
        args.append("--window-position=-2400,0")          # a real window (TikTok balks at headless), kept off screen
    return p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False, args=args,
                                                viewport={"width": 1280, "height": 900}, ignore_default_args=["--enable-automation"])


def logged_in(ctx) -> bool:
    return any(c["name"] == "sessionid" and c.get("value") for c in ctx.cookies("https://www.tiktok.com"))


def tiktok_login(wait_s: int = 900) -> bool:
    """Opens TikTok's login page in a Chrome window; returns once the user has logged in."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _tiktok_context(p, visible=True)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto("https://www.tiktok.com/login")
        end = time.time() + wait_s
        while time.time() < end:
            if logged_in(ctx):
                time.sleep(3)
                ctx.close()
                return True
            time.sleep(2)
        ctx.close()
    return False


def tiktok_post(video: Path, caption: str, ai_label: bool = True, log: Callable = print, dry_run: bool = False) -> str:
    """Uploads through TikTok Studio in the logged-in profile. Returns a note; raises on failure (with a screenshot)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _tiktok_context(p, visible=False)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        shot = WORK / f"tiktok-{int(time.time())}.png"
        try:
            if not logged_in(ctx):
                raise NotLoggedIn("TikTok isn't logged in (run: python3 -m worker.viral login)")
            pg.goto("https://www.tiktok.com/tiktokstudio/upload?from=webapp", wait_until="domcontentloaded", timeout=60000)
            if "login" in pg.url:
                raise NotLoggedIn("TikTok sent us to the login page")
            pg.locator("input[type=file]").first.set_input_files(str(video), timeout=60000)
            editor = pg.locator("div[contenteditable='true']").first
            editor.wait_for(state="visible", timeout=180000)
            editor.click()
            pg.keyboard.press("Meta+A")
            pg.keyboard.press("Backspace")
            pg.wait_for_timeout(500)
            for word in caption.split(" "):
                pg.keyboard.type(word, delay=8)
                pg.keyboard.type(" ")
                if word.startswith("#"):
                    pg.wait_for_timeout(600)               # the hashtag menu closes after the space
            typed = editor.inner_text()
            if video.stem in typed or caption.split(" ")[0] not in typed:
                raise RuntimeError(f"the caption didn't go in (the box says: {typed[:80]!r})")
            if ai_label:
                _ai_label(pg, log)
            # wait for the upload to finish: the Post button becomes enabled
            post = pg.locator("button[data-e2e='post_video_button'], button:has-text('Post')").first
            end = time.time() + 600
            while time.time() < end and (post.get_attribute("disabled") is not None
                                         or post.get_attribute("aria-disabled") == "true"
                                         or post.get_attribute("data-disabled") == "true"):
                pg.wait_for_timeout(2000)
            pg.wait_for_timeout(1500)
            if dry_run:
                pg.screenshot(path=str(shot), full_page=True)
                return f"dry run: ready to post (screenshot {shot})"
            pg.wait_for_timeout(15000)                      # let TikTok's own checks settle; early clicks get ignored
            done = re.compile(r"tiktokstudio/content|/manage")
            for attempt in range(4):
                post.scroll_into_view_if_needed(timeout=10000)
                pg.wait_for_timeout(1000)
                post.click()
                # a "post now?" confirmation (content check still running) may appear
                for label in ("Post now", "Post anyway", "Continue"):
                    try:
                        pg.locator(f"button:has-text('{label}')").first.click(timeout=3000)
                        break
                    except Exception:
                        pass
                try:
                    pg.wait_for_url(done, timeout=45000)
                    return "posted"
                except Exception:
                    log(f"viral: Post click #{attempt + 1} didn't go through, trying again")
            raise RuntimeError("TikTok never accepted the Post click")
        except Exception:
            try:
                pg.screenshot(path=str(shot))
            except Exception:
                pass
            raise
        finally:
            ctx.close()


def _ai_label(pg, log: Callable) -> bool:
    """Turns on 'AI-generated content' in the upload settings (TikTok requires it for AI-made video)."""
    try:
        sw = pg.locator("[data-e2e='aigc_container'] .Switch__content").first
        if not sw.is_visible():                                # it sits in the collapsed "Show more" section
            pg.locator("text=/^Show more$/i").first.click(timeout=5000)
            sw.wait_for(state="visible", timeout=8000)
        sw.scroll_into_view_if_needed(timeout=5000)
        if sw.get_attribute("aria-checked") != "true":
            sw.click(timeout=5000)
            pg.wait_for_timeout(800)
            for label in ("Turn on", "Confirm", "OK"):            # a "label this content?" dialog
                try:
                    pg.locator(f"button:has-text('{label}')").last.click(timeout=2000)
                    break
                except Exception:
                    pass
        on = sw.get_attribute("aria-checked") == "true"
        if not on:
            log("viral: the AI-generated switch didn't turn on")
        return on
    except Exception as exc:
        log(f"viral: couldn't set the AI-generated label: {str(exc)[:120]}")
        return False


# ── how the videos are doing ─────────────────────────────────────────────────
MILESTONES = (10_000, 100_000, 1_000_000, 10_000_000)


def parse_count(text: str) -> int:
    """TikTok's '1.2K' / '3.4M' / '987' -> an int"""
    m = re.match(r"\s*([\d.,]+)\s*([KMB]?)", str(text or "").upper())
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def read_profile_stats(handle: str = "") -> list[dict]:
    """Views of each video on the account's profile page: [{id, url, views, desc}] newest first."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _tiktok_context(p, visible=False)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pg.goto(f"https://www.tiktok.com/@{handle}" if handle else "https://www.tiktok.com/profile",
                    wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(6000)
            for _ in range(4):                                   # load a few screens of videos
                pg.mouse.wheel(0, 2500)
                pg.wait_for_timeout(1500)
            return pg.evaluate("""() => [...document.querySelectorAll('a[href*="/video/"]')].map(a => {
                const v = a.querySelector('[data-e2e="video-views"]'), img = a.querySelector('img');
                return { url: a.href.split('?')[0], id: (a.href.match(/video\\/(\\d+)/) || [])[1] || '',
                         views: v ? v.textContent.trim() : '', desc: img ? (img.alt || '') : '' };
              }).filter(x => x.id)""")
        finally:
            ctx.close()


def viral_news(stats: list[dict], seen: dict, now: float) -> tuple[list[str], dict]:
    """Texts for videos that crossed a milestone or are far above the account's usual, and the updated record.
    `seen` = {video id: {"first": ts, "views": n, "told": [milestones already texted], "desc": ...}}"""
    msgs = []
    for x in stats:
        r = seen.setdefault(x["id"], {"first": now, "told": [], "desc": x.get("desc", "")[:120], "url": x.get("url", "")})
        r["views"] = parse_count(x["views"]) if isinstance(x["views"], str) else int(x["views"] or 0)
    views = sorted(r["views"] for r in seen.values())
    usual = views[len(views) // 2] if views else 0
    for vid, r in seen.items():
        hits = [m for m in MILESTONES if r["views"] >= m and m not in r["told"]]
        if hits:
            r["told"] += hits
            msgs.append(f"🔥 TikTok: \"{r['desc'][:80]}\" just passed {hits[-1]:,} views ({r['views']:,} now). "
                        f"This topic is working. {r['url']}")
        elif len(views) >= 5 and usual >= 100 and r["views"] >= 5 * usual and "5x" not in r["told"]:
            r["told"].append("5x")
            msgs.append(f"📈 TikTok: \"{r['desc'][:80]}\" has {r['views']:,} views, 5x your usual ({usual:,}). "
                        f"{r['url']}")
    return msgs, seen


def sequel_topic(desc: str) -> str:
    """The brief for a Part 2 of a video that did well (its TikTok caption tells what it was)."""
    return (f"PART 2 of our video that went viral (its caption: \"{desc[:150]}\"). Same characters, same style, same "
            "kind of hook, but the next chapter: e.g. Game 2 of the series, the rematch, the next level, what happens "
            "the day after. The title starts with \"Part 2:\" and still asks the question. Open with a quick recap "
            "of how Part 1 ended, then a new twist and a new ending that teases Part 3.")


# ── the worker's thread: a video at each of the day's times ─────────────────
def due_slots(times: str, done: list, now: datetime, late_ok_h: float = 3) -> list[str]:
    """Today's slots that are due and not yet done; a slot more than `late_ok_h` hours old is skipped."""
    out = []
    for s in (x.strip() for x in str(times).split(",") if x.strip()):
        try:
            h, m = (int(x) for x in s.split(":"))
        except ValueError:
            continue
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if s not in done and at <= now and (now - at).total_seconds() <= late_ok_h * 3600:
            out.append(s)
    return out


class ViralService:
    PASS_SECONDS = 60

    def __init__(self, log: Callable[[str], None] = print, notify: Optional[Callable[[str, str], None]] = None):
        self.log, self.notify = log, notify or (lambda kind, text: None)
        self._stop = threading.Event()
        self._busy = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="viral-videos").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.PASS_SECONDS):
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"viral: pass failed: {exc!r}")

    STATS_EVERY = 2 * 3600

    def check_stats(self, now: Optional[float] = None) -> list[str]:
        now = now or time.time()
        st = load_state()
        if now - st.get("stats_at", 0) < self.STATS_EVERY or not st["videos"] or not any(v.get("posted") for v in st["videos"]):
            return []
        st["stats_at"] = now
        save_state(st)
        try:
            stats = read_profile_stats(settings().get("handle", ""))
        except Exception as exc:
            self.log(f"viral: couldn't read the view counts: {exc}")
            return []
        st = load_state()
        msgs, st["tiktok"] = viral_news(stats, st.get("tiktok") or {}, now)
        for vid, r in st["tiktok"].items():           # a video that took off gets a Part 2 at the next slot
            if r.get("told") and not r.get("sequel"):
                r["sequel"] = now
                st.setdefault("sequels", []).append(sequel_topic(r.get("desc", "")))
                msgs = [m + " Making a Part 2 for the next slot." if r.get("desc", "")[:80] in m else m for m in msgs]
        save_state(st)
        for m in msgs:
            self.notify("viral", m)
        return msgs

    def run_once(self, now: Optional[datetime] = None) -> str:
        try:
            self.check_stats()
        except Exception as exc:
            self.log(f"viral: stats pass failed: {exc!r}")
        cfg = settings()
        if not cfg["enabled"] or not self._busy.acquire(blocking=False):
            return ""
        try:
            now = now or datetime.now()
            today = now.date().isoformat()
            st = load_state()
            done = st["slots"].get(today, [])
            due = due_slots(cfg["times"], done, now)
            if not due:
                return ""
            st["slots"] = {today: done + due}                  # several due at once (Mac was asleep) = one video
            save_state(st)
            return self.produce(cfg)
        finally:
            self._busy.release()

    def produce(self, cfg: dict, topic: str = "") -> str:
        st = load_state()
        queued = [v for v in st.get("queue") or [] if Path(v["path"]).exists()]
        if queued:                                         # a video made ahead (and sent to the user to look over)
            rec = queued[0]
            st["queue"] = queued[1:]
            save_state(st)
            return self.deliver(rec, cfg)
        if not topic and st.get("sequels"):             # a sequel to a video that took off goes first
            topic = st["sequels"].pop(0)
            save_state(st)
        try:
            rec = make(topic, log=self.log)
        except Exception as exc:
            self.log(f"viral: couldn't make a video: {exc}")
            self.notify("viral", f"🎬 Couldn't make today's TikTok video: {str(exc)[:160]}")
            return "failed"
        return self.deliver(rec, cfg)

    def deliver(self, rec: dict, cfg: dict) -> str:
        if cfg["post"]:
            try:
                tiktok_post(Path(rec["path"]), rec["caption"], cfg["ai_label"], self.log)
                _mark(rec["path"], posted=True)
                try:                                       # the user asked for a text every time one goes up
                    from core import phone_alerts
                    phone_alerts.send_text(f"🎬 Just posted to TikTok: \"{rec['title']}\"\n"
                                           f"https://www.tiktok.com/@{cfg.get('handle') or 'whatifishowedu'}")
                except Exception as exc:
                    self.log(f"viral: couldn't text the post note: {exc}")
                return "posted"
            except NotLoggedIn:
                note = "TikTok isn't logged in yet, so here it is. Log in once: python3 -m worker.viral login"
            except Exception as exc:
                self.log(f"viral: posting failed: {exc}")
                note = f"Couldn't post it to TikTok ({str(exc)[:100]}), so here it is"
        else:
            note = "Ready to post"
        try:
            from core import phone_alerts
            phone_alerts.send_file(rec["path"], f"🎬 \"{rec['title']}\" — {note}\n\n{rec['caption']}")
            return "texted"
        except Exception as exc:
            self.log(f"viral: couldn't text the video: {exc}")
            return "made"


def _mark(path: str, **fields) -> None:
    st = load_state()
    for v in st["videos"]:
        if v["path"] == path:
            v.update(fields, at=time.time())
    save_state(st)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "queue":                                     # make the next slot's video now and text it for review
        rec = make(" ".join(a for a in argv[1:] if not a.startswith("--")))
        st = load_state()
        st.setdefault("queue", []).append(rec)
        save_state(st)
        from core import phone_alerts
        phone_alerts.send_file(rec["path"], f"🎬 Tonight's TikTok, for you to look over: \"{rec['title']}\". "
                               f"It posts at the next slot unless you say otherwise.\n\n{rec['caption']}")
        print("queued and texted:", rec["path"])
        return 0
    if cmd == "make":
        svc = ViralService()
        resume = next((a.split("=", 1)[1] for a in argv if a.startswith("--resume=")), None)
        rec = make(" ".join(a for a in argv[1:] if not a.startswith("--")), resume=Path(resume) if resume else None,
                   keep_scene="--keep-scene" in argv)
        print(json.dumps(rec, indent=1))
        if settings()["post"] and "--no-post" not in argv:
            print(svc.deliver(rec, settings()))
        return 0
    if cmd == "login":
        print("A Chrome window is opening: log in to TikTok there (it waits up to 15 minutes).")
        print("Logged in." if tiktok_login() else "Timed out before you logged in.")
        return 0
    if cmd == "post" and len(argv) >= 2:
        print(tiktok_post(Path(argv[1]), argv[2] if len(argv) > 2 else "", settings()["ai_label"]))
        return 0
    for v in load_state()["videos"][-10:]:
        print(datetime.fromtimestamp(v["made"]).strftime("%m-%d %H:%M"), "posted" if v.get("posted") else "not posted", v["title"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
