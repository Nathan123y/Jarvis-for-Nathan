"""Ugly Christmas sweater art (the shop's sweater line, added 2026-10-26 from the user's "GET LIT" photo).

Printify can't knit or add real lights, so the look is printed: a chunky pun up top, a Christmas tree drawn
as big knit stitches with glowing lights, and for the all-over-print sweater a black front and back with
fair-isle bands, red and white striped sleeves and red ribbing.

  design(slogan, seed)   transparent PNG (slogan + tree) for the tee and hoodie, on dark garments
  panels(design, sizes)  {print position: image} for the all-over-print sweater
"""
from __future__ import annotations

import random

FONT = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
WHITE = (245, 244, 238, 255)
RED = (200, 30, 40, 255)
GREEN = (40, 160, 70, 255)
DARK_GREEN = (25, 110, 50, 255)
GOLD = (250, 200, 50, 255)
BLACK = (18, 18, 20, 255)
LIGHTS = {"warm": [(255, 225, 140)], "multi": [(255, 90, 90), (90, 200, 255), (255, 220, 90), (150, 255, 140), (255, 140, 230)]}
STITCH = 24                       # pixels per knit stitch at print size


def _knit(img, stitch: int = STITCH):
    """Pixelated into stitch-sized blocks, each with a soft V so it reads as knitting."""
    from PIL import Image, ImageDraw
    w, h = img.size
    small = img.resize((max(1, w // stitch), max(1, h // stitch)), Image.NEAREST)
    big = small.resize((small.width * stitch, small.height * stitch), Image.NEAREST)
    shade = Image.new("RGBA", big.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(shade)
    for y in range(0, big.height, stitch):
        for x in range(0, big.width, stitch):
            d.line([(x + 2, y + 2), (x + stitch // 2, y + stitch - 3), (x + stitch - 2, y + 2)], fill=(0, 0, 0, 45), width=3)
    mask = big.getchannel("A")
    shaded = Image.alpha_composite(big, shade)
    shaded.putalpha(mask)
    return shaded


def tree(width: int, seed: int):
    """A knitted Christmas tree with a star, baubles and glowing lights, on transparent."""
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(seed)
    h = round(width * 1.0)
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top, base = round(h * 0.14), round(h * 0.9)
    tiers = 4
    for i in range(tiers):                                     # stacked, overlapping triangles
        y0 = top + (base - top) * i / tiers * 0.8
        y1 = top + (base - top) * (i + 1) / tiers
        half = width * (0.18 + 0.32 * (i + 1) / tiers)
        d.polygon([(width / 2, y0), (width / 2 - half, y1), (width / 2 + half, y1)], fill=GREEN)
    d.rectangle([width * 0.44, base, width * 0.56, h - 2], fill=(110, 70, 40, 255))
    knit = _knit(img)
    d = ImageDraw.Draw(knit)
    for _ in range(18):                                        # baubles
        y = rnd.uniform(top + 80, base - 40)
        half = width * 0.48 * (y - top) / (base - top)
        x = width / 2 + rnd.uniform(-half * 0.8, half * 0.8)
        r = width * 0.016
        d.ellipse([x - r, y - r, x + r, y + r], fill=rnd.choice([RED, GOLD, WHITE]))
    # the star: five points around the top
    import math
    cx, cy, R = width / 2, top + 10, width * 0.09
    pts = [(cx + (R if k % 2 == 0 else R * 0.45) * math.sin(k * math.pi / 5),
            cy - (R if k % 2 == 0 else R * 0.45) * math.cos(k * math.pi / 5)) for k in range(10)]
    d.polygon(pts, fill=GOLD)
    # lights: a glow layer blurred under bright cores
    colors = LIGHTS[rnd.choice(list(LIGHTS))]
    glow = Image.new("RGBA", knit.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cores = []
    big, core = width * 0.05, width * 0.013
    for _ in range(30):
        y = rnd.uniform(top + 60, base - 10)
        half = width * 0.48 * (y - top) / (base - top)
        x = width / 2 + rnd.uniform(-half * 0.9, half * 0.9)
        c = rnd.choice(colors)
        gd.ellipse([x - big, y - big, x + big, y + big], fill=c + (170,))
        cores.append((x, y, c))
    glow = glow.filter(ImageFilter.GaussianBlur(width * 0.018))
    out = Image.alpha_composite(knit, glow)            # the glow spills over the branches, like the real lights
    od = ImageDraw.Draw(out)
    for x, y, c in cores:
        od.ellipse([x - core, y - core, x + core, y + core], fill=c + (255,))
        od.ellipse([x - core * 0.55, y - core * 0.55, x + core * 0.55, y + core * 0.55], fill=(255, 252, 235, 255))
    return out


def design(slogan: str, seed: int, width: int = 3600):
    """Slogan over the tree, transparent, for printing on dark garments."""
    from PIL import Image, ImageDraw, ImageFont
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pt = 900
    words = slogan.upper().split()
    while pt > 160:
        font = ImageFont.truetype(FONT, pt)
        lines, cur = [], ""
        for wd in words:
            t = f"{cur} {wd}".strip()
            if probe.textlength(t, font=font) <= width * 0.95 or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = wd
        lines.append(cur)
        if len(lines) <= 2 and all(probe.textlength(l, font=font) <= width * 0.95 for l in lines):
            break
        pt -= 30
    line_h = round(pt * 1.12)
    t = tree(round(width * 0.82), seed)
    canvas = Image.new("RGBA", (width, line_h * len(lines) + 60 + t.height), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    y = 0
    for line in lines:
        w = d.textlength(line, font=font)
        d.text(((width - w) / 2, y), line, font=font, fill=WHITE, stroke_width=10, stroke_fill=RED)
        y += line_h
    canvas.alpha_composite(t, ((width - t.width) // 2, y + 60))
    return canvas


def _band(width: int, rows: int = 9):
    """A fair-isle band: red and white diamonds between two white rows, knitted."""
    from PIL import Image, ImageDraw
    s = STITCH * 2
    img = Image.new("RGBA", (width, rows * s), RED)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, s - 1], fill=WHITE)
    d.rectangle([0, (rows - 1) * s, width, rows * s], fill=WHITE)
    mid = rows // 2
    for col in range(width // s + 1):
        k = col % 8
        reach = min(k, 8 - k) - 2                  # a diamond every 8 stitches, clear of the white edge rows
        for r in range(mid - reach, mid + reach + 1):
            d.rectangle([col * s, r * s, col * s + s - 1, r * s + s - 1], fill=WHITE)
    return _knit(img, STITCH)


def _stripes(size: tuple, stripe: int = 150):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", size, WHITE)
    d = ImageDraw.Draw(img)
    for i, y in enumerate(range(0, size[1], stripe)):
        if i % 2 == 0:
            d.rectangle([0, y, size[0], y + stripe - 1], fill=RED)
    return _knit(img, STITCH)


def _rib(size: tuple):
    """Red ribbing: vertical knit ribs."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", size, RED)
    d = ImageDraw.Draw(img)
    for x in range(0, size[0], STITCH * 2):
        d.rectangle([x, 0, x + 5, size[1]], fill=(160, 20, 30, 255))
    return img


def panels(art, sizes: dict) -> dict:
    """{position: RGB image the size of that print area}. sizes: {position: (w, h)} from the catalog."""
    from PIL import Image
    out = {}
    for pos, (w, h) in sizes.items():
        if pos in ("front", "back"):
            img = Image.new("RGBA", (w, h), BLACK)
            band = _band(w)
            img.alpha_composite(band, (0, round(h * 0.05)))
            img.alpha_composite(band, (0, h - band.height - round(h * 0.03)))
            if pos == "front":
                room = (w * 0.9, h - 2 * band.height - h * 0.13)
                k = min(room[0] / art.width, room[1] / art.height)
                a = art.resize((round(art.width * k), round(art.height * k)), Image.LANCZOS)
                img.alpha_composite(a, ((w - a.width) // 2, round(h * 0.05) + band.height + round(h * 0.04)))
        elif "sleeve" in pos:
            img = _stripes((w, h))
        else:                                                  # cuffs, collar, waistband
            img = _rib((w, h))
        out[pos] = img.convert("RGB")
    return out
