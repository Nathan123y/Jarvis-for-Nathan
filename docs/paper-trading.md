# Practice trading with Jarvis (fake money)

Jarvis can run an automatic stock-trading experiment on an Alpaca **paper**
account: real market prices, fake money. The point is to find out, with no risk,
whether the rule beats simply holding the market.

Looking for the same-day version? See [day-trading.md](day-trading.md). It is a separate
experiment with its own paper account.

**This cannot touch real money.** The code only accepts Alpaca's paper-trading
address and refuses any other, and there is no setting that changes that.

## One-time setup

1. Make a free account at alpaca.markets and open its **Paper Trading** area.
   The practice account starts with $100,000 of fake money and needs no deposit.
2. In the paper area, generate API keys. You get a **key ID** and a **secret**.
   The secret is shown once, so copy it straight away.
3. Open Jarvis **Plugin Settings**, find **Alpaca paper trading**, paste both,
   and save. (Or set `ALPACA_PAPER_KEY_ID` and `ALPACA_PAPER_SECRET_KEY` in the
   terminal.) The keys are stored in `config/api_keys.json`, which git ignores.
   Never paste them into chat or a screenshot.
4. In the project folder run: `python3 -m trading check`

   Each line should say `ok`. If one says `FAIL`, it names the problem and
   nothing has been sent anywhere.

## Look before you leap

```
python3 -m trading plan        # today's decision, with nothing sent
python3 -m trading backtest    # the rule replayed over about 8 past years
```

`backtest` prints the rule next to simply holding SPY, year by year, so you can
see the bad years too. It is a replay, not a forecast.

The free IEX price feed only reaches back to mid-2020 for most funds, so the
default replay is about five years and contains a single down year (2022). For a
longer replay, including the 2018 and early-2020 drops, try
`python3 -m trading backtest --feed sip`. That asks Alpaca for the full-market
history, which the free plan allows for data older than 15 minutes. If Alpaca
refuses, it says so and nothing is harmed.

## Start it

```
python3 -m trading run
```

Leave that terminal open. Or say **"start the practice trader"** and Jarvis
starts it in the background. It decides once per trading day, a little after the
market has been open half an hour. The Mac must be awake and online then; if it
was asleep, the trader catches up the moment it wakes, as long as that is
before the last twenty minutes of the session.

## What the rule does

Six broad funds: SPY (US large companies), QQQ (US tech-heavy), IWM (US small
companies), EFA (overseas developed markets), TLT (long government bonds) and
GLD (gold).

- A fund qualifies only if it trades above its 200-day average **and** is up over
  the last six months.
- It holds the strongest three that qualify, an equal share each.
- Anything without a qualifier stays in cash. In a broad sell-off that can be
  mostly cash.
- It checks once a week (the first trading day of the week), so it trades rarely.
  `--rebalance monthly` or `daily` changes that.

Every number was fixed in advance from standard textbook values, not tuned to
recent prices. The rule uses only finished daily prices, never a half-finished day.

## Reading the results

```
python3 -m trading report
```

or ask Jarvis **"how is the practice trader doing?"** (answered instantly from
the local record, no waiting).

The report always shows Jarvis **against just holding SPY** from the same
moment. In a rising market nearly anything makes money, so "up 2%" means little
if SPY is up 3%.

**A few weeks cannot tell you whether it works.** Over weeks, results are mostly
luck in either direction. The report says "too early" until 60 trading days
(about three months) and then still calls it a short sample. Judge by three
things over months: did it beat SPY, did it fall less in the bad stretches, and
does the backtest agree.

## Safety limits (all in `trading/risk.py`)

| Limit | Default |
| --- | --- |
| Only these six funds can ever be traded | on |
| No short selling: a sell never exceeds what is held | on |
| No borrowing: a buy never exceeds cash, minus a 2% cushion | on |
| Biggest single fund / single order | 40% of the account |
| Orders per day | 12 |
| After the account falls 3% in a day | no new buying until tomorrow |
| A fund bought today | not sold today (avoids accidental day trades) |
| Blocked account | trades nothing |

Sells go first and are allowed to fill before buys are sized from the fresh cash
figure. Each order has an ID made from the date, fund and side, so a crash and
restart cannot send the same order twice.

## Controls

| Say or run | What happens |
| --- | --- |
| "pause trading" / `python3 -m trading pause` | Stops sending orders; keeps watching |
| "resume trading" / `python3 -m trading resume` | Starts again |
| "stop the trading bot" / `python3 -m trading stop` | Ends the background process |
| `python3 -m trading run --once` | One pass, then exit (good for testing) |

To start the experiment over, stop the trader and delete the `config/trading/`
folder; the comparison restarts from whatever the account is worth that day. For
a clean $100,000, make a fresh paper account in the Alpaca dashboard and paste
its new keys.

## What gets stored

`config/trading/` holds numbers, ticker symbols and short labels: the decisions,
orders, daily account values, and a log of the background process when Jarvis
starts it for you. No keys, no
conversations. Git ignores the folder.

## Real money

There is no real-money mode, on purpose. If a few months of practice results
look good, going live should be its own separate, reviewed change, with small
fixed dollar caps. Things to weigh first: you can lose money, taxes apply to
gains, and practice fills are more forgiving than real ones (the paper account
ignores slippage, fees and dividends). This is a software experiment, not
financial advice.

## If something goes wrong

- `check` says the keys were rejected: re-copy them from the **paper** area;
  live-account keys will not work here.
- Nothing happens on a trading day: run `python3 -m trading plan`. When Jarvis
  started the trader, its status log is `config/trading/runner.log`.
- Jarvis says it is not running: the Mac may have slept or restarted. Say
  "start the practice trader" again; it picks up where it left off.
