"""The live day-trading loop: look every 15 seconds, buy a fresh breakout, sell before the close.

What keeps it safe (practice money, but written as if it were not)
  * It is meant to own its Alpaca account. The account's identity is pinned on the first pass
    (a different account later means it touches nothing), `check` refuses an account that holds
    other funds, and if the account ever holds anything outside the tickers it may trade (SPY and
    QQQ, or the analyst's fixed list) the trader stops buying, sells only the shares it bought
    itself that day, and never touches the rest.
  * It sells fund by fund (cancel that fund's stop, then sell the position). It never uses a
    "sell everything" call.
  * Every buy carries a protective stop that lives at Alpaca, so a sleeping Mac or a closed
    Jarvis cannot leave a position unprotected during the day.
  * Everything still held 15 minutes before the close is sold. Anything found at the next open
    that this record did not buy that day is sold immediately (the Mac slept through the close).
  * The record of "I am buying this" is written BEFORE the order is sent, so a reply that gets
    lost on the way back can never make the trader sell its own fresh position as a leftover.
  * Pause stops new buys only. It never strands a position: stops, the end-of-day sale and
    the leftover sale all still run.
  * The account falling 1% on the day sells everything and ends buying until tomorrow.
  * A breakout is bought only within 90 seconds of happening, at most once per fund per day,
    with a deterministic order id, so a restart or a retry cannot send the same buy twice.
  * No short selling, no margin: sizes are whole shares, capped by the rule and by cash.
  * Under $25,000 it does not trade at all: every trade here is a day trade, which brokers
    restrict below that size, and a refused sale could strand a position.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from trading.broker import BrokerError, parse_ts
from trading.day.analyst import MAX_PICKS, PlanError, clamp_conviction, pick_config, plan_lines
from trading.day.rule import (EASTERN, FINAL, SESSION_CLOSE, DayConfig, is_full_session,
                              live_signal, simulate_day, summarize_fills, to_sessions)
from trading.day.universe import UNIVERSE
from trading.journal import Journal

IDLE_MIN, IDLE_MAX = 300.0, 600.0       # waits while the market is closed (the Mac may nap through them)
POLL = 15.0
MAX_ATTEMPTS = 3
FLATTEN_RETRY = 10.0
FAILURE_CAP_IN_SESSION = 30.0           # keep retrying quickly while the market is open
MAX_CHASE = 1.005                       # do not buy if the price is already 0.5% above the signal candle's close
DAY_TRADING_MINIMUM = 25_000.0
PLAN_LEAD = 90 * 60.0                   # start making the analyst's plan this long before the open
PREMARKET_ATTEMPTS = 3                  # tries before the open (the rest are kept for after it)
PLAN_ATTEMPTS = 6                       # tries per day; after that the trader sits the day out
PLAN_RETRY = 300.0                      # wait between tries
_UNSURE = ("could not reach", "could not be read", "having trouble")


def _midnight(day: str) -> str:
    return datetime.fromisoformat(day).replace(tzinfo=EASTERN).isoformat()


class DayRunner:
    def __init__(self, broker, journal: Journal, *, cfg: DayConfig = DayConfig(),
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic,
                 analyst: Optional[Callable[[str], dict]] = None):
        """`analyst`, if given, is called with a trading day ("YYYY-MM-DD") and returns that day's
        plan (see trading.day.analyst) or raises PlanError. Without it the trader watches
        `cfg.symbols` exactly as before."""
        self.broker, self.journal, self.cfg = broker, journal, cfg
        self.analyst = analyst
        self._log, self._clock = log, clock
        self._last_plan_try = -1e9
        self.hint = IDLE_MIN
        self._last_flatten = -1e9
        self._last_remembered = -1e9
        self._last_label = ""
        self._in_session = False

    # ── small helpers ─────────────────────────────────────────────────────────
    def _today(self, day: str) -> dict:
        fresh = {"day": day, "decided": {}, "entered": {}, "attempts": {}, "halted": False,
                 "finished": False, "noted": [], "plan": None, "plan_attempts": 0}
        saved = self.journal.state().get("today")
        return {**fresh, **saved} if isinstance(saved, dict) and saved.get("day") == day else fresh

    def _save(self, today: dict) -> None:
        self.journal.update_state(today=today)

    def _note_once(self, today: dict, label: str, **fields) -> None:
        """Record a problem once per day instead of every 15 seconds."""
        if label in today["noted"]:
            return
        today["noted"].append(label)
        self._save(today)
        self.journal.record("error", label=label, **fields)
        self._log(f"problem: {label}")

    def _idle_hint(self, clock: dict) -> None:
        try:
            opens = parse_ts(clock["next_open"]) - parse_ts(clock["timestamp"])
            self.hint = min(IDLE_MAX, max(IDLE_MIN, opens.total_seconds() - 600.0))
        except (ValueError, KeyError):
            self.hint = IDLE_MAX

    def _remember(self, clock: dict, account: dict, positions: dict) -> None:
        if self._clock() - self._last_remembered < 60.0:
            return
        self._last_remembered = self._clock()
        self.journal.update_state(last_seen={
            "t": clock["timestamp"], "equity": account["equity"], "last_equity": account["last_equity"],
            "holdings": [{"symbol": s, "value": round(p["market_value"], 2)} for s, p in positions.items()]})

    def _start_line(self, account: dict, day: str) -> None:
        if self.journal.state().get("start_equity"):
            return
        try:
            spy = self.broker.latest_prices(["SPY"]).get("SPY")
        except BrokerError:
            return
        if spy:
            self.journal.update_state(start_equity=account["equity"], start_spy=spy, started_at=day)
            self.journal.record("start", equity=account["equity"], spy=spy)

    def _decide(self, today: dict, day: str, symbol: str, status: str) -> None:
        today["decided"][symbol] = status
        self.journal.record("decision", day=day, symbol=symbol, status=status)
        self._log(f"{symbol}: {status}")

    def _allowed(self) -> tuple:
        """Every ticker this trader may hold: SPY and QQQ, or the analyst's whole fixed list."""
        return tuple(dict.fromkeys(self.cfg.symbols + (UNIVERSE if self.analyst else ())))

    @staticmethod
    def _sellable(positions: dict, today: dict, allowed) -> list[str]:
        """Which holdings this trader may sell. In a dedicated account, all of them (they can only
        be tickers it may trade). If the account also holds anything else, only what it bought
        itself today (which can include a stock bought under the analyst by a trader that was then
        restarted without it, so that is never left to be held overnight)."""
        held = set(positions)
        if held - set(allowed):
            return sorted(held & set(today["entered"]))
        return sorted(held)

    def _cfg_for(self, symbol: str, today: dict) -> DayConfig:
        """The rule's settings for one ticker. With the analyst on, sizes follow its conviction in
        that pick (the lowest if, impossibly, it is not in the plan)."""
        if not self.analyst:
            return self.cfg
        for pick in (self._saved_plan(today) or {}).get("picks") or []:
            if pick.get("symbol") == symbol:
                return pick_config(self.cfg, symbol, pick.get("conviction"))
        return pick_config(self.cfg, symbol, 1)

    # ── the analyst's plan ────────────────────────────────────────────────────
    def _accept_plan(self, plan: dict, day: str) -> dict:
        """A plan cut down to what this trader will act on: tickers it may trade, once each, at most
        MAX_PICKS, convictions forced into 1..5, and a stand-aside whenever nothing is left. Applied
        to what the analyst hands back AND again to a plan read back from the saved record, so
        neither a bad answer nor an edited file can widen what gets traded."""
        allowed, seen, picks = set(self._allowed()), set(), []
        raw_picks = plan.get("picks")
        for pick in raw_picks if isinstance(raw_picks, list) else []:
            symbol = pick.get("symbol") if isinstance(pick, dict) else None
            if isinstance(symbol, str) and symbol in allowed and symbol not in seen and len(picks) < MAX_PICKS:
                seen.add(symbol)
                picks.append({**pick, "symbol": symbol, "conviction": clamp_conviction(pick.get("conviction"))})
        stand = plan.get("stand_aside") is True or not picks
        return {**plan, "day": day, "picks": [] if stand else picks, "stand_aside": stand}

    def _saved_plan(self, today: dict) -> Optional[dict]:
        """The plan in today's record, cleaned as above, or None if there is none or it is unusable."""
        plan = today.get("plan")
        if not isinstance(plan, dict):
            return None
        try:
            return self._accept_plan(plan, today.get("day"))
        except Exception:                                   # noqa: BLE001 - a damaged record means "no plan"
            return None

    @staticmethod
    def _tries(today: dict) -> int:
        try:
            return max(0, int(today.get("plan_attempts") or 0))
        except (TypeError, ValueError, OverflowError):
            return PLAN_ATTEMPTS

    def _ensure_plan(self, day: str, today: dict, limit: int = PLAN_ATTEMPTS) -> Optional[dict]:
        """Today's plan, making it if there is none yet. None when there is no plan right now
        (the tries up to `limit` are used, or the last one failed a moment ago)."""
        plan = self._saved_plan(today)
        if plan is not None:
            return plan
        tries = self._tries(today)
        if tries >= limit or self._clock() - self._last_plan_try < PLAN_RETRY:
            return None
        self._last_plan_try = self._clock()
        today["plan_attempts"] = tries + 1
        self._save(today)                                   # count the try first: a crash cannot loop forever
        try:
            plan = self._accept_plan(self.analyst(day), day)
        except PlanError as exc:
            reason = str(exc)[:140]
        except Exception as exc:                            # noqa: BLE001 - a broken analyst must not stop the trader
            reason = f"unexpected {type(exc).__name__}"
        else:
            today["plan"] = plan
            self._save(today)
            self.journal.record("plan", **{k: v for k, v in plan.items()
                                           if k in ("day", "stand_aside", "market_view", "events", "picks",
                                                    "dropped", "headlines", "made_at")})
            for line in plan_lines(plan, self.cfg):
                self._log(line)
            return plan
        self.journal.record("error", label="plan_failed", day=day, attempt=tries + 1, reason=reason)
        self._log(f"problem: no plan for {day} ({reason}); try {tries + 1} of {PLAN_ATTEMPTS}"
                  + (". It will not trade today without one." if tries + 1 >= PLAN_ATTEMPTS else
                     ("; it tries again after the open." if tries + 1 >= limit else "")))
        return None

    def _plan_for(self, day: str, seconds: float, today: dict) -> tuple[Optional[dict], bool]:
        """(the plan to trade on, whether it was only just made) while the market is open. A plan can
        still be made late in the morning, but not once the last possible buy time has passed."""
        plan = self._saved_plan(today)
        if plan is not None:
            return plan, False
        if seconds >= self.cfg.last_entry_minute * 60:
            return None, False
        plan = self._ensure_plan(day, today)
        return plan, plan is not None

    def _before_open(self, clock: dict) -> None:
        """Pre-market: make the plan for the coming session once the open is close enough."""
        if self.journal.paused():
            return
        try:
            opens = parse_ts(clock["next_open"])
            wait = (opens - parse_ts(clock["timestamp"])).total_seconds()
        except (ValueError, KeyError):
            return
        if wait <= PLAN_LEAD:
            day = opens.astimezone(EASTERN).date().isoformat()
            self._ensure_plan(day, self._today(day), PREMARKET_ATTEMPTS)

    # ── selling ───────────────────────────────────────────────────────────────
    def _flatten(self, day: str, why: str, symbols: list[str]) -> bool:
        """Sell these funds at market, one by one: cancel the fund's stop, then sell the position.
        True only if every sale was accepted. A refusal is retried soon (a just-cancelled stop can
        take a moment to release its shares)."""
        if not symbols or self._clock() - self._last_flatten < FLATTEN_RETRY:
            return False
        self._last_flatten = self._clock()
        try:
            orders = self.broker.open_orders()
        except BrokerError:
            orders = []
        accepted = True
        for symbol in symbols:
            for order in orders:
                if order.get("symbol") == symbol:
                    try:
                        self.broker.cancel_order(order["id"])
                    except BrokerError:
                        pass                                # already filled or already cancelled
            try:
                self.broker.close_position(symbol)
            except BrokerError as exc:
                accepted = False
                self.journal.record("error", label="sell_failed", symbol=symbol, msg=str(exc)[:140])
        if accepted:
            self.journal.record("flatten", day=day, why=why, symbols=symbols)
            self._log(f"selling {', '.join(symbols)} ({why})")
        else:
            self._log("a sale did not go through; will retry shortly")
        return accepted

    # ── buying ────────────────────────────────────────────────────────────────
    def _enter(self, symbol: str, signal: dict, account: dict, day: str, today: dict) -> bool:
        try:
            price = self.broker.latest_prices([symbol]).get(symbol)
        except BrokerError:
            return False                                    # try again on the next pass
        if not price or price - signal["stop"] < 0.01:
            self._decide(today, day, symbol, "price_through_stop")
            return False
        if price > signal["signal_close"] * MAX_CHASE:
            self._decide(today, day, symbol, "price_ran_away")
            return False
        qty = min(signal["qty"], math.floor(account["cash"] / (price * 1.002)))
        if qty < 1:
            self._decide(today, day, symbol, "not_enough_cash")
            return False
        # Write down the intent first. If the order goes through but its reply is lost, the
        # shares are then recognised as ours, not swept away as a leftover.
        today["entered"][symbol] = {"qty": qty, "stop": signal["stop"], "price": round(price, 2),
                                    "intent": True}
        self._save(today)
        client_id = f"jvd-{day.replace('-', '')}-{symbol}-in"
        try:
            self.broker.submit_entry_with_stop(symbol, qty, signal["stop"], client_order_id=client_id)
        except BrokerError as exc:
            message = str(exc).lower()
            attempts = int(today["attempts"].get(symbol, 0)) + 1
            today["attempts"][symbol] = attempts
            self.journal.record("order_error", symbol=symbol, side="buy", msg=str(exc)[:140])
            if "unique" in message:                          # the first try did go through
                self._mark_entered(today, day, symbol, qty, price, signal)
                return True
            if not any(text in message for text in _UNSURE):  # a clean refusal: nothing was bought
                today["entered"].pop(symbol, None)
            self._log(f"buy of {symbol} was not accepted ({attempts} of {MAX_ATTEMPTS})")
            if attempts >= MAX_ATTEMPTS:
                self._decide(today, day, symbol, "order_failed")
            self._save(today)
            return False
        self._mark_entered(today, day, symbol, qty, price, signal)
        return True

    def _mark_entered(self, today: dict, day: str, symbol: str, qty: int, price: float, signal: dict) -> None:
        today["entered"][symbol] = {"qty": qty, "stop": signal["stop"], "price": round(price, 2)}
        today["decided"][symbol] = "entered"
        self.journal.record("entry", day=day, symbol=symbol, qty=qty, price=round(price, 2),
                            stop=signal["stop"], range_high=round(signal["range"][0], 2),
                            range_low=round(signal["range"][1], 2))
        self._log(f"bought {qty} {symbol} near {price:.2f}, stop {signal['stop']:.2f}")

    def _look_for_entries(self, day: str, seconds: float, account: dict, positions: dict,
                          today: dict, symbols: list[str]) -> str:
        for symbol in [s for s in symbols if s in positions and s in today["entered"]]:
            today["decided"][symbol] = "entered"             # an order whose reply was lost did fill
            symbols = [s for s in symbols if s != symbol]
        if not symbols:
            self._save(today)
            return "watching"
        raw = self.broker.minute_bars(symbols, _midnight(day))
        bought = False
        budget = dict(account)                              # cash left in THIS pass: several buys must not borrow
        for symbol in symbols:
            bars = to_sessions(raw.get(symbol, [])).get(day, [])
            signal = live_signal(bars, self._cfg_for(symbol, today), account["equity"], seconds)
            status = signal["status"]
            if status == "enter":
                before = symbol in today["entered"]
                bought = self._enter(symbol, signal, budget, day, today) or bought
                entry = today["entered"].get(symbol)
                if entry and not before:                    # bought, or an order that may have gone through
                    budget["cash"] -= entry["qty"] * entry["price"] * 1.002
            elif status in FINAL:
                self._decide(today, day, symbol, status)
        self._save(today)
        return "bought" if bought else "watching"

    # ── end of day ────────────────────────────────────────────────────────────
    def _finish_day(self, day: str, account: dict, today: dict) -> None:
        if today["finished"]:
            return
        try:
            orders = self.broker.closed_orders(_midnight(day))
        except BrokerError:
            return                                          # try again on the next pass
        for result in summarize_fills(orders, self._allowed(), day):
            self.journal.record("trade_result", day=day, **{
                k: (round(v, 4) if isinstance(v, float) else v) for k, v in result.items()})
        try:
            spy = self.broker.latest_prices(["SPY"]).get("SPY")
        except BrokerError:
            spy = None
        self.journal.record("snapshot", date=day, equity=account["equity"], spy=spy)
        today["finished"] = True
        self._save(today)
        self._log("flat for the day; result recorded")

    # ── one pass ──────────────────────────────────────────────────────────────
    def step(self) -> str:
        clock = self.broker.clock()
        self._in_session = bool(clock["is_open"])
        if not clock["is_open"]:
            self._idle_hint(clock)
            if self.analyst:
                self._before_open(clock)
            return "market_closed"
        now = parse_ts(clock["timestamp"]).astimezone(EASTERN)
        try:
            close = parse_ts(clock["next_close"]).astimezone(EASTERN)
            close_minute = close.hour * 60 + close.minute
        except (ValueError, KeyError):
            close_minute = SESSION_CLOSE                    # unreadable: assume a normal day, still sell on time
        day = now.date().isoformat()
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        flatten_at = (close_minute - self.cfg.flatten_before_close) * 60
        account, positions = self.broker.account(), self.broker.positions()
        self.hint = POLL
        self._remember(clock, account, positions)

        if account["blocked"]:
            return "account_blocked"
        today = self._today(day)
        pinned, current = self.journal.state().get("account_id"), account.get("account_id")
        if current and not pinned:
            self.journal.update_state(account_id=current)
        elif current and pinned and current != pinned:
            self._note_once(today, "account_changed")
            self.hint = 300.0
            return "account_changed"                        # different account than before: touch nothing
        foreign = sorted(set(positions) - set(self._allowed()))
        if foreign:
            self._note_once(today, "foreign_positions", symbols=foreign)
        self._start_line(account, day)

        if not foreign:
            leftovers = sorted(set(positions) - set(today["entered"]))
            if leftovers:
                self.journal.record("sweep", day=day, symbols=leftovers)
                self._flatten(day, "left over from an earlier day", leftovers)
                return "selling_leftovers"

        start_value = account["last_equity"]
        if (not today["halted"] and start_value > 0
                and account["equity"] <= start_value * (1.0 - self.cfg.daily_loss_halt)):
            today["halted"] = True
            self._save(today)
            self.journal.record("halt", day=day, equity=account["equity"], start=start_value)
            self._log("down 1% on the day: selling everything and stopping for today")

        mine = self._sellable(positions, today, self._allowed())
        if today["halted"] or seconds >= flatten_at:
            if mine:
                self._flatten(day, "daily loss limit" if today["halted"] else "end of the day", mine)
                return "flattening"
            if seconds >= flatten_at:
                self._finish_day(day, account, today)
                self.hint = IDLE_MIN
                return "not_a_dedicated_account" if foreign else "flat_for_the_day"
            return "not_a_dedicated_account" if foreign else "daily_loss_halt"

        if foreign:
            self.hint = 60.0
            return "not_a_dedicated_account"
        if self.journal.paused():
            return "paused"
        if close_minute != SESSION_CLOSE:
            return "early_close_sits_out"
        if account["equity"] < DAY_TRADING_MINIMUM:
            self._note_once(today, "below_day_trading_minimum")
            self.hint = 60.0
            return "below_day_trading_minimum"
        watch = list(self.cfg.symbols)
        if self.analyst:
            plan, just_made = self._plan_for(day, seconds, today)
            if just_made:
                return "plan_made"                          # planning took a while: look at the clock afresh
            if plan is None:
                gave_up = (self._tries(today) >= PLAN_ATTEMPTS
                           or seconds >= self.cfg.last_entry_minute * 60)
                return "no_plan_standing_aside" if gave_up else "waiting_for_plan"
            watch = [p["symbol"] for p in plan.get("picks") or []]
            if plan.get("stand_aside") or not watch:
                return "plan_says_stand_aside"
        undecided = [s for s in watch if s not in today["decided"]]
        if not undecided:
            return "done_for_entries"
        if seconds < self.cfg.range_end * 60:
            return "building_opening_range"
        return self._look_for_entries(day, seconds, account, positions, today, undecided)

    def close_out(self) -> str:
        """Called when the trader is told to stop: sell what it may sell, if the market is open.
        Returns "flat" (nothing of ours is held), "sold", or "held" (shares remain that could not
        be sold right now). In an account that also holds other things, only today's own buys are
        sold."""
        try:
            clock, positions = self.broker.clock(), self.broker.positions()
            day = parse_ts(clock["timestamp"]).astimezone(EASTERN).date().isoformat()
            symbols = self._sellable(positions, self._today(day), self._allowed())
        except (BrokerError, ValueError):
            return "held"
        if not symbols:
            return "flat"
        if not clock["is_open"]:
            return "held"
        self._last_flatten = -1e9
        return "sold" if self._flatten(day, "trader stopped", symbols) else "held"

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
            except Exception as exc:                        # never let one bad pass end the run
                failures += 1
                label = f"error:{type(exc).__name__}"
                self.journal.record("error", label=label)
            if label != self._last_label:
                self._log(f"status: {label}")
                self._last_label = label
            if failures:
                cap = FAILURE_CAP_IN_SESSION if self._in_session else 900.0
                delay = min(30.0 * (2 ** min(failures - 1, 5)), cap)
            else:
                delay = self.hint
            stop.wait(delay)


def review_latest_session(broker, cfg: DayConfig = DayConfig(), *, slippage_bps: float = 2.0) -> dict:
    """What the rule did (or would have done) in the most recent full session. Sends nothing."""
    start = _midnight((datetime.now(EASTERN) - timedelta(days=8)).date().isoformat())
    raw = broker.minute_bars(list(cfg.symbols), start)
    equity = broker.account()["equity"]
    sessions = {s: to_sessions(raw.get(s, [])) for s in cfg.symbols}
    full = sorted({d for by_day in sessions.values() for d, bars in by_day.items() if is_full_session(bars)})
    if not full:
        return {"day": None}
    day = full[-1]
    slip = slippage_bps / 10_000.0
    return {"day": day, "equity": equity,
            "results": {s: simulate_day(sessions[s].get(day, []), cfg, equity, slip) for s in cfg.symbols}}
