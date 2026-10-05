"""The practice DAY trader: opening-range breakout on SPY and QQQ, flat every night.

    rule.py     the rule, its sizing, the minute-level replay and the backtest (pure functions)
    runner.py   the live loop: look every 15 seconds, buy a fresh breakout, sell before the close
    report.py   results next to simply holding SPY, with trading costs estimated
    store.py    where its record lives (config/trading_day/)
    __main__.py the command line: python3 -m trading.day check | plan | backtest | run | ...

Practice money only. See docs/day-trading.md.
"""
