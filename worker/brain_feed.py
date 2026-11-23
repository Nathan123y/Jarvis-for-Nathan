"""Puts the trading bots' trades into the Obsidian brain (core/vault_brain.py).

The bots live outside this repo (~/optionsbots) and keep their own records, so
this only reads them, read-only, once a minute:

  * data/native/paper.db `fills`   every paper options fill (append-only table)
  * data/memebot/trades.jsonl      every memecoin buy and sell

What has been passed on is kept in config/worker/brain_feed.json. The first run
starts at the end of each record instead of replaying the past.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from core import vault_brain

POLL_S = 60.0


def _bots_dir() -> Path:
    return Path(os.environ.get("OPTIONSBOTS_DIR", Path.home() / "optionsbots"))


def _when(ts: str):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except Exception:
        return None


def _money(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def fill_line(row: dict) -> str:
    try:
        c = json.loads(row.get("contract_json") or "{}")
    except ValueError:
        c = {}
    side = str(row.get("side", "")).replace("_", " ").lower()
    what = f"{c.get('underlying', '?')} {c.get('strike', '')}{c.get('right', '')} {c.get('expiration', '')}".strip()
    return f"{row.get('bot_id', 'bot')}: {side} {row.get('quantity')} × {what} at {row.get('price')} (paper)"


def meme_line(t: dict) -> str:
    sym = t.get("symbol", "?")
    if t.get("kind") == "buy":
        return f"memebot: bought {_money(t.get('usd'))} of {sym} at {t.get('price')} (stop {t.get('stop')})"
    if t.get("kind") == "sell":
        return f"memebot: sold {sym} ({t.get('why', '')}), P&L {_money(t.get('pnl'))}"
    return f"memebot: {t.get('kind')} {sym}"


class TradingFeed:
    def __init__(self, state_path: Path, bots_dir: Path | None = None, log=vault_brain.log):
        self.state_path = state_path
        self.bots = bots_dir or _bots_dir()
        self.log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _state(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.state_path)

    def _fills(self, state: dict) -> None:
        db = self.bots / "data" / "native" / "paper.db"
        if not db.exists():
            return
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            con.row_factory = sqlite3.Row
            if "fills_seq" not in state:
                state["fills_seq"] = con.execute("SELECT COALESCE(MAX(seq), 0) FROM fills").fetchone()[0]
                return
            rows = con.execute("SELECT * FROM fills WHERE seq > ? ORDER BY seq LIMIT 200",
                               (state["fills_seq"],)).fetchall()
        finally:
            con.close()
        for r in rows:
            row = dict(r)
            self.log("Trading", fill_line(row), when=_when(row.get("ts")))
            state["fills_seq"] = row["seq"]

    def _meme(self, state: dict) -> None:
        path = self.bots / "data" / "memebot" / "trades.jsonl"
        if not path.exists():
            return
        size = path.stat().st_size
        if "meme_offset" not in state or state["meme_offset"] > size:
            state["meme_offset"] = size
            return
        with open(path, "rb") as f:
            f.seek(state["meme_offset"])
            chunk = f.read(1_000_000)
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        for raw in chunk[:end].splitlines():
            try:
                t = json.loads(raw)
            except ValueError:
                continue
            if isinstance(t, dict):
                self.log("Trading", meme_line(t), when=_when(t.get("ts")))
        state["meme_offset"] += end + 1

    def poll(self) -> None:
        state = self._state()
        before = dict(state)
        for step in (self._fills, self._meme):
            try:
                step(state)
            except Exception as e:
                print(f"[Brain feed] {step.__name__}: {e!r}")
        if state != before:
            self._save(state)

    def start(self) -> None:
        def run():
            while not self._stop.is_set():
                self.poll()
                self._stop.wait(POLL_S)
        self._thread = threading.Thread(target=run, name="brain-trading-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
