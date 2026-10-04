"""The performance trace must stay content-free, bounded, and never raise."""
import os
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import perf_trace
from core.perf_trace import HeartbeatLag


class HeartbeatLagTests(unittest.TestCase):
    def test_first_tick_has_nothing_to_compare_against(self):
        self.assertIsNone(HeartbeatLag(0.1, 0.08).tick(5.0))

    def test_on_time_ticks_report_nothing(self):
        beat = HeartbeatLag(0.1, 0.08)
        beat.tick(1.0)
        self.assertIsNone(beat.tick(1.1))
        self.assertIsNone(beat.tick(1.215))       # 15 ms late is ordinary jitter

    def test_late_tick_reports_how_late(self):
        beat = HeartbeatLag(0.1, 0.08)
        beat.tick(1.0)
        late = beat.tick(1.5)                     # asked for 1.1, arrived 1.5
        self.assertAlmostEqual(late, 0.4, places=6)

    def test_stall_does_not_poison_the_next_measurement(self):
        beat = HeartbeatLag(0.1, 0.08)
        beat.tick(1.0)
        beat.tick(3.0)
        self.assertIsNone(beat.tick(3.1))


class TraceRecordTests(unittest.TestCase):
    def capture(self, call):
        records = queue.Queue()
        with patch.object(perf_trace, "_RECORDS", records), \
             patch.object(perf_trace, "_WRITER_STARTED", True):
            call()
        out = []
        while not records.empty():
            out.append(records.get_nowait())
        return out

    def test_every_record_carries_the_process_id(self):
        (record,) = self.capture(lambda: perf_trace.log_state_transition("A", "B", "why"))
        self.assertEqual(record["pid"], os.getpid())

    def test_audio_write_keeps_old_shape_when_extras_are_absent(self):
        (record,) = self.capture(lambda: perf_trace.log_audio_write(0.05, 4800, False))
        self.assertEqual(set(record), {"kind", "latency_ms", "frames", "underflow", "seq", "pid", "t"})

    def test_audio_write_records_queue_depth_and_timing(self):
        (record,) = self.capture(lambda: perf_trace.log_audio_write(
            0.05, 4800, True, queued_chunks=3, write_ms=41.26, gap_ms=7.04))
        self.assertEqual((record["queued"], record["write_ms"], record["gap_ms"]), (3, 41.3, 7.0))
        self.assertTrue(record["underflow"])

    def test_lag_record_is_numbers_and_labels_only(self):
        (record,) = self.capture(lambda: perf_trace.log_lag("gui", 0.1234, visible=False))
        self.assertEqual((record["kind"], record["source"], record["ms"], record["visible"]),
                         ("lag", "gui", 123.4, False))

    def test_reconnect_reason_is_truncated_not_free_text(self):
        (record,) = self.capture(lambda: perf_trace.log_reconnect("x" * 500, True, 1.5))
        self.assertLessEqual(len(record["reason"]), 48)
        self.assertEqual(record["ms"], 1500.0)

    def test_full_queue_drops_records_instead_of_blocking(self):
        full = queue.Queue(maxsize=1)
        full.put({"kind": "filler"})
        with patch.object(perf_trace, "_RECORDS", full), \
             patch.object(perf_trace, "_WRITER_STARTED", True):
            perf_trace.log_lag("asyncio", 0.2)     # must simply return
        self.assertEqual(full.qsize(), 1)


def _gui_heartbeat_class():
    """JarvisUI._gui_heartbeat, extracted so it runs without PyQt6 installed."""
    import ast
    import time
    source = Path(__file__).resolve().parents[1] / "ui.py"
    owner = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body
                 if isinstance(n, ast.ClassDef) and n.name == "JarvisUI")
    method = next(n for n in owner.body if getattr(n, "name", None) == "_gui_heartbeat")
    stub = ast.ClassDef(name="Beat", bases=[], keywords=[], body=[method], decorator_list=[])
    namespace = {"time": time}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[])),
                 str(source), "exec"), namespace)
    return namespace["Beat"]


