"""Personalised all-over-print basketball jersey art (Printify blueprint 1209, 6000 x 6000 front and back).

Drawn in code, not by an image model, so every letter is exact and the buyer's name and number can be set later:
a navy ground, a big cracked-cream star outline, sweeping ribbons and flames. No team marks, nicknames or player names.
"""
from __future__ import annotations

import math
import random
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SIZE = 6000
CX = 3000                              # the jersey body is centred here (it spans about x 850..5150)
NAVY, NAVY_DARK = (16, 28, 72), (9, 17, 46)
CREAM, CREAM_SHADE = (240, 226, 186), (206, 186, 140)
RED, RED_DARK = (184, 22, 48), (122, 12, 34)
SKY, SKY_DARK = (128, 178, 228), (84, 132, 190)
IMPACT = "/System/Library/Fonts/Supplemental/Impact.ttf"
SERIF = "/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf"
COLLEGE = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
MAX_NAME = 14
MAX_NUMBER = 2


def clean_name(text: str) -> str:
    """What may be printed: letters, spaces, hyphens, apostrophes and periods, upper-cased, trimmed to MAX_NAME."""
    keep = "".join(ch for ch in (text or "") if ch.isalpha() or ch in " -'.")
    return " ".join(keep.upper().split())[:MAX_NAME].strip()


def clean_number(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isdigit())[:MAX_NUMBER]


def _font(path: str, size: int, index: int = 0):
    return ImageFont.truetype(path, size, index=index)


def _ribbon(draw: ImageDraw.ImageDraw, rnd: random.Random, fill, edge, y0: float, amp: float, wave: float,
            width: float, slope: float, x0: float = -200, x1: float = SIZE + 200, phase: Optional[float] = None) -> None:
    """A sweeping band that swells in the middle and tapers at both ends."""
    phase = rnd.uniform(0, math.tau) if phase is None else phase
    top, bottom = [], []
    steps = 90
    for i in range(steps + 1):
        t = i / steps
        x = x0 + (x1 - x0) * t
        y = y0 + slope * (x - SIZE / 2) + amp * math.sin(math.tau * x / wave + phase)
        w = width * (0.15 + 0.85 * math.sin(math.pi * t) ** 0.8)
        dy = amp * math.cos(math.tau * x / wave + phase) * math.tau / wave * 40 * 0     # keep the band's thickness vertical
        top.append((x, y - w / 2 + dy))
        bottom.append((x, y + w / 2 - dy))
    shape = top + bottom[::-1]
    draw.polygon(shape, fill=edge)
    inner = [(x, y + (cy - y) * 0.16) for (x, y), (_, cy) in zip(top, bottom)] + \
            [(x, y + (cy - y) * 0.16) for (x, y), (_, cy) in zip(bottom[::-1], top[::-1])]
    draw.polygon(inner, fill=fill)


def _flame(draw: ImageDraw.ImageDraw, rnd: random.Random, bx: float, by: float, length: float, width: float,
           sway: float, fill, edge) -> None:
    """One curling tongue of flame that rises from (bx, by)."""
    left, right = [], []
    steps = 60
    phase = rnd.uniform(0, math.tau)
    for i in range(steps + 1):
        t = i / steps
        cx = bx + sway * math.sin(t * math.pi * 1.35 + phase * 0.2) * t ** 0.9 * length * 0.35
        cy = by - length * t
        w = width * (1 - t) ** 0.85 * (1 + 0.18 * math.sin(t * 9 + phase))
        left.append((cx - w / 2, cy))
        right.append((cx + w / 2, cy))
    draw.polygon(left + right[::-1], fill=edge)
    core = [(cx + (bx - cx) * 0, cy) for cx, cy in []]
    inner_left = [(x + (rx - x) * 0.22, y) for (x, y), (rx, _) in zip(left, right)]
    inner_right = [(rx - (rx - x) * 0.22, y) for (x, y), (rx, _) in zip(left, right)]
    draw.polygon(inner_left + inner_right[::-1], fill=fill)


