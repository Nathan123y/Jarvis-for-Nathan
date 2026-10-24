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
        "orders', 'make a new product now' (make_now), 'pause the shop' (pause), 'resume the shop' (resume), "
        "'make 2 designs a day' (per_day, value=number), 'what did you list lately' (recent). "
        "Actions: status, make_now, pause, resume, per_day, recent."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["status", "make_now", "pause", "resume", "per_day", "recent"]},
            "value": {"type": "INTEGER", "description": "For per_day: designs a day (0-5); each becomes 3 listings"},
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
    parts.append(f"{len(designs)} designs made, {listed} listings: {where}" +
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
            st = ws.load_state()
            st["make_now"] = True
            ws.save_state(st)
            return "On it. The worker will design a new product within five minutes and list it if Etsy is connected."
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
        return "Etsy shop actions: status, make_now, pause, resume, per_day, recent."
    except Exception as exc:
        return f"The Etsy shop {action} failed ({type(exc).__name__}: {str(exc)[:120]})."
