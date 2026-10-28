"""A small public storefront for the shop, so Pinterest can pin every new product by itself.

Pinterest auto-publishes Pins from an RSS feed, but only for links on a website you have claimed, so the
links can't go straight to Etsy. This builds a static site (GitHub Pages, free) with a page per design,
showing its product photos and "Buy on Etsy" / "Buy on eBay" buttons, plus feed.xml for Pinterest.
Search engines can find it too.

Built from memory/etsy_shop.json after new listings, and pushed to the GitHub Pages repo in
plugin_config.storefront.repo (default Nathan123y/Nathan123y.github.io). The Pinterest claim tag, once
Pinterest gives it, goes in plugin_config.storefront.pinterest_tag and into every page's <head>.
"""
from __future__ import annotations

import html
import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
REPO_DIR = ROOT / "config" / "worker" / "storefront-repo"
DEFAULTS = {"repo": "Nathan123y/Nathan123y.github.io", "name": "Gift Designs", "pinterest_tag": "",
            # designs made from this time on get a feed item (so a Pin) per product; older ones keep one per design
            # (their products were pinned by hand from a bulk upload on 2026-10-26)
            "product_pins_since": 1793044800}
CAMERAS = {"tee": ("person-1", "front-2", "lifestyle"), "mug": ("context-1", "front"), "sticker": ("context-1", "front")}


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config("storefront")
    except Exception:
        stored = {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS and v}}


def site_url(repo: str) -> str:
    owner, name = repo.split("/", 1)
    base = f"https://{owner.lower()}.github.io"
    return base + "/" if name.lower() == f"{owner.lower()}.github.io" else f"{base}/{name}/"


