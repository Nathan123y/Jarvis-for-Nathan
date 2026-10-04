"""Voice control for the practice (paper) trader. Practice money only.

The trader itself is the separate `trading` package, running as its own process,
so nothing here sits in the voice loop. Status is answered from the trader's
local record in a few milliseconds with no network call.
"""
from __future__ import annotations

import subprocess
import sys
import time

from memory.config_manager import BASE_DIR
from trading import report as reports
from trading.broker import BrokerError
from trading.credentials import MISSING, load_keys, make_broker
from trading.journal import Journal
from trading.runner import Runner

PLUGIN = {
    "name": "paper_trading",
    "description": (
        "Check on and control the automatic PRACTICE stock trader, which uses fake money in an "
        "Alpaca paper account. Use for 'how is my practice trading doing', 'is the trading bot "
        "beating the market', 'start the practice trader', 'pause trading', 'resume trading', "
        "'stop the trading bot', 'what would the trader do now'. Actions: status (default), plan, "
        "start, stop, pause, resume, connect. This tool can NOT trade real money and can NOT buy "
        "or sell a specific stock on request; it only runs one fixed rule on a practice account. "
        "If asked to trade real money, say it only does practice trading for now. Never present "
        "practice results as proof of future profit. Never ask the user to say or paste API keys "
        "in chat; they belong in local Plugin Settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "status (default), plan, start, stop, pause, resume, or connect"},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "alpaca_paper",
    "title": "Alpaca paper trading (practice money only)",
    "fields": [
        {"key": "key_id", "type": "password", "label": "Paper API key ID",
         "placeholder": "From your free Alpaca PAPER account"},
        {"key": "secret_key", "type": "password", "label": "Paper API secret key",
         "placeholder": "Shown once when the key is created"},
    ],
}


def _journal() -> Journal:
    return Journal()


def _status() -> str:
    journal = _journal()
    state = {**journal.state(), "paused": journal.paused()}
    if not state.get("start_equity"):
        key, secret = load_keys()
        if not (key and secret):
            return "The practice trader isn't set up yet. " + _connect()
        return ("The practice trader hasn't traded yet. Say 'start the practice trader' and it "
                "will make its first decision during the next market session.")
    snapshots = reports.latest_per_day(journal.events("snapshot"))
    text = reports.spoken(reports.build(state, snapshots))
    if journal.runner_pid() is None:
        text += " The trader isn't running right now, so say 'start the practice trader' to continue."
    return text


def _plan() -> str:
    plan, clock = Runner(make_broker(), _journal()).plan_now()
    if plan is None:
        return "There isn't enough price history yet to judge the trend."
    held = ", ".join(plan.targets) or "cash only"
    text = f"Right now the rule would hold {held}."
    if not plan.due:
        return text + " It isn't a decision day, so it would not trade today."
    if plan.sells or plan.buys:
        text += f" That means {len(plan.sells)} sells and {len(plan.buys)} buys."
    else:
        text += " The account already matches, so no trades."
    return text + " Nothing was sent."


def _start() -> str:
    key, secret = load_keys()
    if not (key and secret):
        return _connect()
    journal = _journal()
    if journal.runner_pid() is not None:
        return "The practice trader is already running."
    journal.dir.mkdir(parents=True, exist_ok=True)
    options = {"cwd": str(BASE_DIR), "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008 | 0x00000200
    else:
        options["start_new_session"] = True
    with open(journal.log_path, "ab") as log:
        subprocess.Popen([sys.executable, "-u", "-m", "trading", "run"],
                         stdout=log, stderr=log, **options)
    for _ in range(12):
        time.sleep(0.25)
        if journal.runner_pid() is not None:
            return ("The practice trader is running. It uses fake money and decides once per "
                    "trading day. Keep this Mac awake during market hours.")
    return "I started it, but couldn't confirm it is running. Check the trading log in the config folder."


def _connect() -> str:
    return ("Open Plugin Settings, find Alpaca paper trading, and paste the key ID and secret from "
            "your free Alpaca paper account. Please don't read the keys out loud to me.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action") or "status").strip().lower()
    try:
        if action == "status":
            result = _status()
        elif action == "plan":
            result = _plan()
        elif action == "start":
            result = _start()
        elif action == "stop":
            result = ("Asked the practice trader to stop." if _journal().stop_runner()
                      else "The practice trader isn't running.")
        elif action == "pause":
            _journal().pause()
            result = "Paused. It keeps watching but won't send any orders."
        elif action == "resume":
            _journal().resume()
            result = "Resumed."
        elif action == "connect":
            result = _connect()
        else:
            result = "I can check status, show the plan, start, stop, pause or resume the practice trader."
    except BrokerError as exc:
        result = str(exc) if str(exc) != MISSING else _connect()
    except Exception:
        result = "Sir, the practice trader tool hit a problem. Details are in the console."
    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result
