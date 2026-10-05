# Practice day trading with Jarvis (fake money)

Jarvis can run an automatic day-trading experiment on an Alpaca **paper** account: it buys
and sells within a single day, using real market prices and fake money. (An earlier, slower
once-a-week trader was removed; this is the only trader now.)

**Please read this first.** Day trading is the hardest version of this game. Most people who
try it lose money after costs, and a practice account hides some of those costs. The point of
this experiment is to find out, with no risk, whether this particular rule survives them.
Expect "no" to be a perfectly normal answer.

**This cannot touch real money.** The code only accepts Alpaca's paper-trading address and
refuses any other, and there is no setting that changes that.

## One-time setup

The day trader sells whatever SPY or QQQ it finds at the end of every day, so give it a paper
account of its own with nothing else in it.

1. In the Alpaca **Paper Trading** area, pick (or create) a paper account with **at least
   $25,000, ideally $100,000**. Alpaca's docs say the first paper account gets $100,000 by
   default; a new one can start with less (one came up with $10,000), and a balance cannot
   be changed after the account is created. If an account is too small, delete it and
   create it again with the right amount. The day trader will not trade below $25,000, and
   `check` tells you if the balance is too low. (To create one: click the paper account
   number in the upper left of the dashboard and choose "Open New Paper Account".)
2. In that account, generate API keys. You get a key ID and a secret. The secret is shown
   once. Generating keys again on the same account cancels the earlier pair.
3. Open Jarvis **Plugin Settings**, find **Alpaca paper day trading**, paste both, and save.
   (Or set `ALPACA_PAPER_KEY_ID` and `ALPACA_PAPER_SECRET_KEY`.) Never paste keys into chat
   or a screenshot.
4. Run `python3 -m trading.day check`. Every line should say `ok`.

Two guards keep it to its own account: `check` refuses an account that holds anything other
than SPY or QQQ, and while running, if the account ever holds something else it stops
buying and sells only the shares it bought itself that day. It also remembers which account
it started on and touches nothing if that ever changes.

## Look before you leap

```
python3 -m trading.day plan        # what the rule did in the latest full session
python3 -m trading.day backtest    # the rule replayed over about 3 past years (takes a few minutes)
```

`backtest` prints the rule next to simply holding SPY, year by year, with the trade count,
win rate and average win and loss. It then reruns the same rule with higher costs, because
a day trader's result is mostly decided by how much each trade costs. **Run it before you
start the trader, and be ready for it to look unimpressive.** It is a replay, not a forecast.

## Start it

```
python3 -m trading.day run
```

or say **"start the practice day trader"** and Jarvis starts it in the background. Leave the
Mac awake and online during market hours (a closed lid puts it to sleep). Closing Jarvis does
not stop it.

## What the rule does

It trades only SPY and QQQ, in New York time:

1. Watches the first 15 minutes after the open (9:30 to 9:45) and notes the highest and lowest
   price.
2. After 9:45, the first time a one-minute candle **closes above that high**, it buys. One buy
   per fund per day, and a candle that starts at 2:00 pm or later cannot trigger one (so the
   last possible buy goes in a few seconds after 2:00). It only acts on a breakout that just
   happened; it never chases an old one.
3. A protective stop at the **low of the opening range** is placed at Alpaca with the buy, so
   it keeps working even if the Mac sleeps.
4. Sized so a stopped-out trade costs about 0.25% of the account, never more than 25% of the
   account in one fund, whole shares only.
5. Anything still held at 3:45 pm is sold. Nothing is held overnight, ever.

It sits out early-close days, days with a missing opening range, a very narrow range (no
room to be wrong) and a stop more than 1.5% away. Every number was fixed from round values
before any result was seen, not tuned to history.

## Safety limits

| Limit | What happens |
| --- | --- |
| Only SPY and QQQ | Anything else in the account: it stops buying and sells only shares it bought itself today |
| Under $25,000 in the account | Does not trade (every trade here is a day trade, which brokers restrict at that size) |
| Account identity | Remembered on first run; if it changes, the trader touches nothing |
| No short selling, no borrowing | Buys only; sizes capped by the rule and by cash |
| Account down 1% on the day | Sells everything, no more buying until tomorrow |
| Protective stop on every buy | Lives at Alpaca, so Jarvis or the Mac can be off |
| Breakout older than 90 seconds | Skipped, not chased |
| Price already through the stop, or 0.5% above the signal | Skipped |
| Same buy twice | Impossible: the order ID is made from the date and fund |
| Shares found from an earlier day | Sold first thing at the next open (dedicated accounts only) |
| Selling | Fund by fund: cancel that fund's stop, sell the position. Never a "sell everything" call |
| Network or Alpaca outage | Retries every 30 seconds or faster while the market is open |
| Pause | Stops new buys only. It still sells on schedule, so a position is never stranded |
| Stop (voice, `stop` or Ctrl+C) | Sells what it bought itself, if the market is open, then exits |

## Reading the results

```
python3 -m trading.day report
```

or ask Jarvis **"how is the day trader doing?"** (answered instantly from the local record).

The report shows the result as Alpaca recorded it, **and an estimate after costs**, next to
just holding SPY. Alpaca's practice account does not charge the spread, fees or slippage a
real account pays, and a day trader trades every day, so the after-cost line is the one to
believe. It says "too early" until 60 trading days (about three months).

## Real money

There is no real-money mode, on purpose. Before anyone considers one: US brokers limit
accounts under $25,000 to three day trades in any five business days (the "pattern day
trader" rule), which this rule would hit within a week; real fills cost more than practice
fills; and you can lose money. A few weeks of practice results prove very little either way.
This is a software experiment, not financial advice.

## What gets stored

`config/trading_day/` holds numbers, ticker symbols and short labels: decisions, entries,
results, daily account values and a log of the background process. No keys, no conversations.
Git ignores the folder. Delete it to start the comparison over.

## If something goes wrong

- `check` says the account holds other funds: use a fresh paper account with nothing in it.
- `check` says the keys were rejected (HTTP 401): the keys were cancelled (generating a new pair
  on the same account does that) or came from a different account. Generate a fresh pair on the
  account you mean to use and paste both again.
- Nothing happens on a trading day: run `python3 -m trading.day plan`, then look at
  `config/trading_day/runner.log` (the background process writes there).
- Jarvis says it is not running: the Mac may have slept or restarted. Say "start the practice
  day trader" again. Anything it was holding is sold first thing.
- Not yet confirmed against a live Alpaca account: the protective-stop order type, and that
  Alpaca rejects a repeated order id. If Alpaca refuses the stop order, the trader records the
  refusal, buys nothing and gives up on that fund for the day (it fails safe, and `runner.log`
  shows it). The first real session is the first real test, so watch it.
- Known differences from the replay: real stops are triggered by Alpaca's own price feed, not the
  candles the backtest uses, and the live trader skips a breakout whose price has already run
  away, which the backtest does not model.
