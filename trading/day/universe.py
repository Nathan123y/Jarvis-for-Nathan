"""The short, fixed list of things the analyst may pick from.

The list lives in code on purpose. The analyst (an AI model reading prices and headlines) chooses
among these and nothing else, so a wrong or manipulated answer can only ever be a different name
from this list, never an unknown or thinly traded stock. Large, heavily traded names only: the
free price feed is one exchange, and a stock that rarely trades there would have gaps.

Changing the list is a code change, deliberately.
"""
from __future__ import annotations

FUNDS = ("SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV")
STOCKS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "JPM")
UNIVERSE = FUNDS + STOCKS

NAMES = {
    "SPY": "S&P 500 fund", "QQQ": "Nasdaq-100 fund", "IWM": "small-company (Russell 2000) fund",
    "DIA": "Dow Jones 30 fund", "XLK": "technology-sector fund", "XLF": "financial-sector fund",
    "XLE": "energy-sector fund", "XLV": "health-care-sector fund",
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon", "GOOGL": "Alphabet (Google)",
    "META": "Meta (Facebook)", "TSLA": "Tesla", "AMD": "Advanced Micro Devices", "JPM": "JPMorgan Chase",
}

# How far below the price the protective stop may sit before the trade is skipped. An individual
# stock swings more than a fund, so it gets more room; funds keep the standard 1.5%.
STOCK_MAX_RISK_PCT = 0.03


def kind(symbol: str) -> str:
    return "stock" if symbol in STOCKS else "fund"
