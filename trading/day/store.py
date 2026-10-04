"""Where the day trader keeps its record: config/trading_day/ (git-ignored, numbers only).

Same files and rules as the weekly trader's record in config/trading/, in a folder of its own,
so the two never share a pause switch, a run lock or a history.
"""
from __future__ import annotations

from pathlib import Path

from trading.journal import Journal


def day_dir() -> Path:
    from memory.config_manager import CONFIG_DIR
    return CONFIG_DIR / "trading_day"


def day_journal() -> Journal:
    return Journal(day_dir())
