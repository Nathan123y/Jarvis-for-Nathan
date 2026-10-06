"""The "while you were away" briefing: gather what the background work recorded,
count it from evidence, and word it.

Pure parts (compose, time helpers) take everything they need as arguments so they
are easy to test; the collectors read files that already exist on this Mac and
record them as events. Nothing here speaks, opens a microphone or draws UI: the
caller decides when and how to deliver (plugins/while_away.py, main.py).

Rules the numbers follow:
  * every count is a number of distinct things (businesses, trades) with an event
    behind it; a source that has never synced, or has gone stale, reads
    "unavailable"/"stale", never 0;
  * "sent" means a send the provider accepted; an uncertain send is reported as
    held, an interested reply is never a sale, an acknowledgement is never a
    finished job;
  * paper trading is always labelled simulated;
  * text that came from outside (names, email snippets) is shown as quoted data.
"""
from __future__ import annotations

import hashlib
import plistlib
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from core.events import Store

TZ_NAME = "America/Los_Angeles"
DEFAULT_THRESHOLD_MIN = 30

# Sources whose freshness the briefing reports on.
WORKER, GMAIL, TRADING, SALES, PROMOTION = "worker", "gmail", "trading", "sales", "promotion"
SOURCE_LABELS = {WORKER: "the background worker", GMAIL: "reply checking", TRADING: "paper trading",
                 SALES: "sales", PROMOTION: "product promotion"}

REPLY_WORDS = (("interested", "interested"), ("call_request", "asked for a call"),
               ("question", "asked a question"), ("declined", "declined"),
               ("opted_out", "opted out"), ("bounced", "bounced"),
               ("automated", "automatic reply"), ("complaint", "complained"), ("unclear", "need a look"))
NEEDS_YOU_REPLIES = ("call_request", "interested", "question", "complaint", "unclear")
ATTENTION_KINDS = ("reply_received", "decision_needed", "job_failed", "job_paused",
                   "connection_missing", "campaign_stopped")


# ── time ─────────────────────────────────────────────────────────────────────
def tz() -> ZoneInfo:
    try:
        return ZoneInfo(TZ_NAME)
    except Exception:                      # tzdata missing: fall back to the Mac's own zone
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz())


def clock(ts: float, now: Optional[float] = None) -> str:
    t = local(ts)
    stamp = t.strftime("%I:%M %p").lstrip("0")
    if now is not None and local(now).date() == t.date():
        return stamp
    return t.strftime("%a ") + stamp


def duration(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}" + (f" {rem} min" if rem and hours < 6 else "")
    days, hrs = divmod(hours, 24)
    return f"{days} day{'s' if days != 1 else ''}" + (f" {hrs} hr" if hrs else "")


_REDACT = re.compile(r"\S+@\S+|https?://\S+|www\.\S+")


def _q(value, limit=120) -> str:
    """Outside text as inert data: no lookalike [TAGS], no stray quotes, bounded."""
    s = _REDACT.sub("(address removed)", str(value or "")).replace("[", "(").replace("]", ")").replace('"', "'")
    return " ".join(s.split())[:limit]


def _usd(cents) -> str:
    return f"${cents / 100:,.2f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ── Mac presence: locked? idle? (read-only, no GUI scripting) ────────────────
