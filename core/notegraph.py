"""The note graph shown in the Jarvis window: notes as dots, [[links]] as lines.

Everything here is local and read-only. It only reads the Markdown files of the
connected Obsidian vault, keeps note titles and which note links to which, and
never sends any of it anywhere. Qt-free so it can be tested.
"""
from __future__ import annotations

import math
import random
import re
import time
from pathlib import Path

MAX_NODES = 300
_MAX_READ = 200_000
_LINK = re.compile(r"\[\[([^\[\]\n]+?)\]\]")


def parse_links(text: str) -> list[str]:
    """Targets of [[wiki links]], without alias, heading or block parts."""
    out = []
    for raw in _LINK.findall(text or ""):
        t = raw.split("|", 1)[0].split("#", 1)[0].split("^", 1)[0].strip()
        if t:
            out.append(t)
    return out


def _key(name: str) -> str:
    return Path(name.replace("\\", "/")).name.lower().removesuffix(".md").strip()


def build_graph(vault: Path, notes) -> dict:
    """{'nodes': [title...], 'edges': [(i, j)...]} for the notes in `notes`.

    `notes` is an iterable of paths inside `vault`. When there are more than
    MAX_NODES notes, the most-connected ones are kept.
    """
    titles: list[str] = []
    paths: list[str] = []
    texts: list[list[str]] = []
    index: dict[str, int] = {}
    for path in notes:
        try:
            with open(path, "rb") as f:
                text = f.read(_MAX_READ).decode("utf-8", "ignore")
        except OSError:
            continue
        k = path.stem.lower().strip()
        if k in index:           # same name in two folders: keep the first
            continue
        index[k] = len(titles)
        titles.append(path.stem)
        paths.append(str(path))
        texts.append(parse_links(text))
    edges = set()
    for i, links in enumerate(texts):
        for t in links:
            j = index.get(_key(t))
            if j is not None and j != i:
                edges.add((min(i, j), max(i, j)))
    if len(titles) > MAX_NODES:
        deg = [0] * len(titles)
        for a, b in edges:
            deg[a] += 1
            deg[b] += 1
        keep = sorted(sorted(range(len(titles)), key=lambda n: (-deg[n], n))[:MAX_NODES])
        remap = {old: new for new, old in enumerate(keep)}
        titles = [titles[n] for n in keep]
        paths = [paths[n] for n in keep]
        edges = {(remap[a], remap[b]) for a, b in edges if a in remap and b in remap}
    return {"nodes": titles, "edges": sorted(edges), "paths": paths}


def layout(n: int, edges, iterations: int = 120, seed: int = 7) -> list[tuple[float, float]]:
    """Force-directed positions in [-1, 1]. Deterministic for a given graph."""
    if n == 0:
        return []
    if n == 1:
        return [(0.0, 0.0)]
    rnd = random.Random(seed)
    pos = [[rnd.uniform(-1, 1), rnd.uniform(-1, 1)] for _ in range(n)]
    k = math.sqrt(4.0 / n)
    temp = 0.25
    for _ in range(iterations):
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                dx, dy = pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]
                d = math.hypot(dx, dy) or 1e-3
                f = k * k / d
                disp[i][0] += dx / d * f; disp[i][1] += dy / d * f
                disp[j][0] -= dx / d * f; disp[j][1] -= dy / d * f
        for a, b in edges:
            dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
            d = math.hypot(dx, dy) or 1e-3
            f = d * d / k
            disp[a][0] -= dx / d * f; disp[a][1] -= dy / d * f
            disp[b][0] += dx / d * f; disp[b][1] += dy / d * f
        for i in range(n):          # gentle pull to the centre
            disp[i][0] -= pos[i][0] * 0.3 * k
            disp[i][1] -= pos[i][1] * 0.3 * k
            d = math.hypot(*disp[i]) or 1e-9
            s = min(d, temp) / d
            pos[i][0] += disp[i][0] * s
            pos[i][1] += disp[i][1] * s
        temp *= 0.97
    m = max(max(abs(x), abs(y)) for x, y in pos) or 1.0
    return [(x / m, y / m) for x, y in pos]


