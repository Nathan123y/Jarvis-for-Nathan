"""The note graph shown in the Jarvis window: notes as dots, [[links]] as lines.

Everything here is local and read-only. It only reads the Markdown files of the
connected Obsidian vault, keeps note titles and which note links to which, and
never sends any of it anywhere. Qt-free so it can be tested.
"""
from __future__ import annotations

import math
import random
import re
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
        edges = {(remap[a], remap[b]) for a, b in edges if a in remap and b in remap}
    return {"nodes": titles, "edges": sorted(edges)}


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
