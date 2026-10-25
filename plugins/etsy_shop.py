"""Voice control for the Etsy print-on-demand shop that the background worker runs (worker/shop.py).

The worker designs one product a day, puts it on a T-shirt, a mug and a sticker in Printify, and
publishes them to the Etsy shop connected there. Here: how it's doing, make one now, pause, resume,
and how many designs a day.
"""
from __future__ import annotations

from datetime import datetime

from memory.config_manager import get_plugin_config, save_plugin_config
from worker import shop as ws

PLUGIN = {
    "name": "etsy_shop",
    "description": (
        "The user's Etsy and eBay print-on-demand shop, which Jarvis runs by itself: every day it invents a design "
        "(AI illustration and slogan), puts it on a T-shirt, a mug and a sticker through Printify, and lists "
        "them on Etsy and eBay; Printify prints and ships orders. Use for 'how is my Etsy shop doing', 'any Etsy "
        "orders', 'make a new product now' (make_now; 'make 10 more t-shirts' = make_now, value=10, "
        "products=['tee']), 'pause the shop' (pause), 'resume the shop' (resume), "
        "'make 2 designs a day' (per_day, value=number), 'what did you list lately' (recent), "
        "'connect Jarvis to Etsy' for the extra listing photos (connect_etsy), and when the user pastes the "
        "localhost address Etsy sent them to (finish_etsy, url=that address). "
        "Actions: status, make_now, pause, resume, per_day, recent, connect_etsy, finish_etsy."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["status", "make_now", "pause", "resume", "per_day", "recent", "connect_etsy", "finish_etsy"]},
            "url": {"type": "STRING", "description": "For finish_etsy: the whole https://localhost:3003/callback?... address"},
            "value": {"type": "INTEGER", "description": "For per_day: designs a day (0-5); for make_now: how many new designs (1-20)"},
            "products": {"type": "ARRAY", "items": {"type": "STRING", "enum": ["tee", "mug", "sticker"]},
                         "description": "For make_now: only these products (e.g. ['tee'] for t-shirts only); empty = all three"},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": ws.NS,
    "title": "Etsy shop (print-on-demand through Printify)",
    "fields": [
        {"key": "designs_per_day", "type": "text", "label": "Designs a day (each = shirt, mug and sticker listings)", "placeholder": "1"},
        {"key": "time", "type": "text", "label": "Make the day's design at (HH:MM)", "placeholder": "10:00"},
    ],
}

SETUP = ("Connect your Etsy shop in Printify first: in Printify open My stores, Add new store, Etsy, and "
         "approve it. Until then designs wait in the queue and nothing is listed.")


