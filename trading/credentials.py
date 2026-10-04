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


def _read() -> tuple[str, str, str]:
    key = os.environ.get("ALPACA_PAPER_KEY_ID", "").strip()
    secret = os.environ.get("ALPACA_PAPER_SECRET_KEY", "").strip()
    if key and secret:
        return "environment variables", key, secret
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NAMESPACE)
    except Exception:
        stored = {}
    return ("Jarvis Plugin Settings", str(stored.get("key_id") or "").strip(),
            str(stored.get("secret_key") or "").strip())


def load_keys() -> tuple[str, str]:
    _, key, secret = _read()
    return key, secret


def key_report() -> list[str]:
    """Plain-language hints about the saved keys. Shape only: it states lengths and
    whether a prefix is paper-style or live-style, and never prints a character of
    either value."""
    source, key, secret = _read()
    lines = [f"Keys read from: {source}."]
    if not key or not secret:
        return lines + ["The key ID or the secret is empty."]
    problems = []
    if key == secret:
        problems.append("The key ID and the secret are identical, so one was pasted into both boxes.")
    if key.upper().startswith("AK"):
        problems.append("The key ID looks like a LIVE-account key. Paper key IDs start with PK. "
                        "Generate keys from the Paper Trading area.")
    elif not key.upper().startswith("PK"):
        problems.append("The key ID does not start with PK, which paper key IDs normally do. "
                        "It may be the secret, or the two boxes may be swapped.")
    if len(key) > 30:
        problems.append(f"The key ID box holds {len(key)} characters. A key ID is normally about 20, "
                        "so the secret may be in the wrong box.")
    if len(secret) < 35:
        problems.append(f"The secret box holds only {len(secret)} characters. A secret is normally 40; "
                        "it may have been cut short when copying.")
    if any(ch.isspace() or ch in "\"'" for ch in key + secret):
        problems.append("A key contains a space or a quote mark inside it. Re-copy it cleanly.")
    if problems:
        return lines + problems
    return lines + [f"The shapes look normal (paper-style key ID of {len(key)} characters, secret of "
                    f"{len(secret)}). If Alpaca still refuses, the keys were probably regenerated or "
                    "deleted: make a new pair in the Paper Trading area and paste both again."]


def make_broker() -> AlpacaPaper:
    key, secret = load_keys()
    if not key or not secret:
        raise BrokerError(MISSING)
    return AlpacaPaper(key, secret)