def _e(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _page(title: str, body: str, cfg: dict, description: str = "", image: str = "") -> str:
    tag = cfg.get("pinterest_tag", "").strip()
    meta = f'<meta name="p:domain_verify" content="{_e(tag)}">' if tag else ""
    og = (f'<meta property="og:title" content="{_e(title)}"><meta property="og:description" content="{_e(description)}">'
          + (f'<meta property="og:image" content="{_e(image)}">' if image else ""))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><meta name="description" content="{_e(description)}">{meta}{og}
<link rel="alternate" type="application/rss+xml" title="{_e(cfg['name'])}" href="feed.xml">
<style>
:root{{--bg:#fbf8f3;--ink:#2a2622;--muted:#6f675e;--card:#fff;--accent:#2f7d4f;--line:#e8e1d6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1a17;--ink:#f1ece4;--muted:#b5ab9e;--card:#262320;--accent:#5cbf85;--line:#3a3530}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,system-ui,sans-serif}}
header,main,footer{{max-width:1040px;margin:0 auto;padding:20px 16px}}header a{{color:inherit;text-decoration:none;font-weight:700;font-size:1.3rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;color:inherit;text-decoration:none}}
.card img{{width:100%;aspect-ratio:1;object-fit:cover;display:block}}.card p{{margin:0;padding:10px 12px;font-size:.95rem}}
.photos{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}}.photos img{{width:100%;border-radius:10px;border:1px solid var(--line)}}
.buy{{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}}.buy a{{background:var(--accent);color:#fff;padding:10px 16px;border-radius:999px;text-decoration:none;font-weight:600}}
.muted{{color:var(--muted)}}footer{{color:var(--muted);font-size:.85rem}}
</style></head><body>
<header><a href="./">{_e(cfg['name'])}</a></header><main>{body}</main>
<footer>Original designs, printed on demand and shipped by our production partner.</footer></body></html>
"""


def _download(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        dest.write_bytes(urllib.request.urlopen(req, timeout=60).read())
        return True
    except Exception:
        return False


def collect(designs: list, printify: Callable) -> list:
    """Each listed design with its live links and product photos: [{"rec", "links": [(label, url, price)], "photos": [url],
    "products": [{"kind", "links", "photos"}]}] (products: the ones with photos, for a page and a Pin each)."""
    from worker.shop import CHANNELS, PRODUCTS, products_in
    out = []
    for rec in designs:
        links, photos, products = [], [], {}
        for channel in CHANNELS:
            for kind, prod in products_in(dict(rec), channel).items():
                if not prod.get("published") or kind not in PRODUCTS:
                    continue
                if not prod.get("url") or (kind not in products and not prod.get("photos")):
                    try:
                        p = printify("GET", f"shops/{prod['shop']}/products/{prod['id']}.json")
                    except Exception:
                        continue
                    prod["url"] = (p.get("external") or {}).get("handle") or ""
                    wanted = CAMERAS.get(kind, ("front",))
                    prod["photos"] = [i["src"] for i in p.get("images") or []
                                      if any(f"camera_label={c}" in i["src"] for c in wanted)][:2] or \
                        [i["src"] for i in (p.get("images") or [])[:1]]
                if prod.get("url"):
                    links.append((f"{PRODUCTS[kind]['name']} on {CHANNELS[channel]['name']}", prod["url"], prod.get("price")))
                    one = products.setdefault(kind, {"kind": kind, "links": [], "photos": []})
                    one["links"].append((f"Buy on {CHANNELS[channel]['name']}", prod["url"], prod.get("price")))
                if kind in products and not products[kind]["photos"]:   # the first channel's photos (Pop-Up Store, else Etsy)
                    photos += prod.get("photos") or []
                    products[kind]["photos"] = prod.get("photos") or []
        if links:
            out.append({"rec": rec, "links": links, "photos": photos,
                        "products": [p for p in products.values() if p["photos"]]})
    return out


def build(designs: list, printify: Callable, out_dir: Path, cfg: Optional[dict] = None,
          download: Callable[[str, Path], bool] = _download) -> int:
    """Writes the site into out_dir; returns how many designs are on it."""
    cfg = cfg or settings()
    url = site_url(cfg["repo"])
    items = collect(designs, printify)
    (out_dir / "p").mkdir(parents=True, exist_ok=True)
    (out_dir / "img").mkdir(exist_ok=True)
    cards, feed = [], []
    for it in sorted(items, key=lambda x: -x["rec"]["made"]):
        rec = it["rec"]
        local = []
        for n, src in enumerate(it["photos"][:6]):
            name = f"img/{rec['id']}-{n}.jpg"
            if download(src, out_dir / name):
                local.append(name)
        art = f"img/{rec['id']}-art.png"
        if Path(rec.get("file", "")).is_file() and not (out_dir / art).exists():
            from PIL import Image
            im = Image.open(rec["file"])
            im.thumbnail((1000, 1000))
            im.save(out_dir / art, "PNG", optimize=True)
        hero = local[0] if local else art
        buttons = "".join(f'<a href="{_e(u)}" rel="nofollow">{_e(label)}' +
                          (f" · ${price / 100:.2f}" if price else "") + "</a>" for label, u, price in it["links"])
        photos = "".join(f'<img src="../{_e(n)}" alt="{_e(rec["title"])}" loading="lazy">' for n in local + [art])
        body = (f'<h1>{_e(rec["title"])}</h1><p class="muted">{_e(rec.get("slogan", "").title())}</p>'
                f'<p>{_e(rec.get("description"))}</p><div class="buy">{buttons}</div><div class="photos">{photos}</div>')
        (out_dir / "p" / f"{rec['id']}.html").write_text(
            _page(f"{rec['title']} | {cfg['name']}", body, cfg, rec.get("description", ""), url + hero).replace(
                'href="feed.xml"', 'href="../feed.xml"').replace('href="./"', 'href="../"'))
        cards.append(f'<a class="card" href="p/{rec["id"]}.html"><img src="{_e(hero)}" alt="{_e(rec["title"])}" '
                     f'loading="lazy"><p>{_e(rec["title"])}</p></a>')
        when = format_datetime(datetime.fromtimestamp(rec["made"], tz=timezone.utc))
        if rec["made"] >= cfg.get("product_pins_since", 0):
            for prod in it["products"]:
                feed.append(_product_page(rec, prod, out_dir, url, cfg, when, download))
            continue
        feed.append(f"<item><title>{_e(rec['title'])}</title><link>{url}p/{rec['id']}.html</link>"
                    f"<guid>{url}p/{rec['id']}.html</guid><pubDate>{when}</pubDate>"
                    f"<description>{_e(rec.get('description'))}</description>"
                    f'<enclosure url="{url}{hero}" type="image/{"png" if hero.endswith(".png") else "jpeg"}" length="0"/></item>')
    (out_dir / "index.html").write_text(_page(cfg["name"], '<h1>New designs</h1><div class="grid">' + "".join(cards) +
                                              "</div>", cfg, "Original gift designs on shirts, mugs and stickers."))
    (out_dir / "feed.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{_e(cfg["name"])}</title>'
        f"<link>{url}</link><description>New designs on shirts, mugs and stickers</description>"
        + "".join([f for f in feed if f][:50]) + "</channel></rss>\n")
    (out_dir / ".nojekyll").write_text("")
    return len(items)


def _product_page(rec: dict, prod: dict, out_dir: Path, url: str, cfg: dict, when: str, download: Callable) -> str:
    """One product's page (its photos, Buy on Etsy / eBay) and its feed item. Pinterest makes a Pin per feed item,
    and auto-published Pins must link to the claimed site, so the Pin opens this page rather than Etsy itself."""
    from worker.shop import PRODUCTS
    name = PRODUCTS[prod["kind"]]["name"]
    from worker.shop import listing_title
    title = listing_title(rec["title"], name, 100)
    slug = f"{rec['id']}-{prod['kind']}"
    local = []
    for n, src in enumerate(prod["photos"][:4]):
        img = f"img/{slug}-{n}.jpg"
        if download(src, out_dir / img):
            local.append(img)
    if not local:
        return ""
    desc = f"{rec.get('description') or ''} Available as a {name.lower()}.".strip()
    buttons = "".join(f'<a href="{_e(u)}" rel="nofollow">{_e(label)}' + (f" · ${price / 100:.2f}" if price else "") + "</a>"
                      for label, u, price in prod["links"])
    photos = "".join(f'<img src="../{_e(n)}" alt="{_e(title)}" loading="lazy">' for n in local)
    body = (f'<h1>{_e(title)}</h1><p>{_e(desc)}</p><div class="buy">{buttons}</div><div class="photos">{photos}</div>'
            f'<p class="muted"><a href="{rec["id"]}.html">See it on other products</a></p>')
    (out_dir / "p" / f"{slug}.html").write_text(
        _page(f"{title} | {cfg['name']}", body, cfg, desc, url + local[0]).replace(
            'href="feed.xml"', 'href="../feed.xml"').replace('href="./"', 'href="../"'))
    page = f"{url}p/{slug}.html"
    return (f"<item><title>{_e(title)}</title><link>{page}</link><guid>{page}</guid><pubDate>{when}</pubDate>"
            f"<description>{_e(desc)}</description>"
            f'<enclosure url="{url}{local[0]}" type="image/jpeg" length="0"/></item>')


def _git(*args: str, cwd: Path = REPO_DIR, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def host_video(path: Path) -> str:
    """Put a video on the storefront site (v/) and return its public address, for Instagram to fetch."""
    import shutil
    import time
    cfg = settings()
    if not (REPO_DIR / ".git").exists():
        raise RuntimeError("the storefront repository isn't set up yet")
    (REPO_DIR / "v").mkdir(exist_ok=True)
    shutil.copy2(path, REPO_DIR / "v" / Path(path).name)
    _git("pull", "--rebase", "--quiet")
    _git("add", "v")
    who = ["-c", "user.name=Nathan Yousif", "-c", "user.email=nathanyousif@Nathans-Laptop.local"]
    _git(*who, "commit", "-q", "-m", f"Video {Path(path).name}")
    r = _git("push", "-q", "origin", "HEAD")
    if r.returncode:
        raise RuntimeError(f"couldn't push the video: {r.stderr[-200:]}")
    url = site_url(cfg["repo"]) + "v/" + Path(path).name
    for _ in range(30):                                    # GitHub Pages takes a minute to serve new files
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            if urllib.request.urlopen(req, timeout=20).status == 200:
                return url
        except Exception:
            pass
        time.sleep(10)
    raise RuntimeError("GitHub Pages didn't start serving the video")


def publish(designs: list, printify: Callable, log: Callable[[str], None] = print) -> str:
    """Build the site and push it. '' when there was nothing new."""
    cfg = settings()
    remote = f"https://github.com/{cfg['repo']}.git"
    if not (REPO_DIR / ".git").exists():
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", remote, str(REPO_DIR)], capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError(f"couldn't clone {cfg['repo']} (does the repository exist?): {r.stderr[-200:]}")
    _git("pull", "--rebase", "--quiet")
    n = build(designs, printify, REPO_DIR, cfg)
    _git("add", "-A")
    if not _git("status", "--porcelain").stdout.strip():
        return ""
    who = ["-c", "user.name=Nathan Yousif", "-c", "user.email=nathanyousif@Nathans-Laptop.local"]
    _git(*who, "commit", "-q", "-m", f"Shop: {n} designs")
    r = _git("push", "-q", "origin", "HEAD")
    if r.returncode:
        raise RuntimeError(f"couldn't push the storefront: {r.stderr[-200:]}")
    log(f"storefront: published {n} designs to {site_url(cfg['repo'])}")
    return site_url(cfg["repo"])