def _run(cmd: list[str], timeout: float = 4.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def screen_state(run: Callable[[list[str]], bytes | str] = None) -> str:
    """"locked", "unlocked" or "unknown". Unknown is treated like locked by callers:
    private details are not read aloud unless the Mac is known to be unlocked."""
    try:
        raw = (run or (lambda c: subprocess.run(c, capture_output=True, timeout=4).stdout))(
            ["ioreg", "-n", "Root", "-d1", "-a"])
        data = plistlib.loads(raw if isinstance(raw, bytes) else str(raw).encode())
    except Exception:
        return "unknown"
    root = data[0] if isinstance(data, list) and data else data
    if not isinstance(root, dict):
        return "unknown"
    if root.get("IOConsoleLocked") is True:
        return "locked"
    users = root.get("IOConsoleUsers")
    if not isinstance(users, list) or not users:
        return "unknown"                    # nobody at the console (login window, fast user switch)
    if any(isinstance(u, dict) and u.get("CGSSessionScreenIsLocked") for u in users):
        return "locked"
    return "unlocked"


def idle_seconds(run: Callable[[list[str]], str] = None) -> Optional[float]:
    """Seconds since the last keyboard/mouse input, or None if it can't be read."""
    try:
        text = (run or _run)(["ioreg", "-c", "IOHIDSystem"])
    except Exception:
        return None
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', text or "")
    return int(m.group(1)) / 1e9 if m else None


# ── collectors: turn files already on this Mac into events ───────────────────
def ingest_trading(store: Store, journal_dir: Optional[Path] = None, now: Optional[float] = None) -> int:
    """The day trader's journal, read-only. Returns how many events were new."""
    from trading.journal import Journal
    journal = Journal(journal_dir)
    if not journal.events_path.exists():
        return 0                            # never ran here: stays "unavailable", not zero
    new = 0
    try:
        rows = journal.events(limit=5000)
    except Exception as exc:
        store.sync_failed(TRADING, f"journal unreadable: {exc!r}")
        return 0
    for row in rows:
        kind, t = row.get("kind"), _iso_ts(row.get("t"))
        if t is None:
            continue
        if kind == "trade_result":
            day, sym = str(row.get("day") or ""), str(row.get("symbol") or "")
            if day and sym:
                new += store.record("trade_result", source=TRADING, source_id=f"{day}:{sym}", ts=t,
                                    task_id=f"{day}:{sym}", campaign_id="paper-trading", status="simulated",
                                    title=f"{sym} {day}", update=True,
                                    detail={k: row.get(k) for k in ("symbol", "pnl", "qty", "bought", "sold", "exit", "day")},
                                    evidence={"journal": "config/trading_day/journal.jsonl", "t": row.get("t")})
        elif kind == "snapshot":
            day = str(row.get("date") or "")
            if day:
                new += store.record("paper_snapshot", source=TRADING, source_id=day, ts=t, campaign_id="paper-trading",
                                    status="simulated", title=f"Paper account {day}", update=True,
                                    detail={"equity": row.get("equity"), "spy": row.get("spy"), "date": day},
                                    evidence={"t": row.get("t")})
    store.sync_ok(TRADING, stale_after=5 * 86400)
    return new



def _iso_ts(text) -> Optional[float]:
    try:
        d = datetime.fromisoformat(str(text))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def _hash(value: str, salt: str = "") -> str:
    return hashlib.sha256((salt + value).encode()).hexdigest()[:12]


def _salt(store: Store) -> str:
    salt = store.get("hash_salt")
    if not salt:
        import secrets
        salt = secrets.token_hex(8)
        store.set("hash_salt", salt)
    return salt


def ingest_promotion(store: Store, state: Optional[dict] = None) -> int:
    """Product-promotion emails this Mac already sent (plugins/product_sales state)."""
    if state is None:
        try:
            from plugins import product_sales
            if not product_sales._STATE_FILE.exists():
                return 0
            state = product_sales._load()
        except Exception as exc:
            store.sync_failed(PROMOTION, f"promotion history unreadable: {exc!r}")
            return 0
    new = 0
    salt = _salt(store)
    for draft_id, draft in (state.get("drafts") or {}).items():
        email = str(draft.get("email") or "")
        lead = (state.get("leads") or {}).get(email, {})
        key = _hash(email.lower(), salt)
        ts = _iso_ts(draft.get("sent_at")) or _iso_ts(lead.get("sent_at")) or _iso_ts(lead.get("updated_at"))
        if draft.get("status") == "sent":
            new += store.record("offer_sent", source=PROMOTION, source_id=f"draft:{draft_id}", ts=ts, update=True,
                                task_id=key, campaign_id="product-promotion", status="sent",
                                title=_q(lead.get("name") or "a contact", 60), private=True,
                                detail={"product": draft.get("product")},
                                evidence={"draft": str(draft_id), "recorded_by": "product_sales"})
        elif draft.get("status") == "uncertain":
            new += store.record("offer_sent", source=PROMOTION, source_id=f"draft:{draft_id}", ts=ts or time.time(),
                                task_id=key, campaign_id="product-promotion", status="uncertain",
                                title=_q(lead.get("name") or "a contact", 60), private=True,
                                evidence={"draft": str(draft_id)})
    for email, lead in (state.get("leads") or {}).items():
        key, ts = _hash(str(email).lower(), salt), _iso_ts(lead.get("updated_at"))
        if lead.get("status") == "replied" and ts:
            new += store.record("reply_received", source=PROMOTION, source_id=f"reply:{key}", ts=ts, task_id=key,
                                campaign_id="product-promotion", status="unclear", private=True,
                                title=_q(lead.get("name") or "a contact", 60),
                                detail={"note": "recorded by you; the reply was not classified"},
                                evidence={"recorded_by": "product_sales"})
        elif lead.get("status") == "bought" and ts:
            new += store.record("sale_paid", source=PROMOTION, source_id=f"bought:{key}", ts=ts, task_id=key,
                                campaign_id="product-promotion", status="manual", private=True,
                                title=_q(lead.get("name") or "a contact", 60),
                                detail={"gross": None, "fee": None, "refund": None, "manual": True},
                                evidence={"recorded_by": "you, via product_sales"})
    store.sync_ok(PROMOTION, stale_after=86400)
    return new


def collect(store: Store) -> None:
    """Refresh every local collector; one failing never stops the others."""
    for name, fn in (("trading", ingest_trading), ("promotion", ingest_promotion)):
        try:
            fn(store)
        except Exception as exc:                # a collector must never break startup
            try:
                store.sync_failed(name, repr(exc))
            except Exception:
                pass


# ── compose (pure) ───────────────────────────────────────────────────────────
def _ids(events: list[dict], kind: str, pred=None) -> set:
    return {(e["task_id"] or e["source_id"]) for e in events if e["kind"] == kind and (pred is None or pred(e))}


def _reply_status_by_business(events: list[dict]) -> dict:
    latest: dict = {}
    for e in sorted((e for e in events if e["kind"] == "reply_received"), key=lambda e: (e["ts"], e["id"])):
        latest[e["task_id"] or e["source_id"]] = e["status"] if e["status"] in dict(REPLY_WORDS) else "unclear"
    return latest


def funnel(events: list[dict]) -> dict:
    """Distinct-business counts per stage. Replies are split into statuses that always add up."""
    sent = _ids(events, "offer_sent", lambda e: e["status"] == "sent")
    held = _ids(events, "offer_sent", lambda e: e["status"] not in ("sent",))
    replies = _reply_status_by_business(events)
    by = {s: 0 for s, _ in REPLY_WORDS}
    for s in replies.values():
        by[s] += 1
    return {"found": len(_ids(events, "biz_found")), "qualified": len(_ids(events, "biz_qualified")),
            "built": len(_ids(events, "site_built")), "checked": len(_ids(events, "site_checked")),
            "previews": len(_ids(events, "preview_published")), "sent": len(sent), "held": len(held - sent),
            "replied": len(replies), "reply_status": by,
            "paid": len(_ids(events, "sale_paid")), "delivered": len(_ids(events, "delivered"))}


def sales_summary(events: list[dict]) -> Optional[dict]:
    rows = [e for e in events if e["kind"] == "sale_paid"]
    if not rows:
        return None
    d = [e["detail"] for e in rows]
    out = {"count": len(rows), "currency": "USD"}
    for key in ("gross", "fee", "refund"):
        known = [x[key] for x in d if isinstance(x.get(key), (int, float))]
        out[key] = sum(known) if len(known) == len(d) else None
        out[key + "_missing"] = len(d) - len(known)
    out["net"] = (out["gross"] - out["fee"] - out["refund"]
                  if None not in (out["gross"], out["fee"], out["refund"]) else None)
    return out


def trading_summary(events: list[dict]) -> Optional[dict]:
    trades = [e for e in events if e["kind"] == "trade_result"]
    snaps = sorted((e for e in events if e["kind"] == "paper_snapshot"), key=lambda e: e["ts"])
    if not trades and not snaps:
        return None
    pnl = [t["detail"].get("pnl") for t in trades]
    known = [p for p in pnl if isinstance(p, (int, float))]
    return {"trades": len(trades), "pnl": sum(known) if known else None,
            "winners": sum(1 for p in known if p > 0), "equity": (snaps[-1]["detail"].get("equity") if snaps else None)}


def _money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def _outreach_lines(name: str, f: dict, awaiting: Optional[int], replies_state: str) -> tuple[str, list[str]]:
    """(spoken sentence, panel lines) for one campaign."""
    spoken = []
    if f["found"] or f["qualified"]:
        spoken.append(f"researched {f['found']} businesses and qualified {f['qualified']}" if f["found"]
                      else f"qualified {f['qualified']} businesses")
    if f["built"] or f["previews"]:
        spoken.append(f"built {f['built']} website concepts, {f['previews']} with a live preview")
    if f["sent"]:
        spoken.append(f"sent {_plural(f['sent'], 'offer')}")
    if f["held"]:
        spoken.append(f"held {_plural(f['held'], 'send')} because delivery wasn't confirmed")
    lines = [f"  {name}"]
    lines.append(f"    found {f['found']} · qualified {f['qualified']} · built {f['built']} · "
                 f"quality-checked {f['checked']} · preview published {f['previews']}")
    lines.append(f"    offers sent {f['sent']}" + (f" · held (unconfirmed) {f['held']}" if f["held"] else ""))
    if replies_state in ("ok",):
        r = f["reply_status"]
        bits = [f"{r[k]} {label}" for k, label in REPLY_WORDS if r[k]]
        lines.append(f"    replies {f['replied']}" + (": " + ", ".join(bits) if bits else "")
                     + (f" · still waiting on {awaiting}" if awaiting is not None else ""))
        if f["replied"]:
            spoken.append(f"got {_plural(f['replied'], 'reply')}")
    else:
        lines.append(f"    replies: {replies_state} (reply checking {replies_state}; not counted as zero)")
    if f["paid"]:
        lines.append(f"    paid {f['paid']}")
    if f["delivered"]:
        lines.append(f"    delivered {f['delivered']}")
    return ("; ".join(spoken), lines)


def compose(events: list[dict], open_items: list[dict], syncs: dict, upcoming: Optional[list],
            window: tuple[float, float], awaiting: dict, *, speak_details: bool = True,
            explicit: bool = True) -> dict:
    """Facts for the voice, the on-screen panel, and the counts they came from."""
    start, end = window
    span = f"{clock(start, end)} to {clock(end, end)} Pacific ({duration(end - start)})"
    campaigns: dict = {}
    for e in events:
        if e["campaign_id"] and e["kind"] not in ("trade_result", "paper_snapshot"):
            campaigns.setdefault(e["campaign_id"], []).append(e)
    counts: dict = {"window": [start, end], "campaigns": {}}
    spoken: list[str] = []
    panel = [f"WHILE YOU WERE AWAY · {span}", ""]
    panel_body: list[str] = []

    replies_state = syncs.get(GMAIL, {}).get("state", "unavailable")
    for cid, evs in sorted(campaigns.items()):
        if not any(e["kind"] in ("biz_found", "biz_qualified", "site_built", "site_checked", "preview_published",
                                 "offer_sent", "reply_received", "delivered") for e in evs):
            continue
        f = funnel(evs)
        counts["campaigns"][cid] = f
        # The legacy promotion path records replies by hand, so its own record is the reply source.
        rstate = "ok" if cid == "product-promotion" else replies_state
        sentence, lines = _outreach_lines(cid.replace("-", " "), f, awaiting.get(cid) if rstate == "ok" else None, rstate)
        if sentence:
            spoken.append(sentence[0].upper() + sentence[1:])
        if rstate != "ok" and f["sent"]:
            spoken.append(f"Reply checking is {rstate}, so I can't say who answered")
        panel_body.extend(lines)
    if panel_body:
        panel += ["OUTREACH"] + panel_body + [""]

    sales = sales_summary(events)
    if sales:
        counts["sales"] = sales
        parts = [f"gross {_usd(sales['gross'])}" if sales["gross"] is not None else "gross unavailable",
                 f"fees {_usd(sales['fee'])}" if sales["fee"] is not None else "fees unavailable",
                 f"refunds {_usd(sales['refund'])}" if sales["refund"] is not None else "refunds unavailable",
                 f"net {_usd(sales['net'])}" if sales["net"] is not None else "net unavailable"]
        panel += ["SALES (verified payments only)", f"  {_plural(sales['count'], 'paid sale')}: " + ", ".join(parts), ""]
        spoken.append(f"{_plural(sales['count'], 'sale')} recorded" +
                      (f", net {_usd(sales['net'])} after fees and refunds" if sales["net"] is not None
                       else " (the amounts weren't verified, so I won't quote a total)"))
    trade = trading_summary(events)
    if trade:
        counts["trading"] = trade
        pnl = f"{_money(trade['pnl'])}" if trade["pnl"] is not None else "unavailable"
        panel += ["PAPER TRADING (simulated, not real money)",
                  f"  {_plural(trade['trades'], 'simulated trade')}, result {pnl} before costs"
                  + (f", account value ${trade['equity']:,.0f}" if isinstance(trade["equity"], (int, float)) else ""), ""]
        spoken.append(f"Your paper account (simulated) made {_plural(trade['trades'], 'trade')}"
                      + (f" for {_money(trade['pnl'])} before costs" if trade["pnl"] is not None else ""))
    elif syncs.get(TRADING, {}).get("known") and syncs[TRADING]["state"] in ("stale", "error", "unavailable"):
        panel += ["PAPER TRADING (simulated)", f"  {syncs[TRADING]['state']}", ""]

    # Needs you: open replies and problems, any age (not only this window).
    need = []
    for e in open_items:
        if e["kind"] == "reply_received" and e["status"] not in NEEDS_YOU_REPLIES:
            continue
        need.append(e)
    need.sort(key=lambda e: (0 if e["status"] == "call_request" else 1 if e["kind"] == "reply_received" else 2, -e["ts"]))
    counts["attention"] = len(need)
    if need:
        panel.append("NEEDS YOU")
        for e in need[:12]:
            label = dict(REPLY_WORDS).get(e["status"]) if e["kind"] == "reply_received" else e["kind"].replace("_", " ")
            snippet = _q(e["detail"].get("snippet") or e["detail"].get("note") or "", 100)
            panel.append(f"  #{e['id']} {_q(e['title'], 70)} — {label}" + (f' — "{snippet}"' if snippet else "")
                         + f" ({clock(e['ts'], end)})")
        panel.append("")
        calls = [e for e in need if e["status"] == "call_request"]
        if calls:
            spoken.append(f"{_plural(len(calls), 'owner')} asked for a call")
        other = len(need) - len(calls)
        if other:
            spoken.append(f"{_plural(other, 'item')} need your attention")

    if upcoming:
        panel.append("COMING UP")
        panel += [f"  {_q(w, 40)}  {_q(t, 80)}" for w, t in upcoming[:8]]
        panel.append("")
        spoken.append("Next up: " + "; ".join(f"{_q(t, 50)} ({_q(w, 30)})" for w, t in upcoming[:2]))

    # Sources that can't vouch for their numbers.
    caveats = []
    for src, st in sorted(syncs.items()):
        if st.get("known") and st["state"] != "ok":
            when = f", last good {clock(st['last_ok'], end)}" if st.get("last_ok") else ""
            caveats.append(f"{SOURCE_LABELS.get(src, src)}: {st['state']}{when}")
    if caveats:
        panel += ["SOURCES"] + [f"  {c}" for c in caveats] + [""]
        spoken.append("Heads up, " + "; ".join(caveats))

    # Unprompted briefings (launch, return) need NEW evidence. Carried-over open items, stale-source
    # notes and deadlines are only added to one that has it; "What did I miss?" shows everything.
    has_news = bool(spoken) and (explicit or bool(events))
    if not panel_body and not sales and not trade and not need and not upcoming and not caveats:
        panel += ["Nothing new while you were away."]
    text_panel = "\n".join(panel).strip()[:3800]
    if not speak_details:
        return {"spoken": "", "panel": text_panel, "counts": counts, "empty": not has_news,
                "private_hold": has_news}
    if has_news:
        lead = f"While you were away, from {clock(start, end)} to now"
        spoken_text = lead + ": " + ". ".join(spoken) + "."
    else:
        spoken_text = ""
    return {"spoken": spoken_text, "panel": text_panel, "counts": counts, "empty": not has_news,
            "private_hold": False}


def upcoming(tasks, canvas_items, today: str, horizon_days: int = 2) -> Optional[list]:
    """[(when, title)] for open missions and Canvas work due within `horizon_days`
    (overdue first). None when neither source could be read (stays "unavailable")."""
    if tasks is None and canvas_items is None:
        return None
    from datetime import date, timedelta
    limit = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
    rows = []
    for t in tasks or []:
        due = t.get("due")
        if t.get("status", "open") == "open" and due and due <= limit:
            rows.append((due, ("overdue" if due < today else "today" if due == today else due),
                         f"Mission: {t.get('title')}"))
    for c in canvas_items or []:
        due = c.get("due") or ""
        if c.get("overdue") or (due and due[:10] <= limit):
            rows.append((due[:10] or "0", "overdue" if c.get("overdue") else ("today" if due[:10] == today else due[:10]),
                         f"{c.get('course') or 'Canvas'}: {c.get('name')}"))
    rows.sort(key=lambda r: r[0])
    return [(w, t) for _, w, t in rows]


# ── orchestration ────────────────────────────────────────────────────────────
def threshold_minutes() -> float:
    try:
        from memory.config_manager import load_api_keys
        value = float(load_api_keys().get("away_threshold_minutes", DEFAULT_THRESHOLD_MIN))
        return value if value > 0 else DEFAULT_THRESHOLD_MIN
    except Exception:
        return DEFAULT_THRESHOLD_MIN


def touch_active(store: Store, now: Optional[float] = None) -> None:
    store.set("last_active", float(now if now is not None else time.time()))


def away_seconds(store: Store, now: Optional[float] = None) -> Optional[float]:
    last = store.get("last_active")
    if not isinstance(last, (int, float)):
        return None
    return float(now if now is not None else time.time()) - float(last)


def sync_snapshot(store: Store, now: float) -> dict:
    out = {}
    for src in (WORKER, GMAIL, TRADING, SALES, PROMOTION):
        st = store.sync_state(src, now)
        out[src] = st
    return out


def prepare(store: Store, now: Optional[float] = None, *, upcoming: Optional[list] = None,
            speak_details: bool = True, since: Optional[float] = None, refresh: bool = True,
            save: bool = True, explicit: bool = True) -> dict:
    """Build the briefing for everything not yet delivered. Saves it (so it can be
    replayed) but does not mark it delivered: only the caller knows whether it was
    actually heard or seen."""
    now = float(now if now is not None else time.time())
    if refresh:
        collect(store)
    after_id, delivered_ts = store.delivered_through()
    last_active = store.get("last_active")
    start = since if since is not None else (
        delivered_ts if isinstance(delivered_ts, (int, float)) else
        (float(last_active) if isinstance(last_active, (int, float)) else now))
    events = store.events(after_id=after_id)
    if events:
        start = min(start, events[0]["ts"]) if since is None else start
    open_items = store.open_items(ATTENTION_KINDS, now=now)
    syncs = {k: {**v, "known": _known(store, k)} for k, v in sync_snapshot(store, now).items()}
    awaiting = {}
    for cid in {e["campaign_id"] for e in events if e["campaign_id"]}:
        allx = [e for e in store.events(kinds=("offer_sent", "reply_received"), until=now)
                if e["campaign_id"] == cid]
        sent = _ids(allx, "offer_sent", lambda e: e["status"] == "sent")
        awaiting[cid] = len(sent - set(_reply_status_by_business(allx)))
    # Past events the user saw before are not replayed as "new": only open actions carry over.
    result = compose(events, open_items, syncs, upcoming, (start, now), awaiting, speak_details=speak_details,
                     explicit=explicit)
    result["through_id"] = max([after_id] + [e["id"] for e in events])
    result["window"] = (start, now)
    result["id"] = (store.save_briefing(window_start=start, window_end=now, through_id=result["through_id"],
                                        spoken=result["spoken"], panel=result["panel"], counts=result["counts"])
                    if save else 0)
    return result


def _known(store: Store, source: str) -> bool:
    with store._conn() as con:
        return con.execute("SELECT 1 FROM sources WHERE name=?", (source,)).fetchone() is not None
