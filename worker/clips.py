"""Game clips: gameplay clips (GTA 6 at launch) cut into vertical shorts and posted to the videos' TikTok and
Instagram accounts, in the gaps between the daily "what if" videos (worker/viral.py).

Clips come from one folder, ~/Movies/Jarvis Clips/inbox: anything dropped there (your own gameplay, footage a
streamer's clipping campaign hands out, clips you saved) is used oldest first, one per time slot.
Name a file "@streamer anything.mp4" (or put the name in a .txt beside it) to credit the streamer on screen.

Each clip:
  1. Gemini watches it and picks the best 15-45 seconds, a hook title, one spoken hook line and the caption.
     A clip with nothing worth posting is moved to inbox/skipped.
  2. ffmpeg builds the 1080x1920 video: the gameplay over a blurred copy of itself, the big hook title on from
     the first frame (yellow key words), the streamer's credit under the picture, the game's own sound.
  3. The narrator (the same Gemini voice as the daily videos) says the hook line over the first seconds.
  4. It posts to TikTok and Instagram through the same logged-in Chrome profiles as the daily videos.

    python3 -m worker.clips make FILE [--no-post]   cut one clip now (and post it, unless --no-post)
    python3 -m worker.clips status                  the inbox and the last clips
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker import viral as wv

HOME = Path.home() / "Movies" / "Jarvis Clips"
INBOX = HOME / "inbox"                  # drop clips here
SKIPPED = INBOX / "skipped"             # clips Gemini found nothing in, or that couldn't be cut
OUT = HOME / "ready"                    # the finished vertical videos (deleted once posted to both apps)
STATE = ROOT / "memory" / "clips.json"
NS = "game_clips"
VIDEO = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
W, H = 1080, 1920
PICTURE_Y, PICTURE_H = 600, 720         # where a widescreen clip sits in the tall frame
DEFAULTS = {"enabled": True,
            "times": "11:00,15:30,20:30",   # between the daily videos (09:00, 13:00, 18:00); hourly posting killed
                                            # views in early November, so three a day and no more by default
            "post": True,
            "game": "GTA 6",
            "hashtags": "gta6,gtavi,gta,gaming,fyp",
            "min_seconds": 15,
            "max_seconds": 45,
            "voice_hook": True,             # the narrator says one hook line over the first seconds
            "ai_label": False}              # real gameplay footage, not AI-made video


class Skip(RuntimeError):
    """This clip isn't worth posting (or can't be cut): set it aside and take the next one."""


class Busy(RuntimeError):
    """Gemini couldn't be reached to watch the clip: try again later, never post a clip nobody watched."""


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS and v is not None})
    return out


def post_settings(cfg: dict) -> dict:
    """The clip settings plus the accounts, which are always the daily videos' own accounts."""
    v = wv.settings()
    return dict(cfg, instagram=v["instagram"], handle=v["handle"], ig_handle=v["ig_handle"])


# ── the inbox ────────────────────────────────────────────────────────────────
def inbox(folder: Path = INBOX, now: Optional[float] = None, settle_s: float = 60) -> list[Path]:
    """Clips waiting, oldest first. A file still being copied in (changed in the last minute) waits."""
    now = now or time.time()
    try:
        files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in VIDEO and not f.name.startswith(".")]
    except OSError:
        return []
    return sorted((f for f in files if now - f.stat().st_mtime >= settle_s), key=lambda f: f.stat().st_mtime)


def credit_for(src: Path) -> str:
    """The streamer to credit: the first line of "<clip>.txt", else a leading "@name" in the file name."""
    note = src.with_suffix(".txt")
    text = ""
    try:
        text = note.read_text().strip().splitlines()[0] if note.exists() else ""
    except (OSError, IndexError):
        text = ""
    m = re.match(r"@?([A-Za-z0-9_.]{2,30})\s*$", text.strip()) or re.match(r"@([A-Za-z0-9_.]{2,30})", src.name)
    return m.group(1).strip(".") if m else ""


def fingerprint(src: Path) -> str:
    """Size plus a hash of the first megabyte: the same clip dropped in twice is posted once."""
    with open(src, "rb") as f:
        return f"{src.stat().st_size}-{hashlib.md5(f.read(1 << 20)).hexdigest()[:16]}"