def _star_points(cx: float, cy: float, r_out: float, r_in: float, turn: float) -> list:
    pts = []
    for i in range(10):
        a = turn + i * math.pi / 5 - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _crackle(mask: Image.Image, rnd: random.Random, strength: int = 1, scale: int = 1) -> Image.Image:
    """Hairline cracks across a mask, like old stitched-on lettering: returns the mask with thin gaps cut into it."""
    w, h = mask.size
    cracks = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(cracks)
    box = mask.getbbox()
    if not box:
        return mask
    x0, y0, x1, y1 = box
    for _ in range(int((x1 - x0) * (y1 - y0) / (260 * scale) ** 2 * 1.1 * strength) + 4):
        x, y = rnd.uniform(x0, x1), rnd.uniform(y0, y1)
        a = rnd.uniform(0, math.tau)
        pts = [(x, y)]
        for _ in range(rnd.randint(3, 7)):
            a += rnd.uniform(-0.9, 0.9)
            step = rnd.uniform(25, 85) * scale
            x, y = x + step * math.cos(a), y + step * math.sin(a)
            pts.append((x, y))
        d.line(pts, fill=255, width=max(2, int(4 * scale)))
    return ImageChops.subtract(mask, cracks.filter(ImageFilter.GaussianBlur(0.6 * scale)))


