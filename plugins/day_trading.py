"""Voice control for the optionsbots paper OPTIONS trader (11 bots, Alpaca paper account) and the paper MEMECOIN bot (its own
Alpaca paper account). Practice money only.

There is one trader and one control command: ~/optionsbots/scripts/bots.py. This plugin only runs that command and
speaks what it prints, so Jarvis and the terminal (`bots status`, `bots pause`, ...) always say and do the same thing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BOTS = Path(os.environ.get("OPTIONSBOTS_DIR", Path.home() / "optionsbots")) / "scripts" / "bots.py"
ACTIONS = ("status", "each", "learned", "start", "stop", "pause", "resume", "crypto", "crypto_start", "crypto_stop", "portfolio", "hype")
ALIASES = {"bots": "each", "learn": "learned", "learning": "learned", "memecoins": "crypto", "memecoin": "crypto", "meme": "crypto",
           "coins": "crypto", "crypto_status": "crypto", "buzz": "hype", "sentiment": "hype", "start_crypto": "crypto_start", "stop_crypto": "crypto_stop", "everything": "portfolio"}

PLUGIN = {
    "name": "day_trading",
    "description": (
        "Check on and control the automatic PRACTICE OPTIONS trader (optionsbots): eleven rule-based bots trading "
        "QQQ, SPY and IWM options in an Alpaca paper account with fake money. It starts by itself at 6:25 am on "
        "weekdays. Use for 'how are the bots doing', 'how is the trader doing', 'did the bots trade today', "
        "'start the trader', 'stop the trader', 'pause the bots', 'resume the bots', 'how is each bot doing', "
        "'what did the bots learn', 'did the bots change anything'. Every weekday after the close the bots review themselves "
        "on two years of past prices and may adjust one setting each; 'learned' says what changed. "
        "Also the PRACTICE MEMECOIN bot: it trades DOGE, SHIB, PEPE, BONK, WIF and TRUMP in its own Alpaca paper account, "
        "buying a coin that breaks above its 5-day high and selling on a trailing stop; it runs whenever the Mac is awake. Use "
        "crypto for 'how are the memecoins doing', crypto_start / crypto_stop to start or stop it, and portfolio for "
        "'how is my whole portfolio doing' (options and memecoins together). The memecoin bot also reads internet hype every hour "
        "(Reddit, 4chan /biz/, CoinGecko trending): a hyped coin is bought sooner, a coin nobody mentions is skipped. Use hype for "
        "'what's the crypto hype', 'which memecoins are trending', 'is anyone talking about PEPE'. "
        "Actions: status (default), each, learned, start, stop, pause, resume, crypto, crypto_start, crypto_stop, portfolio, hype. This tool can NOT trade real money and can NOT "
        "buy or sell a specific option on request; only the bots trade. Never close or open positions at Alpaca "
        "outside the bots: it blocks them from trading. Never present practice results as proof of future profit."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status (default), each, learned, start, stop, pause, resume (options trader); "
                                                        "crypto, crypto_start, crypto_stop (memecoin bot); hype (memecoin buzz); portfolio (both)"},
        },
        "required": [],
    },
}


def bots(action: str, timeout: float = 150) -> str:
    """Run the shared control command and return what it printed."""
    r = subprocess.run([sys.executable, str(BOTS), action, "--jarvis"], capture_output=True, text=True, timeout=timeout)
    if r.stderr.strip():
        print(r.stderr.strip(), file=sys.stderr)
    return r.stdout.strip()


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action") or "status").strip().lower()
    action = ALIASES.get(action.replace(" ", "_"), action.replace(" ", "_"))
    try:
        if not BOTS.exists():
            result = "The options trader isn't installed where I expect it, in the optionsbots folder in your home folder."
        elif action not in ACTIONS:
            result = ("I can show the options trader's status or each bot, what it learned, start, stop, pause or resume it, "
                      "and the memecoin bot's status, or start and stop it.")
        else:
            result = bots(action) or "The options trader didn't say anything back. Check it with 'bots' in a terminal."
    except subprocess.TimeoutExpired:
        result = "The options trader didn't answer in time. Docker may still be starting."
    except Exception:
        result = "Sir, the options trader tool hit a problem. Details are in the console."
    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result
