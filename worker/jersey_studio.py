"""Jersey art made by ChatGPT (image generation through the Codex CLI on the user's ChatGPT Plus login, no API key).

Each concept is an ORIGINAL made-up club in the style of the user's crossover mockups (vintage early-2000s soccer and
basketball streetwear): the palettes and motifs come from the user's catalog, never a real team's logos, names or
players. generate() writes front.png and back.png (and sleeve.png for soccer) plus info.txt into the jersey inbox, where
worker/jersey_shop.py picks it up.

The worker makes `jerseys_per_day` a day from the queue below (plugin_config jersey_line); the user can also run
`python3 -m worker.jersey_studio <slug>` for one now.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / "memory" / "jersey_art" / "inbox"
STATE = ROOT / "memory" / "jersey_studio.json"
NS = "jersey_line"
DEFAULTS = {"enabled": True, "jerseys_per_day": 2, "time": "03:00", "auto_approve": False}

STYLE = """THE STYLE (copy it closely): vintage early-2000s soccer and basketball streetwear. Flowing asymmetric waves and
warped stripes, oversized graphics, washed team colours with aged cream or muted silver accents, distressed varsity
athletic lettering with a printed crackle and faded-ink texture (as if printed on new fabric). One dominant base colour,
one or two supporting colours and one restrained accent. Bold, wearable, eye-catching from across a room.
HARD RULES: no real team logos, crests, wordmarks, team names, league marks, player names, nicknames or likenesses; no
badge that resembles a real club's; no skeletons; spell every word exactly as given."""

# (slug, club name, garment, number, palette, graphics, title). The palettes and motifs are from the user's catalog
# (2026-11-19); the club names are ours.
CONCEPTS = [
    ("starfire", "STARFIRE", "basketball", "10", "navy base, red and sky-blue waves, aged cream accent",
     "a huge cracked-cream star outline behind the chest, red flames climbing from the hem",
     "Starfire Basketball Jersey Vintage Y2K Streetwear Navy Red Flames Star Jersey"),
    ("garnet-royale", "GARNET ROYALE", "soccer", "10", "garnet and royal-blue base stripes, a purple support, gold accent",
     "wavy warped vertical stripes, small gold stars, faint basketball seam contours sweeping across",
     "Garnet Royale Vintage Soccer Jersey Y2K Streetwear Garnet Blue Gold Stripes"),
    ("silver-crown", "SILVER CROWN", "soccer", "7", "white base, black support, muted silver accent",
     "sweeping silver waves, angular shield shapes, one small restrained original crown icon",
     "Silver Crown Vintage Soccer Jersey Y2K Streetwear White Black Silver Waves"),
    ("sunset-tide", "SUNSET TIDE", "soccer", "9", "black base, hot-pink and aqua supports, orange accent",
     "tropical waves, palm-leaf shadows, a pink-to-orange sunset gradient sweeping across",
     "Sunset Tide Vintage Soccer Jersey Y2K Streetwear Pink Aqua Black Tropical"),
    ("serpent-samba", "SERPENT SAMBA", "basketball", "18", "royal purple base, gold support, blue and garnet accents",
     "snake-scale texture flowing into curved football-style stripes, a coiled original serpent motif",
     "Serpent Samba Basketball Jersey Vintage Y2K Streetwear Purple Gold Snake Scales"),
    ("nighthowl", "NIGHTHOWL", "basketball", "21", "deep navy base, electric blue and purple supports, black accent",
     "glowing wolf eyes, sweeping black raven-wing feathers, fast diagonal waves",
     "Nighthowl Basketball Jersey Vintage Y2K Streetwear Navy Purple Wolf Feathers"),
    ("red-district", "RED DISTRICT", "basketball", "9", "red base, black support, white accent",
     "distorted stripes, sharp flowing lines, an original city skyline in faded ink along the hem",
     "Red District Basketball Jersey Vintage Y2K Streetwear Red Black Skyline"),
    ("steel-city-volt", "VOLT WORKS", "soccer", "4", "black base, yellow support, white accent",
     "industrial textures, electric lightning stripes, distressed geometry",
     "Volt Works Vintage Soccer Jersey Y2K Streetwear Black Yellow Lightning"),
    ("golden-gate-waves", "BAY GOLD", "basketball", "15", "royal blue base, gold support, white accent",
     "suspension-bridge cable lines, blue-and-gold waves, an original lion head drawn in faded ink",
     "Bay Gold Basketball Jersey Vintage Y2K Streetwear Royal Blue Gold Bridge"),
    ("emerald", "EMERALD", "basketball", "27", "kelly green base, white support, red accent",
     "vintage striping and an original soaring-bird emblem woven into the waves",
     "Emerald Basketball Jersey Vintage Y2K Streetwear Green White Red Waves"),
    ("sky-claw", "SKY CLAW", "soccer", "6", "sky blue base, navy support, gold outline accent",
     "bear-claw slashes turning into waves, pale-blue gradients, gold outlines",
     "Sky Claw Vintage Soccer Jersey Y2K Streetwear Sky Blue Navy Gold"),
    ("magic-serpent", "STAR TRAIL", "basketball", "1", "electric blue base, black support, silver and white accents",
     "serpentine curves, star trails, distorted blue stripes",
     "Star Trail Basketball Jersey Vintage Y2K Streetwear Blue Black Silver Stars"),
    ("firetide", "FIRETIDE", "basketball", "13", "black base, red support, aqua and orange accents",
     "flames transforming into ocean waves across the body",
     "Firetide Basketball Jersey Vintage Y2K Streetwear Black Red Aqua Flames Waves"),
    ("petal-strike", "PETAL STRIKE", "soccer", "14", "red base, black support, blue and garnet accents",
     "rose petals transforming into curved stripes",
     "Petal Strike Vintage Soccer Jersey Y2K Streetwear Red Black Roses"),
    ("comet-11", "COMET", "basketball", "11", "black base, silver support, French blue and red accents",
     "space-like curves, comet trails, silver starbursts",
     "Comet Basketball Jersey Vintage Y2K Streetwear Black Silver Blue Comet"),
    ("antler-heritage", "ANTLER", "soccer", "12", "forest green base, gold support, cream accent",
     "antler contours, curved stripes, heritage athletic typography",
     "Antler Vintage Soccer Jersey Y2K Streetwear Forest Green Gold Cream"),
]