def _status() -> str:
    cfg = ws.settings()
    st = ws.load_state()
    designs = st["designs"]
    live = {ch: sum(1 for d in designs for p in ws.products_in(dict(d), ch).values() if p.get("published"))
            for ch in ws.CHANNELS}
    listed = sum(live.values())
    waiting = sum(1 for d in designs if not d.get("failed") and not any(p.get("published") for p in d.get("products", {}).values()))
    tokens = get_plugin_config("printify").get("token") and get_plugin_config("cloudflare").get("api_token")
    parts = []
    if not tokens:
        parts.append("The shop needs the Printify and Cloudflare keys in Plugin Settings.")
    parts.append(f"The shop is {'running' if cfg['enabled'] else 'paused'}: {cfg['designs_per_day']} design"
                 f"{'s' if cfg['designs_per_day'] != 1 else ''} a day at {cfg['time']}, each as a shirt, mug and sticker.")
    where = " and ".join(f"{n} on {ws.CHANNELS[ch]['name']}" for ch, n in live.items() if n) or "none live yet"
    parts.append(f"{len(designs)} design{'s' if len(designs) != 1 else ''} made, {listed} listings: {where}" +
                 (f", {waiting} waiting to be listed." if waiting else "."))
    orders = len(st.get("orders_seen") or []) if st.get("orders_started") else 0
    parts.append(f"{orders} order{'s' if orders != 1 else ''} so far." if orders else "No orders yet.")
    try:
        if not ws.Shop(log=lambda m: None).etsy_shop():
            parts.append(SETUP)
    except Exception as exc:
        parts.append(f"I couldn't reach Printify ({str(exc)[:80]}).")
    return " ".join(parts)


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "status").strip().lower()
    try:
        if action == "status":
            return _status()
        if action in ("pause", "resume"):
            save_plugin_config(ws.NS, {"enabled": action == "resume"})
            return ("Paused the shop. Live listings stay up and orders still get made; no new designs until you resume."
                    if action == "pause" else "The shop is running again.")
        if action == "per_day":
            try:
                n = max(0, min(5, int(args.get("value"))))
            except (TypeError, ValueError):
                return "How many designs a day? Up to 5; each one becomes three listings at 20 cents each."
            save_plugin_config(ws.NS, {"designs_per_day": n})
            return (f"Okay, {n} design{'s' if n != 1 else ''} a day: {n * 3} listings, about "
                    f"${n * 3 * 0.20 * 30:.0f} a month in Etsy listing fees.")
        if action == "make_now":
            try:
                n = max(1, min(20, int(args.get("value") or 1)))
            except (TypeError, ValueError):
                n = 1
            only = [k for k in (args.get("products") or []) if k in ws.PRODUCTS]
            st = ws.load_state()
            st["make_now"] = int(st.get("make_now") or 0) + n
            if only:
                st["make_only"] = only
            ws.save_state(st)
            what = " and ".join(ws.PRODUCTS[k]["name"].lower() + "s" for k in only) or "products"
            return (f"On it: {n} new design{'s' if n != 1 else ''} as {what}, one about every five minutes. "
                    "I'll text you as each one is listed.")
        if action == "recent":
            designs = ws.load_state()["designs"][-5:][::-1]
            if not designs:
                return "Nothing designed yet."
            lines = []
            for d in designs:
                live = [ws.PRODUCTS[k]["name"] for k, p in d.get("products", {}).items() if p.get("published") and k in ws.PRODUCTS]
                when = datetime.fromtimestamp(d["made"]).strftime("%b %-d")
                lines.append(f"{when}: {d['title'][:70]} ('{d.get('slogan', '')}')" +
                             (f", live as {', '.join(live)}" if live else ", not listed yet"))
            if player:
                try:
                    player.show_content("ETSY SHOP · RECENT DESIGNS", "\n".join(lines))
                except Exception:
                    pass
            return "Latest designs: " + "; ".join(lines)
        if action == "connect_etsy":
            from worker import etsy_api
            ok, why = etsy_api.ping()
            if not ok:
                return ("Etsy hasn't activated your app yet (new apps wait for Etsy's approval; check "
                        "etsy.com/developers/your-apps). I'll be ready the moment it's active. Etsy says: " + why)
            link = etsy_api.connect_url()
            if player:
                try:
                    player.show_content("CONNECT JARVIS TO ETSY", "Open this link, approve, then copy the whole address "
                                        "of the page it sends you to (it won't load; that's fine) and paste it to me:\n\n" + link)
                except Exception:
                    pass
            return ("Open the link I put on screen and approve. Etsy will send you to a page that doesn't load; "
                    "copy its whole address and give it to me.\n" + link)
        if action == "finish_etsy":
            from worker import etsy_api
            shop_id = etsy_api.finish(str(args.get("url") or ""))
            return (f"Connected to your Etsy shop ({shop_id}). I'll add the extra photos to every listing "
                    "within a few minutes, and to each new one as it goes up.")
        return "Etsy shop actions: status, make_now, pause, resume, per_day, recent, connect_etsy, finish_etsy."
    except Exception as exc:
        return f"The Etsy shop {action} failed ({type(exc).__name__}: {str(exc)[:120]})."
