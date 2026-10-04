"""Alpaca *paper* trading client over plain HTTPS.

Why plain `requests` and not the alpaca-py package: nothing extra to install,
and every call is visible here. Why paper only: PAPER_URL is the only trading
address this class will talk to. Pointing it anywhere else raises before a
single request is made, so a typo or a pasted live key cannot trade real money
(a live key is rejected by the paper endpoint anyway).

Keys are sent only in request headers. They are never put in a URL, a log line
or an exception message; BrokerError text is built from a fixed vocabulary plus
a short, scrubbed message from Alpaca.
"""
from __future__ import annotations

import re
from typing import Any, Optional

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

_SYMBOL = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")
_TIMEOUT = (4, 12)
_MAX_PAGES = 40


class BrokerError(Exception):
    """A failure that is safe to show to the user and to speak aloud."""


def _num(value: Any, default: float = 0.0) -> float:
    """Alpaca sends most numbers as strings; accept either, never raise."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and number not in (float("inf"), float("-inf")) else default


class AlpacaPaper:
    def __init__(self, key_id: str, secret_key: str, *, session=None,
                 trading_url: str = PAPER_URL, data_url: str = DATA_URL):
        if trading_url.rstrip("/") != PAPER_URL:
            raise ValueError("Only the Alpaca paper-trading address is allowed here.")
        if data_url.rstrip("/") != DATA_URL:
            raise ValueError("Unexpected market-data address.")
        self._key = str(key_id or "").strip()
        self._secret = str(secret_key or "").strip()
        if not self._key or not self._secret:
            raise BrokerError("Alpaca paper keys are not set up yet.")
        if session is None:
            import requests
            session = requests.Session()
        self._session = session

    # ── plumbing ──────────────────────────────────────────────────────────────
    def _scrub(self, text: Any) -> str:
        out = " ".join(str(text or "").split())[:160]
        for secret in (self._key, self._secret):
            if secret:
                out = out.replace(secret, "***")
        return out

    def _call(self, method: str, base: str, path: str, *, params=None, body=None):
        headers = {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret,
                   "Accept": "application/json"}
        try:
            response = self._session.request(
                method, base + path, headers=headers, params=params, json=body,
                timeout=_TIMEOUT, allow_redirects=False)
        except Exception:                       # network down, DNS, timeout, TLS
            raise BrokerError("Could not reach Alpaca. Check the internet connection.") from None
        status = getattr(response, "status_code", 0)
        if status == 403:
            try:
                said = self._scrub(response.json().get("message", "")).lower()
            except Exception:
                said = ""
            if "subscription" in said or "sip" in said:
                raise BrokerError("Alpaca's free plan does not allow that price feed for the period asked. "
                                  "Run again without --feed sip.")
        if status in (401, 403):
            raise BrokerError(f"Alpaca rejected the paper keys (HTTP {status}). Check the key ID and "
                              "secret, and that they came from a paper account.")
        if status == 429:
            raise BrokerError("Alpaca is rate-limiting requests. It will retry shortly.")
        if status >= 500:
            raise BrokerError("Alpaca is having trouble right now.")
        if status >= 400:
            detail = ""
            try:
                detail = self._scrub(response.json().get("message", ""))
            except Exception:
                pass
            raise BrokerError(f"Alpaca refused the request ({status}). {detail}".strip())
        if status == 204:
            return {}
        try:
            return response.json()
        except Exception:
            raise BrokerError("Alpaca sent a reply that could not be read.") from None

    def _trading(self, method, path, **kw):
        return self._call(method, PAPER_URL, path, **kw)

    def _data(self, path, **kw):
        return self._call("GET", DATA_URL, path, **kw)

    # ── account ───────────────────────────────────────────────────────────────
    def account(self) -> dict:
        raw = self._trading("GET", "/v2/account")
        if not isinstance(raw, dict):
            raise BrokerError("Alpaca sent an unexpected account reply.")
        return {
            "status": str(raw.get("status", "")),
            "cash": _num(raw.get("cash")),
            "equity": _num(raw.get("equity")),
            "last_equity": _num(raw.get("last_equity"), _num(raw.get("equity"))),
            "buying_power": _num(raw.get("buying_power")),
            "blocked": bool(raw.get("trading_blocked") or raw.get("account_blocked")),
            "pattern_day_trader": bool(raw.get("pattern_day_trader")),
        }

    def clock(self) -> dict:
        raw = self._trading("GET", "/v2/clock")
        if not isinstance(raw, dict) or not raw.get("timestamp"):
            raise BrokerError("Alpaca sent an unexpected market-clock reply.")
        return {"timestamp": str(raw.get("timestamp")), "is_open": bool(raw.get("is_open")),
                "next_open": str(raw.get("next_open", "")), "next_close": str(raw.get("next_close", ""))}

    def positions(self) -> dict[str, dict]:
        raw = self._trading("GET", "/v2/positions")
        out: dict[str, dict] = {}
        for item in raw if isinstance(raw, list) else []:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            out[symbol] = {"qty": _num(item.get("qty")), "qty_text": str(item.get("qty", "0")),
                           "market_value": _num(item.get("market_value")),
                           "price": _num(item.get("current_price")),
                           "side": str(item.get("side", "long"))}
        return out

    def open_orders(self) -> list[dict]:
        raw = self._trading("GET", "/v2/orders", params={"status": "open", "limit": 100})
        return [{"id": str(o.get("id", "")), "symbol": str(o.get("symbol", "")),
                 "side": str(o.get("side", "")), "status": str(o.get("status", ""))}
                for o in (raw if isinstance(raw, list) else [])]

    def get_order(self, order_id: str) -> dict:
        raw = self._trading("GET", f"/v2/orders/{order_id}")
        return {"id": str(raw.get("id", order_id)), "status": str(raw.get("status", "")),
                "filled_qty": _num(raw.get("filled_qty")),
                "filled_avg_price": _num(raw.get("filled_avg_price"))}

    # ── orders ────────────────────────────────────────────────────────────────
    def submit_market_order(self, symbol: str, side: str, *, qty: Optional[str] = None,
                            notional: Optional[float] = None,
                            client_order_id: Optional[str] = None) -> dict:
        symbol = str(symbol).upper()
        if not _SYMBOL.match(symbol):
            raise BrokerError("That is not a valid ticker symbol.")
        if side not in ("buy", "sell"):
            raise BrokerError("An order must be a buy or a sell.")
        if (qty is None) == (notional is None):
            raise BrokerError("Give either a share quantity or a dollar amount, not both.")
        body: dict[str, Any] = {"symbol": symbol, "side": side, "type": "market",
                                "time_in_force": "day"}
        if qty is not None:
            body["qty"] = str(qty)
        else:
            body["notional"] = f"{float(notional):.2f}"
        if client_order_id:
            body["client_order_id"] = str(client_order_id)[:128]
        raw = self._trading("POST", "/v2/orders", body=body)
        return {"id": str(raw.get("id", "")), "status": str(raw.get("status", "")),
                "client_order_id": str(raw.get("client_order_id", ""))}

    def cancel_open_orders(self) -> None:
        self._trading("DELETE", "/v2/orders")

    # ── market data (free IEX feed) ───────────────────────────────────────────
    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        raw = self._data("/v2/stocks/trades/latest",
                         params={"symbols": ",".join(symbols), "feed": "iex"})
        trades = raw.get("trades", raw) if isinstance(raw, dict) else {}
        out: dict[str, float] = {}
        for symbol, trade in (trades.items() if isinstance(trades, dict) else []):
            price = _num(trade.get("p")) if isinstance(trade, dict) else 0.0
            if price > 0:
                out[str(symbol).upper()] = price
        return out

    def daily_bars(self, symbols: list[str], start: str, end: Optional[str] = None,
                   feed: str = "iex") -> dict[str, list[tuple[str, float]]]:
        """Adjusted daily closes as {symbol: [(YYYY-MM-DD, close), ...]}, oldest first.

        feed "iex" is the free real-time feed (one exchange, history from mid-2020 for most
        funds). feed "sip" is the full consolidated market; on the free plan it is limited to
        data older than 15 minutes, so pass an `end` of yesterday or earlier."""
        if feed not in ("iex", "sip"):
            raise BrokerError("The price feed must be iex or sip.")
        out: dict[str, list[tuple[str, float]]] = {s: [] for s in symbols}
        token = None
        for _ in range(_MAX_PAGES):
            params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                      "adjustment": "all", "feed": feed, "limit": 10000, "sort": "asc"}
            if end:
                params["end"] = end
            if token:
                params["page_token"] = token
            raw = self._data("/v2/stocks/bars", params=params)
            bars = raw.get("bars") if isinstance(raw, dict) else None
            for symbol, items in (bars.items() if isinstance(bars, dict) else []):
                for bar in items or []:
                    close = _num(bar.get("c"))
                    if close > 0 and bar.get("t"):
                        out.setdefault(str(symbol).upper(), []).append((str(bar["t"])[:10], close))
            token = raw.get("next_page_token") if isinstance(raw, dict) else None
            if not token:
                break
        return out
