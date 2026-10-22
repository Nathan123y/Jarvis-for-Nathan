# Practice options trading (the optionsbots paper bots)

Jarvis has one trader: the **optionsbots** project in `~/optionsbots`, eleven rule-based bots trading QQQ, SPY and IWM
options in an Alpaca **paper** account (fake money). Its rules, risk limits, safety checks and setup are documented in
that project (`README.md`, `STATUS.md`, `docs/`). The earlier Jarvis FVG day trader was removed; this is the only one.

## One control command

Jarvis and the terminal use the same command, `~/optionsbots/scripts/bots.py` (`bots` in a terminal):

| Terminal | Say to Jarvis | What it does |
|---|---|---|
| `bots` | "how are the bots doing" | account value, today's trades, why setups were skipped, open positions, anything blocking trades |
| `bots each` | "how is each bot doing" | per-bot trades, wins and P&L |
| `bots start` | "start the trader" | starts the stack (opens Docker if needed) and keeps the Mac awake until after the close |
| `bots stop` | "stop the trader" | stops it; open positions stay open and the bots manage them when they restart |
| `bots pause` / `bots resume` | "pause / resume the bots" | blocks / allows new entries; exits keep being managed |

The bots also start by themselves at 6:25 am Pacific on weekdays (launchd job `com.optionsbots.paper-start`).

**Never open or close positions at Alpaca outside the bots** (by hand, by script, or by asking an assistant). The bots
then see books that don't match the account and refuse every new trade until their database is reset.

The "while you were away" digest and the money tracker read the bots' closed trades and account value through
`bots brief`. Nothing here can trade real money.
