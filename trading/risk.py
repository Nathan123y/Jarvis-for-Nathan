"""Order planning and the limits that no strategy, bug or misunderstanding can override.

The strategy says what it would *like* to hold. This module turns that into
concrete orders, then vets every order against hard limits before anything is
sent. A rejected order is simply not sent, and the reason is recorded as a short
label. Limits are plain numbers in `Limits` so they are easy to read and change.

What the rules guarantee, with defaults:
    * only the six funds in the allowlist can ever be traded
    * never short: a sell can never be larger than what is held
    * never margin: a buy can never be larger than cash on hand minus a buffer
    * no single fund above 40% of the account, no single order above 40%
    * at most 12 orders per day
    * after a 3% fall in a day, no new buying until tomorrow (sells still allowed)
    * a fund bought today is not sold today (no accidental day trades)
    * a blocked account trades nothing
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from trading.strategy import UNIVERSE


@dataclass(frozen=True)
class Limits:
    allowed: frozenset = frozenset(UNIVERSE)
    max_position_pct: float = 0.40
    max_order_pct: float = 0.40
    max_order_usd: Optional[float] = None
    max_orders_per_day: int = 12
    daily_loss_halt_pct: float = 0.03
    cash_buffer_pct: float = 0.02
    min_trade_pct: float = 0.02          # ignore drift smaller than 2% of the account
    min_trade_usd: float = 25.0


@dataclass
class Order:
    symbol: str
    side: str                            # "buy" or "sell"
    qty: Optional[str] = None            # whole-position sells use the exact share count text
    notional: Optional[float] = None     # dollar amount for buys and partial trims
    why: str = "rebalance"

    @property
    def dollars(self) -> float:
        return float(self.notional or 0.0)


@dataclass
class Context:
    equity: float
    last_equity: float
    cash: float
    positions: dict                      # symbol -> {"qty": float, "qty_text": str, "market_value": float}
    orders_today: int = 0
    bought_today: frozenset = field(default_factory=frozenset)
    blocked: bool = False


def _cents(amount: float) -> float:
    return math.floor(amount * 100.0 + 1e-6) / 100.0


def plan_orders(targets: dict[str, float], ctx: Context, limits: Limits = Limits()
                ) -> tuple[list[Order], list[Order]]:
    """Return (sells, buys). Sells are listed first because their cash funds the buys.

    Targets are sized against the account *minus the cash buffer*, so a plan that
    says "three equal thirds" always fits inside what the no-margin rule allows.
    Dollar amounts are rounded down to the cent for the same reason.
    """
    sells: list[Order] = []
    buys: list[Order] = []
    floor = max(limits.min_trade_usd, limits.min_trade_pct * ctx.equity)
    investable = ctx.equity * (1.0 - limits.cash_buffer_pct)
    for symbol in sorted(set(targets) | set(ctx.positions)):
        held = ctx.positions.get(symbol, {"qty": 0.0, "qty_text": "0", "market_value": 0.0})
        have = float(held.get("market_value", 0.0))
        want = float(targets.get(symbol, 0.0)) * investable
        if want <= 0 and have > 0:
            sells.append(Order(symbol, "sell", qty=str(held.get("qty_text") or held.get("qty")),
                               why="exit"))
        elif want - have >= floor:
            buys.append(Order(symbol, "buy", notional=_cents(want - have), why="enter" if have <= 0 else "add"))
        elif have - want >= floor:
            sells.append(Order(symbol, "sell", notional=_cents(have - want), why="trim"))
    return sells, buys


def vet(orders: list[Order], ctx: Context, limits: Limits = Limits()
        ) -> tuple[list[Order], list[tuple[Order, str]]]:
    """Split orders into (allowed, [(rejected, reason_label)])."""
    allowed: list[Order] = []
    rejected: list[tuple[Order, str]] = []
    count = ctx.orders_today
    spendable = max(ctx.cash * (1.0 - limits.cash_buffer_pct), 0.0)
    halted = ctx.last_equity > 0 and ctx.equity < ctx.last_equity * (1.0 - limits.daily_loss_halt_pct)
    for order in orders:
        held = ctx.positions.get(order.symbol, {"market_value": 0.0})
        have = float(held.get("market_value", 0.0))
        reason = None
        if ctx.blocked:
            reason = "account_blocked"
        elif order.symbol not in limits.allowed:
            reason = "not_allowed"
        elif count >= limits.max_orders_per_day:
            reason = "too_many_orders_today"
        elif order.side == "sell":
            if order.symbol not in ctx.positions or have <= 0:
                reason = "would_short"
            elif order.symbol in ctx.bought_today:
                reason = "bought_today"
            elif order.notional is not None and order.notional > have * 1.001:
                reason = "would_short"
        else:
            amount = order.dollars
            if halted:
                reason = "daily_loss_halt"
            elif amount <= 0:
                reason = "empty_order"
            elif amount > limits.max_order_pct * ctx.equity:
                reason = "order_too_large"
            elif limits.max_order_usd is not None and amount > limits.max_order_usd:
                reason = "order_over_dollar_cap"
            elif have + amount > limits.max_position_pct * ctx.equity + 0.01:
                reason = "position_too_large"
            elif amount > spendable + 0.005:
                reason = "not_enough_cash"
        if reason:
            rejected.append((order, reason))
            continue
        if order.side == "buy":
            spendable -= order.dollars
        count += 1
        allowed.append(order)
    return allowed, rejected
