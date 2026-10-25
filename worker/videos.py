"""Short vertical product videos for the shop (Reels / TikTok / Shorts), made from the designs and their
product photos. No AI video service: frames are drawn here and encoded with the ffmpeg that the
imageio-ffmpeg package bundles, and the music is synthesized here too, so nothing is borrowed.

Three formats, one of each a day:
  reveal    a hook line, the design zooms in, the slogan lands, then the products, then "shop link in bio"
  pov       "POV: ..." over a person wearing the shirt, slow push in, then the design and the end card
  gifts     "Gift ideas for <niche>": the shirt, the mug and the sticker with their prices

The hook, caption and hashtags come from Gemini once per design (memory/etsy_shop.json, rec["video"]).
Videos go to memory/etsy_shop/videos/ with their caption beside them.
"""
from __future__ import annotations

import math
import random
import subprocess
import wave
from pathlib import Path
from typing import Callable, Optional

W, H, FPS = 1080, 1920, 30
FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
FORMATS = ("reveal", "pov", "gifts")
INK, CREAM, GREEN = (34, 30, 38), (248, 243, 234), (47, 125, 79)

COPY_PROMPT = """Write social video copy for this print-on-demand design (shirt, mug, sticker) sold on Etsy.
Design: {title}. Slogan printed on it: "{slogan}". Who it's for: {niche}.
Reply with JSON only:
{{"hook": "a 3-8 word opening line that makes the target viewer stop scrolling, no hashtags",
  "pov": "a POV line, 'POV: ...', 4-10 words, relatable for {niche}",
  "caption": "1-2 short friendly sentences for the post, ending with 'Link in bio.'",
  "hashtags": ["8 to 12 relevant hashtags without the # sign, lowercase, no brand names"]}}"""


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ── copy ─────────────────────────────────────────────────────────────────────
def video_copy(rec: dict, ask: Optional[Callable] = None) -> dict:
    """Hook, POV line, caption and hashtags for a design; a plain fallback when Gemini is out."""
    if rec.get("video"):
        return rec["video"]
    out = None
    try:
        if ask is None:
            from core import gemini
            ask = lambda prompt: gemini.as_json(prompt, tier=gemini.SMART)
        out = ask(COPY_PROMPT.format(title=rec["title"], slogan=rec.get("slogan", ""), niche=rec.get("niche", "gift lovers")))
    except Exception:
        out = None
    niche = rec.get("niche") or "gift lovers"
    fallback = {"hook": f"Made for {niche}", "pov": f"POV: you found the perfect gift",
                "caption": f"{rec.get('slogan', '').title()}. Shirts, mugs and stickers. Link in bio.",
                "hashtags": ["giftideas", "etsyfinds", "funnyshirt", "smallbusiness", "printondemand"]}
    if not isinstance(out, dict):
        out = {}
    from worker.shop import BLOCKED
    copy = {k: (out.get(k) if out.get(k) and not BLOCKED.search(str(out.get(k))) else fallback[k]) for k in fallback}
    copy["hashtags"] = [h.strip("#").replace(" ", "").lower()[:30] for h in copy["hashtags"]][:12]
    return copy


# ── drawing ──────────────────────────────────────────────────────────────────
def _font(size: int):
    from PIL import ImageFont
    return ImageFont.truetype(FONT, size)


def _cover(img, w: int = W, h: int = H, zoom: float = 1.0, cx: float = 0.5, cy: float = 0.5):
    """`img` filling a w x h frame (cropping the overflow), zoomed in by `zoom` around (cx, cy)."""
    from PIL import Image
    scale = max(w / img.width, h / img.height) * zoom
    cw, ch = w / scale, h / scale
    left = min(max(cx * img.width - cw / 2, 0), img.width - cw)
    top = min(max(cy * img.height - ch / 2, 0), img.height - ch)
    return img.crop((int(left), int(top), int(left + cw), int(top + ch))).resize((w, h), Image.BILINEAR)