def layout3d(n: int, edges, iterations: int = 0, seed: int = 7,
             yield_gil: bool = True) -> list[tuple[float, float, float]]:
    """Force-directed positions inside the unit sphere, for the rotating view.

    Deterministic for a given graph. Linked notes pull together, every note
    pushes the others away, and a soft pull to the middle keeps loose notes
    (no links) floating around the cluster like Obsidian's own graph.
    `yield_gil` hands the interpreter back between rounds so the window stays
    smooth while this runs on a background thread.
    """
    if n == 0:
        return []
    if n == 1:
        return [(0.0, 0.0, 0.0)]
    if not iterations:
        iterations = 90 if n <= 120 else (60 if n <= 220 else 45)
    rnd = random.Random(seed)
    pos = []
    for _ in range(n):                      # start on a random spread in a ball
        while True:
            p = [rnd.uniform(-1, 1) for _ in range(3)]
            if p[0] * p[0] + p[1] * p[1] + p[2] * p[2] <= 1.0:
                break
        pos.append(p)
    k = (8.0 / n) ** (1.0 / 3.0) * 0.55
    k2 = k * k
    temp = 0.22
    for _ in range(iterations):
        dx_ = [0.0] * n
        dy_ = [0.0] * n
        dz_ = [0.0] * n
        for i in range(n):
            xi, yi, zi = pos[i]
            fx = fy = fz = 0.0
            for j in range(i + 1, n):
                xj, yj, zj = pos[j]
                ddx, ddy, ddz = xi - xj, yi - yj, zi - zj
                d2 = ddx * ddx + ddy * ddy + ddz * ddz + 1e-4
                f = k2 / d2                  # repulsion k^2/d, along the unit vector
                ddx *= f; ddy *= f; ddz *= f
                fx += ddx; fy += ddy; fz += ddz
                dx_[j] -= ddx; dy_[j] -= ddy; dz_[j] -= ddz
            dx_[i] += fx; dy_[i] += fy; dz_[i] += fz
        for a, b in edges:
            ddx = pos[a][0] - pos[b][0]
            ddy = pos[a][1] - pos[b][1]
            ddz = pos[a][2] - pos[b][2]
            d = math.sqrt(ddx * ddx + ddy * ddy + ddz * ddz) or 1e-3
            f = d / k                        # attraction d^2/k, along the unit vector
            dx_[a] -= ddx * f; dy_[a] -= ddy * f; dz_[a] -= ddz * f
            dx_[b] += ddx * f; dy_[b] += ddy * f; dz_[b] += ddz * f
        for i in range(n):
            p = pos[i]
            gx = dx_[i] - p[0] * 0.9 * k
            gy = dy_[i] - p[1] * 0.9 * k
            gz = dz_[i] - p[2] * 0.9 * k
            d = math.sqrt(gx * gx + gy * gy + gz * gz) or 1e-9
            s = min(d, temp) / d
            p[0] += gx * s; p[1] += gy * s; p[2] += gz * s
        temp *= 0.96
        if yield_gil:
            time.sleep(0)
    cx = sum(p[0] for p in pos) / n
    cy = sum(p[1] for p in pos) / n
    cz = sum(p[2] for p in pos) / n
    # Scale by a typical radius, not the farthest note: one unlinked note
    # drifting far out would otherwise shrink the whole cluster to a speck.
    # Only linked notes set the scale; unlinked ones are pulled in after.
    linked = {i for e in edges for i in e}
    basis = [pos[i] for i in sorted(linked)] if len(linked) >= 2 else pos
    radii = sorted(math.sqrt((p[0] - cx) ** 2 + (p[1] - cy) ** 2 + (p[2] - cz) ** 2)
                   for p in basis)
    m = (radii[int(0.85 * (len(radii) - 1))] / 0.85) or 1.0
    out = []
    for p in pos:
        x, y, z = (p[0] - cx) / m, (p[1] - cy) / m, (p[2] - cz) / m
        r = math.sqrt(x * x + y * y + z * z)
        if r > 1.0:                          # pull stragglers in to just outside
            r2 = 1.0 + 0.2 * math.tanh((r - 1.0) / 0.2)
            x, y, z = x * r2 / r, y * r2 / r, z * r2 / r
        out.append((x, y, z))
    return out


def project(points, yaw: float, pitch: float, zoom: float, w: float, h: float,
            dist: float = 3.2) -> list[tuple[float, float, float, float]]:
    """Rotate points (yaw about the vertical axis, then pitch) and project them.

    Returns (screen_x, screen_y, size_factor, depth) per point. depth runs from
    -1 at the far side of the sphere to +1 at the near side; size_factor is the
    perspective scale (1.0 at the centre plane).
    """
    cyw, syw = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    half = min(w, h) * 0.38 * zoom
    cx, cy = w / 2.0, h / 2.0
    out = []
    for x, y, z in points:
        x1 = x * cyw + z * syw
        z1 = -x * syw + z * cyw
        y2 = y * cp - z1 * sp
        z2 = y * sp + z1 * cp
        s = dist / (dist - z2)
        out.append((cx + x1 * half * s, cy + y2 * half * s, s, z2))
    return out


def obsidian_uri(path: str) -> str:
    """obsidian:// link that opens this note in the Obsidian app."""
    from urllib.parse import quote
    return "obsidian://open?path=" + quote(str(path), safe="")
