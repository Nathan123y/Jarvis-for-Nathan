"""Persistent, bounded storefront experiment coordinator.

The agent chooses the next *reviewable* step; it never creates accounts, incurs
fees, publishes, scrapes contacts, or sends messages on its own.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


STATE_FILE = Path(__file__).resolve().parents[1] / "memory/store_growth_agent.json"
_LOCK = threading.RLock()

PLUGIN = {
    "name": "store_growth_agent",
    "behavior": "NON_BLOCKING",
    "description": (
        "Coordinate bounded Etsy or Gumroad product experiments and focus on the first "
        "product with owner-verified revenue. Actions: start, next, record, status, reset. "
        "This agent plans and tracks work only: it never creates accounts, accepts terms, "
        "pays fees, publishes, scrapes people, sends unsolicited messages, or claims revenue."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start, next, record, status, or reset"},
            "platform": {"type": "STRING", "description": "etsy or gumroad"},
            "products": {"type": "STRING", "description": "Comma-separated product concepts to test, maximum 10"},
            "product": {"type": "STRING", "description": "Exact product concept when recording results"},
            "views": {"type": "INTEGER", "description": "Verified platform views, non-negative"},
            "orders": {"type": "INTEGER", "description": "Verified platform orders, non-negative"},
            "revenue_cents": {"type": "INTEGER", "description": "Verified gross revenue in cents, non-negative"},
        },
        "required": ["action"],
    },
}


def _load() -> dict:
    if not STATE_FILE.exists():
        return {"platform": "", "products": [], "results": {}, "winner": "", "cursor": 0}
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise RuntimeError("Store growth state is damaged; preserve it before resetting.")
    return data


def _save(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=STATE_FILE.parent, prefix=".growth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, STATE_FILE)
    finally:
        Path(name).unlink(missing_ok=True)


def _number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative whole number.")
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a non-negative whole number.") from None
    if number < 0:
        raise ValueError(f"{label} must be a non-negative whole number.")
    return number


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "").strip().casefold()
    with _LOCK:
        if action == "reset":
            STATE_FILE.unlink(missing_ok=True)
            return "Store growth experiments reset. No storefront or listing was changed."
        if action == "start":
            platform = str(parameters.get("platform") or "").strip().casefold()
            if platform not in {"etsy", "gumroad"}:
                raise ValueError("Choose Etsy or Gumroad.")
            products = []
            for raw in str(parameters.get("products") or "").split(","):
                item = " ".join(raw.split())[:120]
                if item and item.casefold() not in {p.casefold() for p in products}:
                    products.append(item)
            if not products:
                raise ValueError("Give at least one truthful product concept to test.")
            state = {"platform": platform, "products": products[:10], "results": {},
                     "winner": "", "cursor": 0}
            _save(state)
            return f"Started {platform.title()} experiments with {len(state['products'])} product concept(s). Ask for the next step."
        state = _load()
        if not state["products"]:
            return "No growth experiment is active. Start one with a platform and product concepts."
        if action == "record":
            product = " ".join(str(parameters.get("product") or "").split())
            match = next((p for p in state["products"] if p.casefold() == product.casefold()), None)
            if not match:
                raise ValueError("Choose an exact product from the active experiment.")
            result = {"views": _number(parameters.get("views"), "views"),
                      "orders": _number(parameters.get("orders"), "orders"),
                      "revenue_cents": _number(parameters.get("revenue_cents"), "revenue_cents")}
            state["results"][match] = result
            if result["orders"] > 0 and result["revenue_cents"] > 0:
                state["winner"] = match
            _save(state)
            return (f"Recorded verified results for {match}. " +
                    (f"Winner locked: {match}. Focus on improving this listing and fulfillment."
                     if state["winner"] else "No verified revenue yet; continue the bounded experiment."))
        if action == "next":
            if state["winner"]:
                return f"Focus mode: {state['winner']} has verified revenue. Improve its listing, offer, previews, and customer experience; do not start another product yet."
            product = state["products"][state.get("cursor", 0) % len(state["products"])]
            state["cursor"] = state.get("cursor", 0) + 1
            _save(state)
            return (f"Next reviewed experiment: {product} on {state['platform'].title()}. Prepare the product and listing, review them, publish through the platform, then record verified views, orders, and revenue. No external action was taken.")
        if action == "status":
            return (f"Platform: {state['platform'].title()} | Products: {', '.join(state['products'])} | "
                    f"Winner: {state['winner'] or 'none'} | Recorded: {len(state['results'])}")
    return "Choose start, next, record, status, or reset."
