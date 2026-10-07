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

## Start it by itself every weekday (Mac)

```
python3 -m trading.day autostart install --analyst --risk-pct 0.5 --max-fund-pct 50
python3 -m trading.day autostart status      # is it installed, is it running, last log lines
python3 -m trading.day autostart remove
```

`install` adds a small macOS start-up job (in your own `~/Library/LaunchAgents`, no admin rights) that starts
the trader Monday to Friday at 6:30 (`--at 06:15` to change it; it is this Mac's clock, and 6:30 am Pacific is
the New York open). It starts the same command you would type, with the flags you give `install`, kept awake with
`caffeinate`, in the background, logging to `config/trading_day/runner.log`. No key is stored in the job: the trader
reads them from Jarvis Plugin Settings as usual. It runs until the Mac sleeps or restarts; the next weekday
start brings it back. If it is already running (say you started it by hand), the new start just says so and exits.

What it cannot do:

- **Wake a sleeping Mac.** `install` prints one line to run yourself, once, which makes the Mac wake five minutes
  before: `sudo pmset repeat wakeorpoweron MTWRF 06:25:00`. Without it, the Mac must already be awake (a Mac left
  plugged in with the lid open and "prevent sleep" works). A start that falls while the Mac sleeps happens when it
  wakes. Clear the wake time with `sudo pmset repeat cancel`.
- Run with the Mac off, or on battery with the lid closed.
- Read a Jarvis folder inside Documents, Desktop or Downloads: macOS can block background jobs there. If
  `status` shows a permission error, move the folder or give Terminal "Full Disk Access".
- Change its flags later: run `install` again with the new ones.

Not yet confirmed on a real Mac: the launchd job. Run `autostart status` after installing, and check the log the
first morning.

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

## Bigger or smaller positions

Two size settings can be changed when you start it (and in `plan` and `backtest`, to see what a
size would have done):

```
python3 -m trading.day run --risk-pct 0.5 --max-fund-pct 50
```

| Setting | Standard | Largest allowed | Meaning |
| --- | --- | --- | --- |
| `--risk-pct` | 0.25 | 2 | most of the account to lose if the stop is hit, in percent |
| `--max-fund-pct` | 25 | 50 | most of the account to hold in one fund, in percent |

The start-up line shows the sizes in use, and the journal records them. Only the sizes change; the
entry, the stop, the sale before the close, the 1% daily-loss halt and every other safety check stay
as they are. It never borrows, so 50% in each of its two funds is the ceiling, and the second fund is
limited by the cash left after the first.

Please keep in mind what a bigger size does and does not do. It multiplies whatever the rule does,
good days and bad days alike; it does not make the rule better. On a typical day the standard 25%
cap is what limits the size, so `--max-fund-pct` is the setting that matters most. The size settings
are only for the command line: starting it by voice always uses the standard sizes. And if you run
`backtest` with other sizes, remember that picking the best-looking size from several runs only
fits the past.

## Optional: the pre-market analyst

By default the trader runs the same fixed rule on SPY and QQQ every day. With the analyst on, an AI
model does what a human trader does before the open: it looks at recent price moves and the day's news
and decides **what is worth watching today, and how strongly**. Which names it picks, and how big,
change from day to day because they depend on that day's information.

```
python3 -m trading.day analyst                       # preview today's plan; sends and saves nothing
python3 -m trading.day run --analyst                 # trade on the analyst's plan every day
python3 -m trading.day run --analyst --risk-pct 0.5 --max-fund-pct 50
python3 -m trading.day check --analyst               # also test what the analyst needs
```

How it works:

1. About 90 minutes before the open, it reads the last month of daily prices for a **fixed list of 17
   names** (SPY, QQQ, IWM, DIA, XLK, XLF, XLE, XLV and AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AMD,
   JPM) plus about two dozen recent headlines from Jarvis's news search, and asks Gemini (the key Jarvis
   already has) for a plan: up to four picks, each with a **conviction from 1 to 5** and a reason, or
   "stand aside today". Standing aside is a normal answer.
2. A pick is **only bought if the opening-range breakout then fires on it**, with that rule's protective
   stop at the low of the first 15 minutes. The analyst chooses *what* and *how big*; it never picks an
   entry price or an exit. Everything is still sold before the close.
3. Conviction scales the sizes you set (`--risk-pct`, `--max-fund-pct`): conviction 5 is the full size and
   1 is a fifth of it. Those two settings stay the ceiling. It never borrows: every buy is checked against
   the cash left after the buys before it, so several picks breaking out at once cannot add up to more than
   the account. With up to four picks, `--risk-pct` applies to each, so a bad day where every stop is hit
   can cost up to four times it (the start-up line and `analyst` print that number; the 1% daily-loss halt
   sells everything sooner if the account is down that much).
