"""Voice control for the optionsbots paper OPTIONS trader (11 bots, Alpaca paper account). Practice money only.

There is one trader and one control command: ~/optionsbots/scripts/bots.py. This plugin only runs that command and
speaks what it prints, so Jarvis and the terminal (`bots status`, `bots pause`, ...) always say and do the same thing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BOTS = Path(os.environ.get("OPTIONSBOTS_DIR", Path.home() / "optionsbots")) / "scripts" / "bots.py"
ACTIONS = ("status", "each", "start", "stop", "pause", "resume")

PLUGIN = {
    "name": "day_trading",
    "description": (
        "Check on and control the automatic PRACTICE OPTIONS trader (optionsbots): eleven rule-based bots trading "
        "QQQ, SPY and IWM options in an Alpaca paper account with fake money. It starts by itself at 6:25 am on "
        "weekdays. Use for 'how are the bots doing', 'how is the trader doing', 'did the bots trade today', "
        "'start the trader', 'stop the trader', 'pause the bots', 'resume the bots', 'how is each bot doing'. "
        "Actions: status (default), each, start, stop, pause, resume. This tool can NOT trade real money and can NOT "
        "buy or sell a specific option on request; only the bots trade. Never close or open positions at Alpaca "
        "outside the bots: it blocks them from trading. Never present practice results as proof of future profit."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status (default), each, start, stop, pause, or resume"},
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
    action = {"bots": "each"}.get(action, action)
    try:
        if not BOTS.exists():
            result = "The options trader isn't installed where I expect it, in the optionsbots folder in your home folder."
        elif action not in ACTIONS:
            result = "I can show the options trader's status or each bot, start, stop, pause or resume it."
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
