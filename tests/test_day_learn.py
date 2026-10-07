import json
import random
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from trading.day import learn
from trading.day.rule import DayConfig, backtest, to_sessions
from trading.journal import Journal

from day_fakes import DAY, DayFakeBroker
from test_day_runner import RunnerCase


def fake_sessions(n_days=160, seed=3):
    """n full trading days of drifting random-walk minute bars for SPY and QQQ."""
    rnd = random.Random(seed)
    out = {"SPY": {}, "QQQ": {}}
    day0 = 20240102
    for d in range(n_days):
        day = f"2024-{1 + d // 28:02d}-{1 + d % 28:02d}" if d < 12 * 28 else f"2025-{1 + (d - 336) // 28:02d}-{1 + (d - 336) % 28:02d}"
        for sym in out:
            price, bars = 400.0, []
            for m in range(570, 960):
                o = price
                c = o * (1 + rnd.gauss(0.00002, 0.0006))
                bars.append((m, o, max(o, c) * 1.0003, min(o, c) * 0.9997, c))
                price = c
            out[sym][day] = bars
    return out


class SettingsTests(unittest.TestCase):
    def test_only_allowed_values_are_applied(self):
        cfg = learn.make_cfg(DayConfig(), {"range_minutes": 10, "last_entry_minute": 780})
        self.assertEqual((cfg.range_minutes, cfg.last_entry_minute), (10, 780))
        junk = learn.make_cfg(DayConfig(), {"range_minutes": 7, "daily_loss_halt": 0.5, "risk_per_trade": 0.5, "evil": 1})
        self.assertEqual(junk, DayConfig())

    def test_safety_limits_are_not_tunable(self):
        for name in ("daily_loss_halt", "risk_per_trade", "max_position_pct", "max_risk_pct", "min_range_pct", "symbols"):
            self.assertNotIn(name, learn.PARAMS)
        cfg = learn.make_cfg(DayConfig(), {k: v[0] for k, v in learn.PARAMS.items()})
        self.assertEqual((cfg.daily_loss_halt, cfg.risk_per_trade, cfg.max_position_pct), (0.01, 0.0025, 0.25))

    def test_candidates_change_exactly_one_setting_by_one_step(self):
        for cand in learn.candidates({"range_minutes": 10}):
            diffs = [k for k in learn.PARAMS if cand.get(k, getattr(DayConfig(), k)) != ({"range_minutes": 10}).get(k, getattr(DayConfig(), k))]
            self.assertEqual(len(diffs), 1)
        self.assertEqual({c["range_minutes"] for c in learn.candidates({"range_minutes": 10}) if c["range_minutes"] != 10}, {5, 15})

    def test_a_tampered_state_file_cannot_smuggle_in_settings(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "learning.json").write_text(json.dumps({"mode": "yolo", "applied": {"range_minutes": 3, "daily_loss_halt": 9, "flatten_before_close": 30}}))
            s = learn.load(Path(d))
            self.assertEqual(s["mode"], "propose")
            self.assertEqual(s["applied"], {"flatten_before_close": 30})
            Path(d, "learning.json").write_text("not json")
            self.assertEqual(learn.load(Path(d))["applied"], {})

    def test_off_means_no_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            s = learn.load(Path(d)); s["applied"] = {"range_minutes": 10}; s["mode"] = "off"; learn.save(s, Path(d))
            self.assertEqual(learn.effective_cfg(DayConfig(), Path(d)), DayConfig())


class EvaluateTests(unittest.TestCase):
    def test_matches_the_backtest_for_the_same_settings(self):
        sessions = fake_sessions(80)
        days = learn.trading_days(sessions)
        mine = learn.evaluate(sessions, DayConfig(), days)
        theirs = backtest(sessions, DayConfig())
        self.assertAlmostEqual(mine["ret"], theirs["strategy"]["total_return"], places=9)
        self.assertEqual(mine["trades"], theirs["trades"])

    def test_split_keeps_the_newer_days_apart(self):
        train, test = learn.split([f"d{i:03d}" for i in range(100)])
        self.assertEqual((len(train), len(test)), (70, 30))
        self.assertLess(train[-1], test[0])


def metrics(ret, trades=100, pf=1.5, dd=0.03):
    return {"days": 100, "trades": trades, "ret": ret, "dd": dd, "pf": pf, "pnl": ret * 1e5}


