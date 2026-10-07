"""A careful learning loop for the day trader.

What it does, every evening after the close:
  1. Updates a local copy of recent one-minute prices (free feed, same as live).
  2. Reviews how the day went and keeps the history.
  3. Replays a few ALTERNATIVE settings on past days, in shadow: nothing live changes.
  4. Proposes (or, if you switched it on, applies) a change only if it clears a strict bar.
  5. Reverts an applied change if it turns out worse.

What it will NOT do: touch the safety limits (1% daily loss halt, size limits, the stop at the
low of the opening range, flat every night), change more than one setting at a time, change
anything more than once every 20 trading days, or act on thin data. With few trades the honest
answer is "not enough data yet", and that is what it says.

The three settings it may tune, each from a short fixed list:
    range_minutes         how long the opening range is built            5 10 15 20 30
    last_entry_minute     the last time of day a buy may trigger         12:00 13:00 14:00 (New York)
    flatten_before_close  minutes before the close everything is sold    15 30 60

The bar for a change: pick the best single-step alternative using the OLDER 70% of the days, then
it must also win on the NEWER 30% it never saw (with enough trades in both, positive profit
factor, no worse drawdown). That limits fitting to noise; it does not eliminate it, which is why
the default is to propose and wait for your OK.
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import threading
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

from trading.day.rule import DayConfig, curve_metrics, is_full_session, simulate_day, to_sessions

PARAMS: dict[str, tuple] = {
    "range_minutes": (5, 10, 15, 20, 30),
    "last_entry_minute": (12 * 60, 13 * 60, 14 * 60),
    "flatten_before_close": (15, 30, 60),
}
TRAIN_SHARE = 0.70
MIN_DAYS = 120               # trading days of prices needed before any change is even considered
MIN_TRAIN_TRADES = 60
MIN_TEST_TRADES = 30
MIN_TRAIN_GAIN = 0.01        # candidate must beat the current settings by 1 point of return on the older days
MIN_TEST_GAIN = 0.005        # ...and by half a point on the newer days it never saw
MAX_DD_WORSE = 0.02          # and its worst fall may not be more than 2 points deeper
COOLDOWN_DAYS = 20           # trading days between changes
ROLLBACK_MIN_DAYS = 20
ROLLBACK_GAP = 0.01
HISTORY_DAYS = 540
SLIP_BPS = 2.0
SYMBOLS = ("SPY", "QQQ")
_lock = threading.Lock()


# ── settings that may be tuned ───────────────────────────────────────────────
def make_cfg(base: DayConfig, overrides: dict) -> DayConfig:
    """`base` with only the allowed overrides applied. Anything else in `overrides` is ignored."""
    clean = {k: v for k, v in (overrides or {}).items() if k in PARAMS and v in PARAMS[k]}
    if "range_minutes" in clean:
        clean["min_range_bars"] = max(3, round(clean["range_minutes"] * 10 / 15))   # same share of the range as standard
    return replace(base, **clean)


def describe(overrides: dict) -> str:
    if not overrides:
        return "the standard settings"
    parts = []
    for k, v in sorted(overrides.items()):
        if k == "range_minutes":
            parts.append(f"opening range {v} min")
        elif k == "last_entry_minute":
            parts.append(f"last buy at {v // 60}:{v % 60:02d}")
        elif k == "flatten_before_close":
            parts.append(f"sell {v} min before the close")
    return ", ".join(parts)


# ── state file ───────────────────────────────────────────────────────────────
def _dir() -> Path:
    from trading.day.store import day_dir
    return day_dir()


def _state_path(directory: Optional[Path] = None) -> Path:
    return Path(directory or _dir()) / "learning.json"


def load(directory: Optional[Path] = None) -> dict:
    base = {"mode": "propose", "applied": {}, "pending": None, "history": [], "last_change_day": None,
            "last_run_day": None, "previous": None}
    try:
        data = json.loads(_state_path(directory).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base.update(data)
    except (OSError, ValueError):
        pass
    if base["mode"] not in ("propose", "auto", "off"):
        base["mode"] = "propose"
    base["applied"] = {k: v for k, v in (base.get("applied") or {}).items() if k in PARAMS and v in PARAMS[k]}
    return base


def save(state: dict, directory: Optional[Path] = None) -> None:
    path = _state_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="learning-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def overrides(directory: Optional[Path] = None) -> dict:
    s = load(directory)
    return {} if s["mode"] == "off" else dict(s["applied"])


def effective_cfg(base: DayConfig, directory: Optional[Path] = None) -> DayConfig:
    """The rule the live trader should use: standard plus whatever has been approved."""
    return make_cfg(base, overrides(directory))


def _note(state: dict, day: str, kind: str, text: str, **extra) -> None:
    state["history"].append({"day": day, "kind": kind, "text": text, **extra})
    state["history"] = state["history"][-200:]


# ── price cache ──────────────────────────────────────────────────────────────
def _cache_path(directory: Optional[Path] = None) -> Path:
    return Path(directory or _dir()) / "learn_bars.json.gz"


def load_sessions(directory: Optional[Path] = None) -> dict:
    try:
        with gzip.open(_cache_path(directory), "rt", encoding="utf-8") as f:
            data = json.load(f)
        return {s: {d: [tuple(b) for b in bars] for d, bars in days.items()} for s, days in data.items()}
    except (OSError, ValueError, TypeError):
        return {s: {} for s in SYMBOLS}


def save_sessions(sessions: dict, directory: Optional[Path] = None) -> None:
    path = _cache_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="bars-", dir=path.parent)
    os.close(fd)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({s: {d: [list(b) for b in bars] for d, bars in days.items()} for s, days in sessions.items()}, f,
                      separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def refresh_sessions(broker, today: date, directory: Optional[Path] = None, log: Callable[[str], None] = print) -> dict:
    """Bring the local price copy up to date (only the missing days are downloaded)."""
    sessions = load_sessions(directory)
    have = [d for s in sessions.values() for d in s]
    floor = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    start = max(floor, (date.fromisoformat(max(have)) - timedelta(days=4)).isoformat()) if have else floor
    log(f"learning: updating prices from {start}")
    raw = broker.minute_bars(list(SYMBOLS), start, None, feed="iex")
    for symbol in SYMBOLS:
        fresh = to_sessions(raw.get(symbol, []))
        sessions.setdefault(symbol, {}).update({d: b for d, b in fresh.items() if is_full_session(b)})
        sessions[symbol] = {d: b for d, b in sessions[symbol].items() if d >= floor}
    save_sessions(sessions, directory)
    return sessions


# ── replaying settings on past days ──────────────────────────────────────────
def trading_days(sessions: dict) -> list[str]:
    return sorted(d for d, bars in (sessions.get("SPY") or {}).items() if is_full_session(bars))


def evaluate(sessions: dict, cfg: DayConfig, days: list[str], *, slip_bps: float = SLIP_BPS,
             start_equity: float = 100_000.0) -> dict:
    """Results of the rule on `days`. Both funds are sized from the same start-of-day value; profits compound."""
    slip, equity = slip_bps / 10_000.0, start_equity
    curve, trades = [(days[0], equity)] if days else [], []
    for day in days:
        pnl = 0.0
        for symbol in cfg.symbols:
            bars = sessions.get(symbol, {}).get(day)
            if not bars:
                continue
            r = simulate_day(bars, cfg, equity, slip)
            if r["status"] == "traded":
                trades.append(r["trade"])
                pnl += r["trade"]["pnl"]
        equity += pnl
        curve.append((day, equity))
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    m = curve_metrics(curve) if len(curve) >= 2 else {"total_return": 0.0, "max_drawdown": 0.0}
    return {"days": len(days), "trades": len(trades), "ret": m["total_return"], "dd": abs(m["max_drawdown"]),
            "pf": (wins / losses) if losses > 0 else (None if not wins else float("inf")),
            "pnl": sum(t["pnl"] for t in trades)}


def split(days: list[str]) -> tuple[list[str], list[str]]:
    cut = int(len(days) * TRAIN_SHARE)
    return days[:cut], days[cut:]


def candidates(current: dict) -> list[dict]:
    """Every setting that differs from `current` in exactly one place, by one step on its list."""
    out = []
    for key, values in PARAMS.items():
        now = current.get(key, getattr(DayConfig(), key))
        if now not in values:
            continue
        i = values.index(now)
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                out.append({**current, key: values[j]})
    return out


def _trading_days_between(days: list[str], since: Optional[str], until: str) -> int:
    return sum(1 for d in days if (since or "") < d <= until)


def search(sessions: dict, current: dict, base: DayConfig = DayConfig(), today: Optional[str] = None,
           last_change_day: Optional[str] = None) -> dict:
    """Should the settings change? {"verdict": keep|change|not_enough_data|cooldown, "reason", ...}."""
    days = trading_days(sessions)
    if len(days) < MIN_DAYS:
        return {"verdict": "not_enough_data", "reason": f"only {len(days)} full trading days of prices; "
                f"{MIN_DAYS} are needed before any change is considered"}
    if last_change_day and _trading_days_between(days, last_change_day, days[-1]) < COOLDOWN_DAYS:
        return {"verdict": "cooldown", "reason": f"settings changed on {last_change_day}; waiting "
                f"{COOLDOWN_DAYS} trading days before looking again"}
    train, test = split(days)
    cur_cfg = make_cfg(base, current)
    cur_train, cur_test = evaluate(sessions, cur_cfg, train), evaluate(sessions, cur_cfg, test)
    if cur_train["trades"] < MIN_TRAIN_TRADES or cur_test["trades"] < MIN_TEST_TRADES:
        return {"verdict": "not_enough_data", "reason": f"only {cur_train['trades']} older and {cur_test['trades']} "
                f"newer trades; at least {MIN_TRAIN_TRADES} and {MIN_TEST_TRADES} are needed"}
    best, best_train = None, None
    for cand in candidates(current):
        tr = evaluate(sessions, make_cfg(base, cand), train)
        if tr["trades"] < MIN_TRAIN_TRADES:
            continue
        if best is None or tr["ret"] > best_train["ret"]:
            best, best_train = cand, tr
    base_info = {"current": describe(current), "current_train": cur_train, "current_test": cur_test}
    if best is None or best_train["ret"] - cur_train["ret"] < MIN_TRAIN_GAIN:
        return {"verdict": "keep", "reason": "no single-step alternative beat the current settings by enough on the "
                "older days", **base_info}
    new_test = evaluate(sessions, make_cfg(base, best), test)
    why_not = []
    if new_test["trades"] < MIN_TEST_TRADES:
        why_not.append("too few trades on the newer days")
    if new_test["ret"] - cur_test["ret"] < MIN_TEST_GAIN:
        why_not.append(f"it did not win on the newer days it hadn't seen ({new_test['ret']:+.1%} vs {cur_test['ret']:+.1%})")
    if new_test["pf"] is not None and new_test["pf"] <= 1.0:
        why_not.append("its profit factor on the newer days was not above 1")
    if new_test["dd"] - cur_test["dd"] > MAX_DD_WORSE:
        why_not.append("its worst fall was noticeably deeper")
    out = {**base_info, "candidate": best, "candidate_text": describe(best), "candidate_train": best_train,
           "candidate_test": new_test}
    if why_not:
        return {"verdict": "keep", "reason": f"{describe(best)} looked better on older days but was rejected: "
                + "; ".join(why_not), **out}
    return {"verdict": "change", "reason": f"{describe(best)} beat the current settings on the older days "
            f"({best_train['ret']:+.1%} vs {cur_train['ret']:+.1%}) and on newer days it hadn't been picked on "
            f"({new_test['ret']:+.1%} vs {cur_test['ret']:+.1%})", **out}


def rollback_check(sessions: dict, state: dict, base: DayConfig = DayConfig()) -> Optional[str]:
    """Reason to undo the last applied change, or None."""
    if not state.get("previous") and state.get("previous") != {}:
        return None
    since = state.get("last_change_day")
    days = [d for d in trading_days(sessions) if since and d > since]
    if len(days) < ROLLBACK_MIN_DAYS:
        return None
    new = evaluate(sessions, make_cfg(base, state["applied"]), days)
    old = evaluate(sessions, make_cfg(base, state["previous"] or {}), days)
    if new["ret"] < 0 and old["ret"] - new["ret"] > ROLLBACK_GAP:
        return (f"since {since} the new settings returned {new['ret']:+.1%} against {old['ret']:+.1%} for the "
                f"previous ones over the same {len(days)} days")
    return None


# ── actions ──────────────────────────────────────────────────────────────────
def apply_pending(state: dict, day: str, by: str) -> bool:
    p = state.get("pending")
    if not p:
        return False
    state["previous"] = dict(state["applied"])
    state["applied"] = {k: v for k, v in p["overrides"].items() if k in PARAMS and v in PARAMS[k]}
    state["last_change_day"] = day
    state["pending"] = None
    _note(state, day, "applied", f"Now using {describe(state['applied'])} ({by}).")
    return True


def revert(state: dict, day: str, why: str) -> bool:
    if state.get("previous") is None:
        return False
    state["applied"] = {k: v for k, v in (state["previous"] or {}).items() if k in PARAMS}
    state["previous"] = None
    state["last_change_day"] = day
    _note(state, day, "reverted", f"Went back to {describe(state['applied'])}: {why}")
    return True


def review_day(journal, day: str) -> dict:
    """What the live trader did on `day`, from the journal."""
    entries = [e for e in journal.events("entry") if e.get("day") == day]
    results = {}
    for e in journal.events("trade_result"):
        if e.get("day") == day:
            results[e.get("symbol")] = e
    pnl = [r.get("pnl") for r in results.values() if isinstance(r.get("pnl"), (int, float))]
    return {"day": day, "entries": len(entries), "results": len(results), "pnl": round(sum(pnl), 2) if pnl else None}


def nightly(broker, journal, day: str, *, log: Callable[[str], None] = print, directory: Optional[Path] = None,
            refresh=None, base: DayConfig = DayConfig()) -> dict:
    """The evening routine. Never raises: a failure here must not touch live trading."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        state = load(directory)
        if state["mode"] == "off":
            return {"skipped": "learning is off"}
        if state.get("last_run_day") == day:
            return {"skipped": "already ran today"}
        rev = review_day(journal, day)
        pnl_txt = f"{rev['pnl']:+,.0f} dollars" if rev["pnl"] is not None else "no recorded result"
        _note(state, day, "review", f"{rev['entries']} entries; {pnl_txt}.")
        sessions = (refresh or refresh_sessions)(broker, date.fromisoformat(day), directory, log)
        result = {"verdict": "keep", "reason": ""}
        reason = rollback_check(sessions, state, base)
        if reason:
            revert(state, day, reason)
            result = {"verdict": "reverted", "reason": reason}
        elif state.get("pending"):
            result = {"verdict": "waiting", "reason": f"a proposal is waiting for your OK: {state['pending']['text']}"}
        else:
            result = search(sessions, state["applied"], base, day, state.get("last_change_day"))
            if result["verdict"] == "change":
                prop = {"overrides": result["candidate"], "text": result["reason"], "made_on": day}
                state["pending"] = prop
                _note(state, day, "proposed", result["reason"])
                if state["mode"] == "auto":
                    apply_pending(state, day, "applied automatically")
            else:
                _note(state, day, "tested", f"{result['verdict']}: {result['reason']}")
        state["last_run_day"] = day
        save(state, directory)
        log(f"learning: {result['verdict']}: {result['reason']}")
        return result
    except Exception as exc:                                  # noqa: BLE001
        log(f"learning: skipped ({type(exc).__name__}); live trading is unaffected")
        return {"skipped": type(exc).__name__}
    finally:
        _lock.release()


def status_text(state: Optional[dict] = None, directory: Optional[Path] = None) -> str:
    s = state or load(directory)
    lines = [f"Learning mode: {s['mode']} ({'proposes changes and waits for your OK' if s['mode'] == 'propose' else 'applies changes that pass the bar' if s['mode'] == 'auto' else 'does nothing'})",
             f"Settings in use: {describe(s['applied'])}"]
    if s.get("pending"):
        lines.append(f"Waiting for your OK: {s['pending']['text']}")
        lines.append("  approve with `learn apply`, decline with `learn reject`")
    if s.get("last_change_day"):
        lines.append(f"Last change: {s['last_change_day']}")
    lines.append("Safety limits never change: 1% daily loss halt, size limits, stop at the opening-range low, flat every night.")
    recent = s["history"][-8:]
    if recent:
        lines += ["", "Recent:"] + [f"  {h['day']}  {h['kind']}: {h['text']}" for h in recent]
    return "\n".join(lines)
