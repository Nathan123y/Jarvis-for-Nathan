"""Shared test doubles for the paper-trading tests. No network, no real keys."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from trading.broker import BrokerError
from trading.strategy import UNIVERSE

EASTERN = timezone(timedelta(hours=-4))


def weekdays_before(day: str, count: int) -> list[str]:
    """`count` weekday dates ending the day before `day`, oldest first."""
    out, cursor = [], date.fromisoformat(day)
    while len(out) < count:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
    return out[::-1]


def make_bars(day: str, count: int = 300, drifts: dict | None = None, default: float = 0.0004
              ) -> dict[str, list[tuple[str, float]]]:
    drifts = drifts or {}
    stamps = weekdays_before(day, count)
    return {s: [(d, 100.0 * (1.0 + drifts.get(s, default)) ** i) for i, d in enumerate(stamps)]
            for s in UNIVERSE}


RISING = {"QQQ": 0.0009, "SPY": 0.0006, "GLD": 0.0005, "IWM": 0.0003, "EFA": 0.0002, "TLT": 0.0001}
FALLING = {s: -0.001 for s in UNIVERSE}


class FakeBroker:
    def __init__(self, *, day="2026-10-06", is_open=True, minutes_left=330, equity=100_000.0,
                 last_equity=None, positions=None, bars=None, blocked=False, spy=500.0):
        self.day, self.is_open, self.minutes_left = day, is_open, minutes_left
        self.cash = equity
        self.last_equity = equity if last_equity is None else last_equity
        self.held: dict[str, float] = {}
        for symbol, value in (positions or {}).items():
            self.held[symbol] = float(value)
            self.cash -= float(value)
        self.bars = bars if bars is not None else make_bars(day, drifts=RISING)
        self.blocked, self.spy = blocked, spy
        self.orders: list[dict] = []
        self.client_ids: set[str] = set()
        self.fail_next_orders = 0

    @property
    def equity(self) -> float:
        return self.cash + sum(self.held.values())

    def clock(self):
        close = datetime.fromisoformat(self.day + "T16:00:00").replace(tzinfo=EASTERN)
        now = close - timedelta(minutes=self.minutes_left)
        return {"timestamp": now.isoformat(), "is_open": self.is_open,
                "next_open": (close + timedelta(hours=17, minutes=30)).isoformat(),
                "next_close": close.isoformat()}

    def account(self):
        return {"status": "ACTIVE", "cash": self.cash, "equity": self.equity,
                "last_equity": self.last_equity, "buying_power": self.cash * 2,
                "blocked": self.blocked, "pattern_day_trader": False}

    def positions(self):
        return {s: {"qty": v / 100.0, "qty_text": f"{v / 100.0:.6f}", "market_value": v,
                    "price": 100.0, "side": "long"} for s, v in self.held.items() if v > 0}

    def open_orders(self):
        return []

    def latest_prices(self, symbols):
        return {s: (self.spy if s == "SPY" else 100.0) for s in symbols}

    def daily_bars(self, symbols, start, end=None):
        return {s: list(self.bars.get(s, [])) for s in symbols}

    def submit_market_order(self, symbol, side, *, qty=None, notional=None, client_order_id=None):
        if self.fail_next_orders:
            self.fail_next_orders -= 1
            raise BrokerError("Alpaca is having trouble right now.")
        if client_order_id in self.client_ids:
            raise BrokerError("Alpaca refused the request (422). client_order_id must be unique")
        self.client_ids.add(client_order_id)
        self.orders.append({"symbol": symbol, "side": side, "qty": qty, "notional": notional,
                            "client_order_id": client_order_id})
        if side == "buy":
            self.cash -= notional
            self.held[symbol] = self.held.get(symbol, 0.0) + notional
        elif qty is not None:
            self.cash += self.held.pop(symbol, 0.0)
        else:
            self.held[symbol] -= notional
            self.cash += notional
        return {"id": f"order-{len(self.orders)}", "status": "filled", "client_order_id": client_order_id}


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Records every request and replays scripted responses."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item
