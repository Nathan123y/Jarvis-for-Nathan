"""Voice control for the practice DAY trader. Practice money only.

The trader itself is the separate `trading.day` package, running as its own process, so nothing
here sits in the voice loop. Status is answered from its local record in a few milliseconds
with no network call.
"""
from __future__ import annotations

import subprocess
import sys
import time

from memory.config_manager import BASE_DIR
from trading import report as weekly_reports
from trading.broker import BrokerError
from trading.credentials import MISSING_DAY, load_keys, make_broker
from trading.day import report as reports
from trading.day.rule import STATUS_TEXT
from trading.day.runner import review_latest_session
from trading.day.store import day_journal

PLUGIN = {
    "name": "day_trading",
    "description": (
        "Check on and control the automatic PRACTICE DAY trader: it buys SPY or QQQ on an "
        "opening-range breakout, sells everything before the close each day, and uses fake money "
        "in its own Alpaca paper account. Use for 'how is the day trader doing', 'start the "
        "practice day trader', 'stop the day trader', 'pause day trading', 'resume day trading', "
        "'what would the day trader have done yesterday'. Actions: status (default), plan, start, "
        "stop, pause, resume, connect. This is NOT the slow weekly practice trader (that is the "
        "paper_trading tool). This tool can NOT trade real money and can NOT buy or sell a "
        "specific stock on request; it only runs one fixed rule on a practice account. If asked "
        "to trade real money, say it only does practice trading for now. Never present practice "
        "results as proof of future profit; day trading usually loses money after costs. Never "
        "ask the user to say or paste API keys in chat; they belong in local Plugin Settings."
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
    "namespace": "alpaca_paper_day",
    "title": "Alpaca paper day trading (practice money only, second paper account)",
    "fields": [
        {"key": "key_id", "type": "password", "label": "Day trader paper API key ID",
         "placeholder": "From a SECOND free Alpaca PAPER account"},
        {"key": "secret_key", "type": "password", "label": "Day trader paper API secret key",
         "placeholder": "Shown once when the key is created"},
    ],
}


def _journal():
    return day_journal()


def _status() -> str:
    journal = _journal()
    state = {**journal.state(), "paused": journal.paused()}
    if not state.get("start_equity"):
        key, secret = load_keys("day")
        if not (key and secret):
            return "The practice day trader isn't set up yet. " + _connect()
        if journal.runner_pid() is not None:
            paused = " Buying is paused." if journal.paused() else ""
            return ("The practice day trader is running and waiting for the market. It makes its "
                    "first decision after the first fifteen minutes of a session." + paused)
        return ("The practice day trader hasn't traded yet and isn't running. Say 'start the "
                "practice day trader' and it will start during the next market session.")
    snapshots = weekly_reports.latest_per_day(journal.events("snapshot"))
    text = reports.spoken(reports.build(state, snapshots, journal.events("trade_result")))
    if journal.runner_pid() is None:
        text += " The day trader isn't running right now, so say 'start the practice day trader' to continue."
    return text


def _plan() -> str:
    review = review_latest_session(make_broker("day"))
    if not review["day"]:
        return "I couldn't find a full trading session of minute prices to look at."
    parts = []
    for symbol, result in review["results"].items():
        trade = result.get("trade")
        if trade:
            outcome = "stopped out" if trade["reason"] == "stop" else "sold before the close"
            parts.append(f"{symbol}: bought {trade['qty']} shares and was {outcome}, "
                         f"{'up' if trade['pnl'] >= 0 else 'down'} ${abs(trade['pnl']):,.0f}")
        else:
            parts.append(f"{symbol}: {STATUS_TEXT.get(result['status'], result['status'])}")
    return f"On {review['day']} the rule would have done this. " + "; ".join(parts) + ". Nothing was sent."


def _start() -> str:
    key, secret = load_keys("day")
    if not (key and secret):
        return _connect()
    journal = _journal()
    if journal.runner_pid() is not None:
        return "The practice day trader is already running."
    journal.dir.mkdir(parents=True, exist_ok=True)
    options = {"cwd": str(BASE_DIR), "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008 | 0x00000200
    else:
        options["start_new_session"] = True
    with open(journal.log_path, "ab") as log:
        subprocess.Popen([sys.executable, "-u", "-m", "trading.day", "run"],
                         stdout=log, stderr=log, **options)
    for _ in range(16):
        time.sleep(0.25)
        if journal.runner_pid() is not None:
            return ("The practice day trader is running. It uses fake money, buys only on a fresh "
                    "breakout, and sells everything before the close. Keep this Mac awake during "
                    "market hours.")
    return ("I started it, but couldn't confirm it is running. Check the day trader's log in the "
            "config folder; it may need its own second paper account.")


def _connect() -> str:
    return ("The day trader needs its own second free Alpaca paper account, separate from the weekly "
            "trader. Open Plugin Settings, find Alpaca paper day trading, and paste that account's key "
            "ID and secret. Please don't read the keys out loud to me.")


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
            result = ("Asked the practice day trader to stop. It sells anything it holds first."
                      if _journal().stop_runner() else "The practice day trader isn't running.")
        elif action == "pause":
            _journal().pause()
            result = "Paused. It won't make new buys, but it still sells what it holds on schedule."
        elif action == "resume":
            _journal().resume()
            result = "Resumed."
        elif action == "connect":
            result = _connect()
        else:
            result = ("I can check status, show what the rule did yesterday, start, stop, pause or "
                      "resume the practice day trader.")
    except BrokerError as exc:
        result = str(exc) if str(exc) != MISSING_DAY else _connect()
    except Exception:
        result = "Sir, the practice day trader tool hit a problem. Details are in the console."
    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result