def _stamp(img: Image.Image, text: str, font, cx: float, cy: float, outline_width: int, rnd: random.Random,
           spacing: int = 0, arch: float = 0.0, cracked: bool = True) -> None:
    """Paint lettering onto the art: a navy outline, a cream fill, a darker inner edge and (optionally) cracks."""
    mask = _text_mask(text, font, cx, cy, spacing, arch)
    outer = _text_mask(text, font, cx, cy, spacing, arch, stroke=outline_width)
    if cracked:
        mask = _crackle(mask, rnd)
    img.paste(Image.new("RGB", img.size, NAVY_DARK), (0, 0), outer)
    img.paste(Image.new("RGB", img.size, CREAM), (0, 0), mask)
    inner = mask.filter(ImageFilter.GaussianBlur(14)).point(lambda v: 255 if v > 250 else 0)
    edge = ImageChops.subtract(mask, inner)
    img.paste(Image.new("RGB", img.size, CREAM_SHADE), (0, 0), edge.point(lambda v: v // 2))


def _fit(text: str, path: str, width: int, start: int, index: int = 0, minimum: int = 200):
    size = start
    while size > minimum:
        font = _font(path, size, index)
        if font.getbbox(text)[2] - font.getbbox(text)[0] <= width:
            return font
        size -= 20
    return _font(path, minimum, index)


def _text_mask(text: str, font, cx: float, cy: float, spacing: int = 0, arch: float = 0.0, stroke: int = 0) -> Image.Image:
    """The text as a mask centred on (cx, cy); `arch` bows it upward (the middle higher than the ends) by that many px."""
    mask = Image.new("L", (SIZE, SIZE), 0)
    d = ImageDraw.Draw(mask)
    widths = [font.getlength(ch) + spacing for ch in text]
    total = sum(widths) - spacing
    x = cx - total / 2
    ascent, descent = font.getmetrics()
    height = ascent + descent
    for ch, w in zip(text, widths):
        rel = (x + w / 2 - cx) / (total / 2 or 1)
        lift = -arch * (1 - rel * rel)
        d.text((x, cy - height / 2 + lift), ch, font=font, fill=255, stroke_width=stroke, stroke_fill=255)
        x += w
    return mask


def ground(seed: int, back: bool = False) -> Image.Image:
    """Everything behind the lettering: navy, the star outline, ribbons, flames."""
    rnd = random.Random(seed * 2 + (1 if back else 0))
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    # a darker wash toward the hem and shoulders for depth
    shade = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(shade).ellipse([-1500, 3200, 7500, 9000], fill=140)
    img.paste(Image.new("RGB", (SIZE, SIZE), NAVY_DARK), (0, 0), shade.filter(ImageFilter.GaussianBlur(500)))
    d = ImageDraw.Draw(img)
    # the big star outline behind the chest
    star = Image.new("L", (SIZE, SIZE), 0)
    sd = ImageDraw.Draw(star)
    cy = 3350 if not back else 3500
    pts = _star_points(CX + (160 if back else -120), cy, 2750, 1150, rnd.uniform(-0.18, 0.18))
    sd.line(pts + [pts[0]], fill=255, width=210, joint="curve")
    star = _crackle(star, rnd, strength=2, scale=2)
    img.paste(Image.new("RGB", (SIZE, SIZE), CREAM), (0, 0), star)
    d = ImageDraw.Draw(img)
    # ribbons sweeping up from the lower left and across
    plan = [(SKY, SKY_DARK), (RED, RED_DARK), (CREAM, CREAM_SHADE), (RED, RED_DARK), (SKY, SKY_DARK)]
    ys = [4950, 5350, 5800, 1750, 2050] if not back else [5000, 5400, 5850, 1650, 1950]
    for (fill, edge), y in zip(plan, ys):
        _ribbon(d, rnd, fill, edge, y + rnd.uniform(-120, 120), rnd.uniform(150, 260), rnd.uniform(2600, 3600),
                rnd.uniform(300, 450), rnd.uniform(-0.55, -0.2) if not back else rnd.uniform(0.2, 0.55))
    # flames licking up the lower right (lower left on the back)
    side = 1 if not back else -1
    for i in range(7):
        bx = CX + side * (1700 + i * 260) + rnd.uniform(-90, 90)
        _flame(d, rnd, bx, 6200, rnd.uniform(1500, 2500), rnd.uniform(520, 760), -side * rnd.uniform(0.6, 1.3),
               RED if i % 2 == 0 else CREAM, RED_DARK if i % 2 == 0 else CREAM_SHADE)
    for i in range(5):
        bx = CX + side * (1500 + i * 330) + rnd.uniform(-90, 90)
        _flame(d, rnd, bx, 6200, rnd.uniform(900, 1500), rnd.uniform(380, 520), -side * rnd.uniform(0.6, 1.3), SKY, SKY_DARK)
    return img


def front(slogan: str = "BUCKET GETTER", number: str = "10", seed: int = 7) -> Image.Image:
    """The jersey's front panel (the buyer can pick the number)."""
    number = clean_number(number) or "10"
    rnd = random.Random(seed + 11)
    img = ground(seed)
    slogan = (slogan or "").upper().strip()
    if slogan:
        lines = slogan.split(" ", 1) if len(slogan) > 11 and " " in slogan else [slogan]
        y = 3150 if len(lines) == 1 else 2900
        for line in lines:
            font = _fit(line, COLLEGE, 2250, 540, minimum=220)
            _stamp(img, line, font, CX, y, 60, rnd, arch=90)
            y += font.getbbox(line)[3] + 110
        number_y = max(y + 520, 4550)
    else:
        number_y = 4300
    font = _fit(number, IMPACT, 2000 if len(number) > 1 else 1300, 1800, minimum=700)
    _stamp(img, number, font, CX, number_y, 70, rnd, spacing=40)
    return img


def back(name: str = "YOUR NAME", number: str = "10", seed: int = 7) -> Image.Image:
    """The jersey's back panel: the buyer's name over a big number."""
    name, number = clean_name(name) or "YOUR NAME", clean_number(number) or "10"
    rnd = random.Random(seed + 23)
    img = ground(seed, back=True)
    font = _fit(name, COLLEGE, 2250, 520, minimum=200)
    _stamp(img, name, font, CX, 2900, 54, rnd, spacing=30, arch=60)
    font = _fit(number, IMPACT, 2100 if len(number) > 1 else 1400, 1900, minimum=700)
    _stamp(img, number, font, CX, 4350, 70, rnd, spacing=40)
    return img
