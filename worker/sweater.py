"""Ugly Christmas sweater art (the shop's sweater line, added 2026-10-26 from the user's "GET LIT" photo; redrawn
2026-11-10 after its first sale so every design stops being the same flat tree).

Printify can't knit or add real lights, so the look is printed, and drawn here the way a knitter charts a sweater:
one square per stitch. The slogan is set in chunky charted letters, the picture is one of MOTIFS (a lit tree, a
reindeer, a snowman, ...) picked to fit the joke, fair-isle bands run above and below, and every stitch gets a
knitted V so it reads as wool up close. Lights get a soft glow on top.

  design(slogan, seed, motif, ground, layout, bands)   transparent PNG for tees and hoodies
        ground "dark" = cream letters for dark garments, "light" = dark letters for white ones
        layout "tall" = words over the picture, "wide" = words beside it (a hoodie's short print area)
  panels(design, sizes, seed)   {print position: image} for the all-over-print sweater
"""
from __future__ import annotations

import math
import random

STITCH = 20                       # pixels per knit stitch at print size
ACROSS = 184                      # stitches across a design: 3680 px

CREAM = (245, 240, 228, 255)
WHITE = (250, 250, 246, 255)
RED = (200, 30, 40, 255)
DARK_RED = (146, 20, 30, 255)
GREEN = (40, 150, 70, 255)
DARK_GREEN = (22, 100, 50, 255)
LIGHT_GREEN = (96, 190, 100, 255)
GOLD = (248, 196, 50, 255)
BROWN = (112, 72, 44, 255)
TAN = (198, 146, 92, 255)
DARK_TAN = (150, 104, 62, 255)
BLACK = (18, 18, 20, 255)
COAL = (40, 40, 46, 255)
ORANGE = (240, 128, 40, 255)
PINK = (244, 132, 160, 255)
BLUE = (64, 140, 230, 255)
ICE = (176, 218, 244, 255)
SKIN = (246, 200, 160, 255)
NONE = (0, 0, 0, 0)
# (body, accent) of the all-over sweater, picked per design so the line doesn't all look the same
SCHEMES = [(BLACK, RED), ((22, 32, 74, 255), RED), ((105, 16, 30, 255), (30, 120, 60, 255)), (BLACK, (30, 120, 60, 255))]
BULBS = {"warm": [(255, 225, 140)], "multi": [(255, 90, 90), (90, 190, 255), (255, 220, 90), (150, 255, 140), (255, 140, 230)]}
# the pictures a sweater design can carry; the Designer picks the one that fits its pun (worker/shop.py)
MOTIFS = ("tree", "reindeer", "snowman", "gingerbread", "ornament", "lights", "candy_canes", "santa_hat",
          "snowflake", "present", "elf", "wreath")

# ── charted letters: 5 stitches wide, 7 tall ────────────────────────────────────────────────────────────────────
_GLYPHS = {
    "A": ".###.|#...#|#...#|#####|#...#|#...#|#...#", "B": "####.|#...#|#...#|####.|#...#|#...#|####.",
    "C": ".###.|#...#|#....|#....|#....|#...#|.###.", "D": "####.|#...#|#...#|#...#|#...#|#...#|####.",
    "E": "#####|#....|#....|####.|#....|#....|#####", "F": "#####|#....|#....|####.|#....|#....|#....",
    "G": ".###.|#...#|#....|#.###|#...#|#...#|.###.", "H": "#...#|#...#|#...#|#####|#...#|#...#|#...#",
    "I": "###|.#.|.#.|.#.|.#.|.#.|###", "J": "..###|...#.|...#.|...#.|...#.|#..#.|.##..",
    "K": "#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#", "L": "#....|#....|#....|#....|#....|#....|#####",
    "M": "#...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#", "N": "#...#|##..#|#.#.#|#.#.#|#..##|#...#|#...#",
    "O": ".###.|#...#|#...#|#...#|#...#|#...#|.###.", "P": "####.|#...#|#...#|####.|#....|#....|#....",
    "Q": ".###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#", "R": "####.|#...#|#...#|####.|#.#..|#..#.|#...#",
    "S": ".####|#....|#....|.###.|....#|....#|####.", "T": "#####|..#..|..#..|..#..|..#..|..#..|..#..",
    "U": "#...#|#...#|#...#|#...#|#...#|#...#|.###.", "V": "#...#|#...#|#...#|#...#|#...#|.#.#.|..#..",
    "W": "#...#|#...#|#...#|#.#.#|#.#.#|##.##|#...#", "X": "#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#",
    "Y": "#...#|#...#|.#.#.|..#..|..#..|..#..|..#..", "Z": "#####|....#|...#.|..#..|.#...|#....|#####",
    "0": ".###.|#...#|#..##|#.#.#|##..#|#...#|.###.", "1": ".#.|##.|.#.|.#.|.#.|.#.|###",
    "2": ".###.|#...#|....#|...#.|..#..|.#...|#####", "3": "####.|....#|....#|.###.|....#|....#|####.",
    "4": "...#.|..##.|.#.#.|#..#.|#####|...#.|...#.", "5": "#####|#....|####.|....#|....#|#...#|.###.",
    "6": ".###.|#....|#....|####.|#...#|#...#|.###.", "7": "#####|....#|...#.|..#..|..#..|..#..|..#..",
    "8": ".###.|#...#|#...#|.###.|#...#|#...#|.###.", "9": ".###.|#...#|#...#|.####|....#|....#|.###.",
    "'": "#|#|.|.|.|.|.", "!": "#|#|#|#|#|.|#", ".": ".|.|.|.|.|.|#", ",": "..|..|..|..|..|.#|#.",
    "?": ".###.|#...#|....#|...#.|..#..|.....|..#..", "-": "...|...|...|###|...|...|...",
    "&": ".##..|#..#.|#..#.|.##..|#.#.#|#..#.|.##.#", "*": ".....|#.#.#|.###.|#####|.###.|#.#.#|.....",
    ":": ".|.|#|.|.|#|.", "/": "....#|....#|...#.|..#..|.#...|#....|#....", " ": "...|...|...|...|...|...|...",
}
GLYPH_H = 7


