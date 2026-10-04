"""Paper-trading engine for Jarvis.

Practice money only. This package talks to Alpaca's *paper* endpoint, which is
hard-wired in broker.py and refuses any other address, so nothing here can move
real money. It runs as its own process (``python3 -m trading run``) and never
touches Jarvis's voice loop, so it cannot add lag to speech.

    broker.py     Alpaca paper REST client (keys never appear in errors)
    strategy.py   trend + momentum rotation, backtest, rebalance schedule
    risk.py       order planning and the hard limits no strategy can override
    journal.py    local numbers-only record, pause switch, run lock
    report.py     results versus simply holding SPY
    runner.py     the once-a-day decision cycle and the polling loop
    __main__.py   the command line: check, plan, run, report, backtest, ...
"""