def probe(src: Path) -> dict:
    out = subprocess.run([wv.ffmpeg(), "-hide_banner", "-i", str(src)], capture_output=True, text=True, timeout=120).stderr
    return parse_probe(out)


def parse_probe(out: str) -> dict:
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    size = re.search(r"Stream #.*Video:.*?\b(\d{3,5})x(\d{3,5})\b", out)
    if not dur or not size:
        raise Skip("not a video ffmpeg can read")
    return {"duration": int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)),
            "w": int(size.group(1)), "h": int(size.group(2)), "audio": bool(re.search(r"Stream #.*Audio:", out))}


# ── 1. Gemini watches the clip ───────────────────────────────────────────────
PLAN_PROMPT = """You edit gameplay clips of {game} into viral vertical shorts for TikTok and Instagram Reels.
Watch and listen to the attached clip ({duration:.0f} seconds long) and answer with ONE JSON object:

{{"ok": true or false,            // false when nothing in it would make a stranger stop scrolling, when it is not
                                 // gameplay, or when it has slurs, real-world violence, nudity or someone's private details
 "why": "one short sentence",
 "start": seconds, "end": seconds,   // the best {min_s}-{max_s} second stretch. Start 1-3 seconds BEFORE the moment
                                 // builds (never mid-sentence), end right after the payoff or the reaction to it
 "title": "the hook shown on screen from the first frame: 4-9 words, says exactly what is about to happen, makes
           people need to see it. No emojis, no hashtags, no clickbait that the clip does not deliver",
 "highlight": ["1-3 words from the title to colour yellow"],
 "hook": "ONE spoken sentence (8-16 words) a narrator says over the first seconds to set the moment up, deadpan,
          adding something the title does not say. No 'watch till the end', no 'wait for it'",
 "caption": "the post's caption: one short line, lower-key than the title, a question or a take people will answer",
 "hashtags": ["2-4 specific tags without #, e.g. the mission, the vehicle, the glitch"]}}

Only describe what is really in the clip. JSON only."""


def gemini_plan(src: Path, info: dict, cfg: dict, log: Callable = print) -> Optional[dict]:
    """Gemini watches a small copy of the clip. None when it can't be reached."""
    from core import gemini
    from google.genai import types
    small = OUT / f".watch-{int(time.time())}.mp4"
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run([wv.ffmpeg(), "-loglevel", "quiet", "-y", "-i", str(src), "-t", "600", "-vf", "scale=-2:360", "-r", "12",
                    "-c:v", "libx264", "-crf", "32", "-preset", "veryfast", "-c:a", "aac", "-b:a", "64k", str(small)],
                   check=True, timeout=900)
    try:
        prompt = PLAN_PROMPT.format(game=cfg["game"], duration=info["duration"], min_s=cfg["min_seconds"], max_s=cfg["max_seconds"])
        c = gemini.client(timeout_ms=300000)
        for model in ("gemini-3.5-flash", "gemini-3.5-flash-lite"):
            try:
                r = c.models.generate_content(model=model, contents=[types.Part.from_bytes(data=small.read_bytes(),
                                              mime_type="video/mp4"), prompt],
                                              config=types.GenerateContentConfig(response_mime_type="application/json"))
                got = wv._json_from(r.text)
                if isinstance(got, dict):
                    return got
            except Exception as exc:
                log(f"clips: {model} couldn't watch the clip ({str(exc)[:80]})")
        return None
    finally:
        small.unlink(missing_ok=True)


def clean_plan(raw: dict, duration: float, cfg: dict) -> dict:
    """Gemini's answer made safe to cut with: a window inside the clip and of the right length, tidy text."""
    if not raw.get("ok"):
        raise Skip(str(raw.get("why") or "nothing worth posting in it")[:160])
    lo, hi = float(cfg["min_seconds"]), float(cfg["max_seconds"])
    start = max(0.0, min(wv._num(raw.get("start")), max(0.0, duration - 3)))
    end = wv._num(raw.get("end"), duration)
    end = duration if end <= start else min(end, duration)
    if end - start > hi:
        end = start + hi
    if end - start < lo:                                    # too short: grow it, later first, then earlier
        end = min(duration, start + lo)
        start = max(0.0, end - lo)
    if end - start < 6:
        raise Skip("the clip is too short to post")
    title = re.sub(r"\s+", " ", re.sub(r"#\w+|[#\U0001F000-\U0001FFFF☀-➿]", "", str(raw.get("title") or ""))).strip()
    if not title:
        raise Skip("no title for it")
    words = {w.lower() for h in raw.get("highlight") or [] for w in re.findall(r"[\w']+", str(h))}
    tags, seen = [], set()
    for t in list(raw.get("hashtags") or []) + str(cfg["hashtags"]).split(","):
        t = re.sub(r"[^a-z0-9]", "", str(t).lower())
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    return {"start": round(start, 2), "end": round(end, 2), "title": title[:80], "highlight": sorted(words)[:6],
            "hook": re.sub(r"\s+", " ", str(raw.get("hook") or "")).strip()[:200],
            "caption": re.sub(r"\s+", " ", str(raw.get("caption") or title)).strip()[:300], "hashtags": tags[:9]}


