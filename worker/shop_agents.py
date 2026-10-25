"""The shop's crew: four agents that run the print-on-demand shop as a daily cycle, and a notebook they keep for themselves.

  Trend Scout  once a day reads what's going viral (Know Your Meme, which tracks TikTok and X memes too; Reddit's meme and
               funny-phrase subreddits; Bluesky's trending topics; Instagram's big meme pages and hashtags, read only, see ig_scout.py) and picks the five funniest phrases or memes that would work on a
               shirt, skipping news, politics, real people and brands. Its picks go to the Designer.
  Designer     shop.Shop.make_design: turns one pick (or a winner's theme) into a slogan, an illustration and a listing, using the
               notebook's lessons. Hands the design to the Publisher.
  Publisher    shop.Shop.list_pending: puts it on Etsy and eBay as a tee, mug and sticker.
  Analyst      every evening looks at what's been made, what sold and what went wrong (refused ideas, failed listings), and rewrites
               the notebook: at most 12 short lessons, kept if still true, dropped if the results disprove them. The Scout and the
               Designer read the notebook every morning, so each day's work starts from what the last ones taught.

Winners: the moment a design sells, the Analyst marks it a winner, puts it on every product it isn't on yet, and queues two spin-offs
(new jokes on the same theme); afterwards one design in two is a spin-off of a winner, so the shop leans into what sells.

Everything is in memory/etsy_shop/notebook.json (git-ignored).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

NOTEBOOK = Path(__file__).resolve().parent.parent / "memory" / "etsy_shop" / "notebook.json"
MAX_LESSONS = 12
SPINOFFS_PER_WIN = 2
FEED_MAX = 80

SCOUT_PROMPT = """You are the Trend Scout for a print-on-demand shop that sells edgy, meme-literate T-shirts, mugs and stickers to people
aged 16-35 who live online. Below is what is going viral TODAY. Pick the 5 funniest phrases, memes, catchphrases or slang terms that
would make a stranger laugh on a shirt.

Skip: news, politics, sports, anything about a real person (celebrities, streamers, athletes, people from viral clips), song lyrics or
titles, brands, TV/movies/games, and anything sexual, hateful or about tragedies.