# letters whose diagonals fill in when simply thickened get their own bold chart
_BOLD = {
    "M": "##...##|###.###|#######|##.#.##|##...##|##...##|##...##",
    "N": "##...##|###..##|####.##|##.####|##..###|##...##|##...##",
    "W": "##...##|##...##|##...##|##.#.##|#######|###.###|##...##",
    "G": ".####.|##..##|##....|##.###|##..##|##..##|.####.",
    "Q": ".####.|##..##|##..##|##..##|##.###|##..##|.###.#",
    "0": ".####.|##..##|##..##|##..##|##..##|##..##|.####.",
    "&": ".###..|##.##.|##.##.|.###..|###.##|##.##.|.##.##",
    "*": _GLYPHS["*"],
}


def _glyph(ch: str, bold: bool = False) -> list:
    """A letter's rows; bold thickens every upright by a stitch, the way sweater letters are charted."""
    rows = _GLYPHS.get(ch, _GLYPHS["?" if ch.strip() else " "]).split("|")
    if not bold or not ch.strip():
        return rows
    if ch in _BOLD:
        return _BOLD[ch].split("|")
    return ["".join("#" if (c < len(r) and r[c] == "#") or (c and r[c - 1] == "#") else "." for c in range(len(r) + 1))
            for r in rows]


def text_width(line: str, bold: bool = False) -> int:
    """A line's width in letter squares (one square of space between letters)."""
    return sum(len(_glyph(ch, bold)[0]) for ch in line) + max(0, len(line) - 1)


BOLD_WORTH = 1.3                  # bold letters read as well as plain ones this much bigger: on a listing's small photo
                                  # it's the weight of the strokes that carries (at 1.12 thin letters a size up kept winning)


