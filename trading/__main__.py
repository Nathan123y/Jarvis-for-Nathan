"""Command line for the paper trader:  python3 -m trading <command>

    check       test the keys and each connection, sending nothing
    plan        show what the daily decision would do right now, sending nothing
    run         start the automatic paper trader (leave it running; Ctrl+C stops it)
    report      results so far, next to simply holding SPY
    backtest    replay the rule over past years, next to simply holding SPY
    pause       stop sending orders (the trader keeps watching)
    resume      start sending orders again
    stop        stop a runner that is going in the background
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from datetime import date, datetime, timedelta

from trading import report as reports
from trading.broker import BrokerError
from trading.credentials import make_broker
from trading.journal import Journal
from trading.runner import Runner
from trading.strategy import UNIVERSE, StrategyConfig, backtest


def _stamp(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _usd(value: float) -> str:
    return f"${value:,.0f}"


def cmd_check(args) -> int:
    broker = make_broker()
    ok = True
    for label, call in (
        ("keys and account", lambda: broker.account()),
        ("market clock", lambda: broker.clock()),
        ("positions", lambda: broker.positions()),
        ("price data", lambda: broker.latest_prices(["SPY"])),
        ("price history", lambda: broker.daily_bars(["SPY"], (date.today() - timedelta(days=20)).isoformat())),
    ):
        try:
            result = call()
        except BrokerError as exc:
            ok = False
            print(f"  FAIL  {label}: {exc}")
            continue
        detail = ""
        if label == "keys and account":
            detail = f" (status {result['status'] or 'unknown'}, value {_usd(result['equity'])}, practice money only)"
        elif label == "market clock":
            detail = " (market is open)" if result["is_open"] else " (market is closed right now)"
        elif label == "price data" and not result:
            ok = False
            print(f"  FAIL  {label}: no price came back")
            continue
        elif label == "price history":
            if not result.get("SPY"):
                ok = False
                print(f"  FAIL  {label}: no bars came back")
                continue
            detail = f" ({len(result['SPY'])} recent daily bars)"
        print(f"  ok    {label}{detail}")
    print("\nAll good. Next: python3 -m trading plan" if ok else
          "\nSomething above failed. Fix that first; nothing was sent to Alpaca.")
    return 0 if ok else 1


def cmd_plan(args) -> int:
    runner = Runner(make_broker(), Journal(), rebalance=args.rebalance, log=_stamp)
    plan, clock = runner.plan_now()
    if plan is None:
        print("Not enough price history came back to judge the trend yet.")
        return 1
    print(f"Signals as of {plan.day}  (market {'open' if clock['is_open'] else 'closed'}):")
    for row in plan.rows:
        if row["price"] is None:
            print(f"  {row['symbol']:<4} {row['status']}")
            continue
        extra = ""
        if row["sma"]:
            extra = f"  vs 200-day average {row['price'] / row['sma'] - 1:+.1%}, 6-month {row['momentum']:+.1%}"
        print(f"  {row['symbol']:<4} {row['status']:<18} ${row['price']:,.2f}{extra}")
    print("\nWould hold: " + (", ".join(f"{s} {w:.0%}" for s, w in plan.targets.items()) or "cash only"))
    if not plan.due:
        print(f"Not a {args.rebalance} decision day for this account, so no orders would be sent today.")
        return 0
    for order in plan.sells + plan.buys:
        amount = "all shares" if order.qty else _usd(order.dollars)
        print(f"  would {order.side} {order.symbol}: {amount} ({order.why})")
    for order, reason in plan.rejected:
        print(f"  blocked {order.side} {order.symbol}: {reason}")
    if not (plan.sells or plan.buys):
        print("No orders needed; the account already matches.")
    print("\nNothing was sent. `run` makes this decision automatically once per trading day.")
    return 0


def cmd_run(args) -> int:
    journal = Journal()
    broker = make_broker()
    if not journal.claim_runner():
        print(f"The paper trader is already running (process {journal.runner_pid()}). "
              "Use `python3 -m trading stop` first if you want to restart it.")
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    runner = Runner(broker, journal, rebalance=args.rebalance, log=_stamp)
    try:
        if args.once:
            print(runner.step())
            return 0
        _stamp("Paper trader running with practice money only. Ctrl+C stops it.")
        _stamp("It decides once per trading day. Keep this Mac awake and online during market hours.")
        runner.run_forever(stop)
    except KeyboardInterrupt:
        pass
    finally:
        journal.release_runner()
    _stamp("Stopped.")
    return 0


def cmd_report(args) -> int:
    journal = Journal()
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
    print(reports.render(reports.build(state, snapshots, account=account, positions=positions, spy_price=spy)))
    return 0


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def cmd_backtest(args) -> int:
    start = (date.today() - timedelta(days=int(args.years * 365.25))).isoformat()
    print(f"Fetching daily prices from {start} (free IEX data, adjusted for splits and dividends)...")
    series = make_broker().daily_bars(list(UNIVERSE), start)
    try:
        result = backtest(series, StrategyConfig(), rebalance=args.rebalance, slippage_bps=args.slippage_bps)
    except ValueError as exc:
        print(f"Could not run the backtest: {exc}")
        return 1
    mine, base = result["strategy"], result["benchmark"]
    print(f"\n{result['start']} to {result['end']}, decisions {result['rebalance']}, "
          f"{result['slippage_bps']:g} bp cost per dollar traded, {result['rebalances']} rebalances, "
          f"invested {result['time_invested']:.0%} of days\n")
    print(f"{'':<22}{'This rule':>12}{'Hold SPY':>12}")
    for label, key in (("Total return", "total_return"), ("Per year (CAGR)", "cagr"),
                       ("Worst fall from a high", "max_drawdown")):
        print(f"{label:<22}{_pct(mine[key]):>12}{_pct(base[key]):>12}")
    print(f"\n{'Year':<8}{'This rule':>12}{'Hold SPY':>12}")
    for year in sorted(result["strategy_by_year"]):
        print(f"{year:<8}{_pct(result['strategy_by_year'][year]):>12}"
              f"{_pct(result['benchmark_by_year'].get(year, 0.0)):>12}")
    print("\nRead this carefully: it is a replay, not a forecast. The rule's numbers were fixed before"
          "\nany result was seen, but the period is one slice of history, prices are from the IEX"
          "\nexchange only, and real fills can differ. A rule like this usually gives up some gain in"
          "\nsteady rises to fall less in crashes; check whether the years above show that trade.")
    return 0


def cmd_pause(args) -> int:
    Journal().pause()
    print("Paused. The trader keeps watching but will send no orders. `resume` to continue.")
    return 0


def cmd_resume(args) -> int:
    Journal().resume()
    print("Resumed.")
    return 0


def cmd_stop(args) -> int:
    journal = Journal()
    if journal.stop_runner():
        print("Asked the background paper trader to stop.")
    else:
        print("No background paper trader is running.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trading", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, helptext in (("check", cmd_check, "test keys and connections"),
                                    ("plan", cmd_plan, "show today's decision without trading"),
                                    ("run", cmd_run, "run the automatic paper trader"),
                                    ("report", cmd_report, "results versus holding SPY"),
                                    ("backtest", cmd_backtest, "replay the rule over past years"),
                                    ("pause", cmd_pause, "stop sending orders"),
                                    ("resume", cmd_resume, "resume sending orders"),
                                    ("stop", cmd_stop, "stop the background trader")):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(handler=handler)
        if name in ("plan", "run", "backtest"):
            p.add_argument("--rebalance", choices=("weekly", "monthly", "daily"), default="weekly")
        if name == "run":
            p.add_argument("--once", action="store_true", help="do one pass and exit")
        if name == "backtest":
            p.add_argument("--years", type=float, default=8.0)
            p.add_argument("--slippage-bps", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except BrokerError as exc:
        print(f"\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
