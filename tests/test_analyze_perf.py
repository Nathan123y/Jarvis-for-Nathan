"""The analyzer must read a messy log, separate runs, and say only what the data shows."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "analyze_perf", Path(__file__).resolve().parents[1] / "tools" / "analyze_perf.py")
analyze = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyze)


def launch(pid, app, machine="arm64"):
    return {"kind": "launch", "pid": pid, "t": 0.0, "launched_by_app": app,
            "machine": machine, "python": "3.13.0", "git_commit": "abc1234", "git_dirty": False}


def write(pid, t, queued, underflow, gap=5.0):
    return {"kind": "audio_out", "pid": pid, "t": t, "latency_ms": 12.0, "frames": 4800,
            "underflow": underflow, "queued": queued, "gap_ms": gap, "write_ms": 40.0}


def state(pid, t, before, after, reason):
    return {"kind": "state", "pid": pid, "t": t, "from": before, "to": after, "reason": reason}


class AnalyzeTests(unittest.TestCase):
    def test_runs_are_separated_by_process_and_by_new_launch(self):
        records = [launch(1, False), write(1, 1, 0, False), launch(2, True), write(2, 1, 0, False),
                   write(1, 2, 0, False), launch(1, True)]       # pid 1 recycled
        runs = analyze.split_runs(records)
        self.assertEqual([len(r) for r in runs], [3, 2, 1])

    def test_underflows_are_split_by_whether_audio_was_waiting(self):
        run = [launch(1, True)] + [write(1, i, 4, True) for i in range(5)] \
            + [write(1, 9, 0, True)] + [write(1, 10, 1, False)] * 3
        summary = analyze.summarize_run(run)
        self.assertEqual(summary["underflows"], 6)
        self.assertEqual(summary["underflow_audio_waiting"], 5)
        self.assertEqual(summary["underflow_nothing_waiting"], 1)
        text = " ".join(analyze.reading(summary))
        self.assertIn("slow handing it to the speaker", text)
        self.assertIn('output_latency "high"', text)

    def test_old_records_without_queue_depth_are_counted_as_not_recorded(self):
        old = {"kind": "audio_out", "pid": 1, "t": 1, "latency_ms": 10, "frames": 100, "underflow": True}
        summary = analyze.summarize_run([launch(1, False), old])
        self.assertEqual(summary["underflow_unknown"], 1)

    def test_empty_queue_underflows_blame_the_network_not_the_buffer(self):
        run = [launch(1, False)] + [write(1, i, 0, True) for i in range(4)]
        text = " ".join(analyze.reading(analyze.summarize_run(run)))
        self.assertIn("network or the model", text)
        self.assertNotIn('output_latency "high"', text)

    def test_gaps_between_sentences_are_not_stalls(self):
        run = [launch(1, False), write(1, 1, 0, False, gap=20.0), write(1, 2, 0, False, gap=30000.0)]
        self.assertEqual(analyze.summarize_run(run)["gap_worst_ms"], 20.0)

    def test_flicker_is_counted_and_attributed_to_its_reason(self):
        run = [launch(1, False),
               state(1, 10.0, "LISTENING", "SPEAKING", "audio_start"),
               state(1, 12.0, "SPEAKING", "LISTENING", "stall_gap"),
               state(1, 12.8, "LISTENING", "SPEAKING", "audio_start"),       # back within 3 s
               state(1, 20.0, "SPEAKING", "LISTENING", "turn_complete"),
               state(1, 60.0, "LISTENING", "SPEAKING", "audio_start")]       # much later: not a flicker
        summary = analyze.summarize_run(run)
        self.assertEqual(summary["flaps"], {"stall_gap": 1})
        self.assertEqual(summary["left_speaking"], {"stall_gap": 1, "turn_complete": 1})
        self.assertIn("server going quiet mid-answer", " ".join(analyze.reading(summary)))

    def test_hidden_window_stalls_point_at_macos_throttling(self):
        run = [launch(1, True)] + [{"kind": "lag", "pid": 1, "t": i, "source": "gui", "ms": 900.0,
                                    "visible": False} for i in range(4)]
        text = " ".join(analyze.reading(analyze.summarize_run(run)))
        self.assertIn("throttles background apps", text)

    def test_visible_window_stalls_point_at_main_thread_work(self):
        run = [launch(1, True)] + [{"kind": "lag", "pid": 1, "t": i, "source": "gui", "ms": 300.0,
                                    "visible": True} for i in range(4)]
        self.assertIn("slow work on the main thread", " ".join(analyze.reading(analyze.summarize_run(run))))

    def test_rosetta_is_called_out(self):
        text = " ".join(analyze.reading(analyze.summarize_run([launch(1, True, machine="x86_64")])))
        self.assertIn("Rosetta", text)

    def test_side_by_side_only_appears_when_both_launch_paths_were_seen(self):
        app = analyze.summarize_run([launch(1, True), write(1, 60, 0, True)])
        term = analyze.summarize_run([launch(2, False), write(2, 60, 0, False)])
        self.assertEqual(analyze.comparison([app]), [])
        text = "\n".join(analyze.comparison([app, term]))
        self.assertIn("Finder app", text)
        self.assertIn("terminal / VS Code", text)

    def test_reads_a_log_with_garbage_and_a_torn_last_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "perf.jsonl"
            path.write_text(json.dumps(launch(1, True)) + "\nnot json\n[1,2]\n"
                            + json.dumps(write(1, 1, 0, False)) + '\n{"kind": "audio_o')
            records = analyze.load_records(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(analyze.main(["--file", str(path)]), 0)

    def test_missing_log_is_a_clear_message_not_a_crash(self):
        self.assertEqual(analyze.main(["--file", "/nonexistent/perf.jsonl"]), 1)

    def test_rendered_report_contains_no_paths_or_free_text(self):
        record = launch(1, True)
        record.update(executable="/Users/someone/venv/bin/python", cwd="/Users/someone/Projects/Jarvis")
        text = analyze.render(analyze.summarize_run([record, write(1, 1, 0, False)]))
        self.assertNotIn("/Users", text)


if __name__ == "__main__":
    unittest.main()