PANEL = """Make it as FLAT PRINT ARTWORK for an all-over-print {garment} jersey, NOT a photo of a jersey: artwork only,
edge to edge, no garment shape, no mannequin, no shadows, no folds, no collar, no background, no margins, no mockup.
The pattern fills the whole image (its edges get trimmed off at the seams). Keep every letter and number inside the
middle 60% across and between 35% and 90% down from the top (the top corners become the armholes and neckline)."""


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def load(path: Path = STATE) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"made": {}, "failed": {}}


def save(st: dict, path: Path = STATE) -> None:
    path.write_text(json.dumps(st, indent=1))


def concept(slug: str) -> Optional[tuple]:
    return next((c for c in CONCEPTS if c[0] == slug), None)


def prompt(c: tuple) -> str:
    slug, name, garment, number, palette, graphics, _ = c
    kind = "sleeveless basketball" if garment == "basketball" else "short-sleeve soccer"
    parts = [
        f"You are my sportswear designer. Use your image generation tool to make the print artwork for one {kind} jersey "
        f"for an invented club called {name}.", STYLE,
        f"THIS CONCEPT: palette {palette}; graphics: {graphics}.",
        "Make these images, each one tall portrait (2:3, 1024x1536), and save each into the current directory:",
        f"1. front.png: the FRONT panel. {name} in big arched distressed varsity letters over a huge number {number}, "
        "over the waves and motifs.",
        f"2. back.png: the BACK panel, matching the front's colours, waves and texture so the two line up at the side "
        f"seams: a huge number {number} in the same lettering style, and no other words.",
    ]
    if garment == "soccer":
        parts.append("3. sleeve.png: a WIDE landscape image (3:2, 1536x1024) of the sleeve pattern: the same waves, stripes "
                     "and colours, no letters or numbers.")
    parts += [PANEL.format(garment=kind), "When the files are saved, reply with just their file names."]
    return "\n\n".join(parts)


def codex_bin() -> str:
    from core.gemini import _codex_bin
    exe = _codex_bin()
    if not exe:
        raise RuntimeError("the Codex CLI is not installed")
    return exe


def generate(slug: str, inbox: Path = INBOX, timeout: int = 900, run=subprocess.run) -> Path:
    """ChatGPT draws one concept's art; it lands in inbox/<slug>/ for the jersey worker. Returns that folder."""
    from PIL import Image
    c = concept(slug)
    if not c:
        raise ValueError(f"no concept called {slug}")
    need = ["front.png", "back.png"] + (["sleeve.png"] if c[2] == "soccer" else [])
    with tempfile.TemporaryDirectory(prefix="jarvis-jersey-") as tmp:
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}  # the subscription only
        r = run([codex_bin(), "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "--ephemeral", "-"],
                input=prompt(c), capture_output=True, text=True, cwd=tmp, env=env, timeout=timeout)
        missing = [n for n in need if not (Path(tmp) / n).is_file()]
        if missing:
            raise RuntimeError(f"ChatGPT didn't make {', '.join(missing)} (exit {r.returncode}): "
                               f"{(r.stderr or r.stdout or '')[-200:]}")
        for n in need:
            with Image.open(Path(tmp) / n) as im:
                if min(im.size) < 700:
                    raise RuntimeError(f"{n} came back too small ({im.size})")
        dest = inbox / slug
        staging = inbox.parent / f".{slug}-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for n in need:
            shutil.copy2(Path(tmp) / n, staging / n)
        (staging / "info.txt").write_text(f"name: {c[1]}\ntitle: {c[6]}\n" + ("soccer\n" if c[2] == "soccer" else ""))
        inbox.mkdir(parents=True, exist_ok=True)
        staging.rename(dest)                     # the folder appears whole, never half-written for the worker
    return dest


def next_concepts(n: int, path: Path = STATE, made_by_hand: set = frozenset()) -> list:
    st = load(path)
    out = []
    for c in CONCEPTS:
        if c[0] in st["made"] or c[0] in made_by_hand or st["failed"].get(c[0], 0) >= 2:
            continue
        out.append(c[0])
        if len(out) >= n:
            break
    return out


def make(slugs: list, path: Path = STATE, log=print) -> list:
    st = load(path)
    done = []
    for slug in slugs:
        try:
            generate(slug)
            st["made"][slug] = time.time()
            done.append(slug)
            log(f"jersey studio: ChatGPT drew {slug}")
        except Exception as exc:
            st["failed"][slug] = st["failed"].get(slug, 0) + 1
            log(f"jersey studio: {slug} failed: {str(exc)[:300]}")
        save(st, path)
    return done


if __name__ == "__main__":
    import sys
    args = sys.argv[1:] or next_concepts(1)
    print(make(args))