class SearchTests(unittest.TestCase):
    """The decision rule, with the replay results supplied directly."""

    def run_search(self, table, days=200, last_change=None):
        sessions = {"SPY": {f"2024-01-{i}": [] for i in range(1)}}
        names = [f"d{i:04d}" for i in range(days)]
        calls = {"n": 0}

        def fake_eval(_sessions, cfg, ds, **kw):
            key = (cfg.range_minutes, "train" if ds[0] == names[0] else "test")
            return table.get(key, metrics(0.05))
        with mock.patch.object(learn, "trading_days", return_value=names), mock.patch.object(learn, "evaluate", fake_eval):
            return learn.search(sessions, {}, DayConfig(), None, last_change)

    def test_too_little_history(self):
        self.assertEqual(self.run_search({}, days=50)["verdict"], "not_enough_data")

    def test_too_few_trades(self):
        r = self.run_search({(15, "train"): metrics(0.05, trades=10)})
        self.assertEqual(r["verdict"], "not_enough_data")

    def test_a_change_needs_to_win_on_both_older_and_newer_days(self):
        table = {(15, "train"): metrics(0.05), (15, "test"): metrics(0.02),
                 (10, "train"): metrics(0.09), (10, "test"): metrics(0.04),
                 (20, "train"): metrics(0.04), (20, "test"): metrics(0.01)}
        r = self.run_search(table)
        self.assertEqual(r["verdict"], "change")
        self.assertEqual(r["candidate"], {"range_minutes": 10})

    def test_a_change_that_only_wins_on_the_older_days_is_rejected(self):
        table = {(15, "train"): metrics(0.05), (15, "test"): metrics(0.02),
                 (10, "train"): metrics(0.12), (10, "test"): metrics(0.015)}
        r = self.run_search(table)
        self.assertEqual(r["verdict"], "keep")
        self.assertIn("newer days", r["reason"])

    def test_a_small_edge_is_not_enough(self):
        table = {(15, "train"): metrics(0.05), (10, "train"): metrics(0.055)}
        self.assertEqual(self.run_search(table)["verdict"], "keep")

    def test_a_deeper_drawdown_or_losing_profit_factor_blocks_it(self):
        base = {(15, "train"): metrics(0.05), (15, "test"): metrics(0.02), (10, "train"): metrics(0.1)}
        self.assertEqual(self.run_search({**base, (10, "test"): metrics(0.05, dd=0.10)})["verdict"], "keep")
        self.assertEqual(self.run_search({**base, (10, "test"): metrics(0.05, pf=0.9)})["verdict"], "keep")
        self.assertEqual(self.run_search({**base, (10, "test"): metrics(0.05, trades=5)})["verdict"], "keep")

    def test_cooldown_after_a_change(self):
        r = self.run_search({}, last_change="d0190")
        self.assertEqual(r["verdict"], "cooldown")
        self.assertNotEqual(self.run_search({}, last_change="d0010")["verdict"], "cooldown")

    def test_real_data_path_runs_end_to_end(self):
        r = learn.search(fake_sessions(200), {}, DayConfig())
        self.assertIn(r["verdict"], ("keep", "change", "not_enough_data"))


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.journal = Journal(self.dir)
        self.logs = []
        self.sessions = fake_sessions(200)

    def go(self, day="2025-06-01", refresh=None, search=None):
        refresh = refresh or (lambda broker, today, directory, log: self.sessions)
        with mock.patch.object(learn, "search", search or learn.search):
            return learn.nightly(None, self.journal, day, log=self.logs.append, directory=self.dir, refresh=refresh)

    PROPOSAL = {"verdict": "change", "reason": "range 10 beat it", "candidate": {"range_minutes": 10}}

    def test_propose_mode_waits_for_your_ok(self):
        self.go(search=lambda *a, **k: self.PROPOSAL)
        s = learn.load(self.dir)
        self.assertEqual(s["applied"], {})
        self.assertEqual(s["pending"]["overrides"], {"range_minutes": 10})
        self.assertEqual(learn.effective_cfg(DayConfig(), self.dir), DayConfig())     # nothing live changed
        self.assertTrue(learn.apply_pending(s, "2025-06-02", "you approved it"))
        learn.save(s, self.dir)
        self.assertEqual(learn.effective_cfg(DayConfig(), self.dir).range_minutes, 10)

    def test_auto_mode_applies_and_records_why(self):
        s = learn.load(self.dir); s["mode"] = "auto"; learn.save(s, self.dir)
        self.go(search=lambda *a, **k: self.PROPOSAL)
        s = learn.load(self.dir)
        self.assertEqual(s["applied"], {"range_minutes": 10})
        self.assertEqual(s["previous"], {})
        self.assertEqual(s["last_change_day"], "2025-06-01")
        self.assertTrue(any(h["kind"] == "applied" for h in s["history"]))

    def test_it_runs_once_a_day(self):
        calls = []
        self.go(search=lambda *a, **k: calls.append(1) or {"verdict": "keep", "reason": "x"})
        self.go(search=lambda *a, **k: calls.append(1) or {"verdict": "keep", "reason": "x"})
        self.assertEqual(len(calls), 1)

    def test_a_failure_never_raises_and_changes_nothing(self):
        def boom(*a, **k):
            raise RuntimeError("no network")
        r = self.go(refresh=boom)
        self.assertIn("skipped", r)
        self.assertEqual(learn.load(self.dir)["applied"], {})
        self.assertTrue(any("live trading is unaffected" in l for l in self.logs))

    def test_off_does_nothing(self):
        s = learn.load(self.dir); s["mode"] = "off"; learn.save(s, self.dir)
        self.assertEqual(self.go()["skipped"], "learning is off")

    def test_a_pending_proposal_is_not_replaced_while_you_decide(self):
        self.go(search=lambda *a, **k: self.PROPOSAL)
        r = self.go(day="2025-06-02", search=lambda *a, **k: {"verdict": "change", "reason": "other", "candidate": {"range_minutes": 20}})
        self.assertEqual(r["verdict"], "waiting")
        self.assertEqual(learn.load(self.dir)["pending"]["overrides"], {"range_minutes": 10})

    def test_a_bad_change_is_rolled_back(self):
        s = learn.load(self.dir)
        s.update(mode="auto", applied={"range_minutes": 10}, previous={}, last_change_day="2025-05-01")
        learn.save(s, self.dir)
        table = {10: {"ret": -0.03}, 15: {"ret": 0.02}}
        with mock.patch.object(learn, "trading_days", return_value=[f"2025-05-{i:02d}" for i in range(2, 30)] + ["2025-06-01"]), \
                mock.patch.object(learn, "evaluate", lambda sess, cfg, days, **k: table[cfg.range_minutes]):
            r = self.go()
        self.assertEqual(r["verdict"], "reverted")
        self.assertEqual(learn.load(self.dir)["applied"], {})

    def test_no_rollback_without_enough_days_or_when_it_is_doing_fine(self):
        s = {"applied": {"range_minutes": 10}, "previous": {}, "last_change_day": "2025-05-01"}
        days = [f"2025-05-{i:02d}" for i in range(2, 30)]
        with mock.patch.object(learn, "trading_days", return_value=days[:5]):
            self.assertIsNone(learn.rollback_check({}, s))
        with mock.patch.object(learn, "trading_days", return_value=days), \
                mock.patch.object(learn, "evaluate", lambda sess, cfg, d, **k: {"ret": 0.03 if cfg.range_minutes == 10 else 0.0}):
            self.assertIsNone(learn.rollback_check({}, s))

    def test_review_reads_the_days_trades(self):
        self.journal.record("entry", day="2025-06-01", symbol="SPY", qty=1)
        self.journal.record("trade_result", day="2025-06-01", symbol="SPY", pnl=12.5)
        self.assertEqual(learn.review_day(self.journal, "2025-06-01"), {"day": "2025-06-01", "entries": 1, "results": 1, "pnl": 12.5})

    def test_price_cache_round_trips(self):
        sessions = {"SPY": {"2025-01-02": [(570, 1.0, 2.0, 0.5, 1.5)]}, "QQQ": {}}
        learn.save_sessions(sessions, self.dir)
        self.assertEqual(learn.load_sessions(self.dir), sessions)


