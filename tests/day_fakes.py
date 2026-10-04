"""Shared test doubles for the day-trader tests. No network, no real keys."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from trading.broker import BrokerError

EASTERN = ZoneInfo("America/New_York")
DAY = "2026-10-05"                     # a Monday, in daylight-saving time (UTC-4)


def utc_stamp(day: str, minute: int) -> str:
    local = datetime.fromisoformat(day).replace(tzinfo=EASTERN) + timedelta(minutes=minute)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_stamp(day: str, seconds: float) -> str:
    return (datetime.fromisoformat(day).replace(tzinfo=EASTERN) + timedelta(seconds=seconds)).isoformat()


def day_bars(*, breakout: int = 590, rng=(100.0, 100.6), drift: float = 0.002, crash_at=None,
             first: int = 570, last: int = 959) -> list[tuple]:
    """One session of (minute, o, h, l, c): 15 quiet minutes inside `rng` (low, high), quiet drift inside it
    until `breakout` (None = never), then a climb. `crash_at` drops the price far below the range."""
    low, high = rng
    mid = (high + low) / 2.0
    bars = []
    for m in range(first, last + 1):
        if m < 585:
            h = high if m == 575 else mid + 0.1
            l = low if m == 580 else mid - 0.1
            bars.append((m, mid, h, l, mid))
        elif breakout is None or m < breakout:
            bars.append((m, mid, high - 0.1, low + 0.2, mid + 0.1))
        else:
            base = high + 0.2 + (m - breakout) * drift
            o, c = base, base + 0.01
            bar = (m, o, c + 0.02, o - 0.02, c)
            if crash_at is not None and m >= crash_at:
                bar = (m, low - 0.5, low - 0.4, low - 1.0, low - 0.5)
            bars.append(bar)
    return bars


def raw_bars(day: str, bars: list[tuple]) -> list[tuple]:
    return [(utc_stamp(day, m), o, h, l, c) for m, o, h, l, c in bars]


class DayFakeBroker:
    """A tiny pretend Alpaca paper account for the day trader. Orders fill instantly at the
    latest candle's close; bracket stops are simulated only when a test asks for them."""

    def __init__(self, day: str = DAY, *, seconds: float = 9 * 3600 + 51 * 60 + 5, bars=None,
                 equity: float = 100_000.0, last_equity=None, is_open: bool = True,
                 close_minute: int = 960, account_id: str = "acct-day", held=None, blocked: bool = False):
        self.day, self.seconds, self.is_open, self.close_minute = day, seconds, is_open, close_minute
        self.bars = {"SPY": day_bars(), "QQQ": day_bars(rng=(200.0, 201.2))} if bars is None else bars
        self.cash = equity
        self.last_equity = equity if last_equity is None else last_equity
        self.account_id, self.blocked = account_id, blocked
        self.held: dict[str, int] = {}
        self.entry_orders: list[dict] = []
        self.fills: list[dict] = []
        self.client_ids: set[str] = set()
        self.stops: dict[str, str] = {}          # symbol -> id of its open protective stop
        self.close_calls: list[str] = []
        self.fail_entries = 0
        self.lose_reply_after_fill = False
        self.price_override: dict[str, float] = {}
        for symbol, qty in (held or {}).items():
            self.held[symbol] = qty
            self.cash -= qty * self._price(symbol)

    # time and prices
    def at(self, hours: int, minutes: int, secs: int = 0) -> "DayFakeBroker":
        self.seconds = hours * 3600 + minutes * 60 + secs
        return self

    def _price(self, symbol: str) -> float:
        if symbol in self.price_override:
            return self.price_override[symbol]
        done = [b for b in self.bars.get(symbol, []) if b[0] * 60 <= self.seconds]
        return done[-1][4] if done else 100.0

    @property
    def equity(self) -> float:
        return self.cash + sum(q * self._price(s) for s, q in self.held.items())

    # the broker surface the runner uses
    def clock(self):
        return {"timestamp": local_stamp(self.day, self.seconds), "is_open": self.is_open,
                "next_open": local_stamp(self.day, 86400 + 9.5 * 3600),
                "next_close": local_stamp(self.day, self.close_minute * 60)}

    def account(self):
        return {"account_id": self.account_id, "status": "ACTIVE", "cash": self.cash,
                "equity": self.equity, "last_equity": self.last_equity, "buying_power": self.cash,
                "blocked": self.blocked, "pattern_day_trader": False}

    def positions(self):
        return {s: {"qty": float(q), "qty_text": str(q), "market_value": q * self._price(s),
                    "price": self._price(s), "side": "long"} for s, q in self.held.items() if q}

    def minute_bars(self, symbols, start, end=None, feed="iex", **_):
        return {s: raw_bars(self.day, [b for b in self.bars.get(s, []) if b[0] * 60 <= self.seconds])
                for s in symbols}

    def latest_prices(self, symbols):
        return {s: self._price(s) for s in symbols}

    def daily_bars(self, symbols, start, end=None, feed="iex"):
        return {s: [] for s in symbols}

    def submit_entry_with_stop(self, symbol, qty, stop_price, *, client_order_id=None):
        if self.fail_entries:
            self.fail_entries -= 1
            raise BrokerError(self.entry_error)
        if client_order_id in self.client_ids:
            raise BrokerError("Alpaca refused the request (422). client_order_id must be unique")
        self.client_ids.add(client_order_id)
        price = self._price(symbol)
        self.entry_orders.append({"symbol": symbol, "qty": qty, "stop": stop_price,
                                  "client_order_id": client_order_id})
        self.cash -= qty * price
        self.held[symbol] = self.held.get(symbol, 0) + qty
        self.stops[symbol] = f"stop-order-{len(self.entry_orders):04d}"
        self._fill(symbol, "buy", qty, price, "market")
        if self.lose_reply_after_fill:
            raise BrokerError("Could not reach Alpaca. Check the internet connection.")
        return {"id": f"order-{len(self.entry_orders)}", "status": "filled", "client_order_id": client_order_id}

    entry_error = "Alpaca is having trouble right now."

    def hit_stop(self, symbol, price):
        qty = self.held.pop(symbol)
        self.stops.pop(symbol, None)
        self.cash += qty * price
        self._fill(symbol, "sell", qty, price, "stop")

    def open_orders(self):
        return [{"id": oid, "symbol": symbol, "side": "sell", "status": "new"}
                for symbol, oid in self.stops.items()]

    def cancel_order(self, order_id):
        for symbol, oid in list(self.stops.items()):
            if oid == order_id:
                del self.stops[symbol]
                return
        raise BrokerError("Alpaca refused the request (422). order is not cancelable")

    def close_position(self, symbol):
        if symbol in self.stops:                  # the stop still holds the shares
            raise BrokerError("Alpaca refused the request (403). insufficient qty available for order")
        if symbol not in self.held:
            raise BrokerError("Alpaca refused the request (404). position does not exist")
        qty = self.held.pop(symbol)
        price = self._price(symbol)
        self.cash += qty * price
        self.close_calls.append(symbol)
        self._fill(symbol, "sell", qty, price, "market")

    def _fill(self, symbol, side, qty, price, kind):
        self.fills.append({"symbol": symbol, "side": side, "qty": float(qty), "price": price,
                           "type": kind, "filled_at": utc_stamp(self.day, int(self.seconds // 60))})

    def closed_orders(self, after):
        return list(self.fills)


def weekdays(count: int, start: str = "2025-01-06") -> list[str]:
    from datetime import date
    out, day = [], date.fromisoformat(start)
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def history_raw(count: int, **kwargs) -> dict[str, list[tuple]]:
    """`count` identical quiet-climb sessions for SPY and QQQ as Alpaca-style raw candles."""
    out: dict[str, list[tuple]] = {"SPY": [], "QQQ": []}
    for day in weekdays(count):
        out["SPY"] += raw_bars(day, day_bars(**kwargs))
        out["QQQ"] += raw_bars(day, day_bars(rng=(200.0, 201.2), **kwargs))
    return out
