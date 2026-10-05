"""Command line for the practice DAY trader:  python3 -m trading.day <command>

    check       test the keys and each connection, sending nothing
    plan        show what the rule did (or would have done) in the latest full session
    backtest    replay the rule over past years, next to simply holding SPY
    run         start the automatic day trader (leave it running; Ctrl+C stops it)
    report      results so far, next to simply holding SPY, with estimated costs
    pause       stop buying (it still sells what it holds, on schedule)
    resume      start buying again
    stop        stop a runner that is going in the background

Practice money only. Use a paper account of its own with at least $25,000 and nothing else in it.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from datetime import date, datetime, timedelta

from trading.broker import BrokerError
from trading.credentials import key_report, make_broker
from trading.day import report as reports
from trading.day.rule import (LIMIT_MAX_FUND_PCT, LIMIT_RISK_PCT, STANDARD_MAX_FUND_PCT, STANDARD_RISK_PCT,
                              STATUS_TEXT, DayConfig, backtest, size_text, sized_config, to_sessions)
from trading.day.runner import DAY_TRADING_MINIMUM, DayRunner, review_latest_session
from trading.day.store import day_journal


def _stamp(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _usd(value: float) -> str:
    return f"${value:,.0f}"


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _cfg(args) -> DayConfig:
    """The rule with whatever size settings were given on the command line (else the standard)."""
    return getattr(args, "cfg", None) or DayConfig()


def cmd_check(args) -> int:
    broker = make_broker()
    for line in key_report():
        print(f"  keys  {line}")
    print()
    ok = True
    cfg = DayConfig()
    steps = (
        ("keys and account", lambda: broker.account()),
        ("market clock", lambda: broker.clock()),
        ("positions", lambda: broker.positions()),
        ("minute prices", lambda: broker.minute_bars(
            list(cfg.symbols), (date.today() - timedelta(days=4)).isoformat() + "T00:00:00Z")),
    )
    for label, call in steps:
        try:
            result = call()
        except BrokerError as exc:
            ok = False
            print(f"  FAIL  {label}: {exc}")
            continue
        detail = ""
        if label == "keys and account":
            if result["equity"] < DAY_TRADING_MINIMUM:
                ok = False
                print(f"  FAIL  {label}: this account holds {_usd(result['equity'])} but the day trader needs at "
                      f"least {_usd(DAY_TRADING_MINIMUM)} (every trade here is a day trade, which brokers "
                      "restrict on smaller accounts), so it would sit out every day. An Alpaca paper "
                      "account's balance can't be changed once it exists: delete this paper account, create "
                      "a new one with $100,000, and generate new keys for it.")
                continue
            detail = f" (status {result['status'] or 'unknown'}, value {_usd(result['equity'])}, practice money only)"
        elif label == "market clock":
            detail = " (market is open)" if result["is_open"] else " (market is closed right now)"
        elif label == "positions":
            foreign = sorted(set(result) - set(cfg.symbols))
            if foreign:
                ok = False
                print(f"  FAIL  {label}: this account holds {', '.join(foreign)}. The day trader only "
                      "ever handles SPY and QQQ and sells everything at the end of its day, so use a "
                      "fresh paper account with nothing else in it.")
                continue
            detail = f" ({', '.join(sorted(result)) or 'none held'})"
        elif label == "minute prices":
            total = sum(len(to_sessions(result.get(s, [])).get(day, []))
                        for s in cfg.symbols for day in to_sessions(result.get(s, [])))
            if not total:
                ok = False
                print(f"  FAIL  {label}: no regular-hours candles came back")
                continue
            detail = f" ({total} regular-hours candles over the last few days)"
        print(f"  ok    {label}{detail}")
    print("\nAll good. Next: python3 -m trading.day plan, then backtest" if ok else
          "\nSomething above failed. Fix that first; nothing was sent to Alpaca.")
    return 0 if ok else 1


def cmd_plan(args) -> int:
    cfg = _cfg(args)
    review = review_latest_session(make_broker(), cfg)
    if not review["day"]:
        print("No full trading session of minute prices came back from the last week.")
        return 1
    print(f"The rule on the latest full session, {review['day']}, with a {_usd(review['equity'])} account"
          + (f" and these sizes: {size_text(cfg)}" if cfg != DayConfig() else "") + ":\n")
    for symbol, result in review["results"].items():
        text = STATUS_TEXT.get(result["status"], result["status"])
        rng = result.get("range")
        span = f" (opening range {rng[1]:.2f} to {rng[0]:.2f})" if rng else ""
        print(f"  {symbol:<4} {text}{span}")
        trade = result.get("trade")
        if trade:
            h1, m1 = divmod(trade["entered"], 60)
            h2, m2 = divmod(trade["exited"], 60)
            why = "stopped out" if trade["reason"] == "stop" else "sold before the close"
            print(f"       {trade['qty']} shares in at {trade['entry']:.2f} ({h1}:{m1:02d}), out at "
                  f"{trade['exit']:.2f} ({h2}:{m2:02d}), {why}: {'+' if trade['pnl'] >= 0 else '-'}"
                  f"${abs(trade['pnl']):,.0f}")
    print("\nThis is a replay of a finished day. Nothing was sent. `run` makes these decisions live.")
    return 0


def cmd_run(args) -> int:
    journal = day_journal()
    broker = make_broker()
    if not journal.claim_runner():
        print(f"The day trader is already running (process {journal.runner_pid()}). "
              "Use `python3 -m trading.day stop` first if you want to restart it.")
        return 1
    stop = threading.Event()
    for name in ("SIGTERM", "SIGHUP"):                       # SIGHUP: the terminal window was closed
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    cfg = _cfg(args)
    runner = DayRunner(broker, journal, cfg=cfg, log=_stamp)
    left = "flat"
    try:
        if args.once:
            print(runner.step())
            return 0
        _stamp("Day trader running with practice money only. Ctrl+C stops it.")
        _stamp(f"Sizes: {size_text(cfg)}" + (" (the standard)." if cfg == DayConfig() else " (bigger or smaller "
               "than standard: gains and losses scale with it)."))
        journal.record("config", risk_pct=round(cfg.risk_per_trade * 100, 4),
                       max_fund_pct=round(cfg.max_position_pct * 100, 4))
        _stamp("It looks every 15 seconds in market hours and sells everything before the close. "
               "Keep this Mac awake and online during the session.")
        try:
            runner.run_forever(stop)
        except KeyboardInterrupt:
            pass
        left = runner.close_out()               # a trader that was told to stop leaves the account flat
    finally:
        journal.release_runner()
    _stamp({"sold": "Stopped. It was holding shares, so it sold them first.",
            "held": "Stopped. It may still hold shares that could not be sold right now; start it again "
                    "during market hours and it sells them first thing."}.get(left, "Stopped."))
    return 0


def cmd_report(args) -> int:
    journal = day_journal()
    state = {**journal.state(), "paused": journal.paused()}
    snapshots = reports.latest_per_day(journal.events("snapshot"))
    account = positions = spy = None
    try:
        broker = make_broker()
        account, positions = broker.account(), broker.positions()
        spy = broker.latest_prices(["SPY"]).get("SPY")
    except BrokerError as exc:
        print(f"(Using the last recorded numbers: {exc})\n")
        account = positions = None
    built = reports.build(state, snapshots, journal.events("trade_result"),
                          account=account, positions=positions, spy_price=spy)
    print(reports.render(built))
    if not built["started"]:
        running = journal.runner_pid() is not None
        print("The background day trader is running and waiting for the market." if running else
              "The background day trader is not running. Start it with `python3 -m trading.day run`, "
              "or tell Jarvis \"start the practice day trader\".")
    return 0


def cmd_backtest(args) -> int:
    cfg = _cfg(args)
    today = date.today()
    start = (today - timedelta(days=int(args.years * 365.25))).isoformat()
    feed = args.feed
    kind = "IEX-exchange" if feed == "iex" else "full-market (SIP)"
    end = (today - timedelta(days=1)).isoformat() if feed == "sip" else None
    print(f"Fetching one-minute prices from {start} (free {kind} data). This takes a few minutes.")
    broker = make_broker()
    raw = broker.minute_bars(list(cfg.symbols), start, end, feed=feed,
                             progress=lambda n: print(f"  ...{n} pages", flush=True) if n % 10 == 0 else None)
    print("Sorting the candles into trading days...")
    sessions = {s: to_sessions(raw.get(s, [])) for s in cfg.symbols}
    try:
        daily = broker.daily_bars(["SPY"], (date.fromisoformat(start) - timedelta(days=7)).isoformat(),
                                  end, feed=feed).get("SPY", [])
    except BrokerError:
        daily = []
    try:
        result = backtest(sessions, cfg, slippage_bps=args.slippage_bps, benchmark_daily=daily)
    except ValueError as exc:
        print(f"Could not run the backtest: {exc}")
        return 1
    mine, base = result["strategy"], result["benchmark"]
    print(f"\n{result['start']} to {result['end']}: {result['days']} full trading days, "
          f"{result['slippage_bps']:g} bp cost on every buy and every sale")
    custom = cfg != DayConfig()
    if custom:
        print(f"Sizes: {size_text(cfg)} (standard: {STANDARD_RISK_PCT:g}% and {STANDARD_MAX_FUND_PCT:g}%).")
    print()
    print(f"{'':<24}{'This rule':>12}{'Hold SPY':>12}")
    for label, key in (("Total return", "total_return"), ("Per year (CAGR)", "cagr"),
                       ("Worst fall from a high", "max_drawdown")):
        print(f"{label:<24}{_pct(mine[key]):>12}{_pct(base[key]):>12}")
    n = result["trades"]
    print(f"\nTrades: {n} on {result['days_traded']} of {result['days']} days "
          f"({result['days_traded'] / result['days']:.0%}).")
    if n:
        pf = f", profit factor {result['profit_factor']:.2f}" if result["profit_factor"] else ""
        print(f"  {result['win_rate']:.0%} were winners; average win {_usd(result['avg_win'] or 0)}, "
              f"average loss {_usd(result['avg_loss'] or 0)}{pf}.")
        print(f"  {result['stops']} were stopped out, {result['time_exits']} sold before the close; "
              f"held about {result['avg_minutes_held']:.0f} minutes on average.")
        print(f"  Average result per trade: {'+' if result['expectancy'] >= 0 else '-'}"
              f"${abs(result['expectancy']):,.0f}.")
    if result["skipped"]:
        print("  Days with no trade: " + ", ".join(
            f"{STATUS_TEXT.get(k, k)} {v}" for k, v in sorted(result["skipped"].items(), key=lambda kv: -kv[1])) + ".")
    print(f"\n{'Year':<8}{'This rule':>12}{'Hold SPY':>12}")
    for year in sorted(result["strategy_by_year"]):
        print(f"{year:<8}{_pct(result['strategy_by_year'][year]):>12}"
              f"{_pct(result['benchmark_by_year'].get(year, 0.0)):>12}")
    harsher = max(5.0, args.slippage_bps * 2.5)
    if harsher > args.slippage_bps:
        try:
            tougher = backtest(sessions, cfg, slippage_bps=harsher, benchmark_daily=daily)["strategy"]
            print(f"\nCost check: with {harsher:g} bp per fill instead, the same rule returns "
                  f"{_pct(tougher['total_return'])} in total ({_pct(tougher['cagr'])} per year).")
        except ValueError:
            pass
    source = "the IEX exchange only" if feed == "iex" else "the consolidated market tape"
    benchmark = ("dividend-adjusted" if result["benchmark_adjusted"] else
                 "without dividends, so it understates holding SPY by about 1.3 points a year")
    fixed = ("The rule's numbers were fixed before\nany result was seen." if not custom else
             "These sizes were chosen after\nseeing a result, and picking the best-looking size from several runs only fits the past.")
    print(f"\nRead this carefully: it is a replay, not a forecast. {fixed} Candles come from {source}, "
          "so highs and lows can differ from the"
          "\nfull market. Buys fill at the next candle's open and stops at their price (or the open"
          "\nafter a gap), so real fills can be worse; real stops are also triggered by Alpaca's own"
          "\nprice feed, not these candles. The live safety checks (1% down on the day, skipping a"
          f"\nprice that already ran away) are not modelled. Hold SPY is {benchmark}. A day trader"
          "\nthat only beats SPY before costs has not beaten it: watch the cost check above.")
    return 0


def cmd_pause(args) -> int:
    day_journal().pause()
    print("Paused. The day trader will make no new buys. It still sells what it holds on schedule. "
          "`resume` to continue.")
    return 0


def cmd_resume(args) -> int:
    day_journal().resume()
    print("Resumed.")
    return 0


def cmd_stop(args) -> int:
    if day_journal().stop_runner():
        print("Asked the background day trader to stop. If it is holding shares during market hours "
              "it sells them before it exits.")
    else:
        print("No background day trader is running.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trading.day", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, helptext in (("check", cmd_check, "test keys and connections"),
                                    ("plan", cmd_plan, "what the rule did in the latest full session"),
                                    ("backtest", cmd_backtest, "replay the rule over past years"),
                                    ("run", cmd_run, "run the automatic day trader"),
                                    ("report", cmd_report, "results versus holding SPY"),
                                    ("pause", cmd_pause, "stop new buys"),
                                    ("resume", cmd_resume, "allow new buys again"),
                                    ("stop", cmd_stop, "stop the background day trader")):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(handler=handler)
        if name in ("run", "plan", "backtest"):
            p.add_argument("--risk-pct", type=float, default=STANDARD_RISK_PCT, metavar="PCT",
                           help=f"most of the account to risk on one trade, in percent "
                                f"(standard {STANDARD_RISK_PCT:g}, at most {LIMIT_RISK_PCT:g})")
            p.add_argument("--max-fund-pct", type=float, default=STANDARD_MAX_FUND_PCT, metavar="PCT",
                           help=f"most of the account to put in one fund, in percent "
                                f"(standard {STANDARD_MAX_FUND_PCT:g}, at most {LIMIT_MAX_FUND_PCT:g})")
        if name == "run":
            p.add_argument("--once", action="store_true", help="do one pass and exit")
        if name == "backtest":
            p.add_argument("--years", type=float, default=3.0)
            p.add_argument("--slippage-bps", type=float, default=2.0)
            p.add_argument("--feed", choices=("iex", "sip"), default="iex",
                           help="price feed: iex (default, same as live) or sip (full market)")
    args = parser.parse_args(argv)
    if hasattr(args, "risk_pct"):
        try:
            args.cfg = sized_config(args.risk_pct, args.max_fund_pct)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        return args.handler(args)
    except BrokerError as exc:
        print(f"\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
