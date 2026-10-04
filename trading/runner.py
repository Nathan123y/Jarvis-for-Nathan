"""The once-a-day decision cycle and the loop that drives it.

Timing: the strategy only looks at *finished* daily candles, so it decides once
per trading day, some time after the first half hour of the session and before
the last twenty minutes. If the Mac was asleep at the start of that window, the
first poll after it wakes still gets a chance, so a late wake-up costs nothing.
Orders are ordinary market orders for the day, which fill straight away on the
paper account while the market is open.

Safety: every order passes risk.vet() first; sells are sent and allowed to fill
before buys are planned from fresh cash figures, so the account never leans on
margin; each order carries a deterministic client id (date + symbol + side), so
a crash and restart cannot send the same order twice.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from trading.broker import BrokerError
from trading.journal import Journal
from trading.risk import Context, Limits, Order, plan_orders, vet
from trading.strategy import (BENCHMARK, StrategyConfig, due_for_rebalance, explain,
                              target_weights)

WINDOW_MIN_LEFT = 20      # minutes before the close: too late to start a decision
WINDOW_MAX_LEFT = 360     # minutes before the close: the first half hour has passed
MAX_ATTEMPTS = 3
SNAPSHOT_EVERY = 1800.0

_FRACTION = re.compile(r"(\.\d{1,6})\d*")


def parse_ts(text: str) -> datetime:
    """RFC 3339 with any number of fractional digits, as a timezone-aware datetime."""
    cleaned = _FRACTION.sub(r"\1", str(text).strip().replace("Z", "+00:00"))
    stamp = datetime.fromisoformat(cleaned)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


@dataclass
class Plan:
    day: str
    due: bool
    rows: list
    targets: dict
    ctx: Context
    sells: list = field(default_factory=list)
    buys: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


class Runner:
    def __init__(self, broker, journal: Journal, *, limits: Limits = Limits(),
                 cfg: StrategyConfig = StrategyConfig(), rebalance: str = "weekly",
                 log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                 fill_wait: float = 30.0):
        self.broker, self.journal = broker, journal
        self.limits, self.cfg, self.rebalance = limits, cfg, rebalance
        self._log, self._sleep, self._fill_wait = log, sleep, fill_wait
        self._last_label = ""
        self._last_snapshot = -SNAPSHOT_EVERY
        self.hint = 300.0

    # ── planning (shared by `plan`, the voice tool and the real run) ──────────
    def _closes(self, day: str) -> dict[str, list[float]]:
        start = (date.fromisoformat(day) - timedelta(days=430)).isoformat()
        raw = self.broker.daily_bars(list(self.cfg.universe), start)
        return {s: [close for stamp, close in raw.get(s, []) if stamp < day] for s in self.cfg.universe}

    def _context(self, day: str, account: dict, positions: dict) -> Context:
        count, bought = self.journal.orders_today(day)
        return Context(equity=account["equity"], last_equity=account["last_equity"],
                       cash=account["cash"], positions=positions, orders_today=count,
                       bought_today=bought, blocked=account["blocked"])

    def make_plan(self, day: str, account: dict, positions: dict) -> Optional[Plan]:
        closes = self._closes(day)
        if all(len(series) < self.cfg.min_history for series in closes.values()):
            return None
        targets = target_weights(closes, self.cfg)
        ctx = self._context(day, account, positions)
        due = due_for_rebalance(self.journal.state().get("last_rebalance_date"),
                                date.fromisoformat(day), self.rebalance)
        plan = Plan(day, due, explain(closes, self.cfg), targets, ctx)
        if due:
            sells, buys = plan_orders(targets, ctx, self.limits)
            plan.sells, rejected = vet(sells, ctx, self.limits)
            plan.buys, rejected_buys = vet(buys, ctx, self.limits)
            plan.rejected = rejected + rejected_buys
        return plan

    def plan_now(self) -> tuple[Optional[Plan], dict]:
        """What would happen if the daily decision ran right now. Sends nothing."""
        clock = self.broker.clock()
        day = parse_ts(clock["timestamp"]).date().isoformat()
        account, positions = self.broker.account(), self.broker.positions()
        return self.make_plan(day, account, positions), clock

    # ── doing ─────────────────────────────────────────────────────────────────
    def _send(self, orders: list[Order], day: str) -> int:
        errors = 0
        for order in orders:
            client_id = f"jv-{day.replace('-', '')}-{order.symbol}-{order.side}"
            try:
                result = self.broker.submit_market_order(
                    order.symbol, order.side, qty=order.qty, notional=order.notional,
                    client_order_id=client_id)
            except BrokerError as exc:
                errors += 1
                self.journal.record("order_error", symbol=order.symbol, side=order.side, msg=str(exc)[:140])
                self._log(f"order for {order.symbol} was not accepted")
                continue
            self.journal.note_order(day, order.symbol, order.side)
            self.journal.record("order", symbol=order.symbol, side=order.side, why=order.why,
                                dollars=round(order.dollars, 2) if order.notional else None,
                                whole_position=order.qty is not None, status=result["status"])
            self._log(f"{order.side} {order.symbol} sent ({order.why})")
        return errors

    def _wait_for_fills(self) -> None:
        waited = 0.0
        while waited < self._fill_wait:
            try:
                if not self.broker.open_orders():
                    return
            except BrokerError:
                return
            self._sleep(2.0)
            waited += 2.0

    def _execute(self, plan: Plan, day: str) -> int:
        for order, reason in plan.rejected:
            self.journal.record("rejected", symbol=order.symbol, side=order.side, reason=reason)
        errors = self._send(plan.sells, day)
        buys = plan.buys
        if plan.sells:
            self._wait_for_fills()
            account, positions = self.broker.account(), self.broker.positions()
            ctx = self._context(day, account, positions)
            _, fresh_buys = plan_orders(plan.targets, ctx, self.limits)
            buys, rejected = vet(fresh_buys, ctx, self.limits)
            for order, reason in rejected:
                self.journal.record("rejected", symbol=order.symbol, side=order.side, reason=reason)
        return errors + self._send(buys, day)

    def _observe(self, clock: dict, account: dict, positions: dict, spy: Optional[float], day: str) -> None:
        holdings = [{"symbol": s, "value": round(p["market_value"], 2)} for s, p in positions.items()]
        self.journal.update_state(last_seen={
            "t": clock["timestamp"], "equity": account["equity"],
            "last_equity": account["last_equity"], "spy": spy, "holdings": holdings})
        now = time.monotonic()
        if self.journal.state().get("start_equity") and now - self._last_snapshot >= SNAPSHOT_EVERY:
            self._last_snapshot = now
            self.journal.record("snapshot", date=day, equity=account["equity"], spy=spy)

    # ── one pass ──────────────────────────────────────────────────────────────
    def step(self) -> str:
        """Do whatever is due right now and say what happened, as a short label."""
        clock = self.broker.clock()
        if not clock["is_open"]:
            try:
                opens = parse_ts(clock["next_open"]) - parse_ts(clock["timestamp"])
                self.hint = min(1800.0, max(300.0, opens.total_seconds() - 600.0))
            except (ValueError, KeyError):
                self.hint = 900.0
            return "market_closed"
        now, closes_at = parse_ts(clock["timestamp"]), parse_ts(clock["next_close"])
        day = now.date().isoformat()
        account, positions = self.broker.account(), self.broker.positions()
        try:
            spy = self.broker.latest_prices([BENCHMARK]).get(BENCHMARK)
        except BrokerError:
            spy = None
        self._observe(clock, account, positions, spy, day)
        self.hint = 300.0

        if self.journal.paused():
            return "paused"
        if account["blocked"]:
            return "account_blocked"
        left = (closes_at - now).total_seconds() / 60.0
        if not (WINDOW_MIN_LEFT <= left <= WINDOW_MAX_LEFT):
            return "outside_window"
        state = self.journal.state()
        if state.get("last_decision_date") == day:
            return "already_ran"

        self.hint = 60.0
        if not state.get("start_equity") and spy:
            self.journal.update_state(start_equity=account["equity"], start_spy=spy, started_at=day)
            self.journal.record("start", equity=account["equity"], spy=spy)
        plan = self.make_plan(day, account, positions)
        if plan is None:
            self.journal.record("error", label="not_enough_history")
            return "not_enough_history"
        self.journal.record("decision", day=day, due=plan.due,
                            targets={s: round(w, 3) for s, w in plan.targets.items()},
                            signals={r["symbol"]: r["status"] for r in plan.rows},
                            sells=len(plan.sells), buys=len(plan.buys))
        if not plan.due:
            self.journal.update_state(last_decision_date=day)
            return "not_due"

        errors = self._execute(plan, day)
        attempts = int((state.get("attempts") or {}).get(day, 0)) + 1
        self.journal.update_state(attempts={day: attempts})
        if errors == 0:
            self.journal.update_state(last_decision_date=day, last_rebalance_date=day)
            return "rebalanced"
        if attempts >= MAX_ATTEMPTS:
            self.journal.update_state(last_decision_date=day)
        return "rebalance_incomplete"

    # ── the loop ──────────────────────────────────────────────────────────────
    def run_forever(self, stop: Optional[threading.Event] = None) -> None:
        stop = stop or threading.Event()
        failures = 0
        while not stop.is_set():
            try:
                label = self.step()
                failures = 0
            except BrokerError as exc:
                failures += 1
                label = "error:broker"
                self.journal.record("error", label=label, msg=str(exc)[:140])
            except Exception as exc:                    # never let one bad pass end the run
                failures += 1
                label = f"error:{type(exc).__name__}"
                self.journal.record("error", label=label)
            if label != self._last_label:
                self._log(f"status: {label}")
                self._last_label = label
            delay = min(60.0 * (2 ** min(failures, 4)), 900.0) if failures else self.hint
            stop.wait(delay)
