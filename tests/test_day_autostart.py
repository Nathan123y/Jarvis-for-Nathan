import contextlib
import io
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading.day import __main__ as cli
from trading.day import autostart as auto
from trading.journal import Journal


class Calls:
    def __init__(self, fail_bootstrap=False, loaded=True):
        self.calls, self.fail_bootstrap, self.loaded = [], fail_bootstrap, loaded

    def __call__(self, args):
        self.calls.append(args)
        code = 1 if (args[0] == "bootstrap" and self.fail_bootstrap) or (args[0] == "print" and not self.loaded) else 0
        return subprocess.CompletedProcess(args, code, "", "boom" if code else "")


class PlistTests(unittest.TestCase):
    def build(self, **kw):
        return auto.build_plist(python="/opt/py/bin/python3", repo=Path("/Users/me/Jarvis"), log=Path("/x/runner.log"),
                                run_flags=kw.pop("run_flags", ["--analyst", "--risk-pct", "0.5"]), **kw)

    def test_it_starts_monday_to_friday_only_at_the_chosen_time(self):
        plist = self.build(at="06:30")
        self.assertEqual(plist["StartCalendarInterval"],
                         [{"Weekday": d, "Hour": 6, "Minute": 30} for d in (1, 2, 3, 4, 5)])
        self.assertEqual(self.build(at="5:05")["StartCalendarInterval"][0]["Hour"], 5)

    def test_it_runs_the_trader_under_caffeinate_with_the_chosen_flags_from_the_repo(self):
        plist = self.build()
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/caffeinate", "-i", "/opt/py/bin/python3", "-u", "-m",
                                                     "trading.day", "run", "--analyst", "--risk-pct", "0.5"])
        self.assertEqual(plist["WorkingDirectory"], "/Users/me/Jarvis")
        self.assertEqual(plist["StandardOutPath"], "/x/runner.log")

    def test_there_is_no_keep_alive_so_it_cannot_respawn_in_a_loop(self):
        self.assertNotIn("KeepAlive", self.build())
        self.assertNotIn("RunAtLoad", self.build())

    def test_no_secret_or_environment_beyond_a_path_is_written(self):
        plist = self.build()
        self.assertEqual(list(plist["EnvironmentVariables"]), ["PATH"])
        self.assertNotRegex(plistlib.dumps(plist).decode().lower(), r"secret|api_key|token|password")

    def test_bad_times_are_refused(self):
        for bad in ("6", "25:00", "06:60", "six", "", "06:30:00", "-1:00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.build(at=bad)

    def test_the_wake_line_is_five_minutes_earlier_and_wraps_past_the_hour(self):
        self.assertEqual(auto.wake_command("06:30"), "sudo pmset repeat wakeorpoweron MTWRF 06:25:00")
        self.assertEqual(auto.wake_command("06:03"), "sudo pmset repeat wakeorpoweron MTWRF 05:58:00")
        self.assertEqual(auto.wake_command("00:02"), "sudo pmset repeat wakeorpoweron MTWRF 23:57:00")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def install(self, launchctl, **kw):
        return auto.install(python="/p/python3", repo=Path("/r"), log=self.home / "logs" / "runner.log",
                            run_flags=["--analyst"], home=self.home, uid=501, launchctl=launchctl, **kw)

    def test_it_writes_a_valid_plist_in_the_users_own_folder_and_loads_it(self):
        calls = Calls()
        lines = self.install(calls)
        path = self.home / "Library" / "LaunchAgents" / "com.jarvis.daytrader.plist"
        self.assertEqual(plistlib.loads(path.read_bytes())["Label"], "com.jarvis.daytrader")
        self.assertEqual(calls.calls, [["bootout", "gui/501/com.jarvis.daytrader"], ["bootstrap", "gui/501", str(path)]])
        self.assertTrue(any("Monday to Friday at 06:30" in line for line in lines))
        self.assertTrue(any("pmset repeat wakeorpoweron MTWRF 06:25:00" in line for line in lines))

    def test_installing_again_replaces_the_old_copy(self):
        calls = Calls()
        self.install(calls)
        self.install(calls, at="06:45")
        path = self.home / "Library" / "LaunchAgents" / "com.jarvis.daytrader.plist"
        self.assertEqual(plistlib.loads(path.read_bytes())["StartCalendarInterval"][0]["Minute"], 45)

    def test_a_refusal_from_macos_is_reported_plainly(self):
        with self.assertRaises(RuntimeError) as caught:
            self.install(Calls(fail_bootstrap=True))
        self.assertIn("refused to load", str(caught.exception))

    def test_remove_unloads_and_deletes_and_is_fine_when_nothing_is_there(self):
        calls = Calls()
        self.install(calls)
        self.assertIn("Removed", auto.remove(home=self.home, uid=501, launchctl=calls)[0])
        self.assertFalse((self.home / "Library" / "LaunchAgents" / "com.jarvis.daytrader.plist").exists())
        self.assertIn("not installed", auto.remove(home=self.home, uid=501, launchctl=calls)[0])

    def test_status_reports_file_and_loaded_separately(self):
        self.assertEqual(auto.status(home=self.home, uid=501, launchctl=Calls(loaded=False)), (False, False))
        self.install(Calls())
        self.assertEqual(auto.status(home=self.home, uid=501, launchctl=Calls(loaded=False)), (True, False))
        self.assertEqual(auto.status(home=self.home, uid=501, launchctl=Calls()), (True, True))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name))
        patcher = patch.object(cli, "day_journal", lambda: self.journal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, system="Darwin"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("platform.system", return_value=system):
            code = cli.main(["autostart", *argv])
        return code, out.getvalue()

    def test_it_refuses_on_anything_but_a_mac_and_touches_nothing(self):
        with patch.object(auto, "install", side_effect=AssertionError("installed")):
            code, text = self.run_cli("install", system="Linux")
        self.assertEqual(code, 1)
        self.assertIn("only works on a Mac", text)

    def test_install_passes_the_chosen_flags_and_time_through(self):
        seen = {}
        def fake(**kw):
            seen.update(kw)
            return ["Installed."]
        with patch.object(auto, "install", fake):
            code, text = self.run_cli("install", "--analyst", "--risk-pct", "0.5", "--max-fund-pct", "50", "--at", "06:15")
        self.assertEqual(code, 0)
        self.assertEqual(seen["run_flags"], ["--analyst", "--risk-pct", "0.5", "--max-fund-pct", "50"])
        self.assertEqual(seen["at"], "06:15")
        self.assertEqual(seen["log"], self.journal.log_path)
        self.assertIn("Full Disk Access", text)

    def test_without_the_analyst_flag_the_plain_rule_is_what_starts(self):
        seen = {}
        with patch.object(auto, "install", lambda **kw: seen.update(kw) or ["ok"]):
            self.run_cli("install")
        self.assertEqual(seen["run_flags"], ["--risk-pct", "0.25", "--max-fund-pct", "25"])

    def test_sizes_it_cannot_use_and_bad_times_are_refused(self):
        with patch.object(auto, "install", side_effect=AssertionError("installed")):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_cli("install", "--max-fund-pct", "80")
            code, text = self.run_cli("install", "--at", "25:99")
        self.assertEqual(code, 1)
        self.assertIn("must look like 06:30", text)

    def test_status_says_what_is_installed_running_and_the_last_log_lines(self):
        self.journal.dir.mkdir(parents=True, exist_ok=True)
        self.journal.log_path.write_text("a\nb\nstatus: watching\n")
        with patch.object(auto, "status", lambda **kw: (True, True)):
            code, text = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("installed: yes; loaded by macOS: yes", text)
        self.assertIn("status: watching", text)


if __name__ == "__main__":
    unittest.main()