Your own notes from earlier days (what has worked and what hasn't):
{lessons}

Today's feeds:
{feeds}

Reply with JSON only: {{"picks": [{{"phrase": "the phrase or meme", "where": "which feed", "why": "why it's funny and who buys it",
"angle": "a shirt joke idea that riffs on it"}}]}}"""

ANALYST_PROMPT = """You are the Analyst for a small automated print-on-demand shop (edgy meme T-shirts, mugs and stickers on Etsy and
eBay). Each evening you rewrite your own notebook so tomorrow's Trend Scout and Designer do better.

Your notebook so far:
{lessons}

Everything the shop has made, newest first (age in days, what it was, which products are live, sales):
{designs}

Problems since your last review:
{problems}

Rewrite the notebook: keep lessons the data still supports, change or drop ones it contradicts, add new ones it teaches. Be concrete
(themes, joke styles, slogan length, which products sell) and honest: with few or no sales, say what's still unknown instead of
guessing. At most {max_lessons} lessons, one sentence each. Then name up to 3 themes to make more of ("double_down", only themes that
sold or clearly beat the rest) and up to 3 to stop ("avoid").

Reply with JSON only: {{"lessons": ["..."], "double_down": ["..."], "avoid": ["..."]}}"""


def load(path: Path = NOTEBOOK) -> dict:
    try:
        nb = json.loads(path.read_text())
    except (OSError, ValueError):
        nb = {}
    for k in ("lessons", "winners", "feed", "double_down", "avoid", "problems"):
        nb.setdefault(k, [])
    nb.setdefault("picks", {})
    return nb


def save(nb: dict, path: Path = NOTEBOOK) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(nb, indent=1))
    tmp.replace(path)


def log(nb: dict, frm: str, to: str, text: str, clock: Callable[[], float] = time.time) -> None:
    nb["feed"] = (nb["feed"] + [{"at": clock(), "from": frm, "to": to, "text": text[:300]}])[-FEED_MAX:]


def problem(nb: dict, text: str, clock: Callable[[], float] = time.time) -> None:
    """Something the Analyst should learn from tonight (a refused idea, a failed listing)."""
    nb["problems"] = (nb["problems"] + [{"at": clock(), "text": text[:200]}])[-30:]


def lessons_text(nb: dict) -> str:
    out = [f"- {x}" for x in nb["lessons"]] or ["- (none yet: this is the first week)"]
    if nb["double_down"]:
        out.append("Make more of: " + "; ".join(nb["double_down"]))
    if nb["avoid"]:
        out.append("Stop making: " + "; ".join(nb["avoid"]))
    return "\n".join(out)


# ── Trend Scout ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def scout(nb: dict, feeds: str, ask: Optional[Callable[[str], Optional[dict]]], day: str,
          clock: Callable[[], float] = time.time) -> str:
    """Today's picks as text for the Designer (cached per day in the notebook); the raw feeds if the Scout can't think today."""
    if nb["picks"].get("day") == day and nb["picks"].get("text"):
        return nb["picks"]["text"]
    picks = []
    if ask and feeds:
        out = ask(SCOUT_PROMPT.format(lessons=lessons_text(nb), feeds=feeds[:9000]))
        picks = [p for p in (out or {}).get("picks") or [] if isinstance(p, dict) and p.get("phrase")][:5]
    if picks:
        text = "The Trend Scout's picks for today (funniest viral phrases and memes, already filtered):\n" + "\n".join(
            f"{i + 1}. \"{p['phrase']}\" ({p.get('where', '?')}): {p.get('why', '')} Idea: {p.get('angle', '')}" for i, p in enumerate(picks))
        log(nb, "scout", "designer", "picked " + "; ".join(f"\"{p['phrase']}\"" for p in picks), clock)
    else:
        text = feeds
        if feeds:
            log(nb, "scout", "designer", "couldn't rank today's feeds, so passed them on raw", clock)
    nb["picks"] = {"day": day, "text": text, "list": picks}
    return text


# ── Designer's brief ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def designer_notes(nb: dict, spinoff: Optional[dict]) -> str:
    out = ["Notes the shop's agents wrote for themselves from past results (follow them):", lessons_text(nb)]
    if spinoff:
        out.append(f"THIS DESIGN IS A SPIN-OFF OF A WINNER: \"{spinoff.get('slogan')}\" ({spinoff.get('niche')}) sold. Make a NEW joke "
                   "on the same theme, for the same buyers, in the same style; not the same slogan, and don't use the trend list.")
    return "\n".join(out)


def next_spinoff(nb: dict, last_was_spinoff: bool, spinoffs_made: int) -> Optional[str]:
    """After the queued spin-offs, every other daily design riffs on a winner (taking turns, best seller first)."""
    if not nb["winners"] or last_was_spinoff:
        return None
    best = sorted(nb["winners"], key=lambda w: (-w.get("sales", 0), -w.get("at", 0)))
    return best[spinoffs_made % len(best)]["design"]


# ── a sale ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def on_sale(nb: dict, st: dict, design_id: str, price: float, clock: Callable[[], float] = time.time) -> Optional[str]:
    """Mark the design a winner; the first sale queues spin-offs and the products it isn't on yet. Returns a text for the user."""
    rec = next((d for d in st["designs"] if d.get("id") == design_id), None)
    if not rec:
        return None
    w = next((w for w in nb["winners"] if w["design"] == design_id), None)
    first = w is None
    if first:
        w = {"design": design_id, "slogan": rec.get("slogan"), "niche": rec.get("niche"), "sales": 0, "revenue": 0.0, "at": clock()}
        nb["winners"].append(w)
    w["sales"] += 1
    w["revenue"] = round(w["revenue"] + (price or 0), 2)
    w["last"] = clock()
    if not first:
        log(nb, "analyst", "designer", f"\"{rec.get('slogan')}\" sold again ({w['sales']} sales)", clock)
        return None
    rec.pop("only", None)                                   # a tee-only winner goes on the mug and sticker too
    st["make_spinoff"] = (st.get("make_spinoff") or []) + [design_id] * SPINOFFS_PER_WIN
    log(nb, "analyst", "designer", f"WINNER: \"{rec.get('slogan')}\" sold; queued {SPINOFFS_PER_WIN} spin-offs and every product", clock)
    return (f"🏆 First sale for \"{rec.get('slogan')}\" ({rec.get('niche')}). The shop's agents are leaning into it: it's going on "
            f"every product, and {SPINOFFS_PER_WIN} more designs on the same theme are on the way.")


# ── Analyst's evening review ───────────────────────────────────────────────────────────────────────────────────────────────────
def review(nb: dict, st: dict, ask: Optional[Callable[[str], Optional[dict]]], clock: Callable[[], float] = time.time) -> bool:
    if not ask:
        return False
    now = clock()
    sold = {w["design"]: w for w in nb["winners"]}
    rows = []
    for d in reversed(st["designs"][-60:]):
        live = sorted(k for k, p in (d.get("products") or {}).items() if p.get("published"))
        w = sold.get(d.get("id"))
        rows.append(f"- {(now - d.get('made', now)) / 86400:.0f}d old · \"{d.get('slogan')}\" · theme: {d.get('niche')} · "
                    f"{'spin-off · ' if d.get('spinoff_of') else ''}live as {', '.join(live) or 'nothing yet'} · "
                    + (f"SOLD {w['sales']} (${w['revenue']:.2f})" if w else "no sales"))
    since = nb.get("reviewed_at", 0)
    problems = [p["text"] for p in nb["problems"] if p["at"] > since] or ["none"]
    out = ask(ANALYST_PROMPT.format(lessons=lessons_text(nb), designs="\n".join(rows) or "nothing yet",
                                    problems="\n".join(f"- {p}" for p in problems[-15:]), max_lessons=MAX_LESSONS))
    lessons = [str(x).strip() for x in (out or {}).get("lessons") or [] if str(x).strip()][:MAX_LESSONS]
    if not lessons:
        return False
    nb["lessons"] = lessons
    nb["double_down"] = [str(x) for x in (out or {}).get("double_down") or []][:3]
    nb["avoid"] = [str(x) for x in (out or {}).get("avoid") or []][:3]
    nb["reviewed_at"] = now
    nb["reviewed_day"] = datetime.fromtimestamp(now).date().isoformat()
    log(nb, "analyst", "scout", f"rewrote the notebook: {len(lessons)} lessons" +
        (f"; more of {', '.join(nb['double_down'])}" if nb["double_down"] else ""), clock)
    return True


def summary(nb: dict) -> str:
    """What Jarvis says for 'what have the shop's agents learned'."""
    parts = []
    if nb["lessons"]:
        parts.append("Their notebook: " + " ".join(nb["lessons"][:6]))
    else:
        parts.append("They haven't written their first notes yet; the Analyst reviews the shop every evening at 9.")
    if nb["winners"]:
        parts.append("Winners: " + "; ".join(f"\"{w['slogan']}\" ({w['sales']} sold)" for w in nb["winners"][:5]) + ".")
    if nb["double_down"]:
        parts.append("Making more of: " + "; ".join(nb["double_down"]) + ".")
    picks = (nb.get("picks") or {}).get("list") or []
    if picks:
        parts.append("Today's trend picks: " + ", ".join(f"\"{p['phrase']}\"" for p in picks[:5]) + ".")
    return " ".join(parts)