class CommandTests(unittest.TestCase):
    def run_cmd(self, *argv):
        from trading.day import __main__ as cli
        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_apply_reject_revert_and_mode(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("trading.day.store.day_dir", return_value=Path(d)):
            s = learn.load(Path(d)); s["pending"] = {"overrides": {"flatten_before_close": 30}, "text": "sell earlier", "made_on": "x"}; learn.save(s, Path(d))
            self.assertIn("sell earlier", self.run_cmd("learn", "status")[1])
            self.assertEqual(self.run_cmd("learn", "apply")[0], 0)
            self.assertEqual(learn.load(Path(d))["applied"], {"flatten_before_close": 30})
            self.assertEqual(self.run_cmd("learn", "revert")[0], 0)
            self.assertEqual(learn.load(Path(d))["applied"], {})
            self.assertEqual(self.run_cmd("learn", "apply")[0], 1)
            self.assertEqual(self.run_cmd("learn", "mode", "auto")[0], 0)
            self.assertEqual(learn.load(Path(d))["mode"], "auto")
            self.assertEqual(self.run_cmd("learn", "mode", "wild")[0], 1)


class RunnerHookTests(RunnerCase):
    def test_the_review_runs_once_after_the_day_is_recorded_and_cannot_hurt_trading(self):
        called, done = [], threading.Event()

        def hook(day):
            called.append(day)
            done.set()
            raise RuntimeError("review blew up")
        broker = DayFakeBroker()
        runner = self.runner(broker, after_day=hook)
        runner.step()
        broker.at(15, 45, 0)
        self.advance(1000)
        runner.step()
        self.advance(60)
        self.assertEqual(runner.step(), "flat_for_the_day")
        self.assertTrue(done.wait(2))
        self.advance(60)
        runner.step()
        time.sleep(0.1)
        self.assertEqual(called, [DAY])
        self.assertEqual(len(self.journal.events("snapshot")), 1)


if __name__ == "__main__":
    unittest.main()
