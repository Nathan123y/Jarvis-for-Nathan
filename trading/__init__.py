"""Paper-trading engine for Jarvis: the practice day trader.

Practice money only. This package talks to Alpaca's *paper* endpoint, which is
hard-wired in broker.py and refuses any other address, so nothing here can move
real money. It runs as its own process (``python3 -m trading.day run``) and never
touches Jarvis's voice loop, so it cannot add lag to speech.

    broker.py       Alpaca paper REST client (keys never appear in errors)
    credentials.py  where the paper keys come from, and shape-only hints about them
    journal.py      local numbers-only record, pause switch, run lock
    day/            the day trader itself (rule, live runner, report, command line)
"""
