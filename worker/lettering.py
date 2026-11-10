"""How a drawing and its slogan become one print (2026-11-10: "make the products we post even better").

Before, the slogan sat under the picture as a small Arial caption: on a listing's thumbnail the joke, which is the
whole product, couldn't be read, long slogans ran off the bottom, and the same tall design was shrunk to fit every
print area, so it came out tiny on a hoodie (whose area above the pocket is short and wide).

  stack(slogan, width)          the slogan as a block of heavy condensed capitals, each line sized to span the
                                width, the punchiest (shortest) line in a colour picked out of the drawing
  compose(art, slogan, layout)  drawing + slogan: "tall" (picture over words: shirts, totes), "wide" (picture beside
                                words: hoodies, anything short and wide), "over" (words over picture: a bandana,
                                whose point is at the bottom). look "retro" adds the 70s sunset and worn print.
"""
from __future__ import annotations

import colorsys

INK = (34, 30, 38, 255)
WIDTH = 3000                         # a tall design's width: a shirt's print area is 2767 px and up
FACES = {
    "block": ("/System/Library/Fonts/Supplemental/Futura.ttc", 4),          # Futura Condensed ExtraBold
    "heavy": ("/System/Library/Fonts/Avenir Next Condensed.ttc", 8),        # Avenir Next Condensed Heavy
    "impact": ("/System/Library/Fonts/Supplemental/Impact.ttf", 0),
    "slab": ("/System/Library/Fonts/Supplemental/SuperClarendon.ttc", 7),   # Superclarendon Black
}
FALLBACK = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
PLAIN_FACES = ("block", "heavy", "impact")     # the plain look takes turns between these, by design
MAX_RATIO = 1.6                      # the biggest line is at most this much bigger than the smallest
SHIRT_ASPECT = 0.82                  # a T-shirt's print area, width / height
# words that can't carry a line on their own: "THE" alone in huge red letters isn't the joke
SMALL_WORDS = {"A", "AN", "THE", "TO", "OF", "AND", "OR", "IN", "ON", "AT", "FOR", "MY", "I", "I'M", "IS", "IT", "IT'S",
               "AS", "BY", "SO", "BUT", "WITH", "THIS", "THAT", "AM", "BE", "WE", "ME", "NO", "NOT", "YOUR", "OUR"}
_FONTS: dict = {}


def _filler(line: str) -> bool:
    """A line made only of small words (or one very short word)."""
    words = [w.strip(".,!?'\"") for w in line.split()]
    return all(w in SMALL_WORDS for w in words) or len(line) <= 2


def font(face: str, size: int):
    from PIL import ImageFont
    key = (face, int(size))
    if key not in _FONTS:
        path, index = FACES.get(face, FACES["block"])
        try:
            _FONTS[key] = ImageFont.truetype(path, int(size), index=index)
        except OSError:
            _FONTS[key] = ImageFont.truetype(FALLBACK, int(size))
    return _FONTS[key]


def _splits(words: list, n: int):
    """Every way to break `words` into `n` lines, in order."""
    if n == 1:
        yield [" ".join(words)]
        return
    for i in range(1, len(words) - n + 2):
        for rest in _splits(words[i:], n - 1):
            yield [" ".join(words[:i])] + rest


def layout_lines(slogan: str, width: int, face: str = "block", max_height: float = 0, max_lines: int = 4) -> list:
    """[(line, font size)]: the slogan broken and sized so its smallest letters are as big as they can be with every
    line spanning `width` (short lines are held to MAX_RATIO times the smallest) and the block within `max_height`
    (default: a little over half the width)."""
    words = slogan.split()
    if not words:
        return []
    ref = font(face, 200)
    limit = max_height or width * 0.56
    best, best_score = None, -1.0
    for n in range(1, min(max_lines, len(words)) + 1):
        for lines in _splits(words, n):
            sizes = [200 * width / max(1.0, ref.getlength(l)) for l in lines]
            cap = min(min(sizes) * MAX_RATIO, width * 0.30)
            sizes = [min(s, cap) for s in sizes]
            height = sum(sizes) * 0.9
            if height > limit:
                sizes = [s * limit / height for s in sizes]
            # the smallest letters decide how well it reads; among equals, fewer and more even lines, and no line
            # left holding only a "the" or a "to"
            score = min(sizes) * (1 - 0.03 * (n - 1)) * (0.9 + 0.1 * min(sizes) / max(sizes))
            if n > 1:
                score *= 0.72 ** sum(1 for l in lines if _filler(l))
            if score > best_score:
                best, best_score = list(zip(lines, sizes)), score
    return best


