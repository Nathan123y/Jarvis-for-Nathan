"""Money tracker: log spending and income by voice, set budgets, hear where the
week's money went.

"I spent $20 on food", "got paid $300", "set a $100 weekly food budget",
"how much have I spent this week?". Logging something warns when a category is
near or over its budget. The week summary also mentions the paper day trader's
simulated result. Saved locally in memory/money.json (git-ignored).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PLUGIN = {
    "name": "money_tracker",
    "description": (
        "Track the user's personal spending and income, and budgets. Use for 'I spent $20 on "
        "food', 'I paid 45 for gas', 'I got paid $300', 'I made $50 selling shoes', 'set a $100 "
        "weekly food budget', 'how much have I spent this week / this month / on food', 'show "
        "my spending', 'undo that expense'. Actions: add (amount, category, note, kind=expense|"
        "income), budget (category, amount, period=week|month), summary (period=today|week|"
        "month, optional category), recent, undo. Use short lowercase categories like food, "
        "gas, shopping, school, fun, bills, subscriptions, gym, other. Only log amounts the "
        "user actually said. This is NOT the day_trading tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["add", "budget", "summary", "recent", "undo"]},
            "amount": {"type": "NUMBER", "description": "Dollars"},
            "category": {"type": "STRING"},
            "note": {"type": "STRING", "description": "What it was, e.g. 'Chipotle'"},
            "kind": {"type": "STRING", "enum": ["expense", "income"]},
            "period": {"type": "STRING", "enum": ["today", "week", "month"]},
        },
        "required": ["action"],
    },
}

DATA_FILE = Path(__file__).resolve().parents[1] / "memory" / "money.json"
_lock = threading.Lock()
_ALIASES = {"groceries": "food", "grocery": "food", "eating out": "food", "restaurant": "food",
            "lunch": "food", "dinner": "food", "breakfast": "food", "coffee": "food",
            "fuel": "gas", "gasoline": "gas", "clothes": "shopping", "amazon": "shopping",
            "subscription": "subscriptions", "rent": "bills", "phone": "bills",
            "books": "school", "tuition": "school", "entertainment": "fun", "movies": "fun",
            "games": "fun"}


def _load() -> dict:
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("entries", [])
            data.setdefault("budgets", {})
            return data
    except Exception:
        pass
    return {"entries": [], "budgets": {}}


def _save(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(DATA_FILE)


def _category(value) -> str:
    c = " ".join(str(value or "other").lower().split())[:30] or "other"
    return _ALIASES.get(c, c)


def _money(v: float) -> str:
    return f"${v:,.2f}".replace(".00", "")


def _start(period: str, today: date | None = None) -> date:
    today = today or date.today()
    if period == "today":
        return today
    if period == "month":
        return today.replace(day=1)
    return today - timedelta(days=today.weekday())            # week starts Monday


def _spent(entries, category: str | None, since: date) -> float:
    s = since.isoformat()
    return sum(e["amount"] for e in entries if e["kind"] == "expense" and e["day"] >= s
               and (category is None or e["category"] == category))


def _budget_note(data: dict, category: str, today: date | None = None) -> str:
    b = data["budgets"].get(category)
    if not b:
        return ""
    spent = _spent(data["entries"], category, _start(b["period"], today))
    limit = b["amount"]
    if spent > limit:
        return f" You're over your {b['period']}ly {category} budget: {_money(spent)} of {_money(limit)}."
    if spent >= 0.8 * limit:
        return f" Heads up: {_money(spent)} of your {_money(limit)} {b['period']}ly {category} budget is used."
    return f" {_money(limit - spent)} left in your {category} budget this {b['period']}."


def _paper_week() -> str:
    """The paper day trader's simulated result this week, if it traded."""
    try:
        from trading.day.store import day_journal
        monday = _start("week").isoformat()
        pnl = [e["pnl"] for e in day_journal().events()
               if e.get("kind") == "trade_result" and e.get("day", "") >= monday]
    except Exception:
        return ""
    if not pnl:
        return ""
    total = sum(pnl)
    return f" The paper day trader (simulated money) is {'+' if total >= 0 else '-'}{_money(abs(total))} this week."


