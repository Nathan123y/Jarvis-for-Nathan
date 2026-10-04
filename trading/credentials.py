"""Where the Alpaca paper keys come from. Never printed, never logged.

There are two practice traders and each has its own free Alpaca paper account:

    "weekly"  the slow rotation rule              ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET_KEY
                                                  Plugin Settings: "Alpaca paper trading"
    "day"     the opening-range day trader        ALPACA_PAPER_DAY_KEY_ID / ALPACA_PAPER_DAY_SECRET_KEY
                                                  Plugin Settings: "Alpaca paper day trading"

Order of lookup for each: environment variables first, then Jarvis's Plugin Settings
(stored in the git-ignored config/api_keys.json). The two must be different accounts:
the day trader sells everything it finds at the end of its day, so sharing an account
with the weekly trader would wipe out the weekly trader's holdings.
"""
from __future__ import annotations

import os
from typing import Optional

from trading.broker import AlpacaPaper, BrokerError

NAMESPACE = "alpaca_paper"
DAY_NAMESPACE = "alpaca_paper_day"

_PROFILES = {
    "weekly": (NAMESPACE, "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY"),
    "day": (DAY_NAMESPACE, "ALPACA_PAPER_DAY_KEY_ID", "ALPACA_PAPER_DAY_SECRET_KEY"),
}

MISSING = ("The Alpaca paper keys are not set up. Create a free paper account at alpaca.markets, "
           "generate its API keys, and paste them into Jarvis Plugin Settings under "
           "\"Alpaca paper trading\" (or set ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY).")
MISSING_DAY = ("The day trader's keys are not set up. It needs its own, second free paper account: in "
               "the Alpaca Paper Trading area open a new paper account, generate its API keys, and "
               "paste them into Jarvis Plugin Settings under \"Alpaca paper day trading\" (or set "
               "ALPACA_PAPER_DAY_KEY_ID and ALPACA_PAPER_DAY_SECRET_KEY).")
SAME_KEYS = ("The day trader is set up with the same keys as the weekly trader. It sells everything "
             "at the end of each day, which would wipe out the weekly trader's holdings, so it "
             "needs its own second paper account and keys.")
UNVERIFIED = ("Couldn't confirm that these keys belong to a different Alpaca account than the weekly "
              "trader's (the weekly trader's own keys could not be checked just now). The day trader "
              "sells everything at the end of each day, so it won't start until that is certain. Check "
              "the weekly keys with `python3 -m trading check`, or try again in a minute.")
SAME_ACCOUNT = ("These keys belong to the same Alpaca account as the weekly trader. The day trader "
                "sells everything at the end of each day, so it needs its own, second paper "
                "account. Open a new paper account in the Alpaca dashboard and paste its keys.")


def _read(profile: str = "weekly") -> tuple[str, str, str]:
    namespace, key_var, secret_var = _PROFILES[profile]
    key = os.environ.get(key_var, "").strip()
    secret = os.environ.get(secret_var, "").strip()
    if key and secret:
        return "environment variables", key, secret
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(namespace)
    except Exception:
        stored = {}
    return ("Jarvis Plugin Settings", str(stored.get("key_id") or "").strip(),
            str(stored.get("secret_key") or "").strip())


def load_keys(profile: str = "weekly") -> tuple[str, str]:
    _, key, secret = _read(profile)
    return key, secret


def key_report(profile: str = "weekly") -> list[str]:
    """Plain-language hints about the saved keys. Shape only: it states lengths and
    whether a prefix is paper-style or live-style, and never prints a character of
    either value."""
    source, key, secret = _read(profile)
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
        problems.append(f"The key ID box holds {len(key)} characters. A key ID is usually 20 to 30, "
                        "so the secret may be in the wrong box.")
    if len(secret) < 35:
        problems.append(f"The secret box holds only {len(secret)} characters. A secret is usually 40 or more; "
                        "it may have been cut short when copying.")
    if any(ch.isspace() or ch in "\"'" for ch in key + secret):
        problems.append("A key contains a space or a quote mark inside it. Re-copy it cleanly.")
    if profile == "day":
        weekly_key, _ = load_keys("weekly")
        if weekly_key and weekly_key == key:
            problems.append("These are the same keys as the weekly trader's. The day trader needs "
                            "its own second paper account.")
    if problems:
        return lines + problems
    return lines + [f"The shapes look normal (paper-style key ID of {len(key)} characters, secret of "
                    f"{len(secret)}). If Alpaca still refuses, the keys were probably regenerated or "
                    "deleted: make a new pair in the Paper Trading area and paste both again."]


def make_broker(profile: str = "weekly") -> AlpacaPaper:
    key, secret = load_keys(profile)
    if not key or not secret:
        raise BrokerError(MISSING_DAY if profile == "day" else MISSING)
    if profile == "day":
        weekly_key, _ = load_keys("weekly")
        if weekly_key and weekly_key == key:
            raise BrokerError(SAME_KEYS)
    return AlpacaPaper(key, secret)


def shares_weekly_account(day_broker: AlpacaPaper) -> Optional[bool]:
    """Do the day trader's keys open the very same Alpaca account as the weekly trader's?

    True: the same account (refuse to run). False: different accounts, or there are no weekly
    keys to compare against. None: the weekly side could not be checked, so it is unknown and
    the caller must not go ahead. Different key IDs can still belong to one account (keys can be
    regenerated), which is why the account itself is compared. A failure on the day trader's own
    side is raised: it has to work anyway."""
    key, secret = load_keys("weekly")
    if not (key and secret):
        return False
    mine = day_broker.account().get("account_id")
    try:
        theirs = AlpacaPaper(key, secret).account().get("account_id")
    except BrokerError:
        return None
    if not mine or not theirs:
        return None
    return mine == theirs
