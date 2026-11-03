"""The shop's retro / vintage look (2026-11-03): the slogan arched over the illustration in a chunky 70s slab font,
a striped sunset behind the drawing, the rest of the slogan straight underneath, and a worn, screen-printed
texture over it all. It reads as a "real brand" design instead of a picture with a caption under it.

design(art, slogan, seed) -> a transparent RGBA PNG, about 3000 px wide, for light-coloured garments and mugs."""
import math
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SLAB = ("/System/Library/Fonts/Supplemental/SuperClarendon.ttc", 7)        # Superclarendon Black
CONDENSED = ("/System/Library/Fonts/Supplemental/Futura.ttc", 4)           # Futura Condensed ExtraBold
WIDTH = 3000
# (ink for the words, sunset bands top to bottom): Christmas, 70s, cosy, candy cane
SCHEMES = [
    ((58, 34, 28), [(196, 52, 46), (226, 112, 52), (233, 168, 64), (38, 104, 74)]),
    ((62, 38, 26), [(231, 148, 52), (219, 104, 50), (182, 64, 46), (120, 46, 40)]),
    ((40, 52, 44), [(186, 60, 50), (214, 168, 92), (82, 128, 98), (40, 82, 64)]),
    ((50, 30, 40), [(210, 62, 70), (240, 156, 150), (236, 210, 170), (196, 52, 46)]),
]


def _font(spec, size):
    return ImageFont.truetype(spec[0], size, index=spec[1])


def split(slogan: str) -> tuple:
    """(the arched top, the straight bottom): short slogans arch whole, longer ones split near the middle."""
    words = slogan.split()
    if len(words) <= 2 or len(slogan) <= 12:
        return slogan, ""
    best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    return " ".join(words[:best]), " ".join(words[best:])