def _fit(img, box_w: int, box_h: int, scale: float = 1.0):
    from PIL import Image
    s = min(box_w / img.width, box_h / img.height) * scale
    return img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.BILINEAR)


def _text(frame, text: str, y: int, size: int = 92, fill=(255, 255, 255), stroke=INK, width: int = W - 120,
          alpha: float = 1.0, box: Optional[tuple] = None):
    """Centred, wrapped, outlined text; `box` draws a rounded backdrop of that colour behind it."""
    from PIL import Image, ImageDraw
    if alpha <= 0 or not text:
        return
    f = _font(size)
    d = ImageDraw.Draw(frame)
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if d.textlength(trial, font=f) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    lines.append(cur)
    line_h = int(size * 1.18)
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    if box:
        tw = max(ld.textlength(l, font=f) for l in lines)
        pad = int(size * 0.45)
        ld.rounded_rectangle(((W - tw) / 2 - pad, y - pad * 0.6, (W + tw) / 2 + pad, y + line_h * len(lines) + pad * 0.3),
                             radius=pad, fill=box + (235,))
    for i, line in enumerate(lines):
        tw = ld.textlength(line, font=f)
        ld.text(((W - tw) / 2, y + i * line_h), line, font=f, fill=fill + (255,),
                stroke_width=0 if box else max(3, size // 14), stroke_fill=stroke + (255,))
    if alpha < 1:
        layer.putalpha(layer.getchannel("A").point(lambda a: int(a * alpha)))
    frame.alpha_composite(layer)


def _ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def _end_card(frame, shop_name: str, t: float):
    from PIL import ImageDraw
    d = ImageDraw.Draw(frame)
    d.rectangle((0, 0, W, H), fill=CREAM + (255,))
    _text(frame, "Shirts · Mugs · Stickers", 640, 64, fill=INK, stroke=CREAM, alpha=_ease(t * 3))
    _text(frame, shop_name, 780, 104, fill=GREEN, stroke=CREAM, alpha=_ease(t * 3 - 0.3))
    _text(frame, "Link in bio", 1000, 84, fill=(255, 255, 255), alpha=_ease(t * 3 - 0.6), box=GREEN)


# ── the three formats ────────────────────────────────────────────────────────
def scenes(fmt: str, rec: dict, copy: dict, photos: dict, shop_name: str, products: list) -> list:
    """[(seconds, draw(frame, t))]: each draw paints one frame of its scene, t from 0 to 1."""
    from PIL import Image
    design = Image.open(rec["file"]).convert("RGBA")
    load = lambda p: Image.open(p).convert("RGBA")
    people = [load(p) for p in photos.get("tee", []) if "person" in p or "lifestyle" in p or "duo" in p]
    closeup = [load(p) for p in photos.get("tee", []) + photos.get("mug", []) if p.endswith("-design.jpg")][:1]
    detail = [load(p) for p in photos.get("tee", []) + photos.get("mug", []) if p.endswith("-detail.jpg")][:1]
    sticker = [load(p) for p in photos.get("sticker", []) if "context" in p]
    mug = [load(p) for p in photos.get("mug", []) if p.endswith("-front.jpg")]
    shots = people[:3] + sticker[:1] or closeup
    bg = Image.new("RGBA", (W, H), CREAM + (255,))

    def design_zoom(frame, t):
        frame.alpha_composite(bg)
        art = _fit(design, W - 160, H - 900, 0.82 + 0.18 * _ease(t))      # below the headline
        frame.alpha_composite(art, ((W - art.width) // 2, (H - art.height) // 2 + 120))

    def kenburns(img, label: str = "", z0=1.0, z1=1.12, cy=0.42):
        def draw(frame, t):
            frame.alpha_composite(_cover(img, zoom=z0 + (z1 - z0) * _ease(t), cy=cy))
            if label:
                _text(frame, label, 260, 70, alpha=_ease(t * 4), box=INK)
        return draw

    def hook_over(img, line: str):
        def draw(frame, t):
            frame.alpha_composite(_cover(img, zoom=1.08 - 0.08 * t, cy=0.4))
            _text(frame, line, 300, 88, alpha=_ease(t * 3), box=INK)
        return draw

    end = (3.0, lambda frame, t: _end_card(frame, shop_name, t))
    if fmt == "reveal":
        out = [(2.2, lambda f, t: (design_zoom(f, t * 0.3), _text(f, copy["hook"], 260, 92, fill=INK, stroke=CREAM,
                                                                      alpha=_ease(t * 3))))]
        out.append((2.6, lambda f, t: (design_zoom(f, 0.3 + 0.7 * t),)))
        out += [(1.8, kenburns(img)) for img in shots[:3]]
        return out + [end]
    if fmt == "pov":
        lead = people[0] if people else (closeup or [design])[0]
        out = [(3.2, hook_over(lead, copy["pov"]))]
        if detail:
            out.append((2.0, kenburns(detail[0], z0=1.0, z1=1.18, cy=0.5)))
        out += [(1.8, kenburns(img)) for img in (people[1:3] or shots[:2])]
        return out + [end]
    # gifts
    niche = rec.get("niche") or "gift lovers"
    out = [(2.4, lambda f, t: (design_zoom(f, 0.6), _text(f, f"Gift ideas for {niche}", 300, 96, fill=INK,
                                                         stroke=CREAM, alpha=_ease(t * 3))))]
    pics = {"tee": people[:1] or closeup, "mug": mug[:1] or closeup[:1], "sticker": sticker[:1] or detail[:1]}
    for n, (kind, name, price) in enumerate(products[:3], 1):
        img = (pics.get(kind) or closeup or [design])[0]
        out.append((2.2, kenburns(img, f"{n}. The {name.lower()} · ${price / 100:.2f}")))
    return out + [end]


# ── music ────────────────────────────────────────────────────────────────────
def music(path: Path, seconds: float, seed: int = 0, rate: int = 44100) -> None:
    """A gentle, original backing loop: soft chords, a plucked arpeggio and a light kick."""
    import numpy as np
    rng = random.Random(seed)
    root = rng.choice([57, 60, 62, 64, 65])                      # A3 / C4 / D4 / E4 / F4
    prog = rng.choice([[0, 7, 9, 5], [0, 5, 9, 7], [9, 5, 0, 7]])
    bpm = rng.choice([92, 100, 108])
    beat = 60 / bpm
    n = int(seconds * rate)
    t = np.arange(n) / rate
    out = np.zeros(n)
    hz = lambda m: 440 * 2 ** ((m - 69) / 12)
    bar = beat * 4
    for i in range(int(seconds / bar) + 1):
        start = int(i * bar * rate)
        if start >= n:
            break
        seg = slice(start, min(n, start + int(bar * rate)))
        tt = t[seg] - i * bar
        chord = root + prog[i % 4]
        env = np.minimum(1, tt / 0.3) * np.exp(-tt / (bar * 1.4))
        for iv in (0, 4 if prog[i % 4] in (0, 5, 7) else 3, 7):
            out[seg] += 0.08 * env * np.sin(2 * np.pi * hz(chord + iv - 12) * tt)
        for k in range(8):                                     # eighth-note arpeggio
            s = start + int(k * beat / 2 * rate)
            e = min(n, s + int(beat * rate))
            if s >= n:
                break
            ta = t[s:e] - t[s]
            note = chord + (0, 7, 12, 16, 12, 7, 4, 7)[k]
            out[s:e] += 0.05 * np.exp(-ta * 9) * np.sin(2 * np.pi * hz(note) * ta)
        for k in range(4):                                     # kick on each beat
            s = start + int(k * beat * rate)
            e = min(n, s + int(0.18 * rate))
            if s >= n:
                break
            tk = t[s:e] - t[s]
            out[s:e] += 0.18 * np.exp(-tk * 22) * np.sin(2 * np.pi * (55 + 90 * np.exp(-tk * 30)) * tk)
    fade = int(1.2 * rate)
    out[-fade:] *= np.linspace(1, 0, fade)
    out[: int(0.05 * rate)] *= np.linspace(0, 1, int(0.05 * rate))
    pcm = (np.clip(out / max(1e-9, np.abs(out).max()) * 0.8, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# ── rendering ────────────────────────────────────────────────────────────────
def render(parts: list, out: Path, seed: int = 0) -> Path:
    """Draw every frame, add the music, encode H.264 for Reels/TikTok/Shorts."""
    from PIL import Image
    out.parent.mkdir(parents=True, exist_ok=True)
    total = sum(sec for sec, _ in parts)
    wav = out.with_suffix(".wav")
    music(wav, total + 0.2, seed)
    cmd = [ffmpeg(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-i", str(wav), "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    prev = None
    try:
        for sec, draw in parts:
            frames = int(sec * FPS)
            for i in range(frames):
                frame = Image.new("RGBA", (W, H), CREAM + (255,))
                draw(frame, i / max(1, frames - 1))
                if prev is not None and i < 6:                 # a quick crossfade from the last scene
                    frame = Image.blend(prev, frame, (i + 1) / 7)
                proc.stdin.write(frame.convert("RGB").tobytes())
                last = frame
            prev = last
        proc.stdin.close()
        err = proc.stderr.read().decode()[-300:]
        if proc.wait(timeout=300):
            raise RuntimeError(f"ffmpeg failed: {err}")
    finally:
        if proc.poll() is None:
            proc.kill()
        wav.unlink(missing_ok=True)
    return out


def main_photos(rec: dict, printify: Optional[Callable], folder: Path) -> dict:
    """{kind: path} of each product's main photo (the one Etsy shows), saved once; the mug's only good one."""
    import requests
    out = {}
    for kind, prod in (rec.get("products") or {}).items():
        dest = folder / f"{rec['id']}-{kind}-front.jpg"
        if not dest.exists() and printify:
            try:
                imgs = printify("GET", f"shops/{prod['shop']}/products/{prod['id']}.json").get("images") or []
                src = next((i["src"] for i in imgs if i.get("is_default")), imgs[0]["src"] if imgs else None)
                r = requests.get(src, headers={"User-Agent": "Mozilla/5.0"}, timeout=60) if src else None
                if r is not None and r.status_code == 200:
                    folder.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(r.content)
            except Exception:
                pass
        if dest.exists():
            out[kind] = str(dest)
    return out


def make(rec: dict, fmt: str, out_dir: Path, shop_name: str, ask: Optional[Callable] = None,
         printify: Optional[Callable] = None) -> dict:
    """One video of `fmt` for a design: {"file", "caption", "format", "design"}."""
    from worker.shop import PRODUCTS
    copy = rec["video"] = video_copy(rec, ask)
    photos = {k: list(p.get("extra_photos") or []) for k, p in (rec.get("products") or {}).items()}
    for kind, path in main_photos(rec, printify, out_dir.parent / "photos").items():
        photos.setdefault(kind, []).append(path)
    products = [(k, PRODUCTS[k]["name"], p.get("price", 0)) for k, p in (rec.get("products") or {}).items() if k in PRODUCTS]
    path = out_dir / f"{rec['id']}-{fmt}.mp4"
    render(scenes(fmt, rec, copy, photos, shop_name, products), path, seed=hash((rec["id"], fmt)) & 0xFFFF)
    caption = copy["caption"] + "\n\n" + " ".join("#" + h for h in copy["hashtags"])
    path.with_suffix(".txt").write_text(caption)
    return {"file": str(path), "caption": caption, "format": fmt, "design": rec["id"]}