def caption_for(plan: dict, credit: str) -> str:
    by = f" (clip: {credit} on Twitch)" if credit else ""
    return f"{plan['caption']}{by} " + " ".join("#" + t for t in plan["hashtags"])


# ── 2. the vertical video ────────────────────────────────────────────────────
TITLE_FONT = ("/System/Library/Fonts/Supplemental/Impact.ttf", 0)
SMALL_FONT = ("/System/Library/Fonts/Avenir Next Condensed.ttc", 8)
YELLOW, WHITE = (255, 222, 33, 255), (255, 255, 255, 255)


def _font(spec, size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(spec[0], size, index=spec[1])
    except OSError:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Black.ttf", size)


def wrap_title(words: list[str], width_of: Callable[[str], float], max_w: float) -> list[list[str]]:
    lines, cur = [], []
    for w in words:
        if cur and width_of(" ".join(cur + [w])) > max_w:
            lines.append(cur)
            cur = []
        cur.append(w)
    return lines + ([cur] if cur else [])


def overlay(plan: dict, credit: str, out: Path, tall: bool = False) -> Path:
    """The see-through layer over the whole frame: the hook title above the picture (key words yellow) and the
    streamer's credit under it. `tall` = the clip already fills the frame, so the title sits on the footage."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    words = plan["title"].upper().split()
    top, bottom = (230, 560) if not tall else (230, 620)        # clear of TikTok's top bar; ends above the picture
    size = 132
    while True:                                                  # the biggest size that fits the space
        f = _font(TITLE_FONT, size)
        lines = wrap_title(words, lambda s: d.textlength(s, font=f), W - 120)
        line_h = int(size * 1.12)
        if (len(lines) * line_h <= bottom - top and all(d.textlength(" ".join(l), font=f) <= W - 120 for l in lines)) or size <= 56:
            break
        size -= 6
    y = bottom - len(lines) * line_h if not tall else top        # hug the picture; on a tall clip start at the top
    space = d.textlength(" ", font=f)
    for line in lines:
        x = (W - d.textlength(" ".join(line), font=f)) / 2
        for w in line:
            hot = re.sub(r"[^\w']", "", w).lower() in plan["highlight"]
            d.text((x, y), w, font=f, fill=YELLOW if hot else WHITE, stroke_width=max(6, size // 12), stroke_fill=(0, 0, 0, 255))
            x += d.textlength(w, font=f) + space
        y += line_h
    if credit:
        f2 = _font(SMALL_FONT, 44)
        text = f"clip: twitch.tv/{credit}"
        tw = d.textlength(text, font=f2)
        cy = PICTURE_Y + PICTURE_H + 26 if not tall else 1440
        d.rounded_rectangle(((W - tw) / 2 - 26, cy, (W + tw) / 2 + 26, cy + 70), radius=35, fill=(0, 0, 0, 170))
        d.text(((W - tw) / 2, cy + 8), text, font=f2, fill=WHITE)
    img.save(out)
    return out


def hook_voice(text: str, out: Path, log: Callable = print) -> Optional[Path]:
    """The narrator's one line, or None when the voice isn't free right now (the clip goes out without it)."""
    v = wv.settings()
    for model in [v["tts_model"]] + [m for m in wv.TTS_MODELS if m != v["tts_model"]]:
        if model == "none":
            continue
        try:
            out.write_bytes(wv.gemini_tts(text, model, v["voice"]))
            wv._tempo(out, float(v.get("voice_speed") or 1))
            return out
        except Exception as exc:
            log(f"clips: {model} voice failed ({str(exc)[:80]})")
    return None


def wav_seconds(path: Path) -> float:
    import wave
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def edit_command(src: Path, layer: Path, out: Path, plan: dict, info: dict, voice: Optional[Path] = None,
                 voice_s: float = 0) -> list[str]:
    """The ffmpeg call: blurred backdrop + the picture + the title layer; the clip's sound, ducked under the hook."""
    length = plan["end"] - plan["start"]
    tall = info["h"] > info["w"]
    cmd = [wv.ffmpeg(), "-loglevel", "error", "-y", "-ss", f"{plan['start']:.2f}", "-t", f"{length:.2f}", "-i", str(src),
           "-loop", "1", "-i", str(layer)]
    if voice:
        cmd += ["-i", str(voice)]
    if not info["audio"]:
        cmd += ["-f", "lavfi", "-t", f"{length:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cover = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if tall:
        video = f"[0:v]fps=30,{cover},setsar=1[v0]"
    else:
        video = (f"[0:v]fps=30,split[a][b];"
                 f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,scale={W}:{H},"
                 f"eq=brightness=-0.16:saturation=1.15[bg];"
                 f"[b]scale=-2:{PICTURE_H},crop='min(iw,{W})':{PICTURE_H}[fg];"
                 f"[bg][fg]overlay=(W-w)/2:{PICTURE_Y},setsar=1[v0]")
    video += ";[v0][1:v]overlay=0:0:shortest=1,format=yuv420p[v]"
    game = "[0:a]" if info["audio"] else f"[{3 if voice else 2}:a]"
    if voice:
        audio = (f"{game}aresample=48000,loudnorm=I=-16:TP=-2,volume=0.22:enable='lt(t,{voice_s + .35:.2f})'[g];"
                 f"[2:a]aresample=48000,adelay=150:all=1,volume=1.6[n];[g][n]amix=inputs=2:duration=first:normalize=0,"
                 f"alimiter=limit=0.8[au]")
    else:
        audio = f"{game}aresample=48000,loudnorm=I=-16:TP=-2[au]"
    return cmd + ["-filter_complex", video + ";" + audio, "-map", "[v]", "-map", "[au]", "-t", f"{length:.2f}",
                  "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-c:a", "aac", "-b:a", "192k",
                  "-movflags", "+faststart", str(out)]


def make(src: Path, cfg: Optional[dict] = None, log: Callable = print, plan_with: Callable = gemini_plan) -> dict:
    """One clip, from the file to the finished vertical video. Raises Skip or Busy."""
    cfg = cfg or settings()
    info = probe(src)
    raw = plan_with(src, info, cfg, log)
    if raw is None:
        raise Busy("Gemini couldn't watch the clip")
    plan = clean_plan(raw, info["duration"], cfg)
    credit = credit_for(src)
    OUT.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", plan["title"].lower()).strip("-")[:50]
    out = OUT / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.mp4"
    layer, voice_file = out.with_suffix(".png"), out.with_suffix(".wav")
    try:
        overlay(plan, credit, layer, tall=info["h"] > info["w"])
        voice = hook_voice(plan["hook"], voice_file, log) if cfg["voice_hook"] and plan["hook"] else None
        voice_s = wav_seconds(voice) if voice else 0
        if voice and voice_s > (plan["end"] - plan["start"]) * .6:   # a line that long would talk over the moment
            voice = None
        done = subprocess.run(edit_command(src, layer, out, plan, info, voice, voice_s), capture_output=True, text=True, timeout=1800)
        if done.returncode or not out.exists():
            raise Skip(f"ffmpeg couldn't cut it ({done.stderr.strip()[-200:]})")
    finally:
        layer.unlink(missing_ok=True)
        voice_file.unlink(missing_ok=True)
    log(f"clips: cut \"{plan['title']}\" ({plan['end'] - plan['start']:.0f}s from {src.name})")
    return {"path": str(out), "title": plan["title"], "caption": caption_for(plan, credit), "credit": credit,
            "src": str(src), "key": fingerprint(src), "voiced": bool(voice), "made": time.time()}


# ── 3. the daily slots ───────────────────────────────────────────────────────
class ClipService:
    PASS_SECONDS = 60
    BUSY_WAIT_MIN = 10

    def __init__(self, log: Callable[[str], None] = print, notify: Optional[Callable[[str, str], None]] = None):
        self.log, self.notify = log, notify or (lambda kind, text: None)
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._poster = wv.ViralService(log, notify)        # the daily videos' own posting, texting and retries

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="game-clips").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.PASS_SECONDS):
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"clips: pass failed: {exc!r}")

    def run_once(self, now: Optional[datetime] = None) -> str:
        cfg = settings()
        st = wv.load_state(STATE)
        if not cfg["enabled"] or time.time() < st.get("busy_until", 0) or not self._busy.acquire(blocking=False):
            return ""
        try:
            now = now or datetime.now()
            today = now.date().isoformat()
            done = st["slots"].get(today, [])
            due = wv.due_slots(cfg["times"], done, now, late_ok_h=2)
            if not due:
                return ""
            seen = {v.get("key") for v in st["videos"]}
            src = None
            for f in inbox():
                if fingerprint(f) in seen:                  # the same clip dropped in again
                    self._set_aside(f, "already posted")
                    continue
                src = f
                break
            if src is None:
                return "empty"                              # the slot stays open in case clips arrive in time
            try:
                rec = make(src, cfg, self.log)
            except Busy as exc:
                st = wv.load_state(STATE)
                st["busy_until"] = time.time() + self.BUSY_WAIT_MIN * 60
                wv.save_state(st, STATE)
                self.log(f"clips: {exc}; trying again in {self.BUSY_WAIT_MIN} minutes")
                return "waiting"
            except Skip as exc:
                self._set_aside(src, str(exc))
                return "skipped"                            # the slot stays open: the next pass takes the next clip
            st = wv.load_state(STATE)
            st["slots"] = {today: done + due}               # several due at once = one clip
            st["videos"] = (st["videos"] + [rec])[-300:]
            wv.save_state(st, STATE)
            return self.deliver(rec, cfg)
        finally:
            self._busy.release()

    def _set_aside(self, src: Path, why: str) -> None:
        SKIPPED.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(SKIPPED / src.name))
            note = src.with_suffix(".txt")
            if note.exists():
                shutil.move(str(note), str(SKIPPED / note.name))
        except OSError as exc:
            self.log(f"clips: couldn't move {src.name} aside: {exc}")
        self.log(f"clips: skipped {src.name}: {why}")

    def deliver(self, rec: dict, cfg: dict) -> str:
        pcfg = post_settings(cfg)
        tt = self._poster._deliver_tiktok(rec, pcfg)
        ig = self._poster.post_instagram(rec, pcfg) if pcfg["post"] and pcfg.get("instagram") else "off"
        self._mark(rec, posted=tt == "posted", ig_posted=ig == "posted")
        self._poster._text_result(rec, pcfg, tt, ig)
        if tt == "posted" and ig in ("posted", "off"):      # up everywhere: free the space, clip and source both
            Path(rec["path"]).unlink(missing_ok=True)
            src = Path(rec["src"])
            if src.parent == INBOX:
                src.unlink(missing_ok=True)
                src.with_suffix(".txt").unlink(missing_ok=True)
        elif Path(rec["src"]).parent == INBOX:              # made (and texted) but not up: never cut it a second time
            self._set_aside(Path(rec["src"]), "made but not posted everywhere")
        return tt

    def _mark(self, rec: dict, **fields) -> None:
        st = wv.load_state(STATE)
        for v in st["videos"]:
            if v.get("path") == rec["path"]:
                v.update(fields)
        wv.save_state(st, STATE)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "make" and len(argv) >= 2:
        cfg = settings()
        rec = make(Path(argv[1]).expanduser(), cfg)
        print(json.dumps(rec, indent=1))
        if cfg["post"] and "--no-post" not in argv:
            st = wv.load_state(STATE)
            st["videos"].append(rec)
            wv.save_state(st, STATE)
            print(ClipService().deliver(rec, cfg))
        return 0
    INBOX.mkdir(parents=True, exist_ok=True)
    print(f"inbox ({INBOX}): {len(inbox(settle_s=0))} clip(s) waiting")
    for v in wv.load_state(STATE)["videos"][-10:]:
        print(datetime.fromtimestamp(v["made"]).strftime("%m-%d %H:%M"),
              "TikTok" if v.get("posted") else "-", "Instagram" if v.get("ig_posted") else "-", v["title"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