class GuiHeartbeatTests(unittest.TestCase):
    def make(self, visible=True, minimized=False):
        from types import SimpleNamespace
        beat = _gui_heartbeat_class()()
        beat._gui_beat = HeartbeatLag(0.1, 0.08)
        beat._win = SimpleNamespace(isVisible=lambda: visible, isMinimized=lambda: minimized)
        return beat

    def run_with_ticks(self, beat, ticks):
        logged = []
        with patch.object(perf_trace, "log_lag", lambda *a, **k: logged.append((a, k))), \
             patch("time.monotonic", side_effect=ticks):
            for _ in ticks:
                beat._gui_heartbeat()
        return logged

    def test_on_time_firings_log_nothing(self):
        self.assertEqual(self.run_with_ticks(self.make(), [1.0, 1.1, 1.2]), [])

    def test_a_late_firing_is_logged_with_visibility(self):
        (call,) = self.run_with_ticks(self.make(visible=True), [1.0, 1.1, 1.9])
        self.assertEqual(call[0][0], "gui")
        self.assertAlmostEqual(call[0][1], 0.7, places=6)
        self.assertTrue(call[1]["visible"])

    def test_a_minimised_window_is_reported_as_not_visible(self):
        (call,) = self.run_with_ticks(self.make(minimized=True), [1.0, 2.0])
        self.assertFalse(call[1]["visible"])

    def test_a_failure_inside_the_heartbeat_never_escapes(self):
        beat = self.make()
        del beat._win
        self.run_with_ticks(beat, [1.0, 3.0])        # must not raise


class LoopLagMonitorTests(unittest.IsolatedAsyncioTestCase):
    """The monitor from main.py, run against a loop that is really blocked."""

    def monitor(self, logged):
        import ast
        import asyncio
        import time
        source = Path(__file__).resolve().parents[1] / "main.py"
        owner = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body
                     if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
        method = next(n for n in owner.body if getattr(n, "name", None) == "_run_loop_lag_monitor")
        stub = ast.ClassDef(name="Mon", bases=[], keywords=[], body=[method], decorator_list=[])
        namespace = {"asyncio": asyncio, "time": time, "HeartbeatLag": HeartbeatLag,
                     "log_lag": lambda *args, **kwargs: logged.append(args)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[])),
                     str(source), "exec"), namespace)
        return namespace["Mon"]()

    async def test_a_blocked_loop_is_recorded_and_a_healthy_one_is_not(self):
        import asyncio
        import time
        logged = []
        task = asyncio.create_task(self.monitor(logged)._run_loop_lag_monitor())
        await asyncio.sleep(0.3)
        self.assertEqual(logged, [], "a healthy loop must write nothing")
        time.sleep(0.3)                      # the kind of stall that is heard as a gap
        await asyncio.sleep(0.15)
        task.cancel()
        self.assertTrue(logged, "a 300 ms block must be recorded")
        source, overshoot = logged[0]
        self.assertEqual(source, "asyncio")
        self.assertGreater(overshoot, 0.2)


class RotationTests(unittest.TestCase):
    def test_oversized_log_is_moved_aside_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "perf.jsonl"
            log.write_text("x" * 100)
            self.assertTrue(perf_trace._rotate_if_large(log, limit=50))
            self.assertFalse(log.exists())
            self.assertEqual((Path(tmp) / "perf.jsonl.1").read_text(), "x" * 100)

    def test_small_or_missing_log_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "perf.jsonl"
            self.assertFalse(perf_trace._rotate_if_large(log, limit=50))
            log.write_text("tiny")
            self.assertFalse(perf_trace._rotate_if_large(log, limit=50))
            self.assertTrue(log.exists())


if __name__ == "__main__":
    unittest.main()