def arch(text: str, ink, width: int, radius: int):
    """`text` along the top of a circle of `radius`, each letter turned to the curve."""
    size = 360
    while True:
        font = _font(SLAB, size)
        widths = [font.getlength(ch) for ch in text]
        span = sum(widths) * 1.04 / radius                           # the angle the text covers
        if (span < 2.0 and 2 * radius * math.sin(span / 2) < width * 0.92) or size < 120:
            break
        size -= 20
    h = int(radius * (1 - math.cos(span / 2))) + size * 2
    canvas = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    cx, cy = width / 2, radius + size * 1.1
    a = -span / 2
    for ch, w in zip(text, widths):
        mid = a + (w * 1.04 / 2) / radius
        tile = Image.new("RGBA", (int(w) + size, size * 2), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((size / 2, size / 2), ch, font=font, fill=ink)
        tile = tile.rotate(-math.degrees(mid), resample=Image.BICUBIC, expand=True)
        x = cx + radius * math.sin(mid) - tile.width / 2
        y = cy - radius * math.cos(mid) - tile.height / 2
        canvas.alpha_composite(tile, (int(x), max(0, int(y))))
        a += w * 1.04 / radius
    box = canvas.getbbox()
    return canvas.crop(box) if box else canvas


def sunset(diameter: int, bands: list):
    """A half sun cut into stripes that get thinner towards the bottom, the classic 70s sunset."""
    r = diameter // 2
    sun = Image.new("RGBA", (diameter, r), (0, 0, 0, 0))
    d = ImageDraw.Draw(sun)
    d.pieslice((0, 0, diameter, diameter), 180, 360, fill=bands[0])
    weights = [1.6, 1.25, 1.0, 0.8][:len(bands)]
    gaps = [0.0, 0.035, 0.03, 0.025]
    y = 0.0
    for color, wgt, gap in zip(bands, weights, gaps):
        y += gap * r
        h = r * wgt / sum(weights) * (1 - sum(gaps))
        mask = Image.new("L", sun.size, 0)
        ImageDraw.Draw(mask).rectangle((0, int(y), diameter, int(y + h)), fill=255)
        layer = Image.new("RGBA", sun.size, color)
        sun.paste(layer, (0, 0), Image.composite(mask, Image.new("L", sun.size, 0), sun.getchannel("A")))
        y += h
    alpha = sun.getchannel("A")
    y = 0.0
    for wgt, gap in zip(weights, gaps):                           # the gaps between stripes are cut out
        ImageDraw.Draw(alpha).rectangle((0, int(y), diameter, int(y + gap * r)), fill=0)
        y += gap * r + r * wgt / sum(weights) * (1 - sum(gaps))
    sun.putalpha(alpha)
    return sun


def distress(img, seed: int, amount: float = 0.05):
    """Speckles and fine scratches taken out of the ink, like an old screen print."""
    rnd = random.Random(seed)
    w, h = img.size
    small = Image.new("L", (w // 3, h // 3), 0)
    d = ImageDraw.Draw(small)
    for _ in range(int(small.width * small.height * amount / 12)):
        x, y = rnd.randrange(small.width), rnd.randrange(small.height)
        s = rnd.choice((1, 1, 2, 2, 3))
        d.ellipse((x, y, x + s, y + s), fill=255)
    for _ in range(40):                                            # a few long scratches
        x, y = rnd.randrange(small.width), rnd.randrange(small.height)
        ang, ln = rnd.uniform(-0.4, 0.4), rnd.randrange(40, 180)
        d.line((x, y, x + ln * math.cos(ang), y + ln * math.sin(ang)), fill=255, width=1)
    holes = small.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3)).point(lambda v: 255 if v > 110 else 0)
    alpha = img.getchannel("A")
    img.putalpha(Image.composite(Image.new("L", img.size, 0), alpha, holes))
    return img


def design(art, slogan: str, seed: int = 0, width: int = WIDTH):
    """The full retro design: arched words, the sunset with the drawing over it, straight words underneath."""
    ink, bands = SCHEMES[seed % len(SCHEMES)]
    ink = (*ink, 255)
    top, bottom = split(slogan.upper())
    head = arch(top, ink, width, radius=int(width * 0.62))
    sun = sunset(int(width * 0.78), [(*b, 255) for b in bands])
    art = art.convert("RGBA")
    box = art.getbbox()
    art = art.crop(box) if box else art
    scale = min(width * 0.7 / art.width, sun.height * 1.55 / art.height)
    art = art.resize((max(1, int(art.width * scale)), max(1, int(art.height * scale))), Image.LANCZOS)
    middle_h = max(art.height, sun.height)
    foot = None
    if bottom:
        size = 330
        while size > 120:
            font = _font(CONDENSED, size)
            if font.getlength(bottom) <= width * 0.9:
                break
            size -= 15
        tw = font.getlength(bottom)
        foot = Image.new("RGBA", (width, int(size * 1.3)), (0, 0, 0, 0))
        fd = ImageDraw.Draw(foot)
        fd.text(((width - tw) / 2, 0), bottom, font=font, fill=ink)
        # a thin rule either side of the bottom line, if there's room
        if tw < width * 0.7:
            y = int(size * 0.62)
            for x0, x1 in (((width - tw) / 2 - width * 0.12, (width - tw) / 2 - width * 0.03),
                           ((width + tw) / 2 + width * 0.03, (width + tw) / 2 + width * 0.12)):
                fd.rectangle((x0, y - 10, x1, y + 10), fill=ink)
        fb = foot.getbbox()
        foot = foot.crop((0, fb[1], width, fb[3])) if fb else foot
    gap = int(width * 0.03)
    total = head.height + gap + middle_h + (gap + foot.height if foot else 0) + 24
    canvas = Image.new("RGBA", (width, total), (0, 0, 0, 0))
    canvas.alpha_composite(head, ((width - head.width) // 2, 0))
    y = head.height + gap
    canvas.alpha_composite(sun, ((width - sun.width) // 2, y + middle_h - sun.height))
    # a ground line under the sun, so the sun sits on a horizon
    ImageDraw.Draw(canvas).rectangle(((width - sun.width) // 2 - 40, y + middle_h - 8,
                                      (width + sun.width) // 2 + 40, y + middle_h + 8), fill=ink)
    canvas.alpha_composite(art, ((width - art.width) // 2, y + middle_h - art.height))
    if foot:
        canvas.alpha_composite(foot, (0, y + middle_h + gap))
    return distress(canvas, seed)
