"""The day_trading plugin is only a voice front for ~/optionsbots/scripts/bots.py (tested in the optionsbots repo)."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.plugin_loader import _validate
from plugins import day_trading as plugin


class LoaderTests(unittest.TestCase):
    def test_jarvis_accepts_the_plugin(self):
        path = Path(plugin.__file__)
        spec = importlib.util.spec_from_file_location("day_trading_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        record = _validate(module, path.name)
        self.assertTrue(record.valid, record.error)
        self.assertEqual(record.name, "day_trading")

    def test_the_description_rules_out_real_money_and_manual_trades(self):
        text = plugin.PLUGIN["description"].lower()
        self.assertIn("can not trade real money", text)
        self.assertIn("eleven", text)
        self.assertIn("never close or open positions at alpaca", text)
        self.assertIn("practice memecoin bot", text)


class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "bots.py"
        script.write_text("")
        p = patch.object(plugin, "BOTS", script)
        p.start()
        self.addCleanup(p.stop)

    def test_it_runs_the_shared_command_and_speaks_its_output(self):
        done = subprocess.CompletedProcess([], 0, stdout="The paper account is at $1,000.00.\n", stderr="")
        with patch.object(plugin.subprocess, "run", return_value=done) as run:
            self.assertEqual(plugin.run({}), "The paper account is at $1,000.00.")
            plugin.run({"action": "Pause"})
            plugin.run({"action": "bots"})
            plugin.run({"action": "learning"})
            plugin.run({"action": "memecoins"})
            plugin.run({"action": "start crypto"})
            plugin.run({"action": "portfolio"})
        self.assertEqual([c.args[0][1:] for c in run.call_args_list],
                         [[str(plugin.BOTS), a, "--jarvis"] for a in ("status", "pause", "each", "learned", "crypto", "crypto_start", "portfolio")])

    def test_unknown_actions_run_nothing(self):
        with patch.object(plugin.subprocess, "run") as run:
            self.assertIn("start, stop, pause or resume", plugin.run({"action": "buy SPY"}))
        run.assert_not_called()

    def test_a_missing_install_or_a_timeout_is_spoken(self):
        plugin.BOTS.unlink()
        self.assertIn("isn't installed", plugin.run({}))
        plugin.BOTS.write_text("")
        with patch.object(plugin.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIn("didn't answer in time", plugin.run({}))


if __name__ == "__main__":
    unittest.main()