def stack(slogan: str, width: int, ink: tuple = INK, accent: tuple = None, face: str = "block",
          max_height: float = 0, max_lines: int = 4):
    """The slogan as a block of capitals `width` wide, on transparent. With an `accent`, the biggest line of a
    slogan of two or more lines is set in it."""
    from PIL import Image, ImageDraw
    rows = layout_lines(slogan.upper(), width, face, max_height, max_lines)
    if not rows:
        return Image.new("RGBA", (width, 1), (0, 0, 0, 0))
    sizes = [s for _, s in rows]
    loud = -1
    if accent and len(rows) > 1:
        real = [i for i, (line, _) in enumerate(rows) if not _filler(line)] or list(range(len(rows)))
        loud = max(real, key=lambda i: sizes[i])               # the biggest line that says something
        if sizes[loud] < min(sizes) * 1.08:
            loud = real[-1]                                    # all one size: the last line is the punchline
    drawn, y = [], 0
    for i, (line, size) in enumerate(rows):
        f = font(face, size)
        box = f.getbbox(line)
        cap = f.getbbox("H")
        drawn.append((line, f, box, y - cap[1], accent if i == loud else ink))
        y += (cap[3] - cap[1]) + round(size * 0.17)
    last = drawn[-1]
    height = y - round(rows[-1][1] * 0.17) + max(0, last[2][3] - last[1].getbbox("H")[3])   # room for a comma or a Q
    canvas = Image.new("RGBA", (width, int(height) + 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    for line, f, box, top, colour in drawn:
        d.text(((width - (box[2] - box[0])) / 2 - box[0], top), line, font=f, fill=colour)
    return canvas


def accent_from(art, default: tuple = (178, 34, 34, 255)) -> tuple:
    """A colour for the slogan's loud line, taken from the drawing so the two belong together: its most common vivid
    colour, deepened until it reads on a white shirt. Yellows go to burnt orange (dark yellow is mud)."""
    small = art.convert("RGBA").resize((64, 64))
    px = small.load()
    bins: dict = {}
    for r, g, b, a in (px[x, y] for y in range(64) for x in range(64)):
        if a < 128:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s > 0.45 and v > 0.35:
            k = int(h * 12) % 12
            w = s * s * s * v                                  # vivid beats merely common: a Santa hat over brown fur
            acc = bins.setdefault(k, [0.0, 0.0, 0.0, 0.0])
            acc[0] += w
            acc[1] += r * w
            acc[2] += g * w
            acc[3] += b * w
    if not bins:
        return default
    w, r, g, b = max(bins.values(), key=lambda x: x[0])
    if w < 18:                                                 # a fleck of colour isn't the drawing's colour
        return default
    h, l, s = colorsys.rgb_to_hls(r / w / 255, g / w / 255, b / w / 255)
    if 35 / 360 <= h < 75 / 360:
        h = 24 / 360
    r, g, b = colorsys.hls_to_rgb(h, min(l, 0.36), max(s, 0.62))
    return (round(r * 255), round(g * 255), round(b * 255), 255)


def _sharp(art, width: int, max_height: float = 0):
    """The drawing enlarged to `width` (or to `max_height`, whichever is reached first), sharpened a little."""
    from PIL import Image, ImageFilter
    k = width / art.width
    if max_height and art.height * k > max_height:
        k = max_height / art.height
    art = art.resize((max(1, round(art.width * k)), max(1, round(art.height * k))), Image.LANCZOS)
    return art.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))


def compose(art, slogan: str, layout: str = "tall", look: str = "plain", seed: int = 0, width: int = WIDTH):
    """The print: a transparent RGBA. Without a slogan it's the drawing alone at `width`."""
    from PIL import Image
    art = art.convert("RGBA")
    box = art.getbbox()
    art = art.crop(box) if box else art
    if not slogan:
        return _sharp(art, width)
    if look == "retro":
        from worker import retro
        return retro.design(art, slogan, seed=seed, width=width, layout=layout)
    face = PLAIN_FACES[seed % len(PLAIN_FACES)]
    accent = accent_from(art)
    gap = round(width * 0.04)
    if layout == "wide":
        total = round(width * 1.5)
        pic = _sharp(art, round(total * 0.42), max_height=total * 0.6)
        words = stack(slogan, total - pic.width - gap * 2, INK, accent, face, max_height=max(pic.height, total * 0.42))
        canvas = Image.new("RGBA", (total, max(pic.height, words.height)), (0, 0, 0, 0))
        canvas.alpha_composite(pic, (0, (canvas.height - pic.height) // 2))
        canvas.alpha_composite(words, (pic.width + gap * 2, (canvas.height - words.height) // 2))
        return canvas
    words = stack(slogan, width, INK, accent, face)
    # the whole print keeps a shirt's proportions, so it's printed at full width: a long slogan takes its room from
    # the picture instead of the lot being shrunk to fit the height
    pic = _sharp(art, width, max_height=max(width * 0.62, min(width * 1.12, width / SHIRT_ASPECT - gap - words.height)))
    canvas = Image.new("RGBA", (width, pic.height + gap + words.height), (0, 0, 0, 0))
    if layout == "over":
        canvas.alpha_composite(words, (0, 0))
        canvas.alpha_composite(pic, ((width - pic.width) // 2, words.height + gap))
    else:
        canvas.alpha_composite(pic, ((width - pic.width) // 2, 0))
        canvas.alpha_composite(words, (0, pic.height + gap))
    return canvas