def _summary(data: dict, period: str, category: str | None) -> tuple[str, str]:
    since = _start(period)
    s = since.isoformat()
    rows = [e for e in data["entries"] if e["day"] >= s]
    spent = sum(e["amount"] for e in rows if e["kind"] == "expense" and (not category or e["category"] == category))
    earned = sum(e["amount"] for e in rows if e["kind"] == "income")
    label = {"today": "today", "week": "this week", "month": "this month"}[period]
    if category:
        return f"You've spent {_money(spent)} on {category} {label}." + _budget_note(data, category), ""
    by_cat: dict[str, float] = {}
    for e in rows:
        if e["kind"] == "expense":
            by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
    top = sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)
    spoken = f"You've spent {_money(spent)} {label}"
    if top:
        spoken += ", mostly " + ", ".join(f"{c} {_money(v)}" for c, v in top[:3])
    spoken += "." + (f" You brought in {_money(earned)}." if earned else "")
    over = [c for c, b in data["budgets"].items() if _spent(data["entries"], c, _start(b["period"])) > b["amount"]]
    if over:
        spoken += " Over budget: " + ", ".join(over) + "."
    if period == "week":
        spoken += _paper_week()
    panel = [f"MONEY · {label.upper()}", ""] + [f"  {c:<14} {_money(v):>10}" for c, v in top]
    panel += ["", f"  {'spent':<14} {_money(spent):>10}", f"  {'income':<14} {_money(earned):>10}"]
    if data["budgets"]:
        panel += ["", "BUDGETS"] + [
            f"  {c:<14} {_money(_spent(data['entries'], c, _start(b['period'])))} / {_money(b['amount'])} per {b['period']}"
            for c, b in data["budgets"].items()]
    return spoken, "\n".join(panel)


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "summary").strip().lower()

    def show(title, text):
        if player and text:
            try:
                player.show_content(title, text)
            except Exception:
                pass

    try:
        with _lock:
            data = _load()
            if action == "add":
                try:
                    amount = round(abs(float(args.get("amount"))), 2)
                except (TypeError, ValueError):
                    return "How much was it?"
                if not amount:
                    return "How much was it?"
                kind = "income" if str(args.get("kind") or "").lower() == "income" else "expense"
                cat = _category(args.get("category") or ("income" if kind == "income" else "other"))
                data["entries"].append({"id": int(time.time() * 1000), "day": date.today().isoformat(),
                                        "at": datetime.now().isoformat(timespec="minutes"),
                                        "amount": amount, "kind": kind, "category": cat,
                                        "note": " ".join(str(args.get("note") or "").split())[:80]})
                _save(data)
                if kind == "income":
                    return f"Logged {_money(amount)} income."
                return f"Logged {_money(amount)} on {cat}." + _budget_note(data, cat)
            if action == "budget":
                cat = _category(args.get("category"))
                period = "month" if str(args.get("period") or "").lower() == "month" else "week"
                try:
                    amount = round(abs(float(args.get("amount"))), 2)
                except (TypeError, ValueError):
                    return "How much should the budget be?"
                if not amount:
                    data["budgets"].pop(cat, None)
                    _save(data)
                    return f"Removed the {cat} budget."
                data["budgets"][cat] = {"amount": amount, "period": period}
                _save(data)
                return f"Set a {_money(amount)} {period}ly {cat} budget." + _budget_note(data, cat)
            if action == "undo":
                if not data["entries"]:
                    return "There's nothing to undo."
                e = data["entries"].pop()
                _save(data)
                return f"Removed {_money(e['amount'])} {e['category']} from {e['day']}."
            if action == "recent":
                rows = data["entries"][-10:][::-1]
                if not rows:
                    return "Nothing logged yet."
                lines = [f"{e['day']}  {'+' if e['kind'] == 'income' else '-'}{_money(e['amount'])}  "
                         f"{e['category']}{'  · ' + e['note'] if e['note'] else ''}" for e in rows]
                show("RECENT MONEY", "\n".join(lines))
                return "Latest entries, newest first:\n" + "\n".join(lines)
            period = str(args.get("period") or "week").lower()
            period = period if period in ("today", "week", "month") else "week"
            cat = _category(args["category"]) if args.get("category") else None
            spoken, panel = _summary(data, period, cat)
            show("MONEY", panel)
            return spoken
    except Exception as exc:
        return f"The money tracker had a problem ({type(exc).__name__})."