4. A single stock swings more than a fund, so a stock may have its stop up to 3% below the price (funds
   keep 1.5%); a wider stop than that is skipped.
5. The plan, its reasoning and the headlines count are written to the record (`report` is unchanged).
   Asking Jarvis "what's the analyst's plan?" reads back the latest plan's day, tickers and convictions
   only: the model's free text was written from web headlines, so it is never handed to the voice
   assistant. The reasoning is in the terminal log and the record.

What stays in code, whatever the model says: it can only choose from the fixed list, only long, at most
four picks, conviction forced into 1 to 5, its text trimmed and cleaned, and nothing it writes can change a
size, a stop or an order. Headlines come from the open web, so they are treated as untrusted text; the
worst a hostile headline can do is nudge the model toward a different name from the same list at a size the
code still caps. **If a plan cannot be made (no key, no answer, nonsense), the trader trades nothing that
day.** It makes three tries before the open and keeps three more for after it (five minutes apart, until
2:00 pm New York time), then sits the day out and says so. It never falls back to guessing.

What it cannot do: predict the market. It is an AI's reading of the day, and it can be confidently wrong.
Whether it adds anything over the plain rule is exactly what the practice account is for, and it can only
be judged on live days: replaying it over past days would let the model "know" what happened next, so there
is no honest backtest of it. Judge it over 60 or more trading days, against SPY, after costs.

Things to know:

- **Not yet confirmed against the real services:** the Gemini call, the news fetch and the quality of the
  plans. `analyst` is the first test: run it once, read the plan, and add `--show-input` to see exactly what
  the model was shown. `check --analyst` also reports, name by name, how many of the first 15 minutes have a
  price on the free feed. The rule needs at least 10, so a thinly traded name on a given day is skipped.
- The free price feed is one exchange (IEX), so individual stocks have thinner candles than the big funds.
- Starting the trader by voice always uses the plain rule and the standard sizes. Use the command line for
  `--analyst`.
- The Mac has to be awake and online a little before the open for the plan to be made in time. If the
  trader is started later, it makes the plan then, as long as it is before 2:00 pm New York time (a breakout
  that already happened is not chased). Making a plan takes up to a couple of minutes, and `stop` waits for
  that to finish (Ctrl+C does not). It cannot be holding anything while it plans.
- On an early-close day the analyst still makes a plan before the open (it cannot know yet), and the
  trader then sits the day out.
- If you restart the trader without `--analyst` (or by voice) while it holds a stock it bought under the
  analyst, it buys nothing new and still sells that stock at the 3:45 pm close, so nothing is held
  overnight.
- Keep this trader's account to itself. `check --analyst` allows the 17 names, and anything the trader
  did not buy that day is sold when the market opens.
- Options are not part of this. The analyst buys shares only.

## Safety limits

| Limit | What happens |
| --- | --- |
| Only SPY and QQQ (the analyst's fixed list when it is on) | Anything else in the account: it stops buying and sells only shares it bought itself today |
| Analyst plan | Only the fixed list, long only, at most four picks, conviction 1 to 5; no plan means no trades that day |
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
results, daily account values and a log of the background process. With the analyst on it also keeps
each day's plan in the model's own words (its outlook, the picks and its reasons; the headlines it read
are counted, not saved). No keys, no conversations. Git ignores the folder. Delete it to start the
comparison over.

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

## Learning from past days

After each day's result is recorded, the trader re-tests its own rule on up to ~18 months of
minute bars and asks whether a slightly different setting would have done better.

- Only three settings can ever change: `range_minutes` (5/10/15/20/30), `last_entry_minute`
  (12:00/13:00/14:00 ET) and `flatten_before_close` (15/30/60). Risk, position size and the daily
  loss halt are never touched.
- A change is considered only with at least 120 trading days of prices and enough trades, one step
  at a time, and it must win on older days *and* on the newest 30% it was not chosen on. At most
  one change per 20 trading days; a change that then does clearly worse is rolled back.
- Default mode is `propose`: it never changes anything by itself. You approve with `learn apply`.
- Early on it will mostly say "not enough data yet". That is expected and is not a bug.

```
python3 -m trading.day learn status        # what it knows, what it proposes
python3 -m trading.day learn run           # run the analysis now (first run downloads prices, a few minutes)
python3 -m trading.day learn mode auto     # or propose / off
python3 -m trading.day learn apply         # accept a proposal (applies from the next session)
python3 -m trading.day learn reject
python3 -m trading.day learn revert        # back to the previous settings
```
