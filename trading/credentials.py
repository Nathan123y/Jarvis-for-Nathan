"""Where the Alpaca paper keys come from. Never printed, never logged.

Order of lookup:
  1. environment variables ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY
  2. Jarvis's Plugin Settings (stored in the git-ignored config/api_keys.json)
"""
from __future__ import annotations

import os

from trading.broker import AlpacaPaper, BrokerError

NAMESPACE = "alpaca_paper"
MISSING = ("The Alpaca paper keys are not set up. Create a free paper account at alpaca.markets, "
           "generate its API keys, and paste them into Jarvis Plugin Settings under "
           "\"Alpaca paper trading\" (or set ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY).")


def load_keys() -> tuple[str, str]:
    key = os.environ.get("ALPACA_PAPER_KEY_ID", "").strip()
    secret = os.environ.get("ALPACA_PAPER_SECRET_KEY", "").strip()
    if key and secret:
        return key, secret
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NAMESPACE)
    except Exception:
        stored = {}
    return str(stored.get("key_id") or "").strip(), str(stored.get("secret_key") or "").strip()


def make_broker() -> AlpacaPaper:
    key, secret = load_keys()
    if not key or not secret:
        raise BrokerError(MISSING)
    return AlpacaPaper(key, secret)