def fit_lines(slogan: str, across: int, max_lines: int = 3, max_scale: int = 4, frame=None) -> tuple:
    """(lines, scale, bold): how to set the slogan in a column `across` stitches wide so its letters print biggest.
    scale = stitches per letter square; bold thickens the uprights. frame(text_w, text_h) -> (design_w, design_h,
    aspect of the print area) lets the choice account for the whole design being shrunk to fit a shirt or a hoodie:
    three big lines that make the design too tall can lose to two smaller ones. Without it, the biggest letters that
    fit the column win, in the fewest lines."""
    words = slogan.upper().split() or [""]
    best, best_score = None, 0.0
    for scale in range(max_scale, 0, -1):
        room = across // scale
        for bold in (True, False):
            if any(text_width(w, bold) > room for w in words):
                continue
            for n in range(1, max_lines + 1):
                lines = _balance(words, n, room, bold)
                if not lines:
                    continue
                shrink = 1.0
                if frame:
                    w, h, aspect = frame(max(text_width(l, bold) for l in lines) * scale, _text_height(n, scale))
                    shrink = min(aspect / w, 1.0 / h)
                score = scale * shrink * (BOLD_WORTH if bold else 1.0) * (1 - 0.03 * (n - 1))
                if score > best_score * 1.001:
                    best, best_score = (lines, scale, bold), score
                if not frame:
                    break                                       # fewest lines at this size
            if best and not frame:
                return best
    return best or ([slogan.upper()[: across // 6]], 1, False)   # a word too long even at one stitch a square


LINE_GAP = 3                      # squares between lines of letters


def _text_height(n: int, scale: int) -> int:
    return (n * GLYPH_H + (n - 1) * LINE_GAP) * scale


def _balance(words: list, n: int, room: int, bold: bool = False):
    """`words` in exactly `n` lines, each within `room` squares, as even as they go; None if they can't."""
    if n == 1:
        line = " ".join(words)
        return [line] if text_width(line, bold) <= room else None
    best, best_score = None, None
    for i in range(1, len(words) - n + 2):
        head = " ".join(words[:i])
        if text_width(head, bold) > room:
            break
        rest = _balance(words[i:], n - 1, room, bold)
        if rest:
            widths = [text_width(l, bold) for l in [head] + rest]
            score = max(widths) - min(widths)
            if best_score is None or score < best_score:
                best, best_score = [head] + rest, score
    return best


def text_chart(lines: list, scale: int, ink, bold: bool = False, shadow=None):
    """The lines as a chart (one pixel a stitch), centred. shadow: a colour for a drop shadow half a square down and
    to the right, which is what gives charted letters their weight."""
    from PIL import Image
    off = max(1, scale // 2) if shadow else 0
    widths = [text_width(l, bold) * scale for l in lines]
    w = max(widths + [1]) + off
    h = _text_height(len(lines), scale) + off
    layers = []
    for colour, shift in ((shadow, off), (ink, 0)) if shadow else ((ink, 0),):
        img = Image.new("RGBA", (w, h), NONE)
        px = img.load()
        y0 = shift
        for line, lw in zip(lines, widths):
            x0 = (w - off - lw) // 2 + shift
            for ch in line:
                rows = _glyph(ch, bold)
                for r, row in enumerate(rows):
                    for c, bit in enumerate(row):
                        if bit == "#":
                            for dy in range(scale):
                                for dx in range(scale):
                                    px[x0 + c * scale + dx, y0 + r * scale + dy] = colour
                x0 += (len(rows[0]) + 1) * scale
            y0 += (GLYPH_H + LINE_GAP) * scale
        layers.append(img)
    out = layers[0]
    for layer in layers[1:]:
        out.alpha_composite(layer)
    return out


# ── knitting a chart ─────────────────────────────────────────────────────────────────────────────────────────────
_TILES: dict = {}


def _stitch_tile(stitch: int):
    """One stitch's shading: a V of two plump strands, darker in the gaps between stitches."""
    if stitch not in _TILES:
        from PIL import Image, ImageDraw, ImageFilter
        big = stitch * 4
        tile = Image.new("L", (big, big), 150)
        d = ImageDraw.Draw(tile)
        wd = int(big * 0.34)
        d.line([(0, -big * 0.15), (big / 2, big * 0.95)], fill=255, width=wd)
        d.line([(big, -big * 0.15), (big / 2, big * 0.95)], fill=255, width=wd)
        d.line([(big / 2, -big * 0.2), (big / 2, big * 0.42)], fill=120, width=max(1, big // 14))   # the gap in the V
        tile = tile.filter(ImageFilter.GaussianBlur(big / 22)).resize((stitch, stitch), Image.LANCZOS)
        _TILES[stitch] = tile
    return _TILES[stitch]


def knit(chart, stitch: int = STITCH, depth: float = 0.30):
    """A chart blown up to print size, each stitch shaded as a knitted V. `depth` is how dark the gaps go."""
    import numpy as np
    from PIL import Image
    big = chart.resize((chart.width * stitch, chart.height * stitch), Image.NEAREST)
    tile = np.asarray(_stitch_tile(stitch), dtype=np.float32) / 255.0
    shade = np.tile(tile, (chart.height, chart.width))
    arr = np.asarray(big, dtype=np.float32)
    arr[..., :3] *= (1 - depth + depth * shade)[..., None]
    return Image.fromarray(arr.clip(0, 255).astype("uint8"), "RGBA")


def glow(img, lights: list, stitch: int = STITCH):
    """Bulbs that shine: a soft halo of each light's colour, then a bright core. lights: [(x, y, rgb, size)] in
    stitches."""
    from PIL import Image, ImageDraw, ImageFilter
    if not lights:
        return img
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    for x, y, c, size in lights:
        r = size * stitch * 2.3
        hd.ellipse([x * stitch - r, y * stitch - r, x * stitch + r, y * stitch + r], fill=tuple(c) + (175,))
    halo = halo.filter(ImageFilter.GaussianBlur(stitch * 1.5))
    mask = img.getchannel("A").filter(ImageFilter.GaussianBlur(stitch * 0.8))   # the halo stays on (or right beside) the yarn
    halo.putalpha(Image.composite(halo.getchannel("A"), Image.new("L", img.size, 0), mask.point(lambda v: 255 if v > 30 else 0)))
    out = Image.alpha_composite(img, halo)
    od = ImageDraw.Draw(out)
    for x, y, c, size in lights:
        r = size * stitch * 0.62
        od.ellipse([x * stitch - r, y * stitch - r, x * stitch + r, y * stitch + r], fill=tuple(c) + (255,))
        r *= 0.5
        od.ellipse([x * stitch - r, y * stitch - r, x * stitch + r, y * stitch + r], fill=(255, 252, 236, 255))
    return out


# ── the pictures, each drawn as a chart: (image, lights) ─────────────────────────────────────────────────────────
def _new(w: int, h: int):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), NONE)
    return img, ImageDraw.Draw(img)


def _star(d, cx: float, cy: float, r: float, fill=GOLD):
    pts = [(cx + (r if k % 2 == 0 else r * 0.42) * math.sin(k * math.pi / 5),
            cy - (r if k % 2 == 0 else r * 0.42) * math.cos(k * math.pi / 5)) for k in range(10)]
    d.polygon(pts, fill=fill)


def _bow(d, cx: int, cy: int, s: int, fill=RED, knot=DARK_RED):
    d.polygon([(cx, cy), (cx - s, cy - s // 2 - 1), (cx - s, cy + s // 2 + 1)], fill=fill)
    d.polygon([(cx, cy), (cx + s, cy - s // 2 - 1), (cx + s, cy + s // 2 + 1)], fill=fill)
    d.rectangle([cx - 1, cy - 1, cx + 1, cy + 1], fill=knot)


def _tree(rnd, w: int = 100, h: int = 118):
    img, d = _new(w, h)
    cx = w // 2
    top, base = 15, h - 17
    tiers = 5
    edges = []
    for i in range(tiers):                                       # stacked boughs, wider towards the ground
        y0 = top + (base - top) * i / tiers * 0.80
        y1 = top + (base - top) * (i + 1) / tiers
        half = 9 + (w / 2 - 11) * (i + 1) / tiers
        d.polygon([(cx, y0 - 1), (cx - half, y1), (cx + half, y1)], fill=GREEN)
        d.polygon([(cx, y0 + (y1 - y0) * 0.55), (cx - half, y1), (cx + half, y1)], fill=DARK_GREEN)   # the shaded underside
        for x in range(int(cx - half) + 1, int(cx + half), 4):   # a toothed lower edge, like needles
            d.polygon([(x, y1), (x + 2, y1 + 2), (x + 4, y1)], fill=DARK_GREEN)
        edges.append((y0, y1, half))
    px = img.load()
    for _ in range(150):                                         # lighter needles
        y = rnd.randrange(top, base)
        x = rnd.randrange(8, w - 8)
        if px[x, y] == GREEN and px[x + 1, y + 1] == GREEN:
            px[x, y] = px[x + 1, y + 1] = LIGHT_GREEN
    d.rectangle([cx - 5, base + 2, cx + 4, h - 6], fill=BROWN)    # trunk
    d.rectangle([cx - 5, base + 2, cx - 3, h - 6], fill=DARK_TAN)
    # presents either side of the trunk
    d.rectangle([cx - 27, h - 15, cx - 10, h - 4], fill=RED)
    d.rectangle([cx - 20, h - 15, cx - 18, h - 4], fill=GOLD)
    d.rectangle([cx - 27, h - 11, cx - 10, h - 10], fill=GOLD)
    d.rectangle([cx + 9, h - 12, cx + 23, h - 4], fill=BLUE)
    d.rectangle([cx + 15, h - 12, cx + 16, h - 4], fill=CREAM)
    d.rectangle([cx + 9, h - 9, cx + 23, h - 9], fill=CREAM)
    d.rectangle([4, h - 4, w - 5, h - 1], fill=CREAM)             # snow on the ground
    for x in range(4, w - 5, 9):
        d.rectangle([x + 2, h - 5, x + 6, h - 5], fill=CREAM)
    colors = BULBS[rnd.choice(list(BULBS))]
    lights = []
    for i, (y0, y1, half) in enumerate(edges):                   # a garland swagged across each tier, bulbs along it
        ya, yb = y0 + (y1 - y0) * 0.50, y0 + (y1 - y0) * 0.88
        if i % 2:
            ya, yb = yb, ya
        reach = half * 0.80 * (0.55 if i == 0 else 1)
        n = max(3, int(reach * 2 / 7))
        for k in range(n + 1):
            t = k / n
            x = cx - reach + 2 * reach * t
            y = ya + (yb - ya) * t + math.sin(t * math.pi) * 3.2
            if k < n:
                x2 = cx - reach + 2 * reach * (k + 1) / n
                y2 = ya + (yb - ya) * (k + 1) / n + math.sin((k + 1) / n * math.pi) * 3.2
                d.line([(x, y), (x2, y2)], fill=GOLD)
            lights.append((x + 0.5, y + 0.5, colors[(k + i) % len(colors)], 1.35))
    for _ in range(9):                                           # baubles
        i = rnd.randrange(1, tiers)
        y0, y1, half = edges[i]
        y = rnd.uniform(y0 + (y1 - y0) * 0.35, y1 - 3)
        x = cx + rnd.uniform(-half * 0.6, half * 0.6) * (y - y0) / (y1 - y0)
        c = rnd.choice([RED, CREAM, BLUE, PINK])
        d.ellipse([x - 1.5, y - 1.5, x + 1.5, y + 1.5], fill=c)
    _star(d, cx, top - 5, 9)
    lights.append((cx, top - 5, (255, 236, 150), 1.6))
    return img, lights


def _reindeer(rnd, w: int = 100, h: int = 112):
    img, d = _new(w, h)
    cx = w // 2
    for side in (-1, 1):                                         # antlers
        bx = cx + side * 15
        d.line([(bx, 44), (bx + side * 9, 22), (bx + side * 10, 6)], fill=TAN, width=4)
        d.line([(bx + side * 6, 30), (bx + side * 20, 24), (bx + side * 26, 12)], fill=TAN, width=3)
        d.line([(bx + side * 9, 20), (bx + side * 1, 8)], fill=TAN, width=3)
        d.line([(bx + side * 17, 25), (bx + side * 24, 30)], fill=TAN, width=3)
        d.polygon([(cx + side * 22, 50), (cx + side * 42, 40), (cx + side * 40, 56), (cx + side * 26, 60)], fill=BROWN)   # ear
        d.polygon([(cx + side * 27, 52), (cx + side * 38, 46), (cx + side * 37, 54), (cx + side * 29, 57)], fill=PINK)
    d.ellipse([cx - 27, 38, cx + 27, 92], fill=BROWN)             # head
    d.ellipse([cx - 20, 62, cx + 20, 100], fill=TAN)              # muzzle
    for side in (-1, 1):
        ex = cx + side * 12
        d.ellipse([ex - 5, 52, ex + 5, 64], fill=WHITE)
        d.ellipse([ex - 2 + side, 56, ex + 2 + side, 62], fill=BLACK)
        img.putpixel((ex + side, 57), WHITE)
        d.ellipse([cx + side * 19 - 3, 70, cx + side * 19 + 3, 75], fill=PINK)   # rosy cheeks
    d.ellipse([cx - 9, 66, cx + 9, 82], fill=RED)                 # the nose
    d.rectangle([cx - 5, 69, cx - 3, 71], fill=WHITE)
    d.arc([cx - 9, 80, cx + 9, 94], 20, 160, fill=BLACK, width=2)   # smile
    # a scarf under the chin
    d.rectangle([cx - 24, 98, cx + 24, 106], fill=RED)
    for x in range(cx - 24, cx + 24, 8):
        d.rectangle([x, 98, x + 3, 106], fill=CREAM)
    d.rectangle([cx + 8, 104, cx + 18, h - 1], fill=RED)
    d.rectangle([cx + 8, h - 3, cx + 18, h - 3], fill=CREAM)
    lights = [(cx + 0.5, 73, (255, 80, 80), 2.2)]                 # the nose glows
    colors = BULBS["multi"]
    for k, (x, y) in enumerate([(cx - 25, 18), (cx - 37, 22), (cx - 16, 10), (cx + 25, 18), (cx + 38, 22), (cx + 16, 10),
                                (cx - 30, 32), (cx + 30, 32)]):
        lights.append((x, y, colors[k % len(colors)], 1.0))       # fairy lights caught in the antlers
    return img, lights


def _snowman(rnd, w: int = 96, h: int = 118):
    img, d = _new(w, h)
    cx = w // 2
    d.line([(cx - 16, 62), (cx - 40, 46)], fill=BROWN, width=2)   # stick arms
    d.line([(cx - 33, 51), (cx - 38, 40)], fill=BROWN, width=2)
    d.line([(cx + 16, 62), (cx + 40, 46)], fill=BROWN, width=2)
    d.line([(cx + 33, 51), (cx + 38, 40)], fill=BROWN, width=2)
    d.ellipse([cx - 30, 62, cx + 30, h - 2], fill=WHITE)          # body
    d.ellipse([cx - 22, 34, cx + 22, 76], fill=WHITE)             # chest
    d.ellipse([cx - 16, 14, cx + 16, 44], fill=WHITE)             # head
    d.chord([cx - 30, 62, cx + 30, h - 2], 20, 160, fill=ICE)     # a little shading underneath
    d.rectangle([cx - 15, 10, cx + 15, 14], fill=COAL)            # hat brim
    d.rectangle([cx - 10, -2, cx + 10, 10], fill=COAL)
    d.rectangle([cx - 10, 6, cx + 10, 9], fill=RED)
    for ex in (cx - 6, cx + 5):
        d.rectangle([ex, 22, ex + 1, 24], fill=COAL)              # coal eyes
    d.polygon([(cx - 1, 27), (cx + 12, 30), (cx - 1, 32)], fill=ORANGE)   # carrot nose
    for k, x in enumerate(range(cx - 7, cx + 8, 3)):              # a coal smile
        img.putpixel((x, 36 + (1 if k in (0, 4) else 2 if k in (1, 3) else 3) - 2), COAL)
    d.rectangle([cx - 17, 41, cx + 17, 47], fill=RED)             # scarf
    for x in range(cx - 17, cx + 17, 6):
        d.rectangle([x, 41, x + 2, 47], fill=GREEN)
    d.rectangle([cx + 6, 46, cx + 14, 64], fill=RED)
    d.rectangle([cx + 6, 60, cx + 14, 61], fill=GREEN)
    for y in (56, 68, 82, 96):                                    # buttons
        d.ellipse([cx - 2, y - 2, cx + 2, y + 2], fill=COAL)
    return img, []


def _gingerbread(rnd, w: int = 96, h: int = 116):
    img, d = _new(w, h)
    cx = w // 2
    d.ellipse([cx - 19, 2, cx + 19, 40], fill=TAN)                # head
    d.rounded_rectangle([cx - 17, 34, cx + 17, 84], radius=8, fill=TAN)   # body
    d.rounded_rectangle([cx - 44, 40, cx + 44, 56], radius=8, fill=TAN)   # arms
    d.rounded_rectangle([cx - 20, 72, cx - 3, h - 2], radius=7, fill=TAN)  # legs
    d.rounded_rectangle([cx + 3, 72, cx + 20, h - 2], radius=7, fill=TAN)
    for x0 in (cx - 43, cx + 33):                                 # icing cuffs
        for k, x in enumerate(range(x0, x0 + 10, 2)):
            d.rectangle([x, 43 + (k % 2) * 2, x + 1, 52 - (k % 2) * 2], fill=WHITE)
    for x0 in (cx - 19, cx + 4):
        for k, x in enumerate(range(x0, x0 + 15, 2)):
            d.rectangle([x, h - 12 + (k % 2) * 2, x + 1, h - 9 + (k % 2) * 2], fill=WHITE)
    for ex in (cx - 9, cx + 6):
        d.rectangle([ex, 14, ex + 3, 18], fill=COAL)              # eyes
        img.putpixel((ex + 1, 15), WHITE)
    d.arc([cx - 10, 16, cx + 10, 32], 25, 155, fill=WHITE, width=2)   # icing smile
    d.ellipse([cx - 15, 22, cx - 10, 27], fill=PINK)
    d.ellipse([cx + 10, 22, cx + 15, 27], fill=PINK)
    for y, c in ((46, RED), (58, GREEN), (70, RED)):              # gumdrop buttons
        d.ellipse([cx - 3, y - 3, cx + 3, y + 3], fill=c)
    _bow(d, cx, 37, 6, RED, DARK_RED)
    return img, []


def _ornament(rnd, w: int = 96, h: int = 116):
    img, d = _new(w, h)
    cx = w // 2
    d.arc([cx - 6, 0, cx + 6, 14], 180, 360, fill=GOLD, width=2)  # hook
    d.rectangle([cx - 7, 12, cx + 7, 20], fill=GOLD)              # cap
    d.rectangle([cx - 7, 15, cx + 7, 16], fill=DARK_TAN)
    box = [cx - 44, 20, cx + 44, 108]
    d.ellipse(box, fill=RED)
    d.chord(box, 20, 160, fill=DARK_RED)                          # shaded underside
    band = img.copy()                                             # a patterned band round the middle, cut to the ball
    bd = __import__("PIL.ImageDraw", fromlist=["Draw"]).Draw(band)
    bd.rectangle([0, 50, w, 78], fill=CREAM)
    bd.rectangle([0, 53, w, 54], fill=GREEN)
    bd.rectangle([0, 74, w, 75], fill=GREEN)
    for k, x in enumerate(range(-2, w, 8)):
        bd.polygon([(x, 71), (x + 4, 58), (x + 8, 71)], fill=RED if k % 2 else GREEN)
    ball = img.getchannel("A").point(lambda v: 255 if v else 0)
    img.paste(band, (0, 0), ball)
    d = __import__("PIL.ImageDraw", fromlist=["Draw"]).Draw(img)
    d.rectangle([cx - 7, 12, cx + 7, 20], fill=GOLD)
    d.rectangle([cx - 7, 15, cx + 7, 16], fill=DARK_TAN)
    d.ellipse([cx - 30, 30, cx - 20, 42], fill=PINK)              # shine
    d.ellipse([cx - 28, 32, cx - 24, 37], fill=WHITE)
    for x, y in ((cx - 26, 88), (cx - 8, 96), (cx + 12, 94), (cx + 28, 84), (cx + 22, 36), (cx, 30)):
        _star(d, x, y, 3.2, GOLD)
    return img, [(cx - 25.5, 35, (255, 240, 240), 1.3)]


def _lights(rnd, w: int = 110, h: int = 86):
    img, d = _new(w, h)
    lights = []
    colors = [RED, GOLD, GREEN, BLUE, PINK, ORANGE]
    k = 0
    for row, (ya, sag) in enumerate(((6, 16), (44, 16))):
        pts = [(x, ya + math.sin((x - 2) / (w - 5) * math.pi) * sag) for x in range(2, w - 2)]
        d.line(pts, fill=DARK_GREEN, width=2)                     # the wire
        for x in range(9 + row * 6, w - 8, 15):
            y = ya + math.sin((x - 2) / (w - 5) * math.pi) * sag
            c = colors[k % len(colors)]
            k += 1
            d.rectangle([x - 2, y + 1, x + 2, y + 4], fill=COAL)  # socket
            d.ellipse([x - 5, y + 4, x + 5, y + 20], fill=c)      # bulb
            d.ellipse([x - 3, y + 7, x - 1, y + 11], fill=WHITE)
            lights.append((x + 0.5, y + 12, c[:3], 1.5))
    return img, lights


def _candy_canes(rnd, w: int = 116, h: int = 116):
    """Two canes crossed in an X, hooks turned outwards, tied with a bow where they cross."""
    from PIL import Image, ImageDraw
    img, _ = _new(w, h)
    cx = w // 2
    for flip in (False, True):
        cane, d = _new(w, h)
        d.line([(cx, h - 2), (cx, 24)], fill=WHITE, width=10)             # the stick, through the middle
        d.arc([cx - 5, 8, cx + 27, 40], 180, 360, fill=WHITE, width=10)   # the hook, over to the right
        d.line([(cx + 22, 24), (cx + 22, 34)], fill=WHITE, width=10)
        stripes, sd = _new(w, h)
        for y in range(-w, h + w, 12):
            sd.polygon([(0, y), (w, y - w * 0.8), (w, y - w * 0.8 + 6), (0, y + 6)], fill=RED)
        cane.paste(stripes, (0, 0), Image.composite(stripes.getchannel("A"), Image.new("L", (w, h), 0), cane.getchannel("A")))
        cane = cane.rotate(-24, resample=Image.NEAREST, center=(cx, h * 0.62))   # leaning, crossing below the middle
        if flip:
            cane = cane.transpose(Image.FLIP_LEFT_RIGHT)
        img.alpha_composite(cane)
    d = ImageDraw.Draw(img)
    cy = round(h * 0.62)
    d.polygon([(cx, cy), (cx - 5, cy + 20), (cx - 13, cy + 14)], fill=DARK_GREEN)   # ribbon tails
    d.polygon([(cx, cy), (cx + 5, cy + 20), (cx + 13, cy + 14)], fill=DARK_GREEN)
    _bow(d, cx, cy, 14, GREEN, DARK_GREEN)
    return img, []


def _santa_hat(rnd, w: int = 104, h: int = 100):
    img, d = _new(w, h)
    d.polygon([(14, 76), (40, 22), (70, 8), (88, 26), (92, 44), (80, 30), (64, 30), (88, 76)], fill=RED)
    d.polygon([(40, 76), (64, 30), (80, 30), (88, 76)], fill=DARK_RED)
    d.polygon([(14, 76), (40, 22), (70, 8), (64, 30), (40, 76)], fill=RED)
    for x in range(6, 92, 11):                                    # fur trim: a row of soft tufts
        d.ellipse([x, 68, x + 16, 84], fill=WHITE)
    d.rounded_rectangle([6, 74, 98, 96], radius=9, fill=WHITE)
    d.rounded_rectangle([8, 89, 96, 96], radius=5, fill=ICE)
    d.ellipse([82, 36, 104, 58], fill=WHITE)                      # pom-pom
    d.chord([82, 36, 104, 58], 20, 160, fill=ICE)
    return img, []


def _snowflake(rnd, w: int = 101, h: int = 101):
    img, d = _new(w, h)
    c = w // 2
    for a in range(0, 360, 45):
        dx, dy = math.cos(math.radians(a)), math.sin(math.radians(a))
        long = 47 if a % 90 == 0 else 34
        d.line([(c, c), (c + dx * long, c + dy * long)], fill=WHITE, width=5 if a % 90 == 0 else 3)
        for t, ln in ((0.45, 12), (0.72, 9)) if a % 90 == 0 else ((0.6, 8),):
            bx, by = c + dx * long * t, c + dy * long * t
            for s in (-1, 1):
                ang = math.radians(a + s * 45)
                d.line([(bx, by), (bx + math.cos(ang) * ln, by + math.sin(ang) * ln)], fill=WHITE, width=3)
    d.ellipse([c - 9, c - 9, c + 9, c + 9], fill=ICE)
    d.ellipse([c - 4, c - 4, c + 4, c + 4], fill=WHITE)
    return img, []


def _present(rnd, w: int = 96, h: int = 104):
    img, d = _new(w, h)
    cx = w // 2
    d.rectangle([10, 46, w - 11, h - 2], fill=RED)                # box
    d.rectangle([w - 30, 46, w - 11, h - 2], fill=DARK_RED)
    for y in range(52, h - 4, 10):                                # spots on the paper
        for x in range(16 + (y // 10 % 2) * 6, w - 14, 12):
            if not cx - 9 < x < cx + 7:
                d.rectangle([x, y, x + 1, y + 1], fill=CREAM)
    d.rectangle([4, 32, w - 5, 48], fill=RED)                     # lid
    d.rectangle([4, 45, w - 5, 48], fill=DARK_RED)
    d.rectangle([cx - 7, 32, cx + 6, h - 2], fill=GOLD)           # ribbon
    d.rectangle([cx + 3, 32, cx + 6, h - 2], fill=DARK_TAN)
    for s in (-1, 1):                                             # a big bow
        d.polygon([(cx, 30), (cx + s * 30, 4), (cx + s * 36, 26), (cx + s * 10, 34)], fill=GOLD)
        d.polygon([(cx + s * 4, 28), (cx + s * 26, 12), (cx + s * 29, 22), (cx + s * 10, 30)], fill=DARK_TAN)
    d.ellipse([cx - 7, 22, cx + 6, 36], fill=GOLD)
    return img, []


def _elf(rnd, w: int = 100, h: int = 116):
    img, d = _new(w, h)
    cx = w // 2
    for s in (-1, 1):                                             # pointed ears
        d.polygon([(cx + s * 22, 58), (cx + s * 46, 46), (cx + s * 26, 76)], fill=SKIN)
        d.polygon([(cx + s * 26, 60), (cx + s * 38, 54), (cx + s * 28, 70)], fill=PINK)
    d.ellipse([cx - 28, 40, cx + 28, 98], fill=SKIN)              # face
    d.polygon([(cx - 30, 52), (cx - 6, 4), (cx + 30, 52)], fill=GREEN)   # hat
    d.polygon([(cx + 2, 20), (cx - 6, 4), (cx + 30, 52), (cx + 6, 52)], fill=DARK_GREEN)
    d.rounded_rectangle([cx - 33, 46, cx + 33, 58], radius=5, fill=RED)
    for x in range(cx - 30, cx + 30, 8):
        d.polygon([(x, 58), (x + 4, 63), (x + 8, 58)], fill=RED)  # zigzag brim
    d.ellipse([cx - 11, -1, cx - 1, 9], fill=GOLD)                # bell
    for s in (-1, 1):
        ex = cx + s * 11
        d.ellipse([ex - 4, 66, ex + 4, 76], fill=WHITE)
        d.ellipse([ex - 2, 69, ex + 2, 75], fill=COAL)
        img.putpixel((ex - 1, 70), WHITE)
        d.ellipse([cx + s * 19 - 4, 78, cx + s * 19 + 4, 84], fill=PINK)
    d.ellipse([cx - 3, 76, cx + 3, 82], fill=(232, 160, 120, 255))   # nose
    d.arc([cx - 11, 76, cx + 11, 92], 25, 155, fill=DARK_RED, width=2)
    d.polygon([(cx - 26, 100), (cx, 92), (cx + 26, 100), (cx + 34, h - 1), (cx - 34, h - 1)], fill=GREEN)   # collar
    for x in range(cx - 30, cx + 28, 10):
        d.polygon([(x, 104), (x + 5, h - 2), (x + 10, 104)], fill=RED)
    return img, [(cx - 6 + 0.5, 4, (255, 226, 120), 1.2)]


def _wreath(rnd, w: int = 104, h: int = 108):
    img, d = _new(w, h)
    cx, cy, R = w // 2, 56, 44
    for k in range(40):                                           # overlapping tufts round the ring
        a = k / 40 * 2 * math.pi
        x, y = cx + math.cos(a) * (R - 10), cy + math.sin(a) * (R - 10)
        c = (GREEN, DARK_GREEN, LIGHT_GREEN)[k % 3]
        d.ellipse([x - 10, y - 10, x + 10, y + 10], fill=c if k % 3 != 2 else GREEN)
        d.ellipse([x - 5 + math.cos(a) * 5, y - 5 + math.sin(a) * 5, x + 5 + math.cos(a) * 5, y + 5 + math.sin(a) * 5], fill=c)
    d.ellipse([cx - R + 22, cy - R + 22, cx + R - 22, cy + R - 22], fill=NONE)   # the hole
    lights = []
    for k in range(10):                                           # berries and a few lights
        a = k / 10 * 2 * math.pi + 0.3
        x, y = cx + math.cos(a) * (R - 10), cy + math.sin(a) * (R - 10)
        if k % 2:
            d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=RED)
            img.putpixel((int(x) - 1, int(y) - 1), PINK)
        else:
            lights.append((x, y, (255, 225, 140), 1.1))
    by = cy - R + 9
    d.polygon([(cx, by), (cx - 8, by + 26), (cx - 16, by + 20)], fill=DARK_RED)     # bow tails
    d.polygon([(cx, by), (cx + 8, by + 26), (cx + 16, by + 20)], fill=DARK_RED)
    _bow(d, cx, by, 17, RED, DARK_RED)
    return img, lights


_DRAW = {"tree": _tree, "reindeer": _reindeer, "snowman": _snowman, "gingerbread": _gingerbread, "ornament": _ornament,
         "lights": _lights, "candy_canes": _candy_canes, "santa_hat": _santa_hat, "snowflake": _snowflake,
         "present": _present, "elf": _elf, "wreath": _wreath}


def motif(name: str, seed: int = 0) -> tuple:
    """(chart, lights) for one of MOTIFS; anything else is the tree."""
    return _DRAW.get(name if name in _DRAW else "tree")(random.Random(seed))


# ── fair-isle bands ──────────────────────────────────────────────────────────────────────────────────────────────
def band_chart(width: int, accent=RED, ink=CREAM, kind: int = 0):
    """A fair-isle band `width` stitches wide: an accent stripe carrying a repeat (0 diamonds, 1 zigzag and dots,
    2 eight-point stars), ruled off top and bottom."""
    from PIL import Image, ImageDraw
    rows = (11, 9, 13)[kind % 3]
    img = Image.new("RGBA", (width, rows), accent)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 0], fill=ink)
    d.rectangle([0, rows - 1, width, rows - 1], fill=ink)
    mid = rows // 2
    if kind % 3 == 0:
        for x in range(4, width + 8, 10):
            for r in range(-3, 4):
                reach = 3 - abs(r)
                d.rectangle([x - reach, mid + r, x + reach, mid + r], fill=ink)
            img.putpixel((min(width - 1, max(0, x)), mid), accent)
            if 0 <= x + 5 < width:
                img.putpixel((x + 5, mid), ink)
    elif kind % 3 == 1:
        for x in range(0, width):
            y = mid + (abs((x % 8) - 4) - 2)
            img.putpixel((x, y), ink)
            if x % 8 == 4:
                img.putpixel((x, mid + 2), ink)
            if x % 8 == 0:
                img.putpixel((x, mid - 2), ink)
    else:
        for x in range(6, width + 12, 14):
            for k in range(-4, 5):
                for px_, py_ in ((x + k, mid), (x, mid + k), (x + k, mid + k), (x + k, mid - k)):
                    if abs(k) <= (4 if px_ == x or py_ == mid else 3) and 0 <= px_ < width and 0 < py_ < rows - 1:
                        img.putpixel((px_, py_), ink)
            for sx, sy in ((x + 7, mid - 3), (x + 7, mid + 3)):
                if 0 <= sx < width:
                    img.putpixel((sx, sy), ink)
    return img


_FLAKE = ("...#...", ".#.#.#.", "..###..", "#######", "..###..", ".#.#.#.", "...#...")


def _snow(chart, rnd, ink, flakes: int = 7, density: float = 0.004):
    """Snow in the empty yarn around the letters and the picture: a few charted flakes and loose single stitches,
    never touching either."""
    px = chart.load()
    w, h = chart.size

    def clear(x0, y0, x1, y1):
        return x0 >= 0 and y0 >= 0 and x1 < w and y1 < h and \
            all(px[x, y][3] == 0 for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
    placed = 0
    for _ in range(flakes * 60):
        if placed >= flakes:
            break
        x, y = rnd.randrange(2, max(3, w - 9)), rnd.randrange(2, max(3, h - 9))
        if clear(x - 4, y - 4, x + 10, y + 10):
            for r, row in enumerate(_FLAKE):
                for c, bit in enumerate(row):
                    if bit == "#":
                        px[x + c, y + r] = ink
            placed += 1
    for _ in range(int(w * h * density)):
        x, y = rnd.randrange(1, w - 1), rnd.randrange(1, h - 1)
        if clear(x - 3, y - 3, x + 3, y + 3):
            px[x, y] = ink


# ── the design ───────────────────────────────────────────────────────────────────────────────────────────────────
TALL_ASPECT = 0.82                # a T-shirt's print area, width / height
WIDE_ASPECT = 1.5                 # a hoodie's, above the pocket
BAND_ROWS = 34                    # about what the two fair-isle bands and their gaps add to a design's height


def design(slogan: str, seed: int, width: int = ACROSS * STITCH, motif_name: str = "tree", ground: str = "dark",
           layout: str = "tall", bands: bool = True):
    """The slogan and its picture, knitted, on transparent. ground: "dark" (cream letters, for dark garments) or
    "light" (dark letters, for white ones). layout: "tall" = words over the picture, "wide" = words beside it
    (wider than `width`: it's for print areas that are short, like a hoodie's above the pocket).
    bands: fair-isle stripes above and below (left off for the all-over sweater, which has its own)."""
    from PIL import Image
    rnd = random.Random(seed)
    across = max(60, width // STITCH)
    accent = SCHEMES[seed % len(SCHEMES)][1] if ground == "dark" else RED
    ink, shadow = (CREAM, accent) if ground == "dark" else (DARK_GREEN, RED)
    snow_ink = CREAM if ground == "dark" else (150, 190, 226, 255)
    pic, lights = motif(motif_name, seed)
    kind = seed % 3
    pad = 6
    extra = BAND_ROWS if bands else 0
    if layout == "wide":
        lines, scale, bold = fit_lines(slogan, across - 4, max_lines=4, frame=lambda tw, th: (
            tw + 16 + pic.width, max(th, pic.height) + extra, WIDE_ASPECT))
        words = text_chart(lines, scale, ink, bold, shadow)
        across = words.width + 14 + pic.width
        body_h = max(pic.height, words.height)
        body = Image.new("RGBA", (across, body_h), NONE)
        body.alpha_composite(words, (2, (body_h - words.height) // 2))
        ox, oy = across - pic.width - 2, (body_h - pic.height) // 2
        body.alpha_composite(pic, (ox, oy))
    else:
        lines, scale, bold = fit_lines(slogan, across - 4, frame=lambda tw, th: (
            across, th + pad + pic.height + extra, TALL_ASPECT))
        words = text_chart(lines, scale, ink, bold, shadow)
        body_h = words.height + pad + pic.height
        body = Image.new("RGBA", (across, body_h), NONE)
        body.alpha_composite(words, ((across - words.width) // 2, 0))
        ox, oy = (across - pic.width) // 2, words.height + pad
        body.alpha_composite(pic, (ox, oy))
    _snow(body, rnd, snow_ink, flakes=7 if ground == "dark" else 4)
    lights = [(x + ox, y + oy, c, s) for x, y, c, s in lights]
    if bands:
        top, foot = band_chart(across, accent, CREAM, kind), band_chart(across, accent, CREAM, (kind + 1) % 3)
        chart = Image.new("RGBA", (across, top.height + pad + body_h + pad + foot.height), NONE)
        chart.alpha_composite(top, (0, 0))
        chart.alpha_composite(body, (0, top.height + pad))
        chart.alpha_composite(foot, (0, chart.height - foot.height))
        lights = [(x, y + top.height + pad, c, s) for x, y, c, s in lights]
    else:
        chart = body
    return glow(knit(chart), lights)


# ── the all-over-print sweater ───────────────────────────────────────────────────────────────────────────────────
FRONT_WIDTH = 0.66                # how much of the sweater's front panel the design may span, and
FRONT_TOP = 0.19                  # how far down it starts (shares of the panel)


def _band_rows(ch: int, seed: int) -> tuple:
    """(first clear row under the top bands, first row of the bottom bands) on a panel `ch` stitches tall."""
    kind = seed % 3
    heights = lambda kinds: sum((11, 9, 13)[k % 3] for k in kinds)
    upper = round(ch * 0.045) + heights((kind, kind + 1)) + 1
    lower = ch - round(ch * 0.03) - heights((kind + 2, kind))
    return upper, lower


def _field(size: tuple, body, accent, seed: int, clear: tuple = None):
    """A panel of plain knitting in the body colour, dotted with snow (except inside the `clear` box, in stitches,
    where the design goes), with two fair-isle bands near the top and two near the bottom."""
    from PIL import Image
    w, h = size
    cw, ch = -(-w // STITCH), -(-h // STITCH)
    chart = Image.new("RGBA", (cw, ch), body)
    px = chart.load()
    for y in range(3, ch - 3, 6):                                  # an even scatter: a stitch every few, offset by row
        for x in range((y // 6 % 2) * 4 + 2, cw - 1, 8):
            if not (clear and clear[0] <= x <= clear[2] and clear[1] <= y <= clear[3]):
                px[x, y] = CREAM
    kind = seed % 3
    y = round(ch * 0.045)
    for k, b in enumerate((band_chart(cw, accent, CREAM, kind), band_chart(cw, CREAM, accent, kind + 1))):
        chart.paste(b, (0, y))
        y += b.height + (1 if k == 0 else 0)
    y = ch - round(ch * 0.03)
    for b in (band_chart(cw, accent, CREAM, kind + 2), band_chart(cw, CREAM, accent, kind)):
        y -= b.height
        chart.paste(b, (0, y))
    return chart


def panels(art, sizes: dict, seed: int = 0) -> dict:
    """{position: RGB image the size of that print area}. sizes: {position: (w, h)} from the catalog; seed picks
    the colour scheme. art: the design without its own bands, on transparent."""
    from PIL import Image
    body, accent = SCHEMES[seed % len(SCHEMES)]
    out = {}
    for pos, (w, h) in sizes.items():
        if pos in ("front", "back"):
            upper, lower = _band_rows(-(-h // STITCH), seed)
            clear = a = None
            if pos == "front":
                # the sleeves are sewn in over the panel's upper corners: on Printify's photo only the middle
                # three quarters of the chest shows, so the words keep to two thirds of the width (at 0.88 the
                # first and last letters went under the sleeves) and start below the collar
                top = max(upper * STITCH, round(h * FRONT_TOP))
                room = (w * FRONT_WIDTH, lower * STITCH - top - h * 0.04)
                k = min(room[0] / art.width, room[1] / art.height)
                a = art.resize((round(art.width * k), round(art.height * k)), Image.LANCZOS)
                at = ((w - a.width) // 2, top + round((lower * STITCH - top - a.height) / 2))
                clear = (at[0] // STITCH - 2, at[1] // STITCH - 2, (at[0] + a.width) // STITCH + 2, (at[1] + a.height) // STITCH + 2)
            img = knit(_field((w, h), body, accent, seed, clear), depth=0.22).crop((0, 0, w, h))
            if a is not None:
                img.alpha_composite(a, at)
        elif "sleeve" in pos:
            img = _stripes((w, h), accent=accent)
        else:                                                  # cuffs, collar, waistband
            img = _rib((w, h), accent)
        out[pos] = img.convert("RGB")
    return out


def _stripes(size: tuple, stripe: int = 7, accent=RED):
    """Sleeves: cream and accent stripes, `stripe` stitches each, knitted."""
    from PIL import Image, ImageDraw
    w, h = size
    cw, ch = -(-w // STITCH), -(-h // STITCH)
    chart = Image.new("RGBA", (cw, ch), CREAM)
    d = ImageDraw.Draw(chart)
    for i, y in enumerate(range(0, ch, stripe)):
        if i % 2 == 0:
            d.rectangle([0, y, cw, y + stripe - 1], fill=accent)
    return knit(chart, depth=0.22).crop((0, 0, w, h))


def _rib(size: tuple, accent=RED):
    """Ribbing: vertical knit ribs."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", size, accent)
    d = ImageDraw.Draw(img)
    dark = tuple(round(c * 0.72) for c in accent[:3]) + (255,)
    for x in range(0, size[0], STITCH * 2):
        d.rectangle([x, 0, x + STITCH // 2, size[1]], fill=dark)
    return img
