"""Where the day trader keeps its record: config/trading_day/ (git-ignored).

Holds the decisions, entries, results, daily account values, the analyst's plans (when it is
on), the pause switch, the run lock and the background process's log. No keys, no
conversations. Delete the folder to start the comparison over.
"""
from __future__ import annotations

from pathlib import Path

from trading.journal import Journal


def day_dir() -> Path:
    from memory.config_manager import CONFIG_DIR
    return CONFIG_DIR / "trading_day"


def day_journal() -> Journal:
    return Journal(day_dir())
