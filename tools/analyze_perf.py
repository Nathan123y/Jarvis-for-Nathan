"""Summarise Jarvis's performance trace, one block per run.

    python3 tools/analyze_perf.py            # the last 3 runs
    python3 tools/analyze_perf.py --runs 10  # more history
    python3 tools/analyze_perf.py --file /path/to/perf.jsonl

Reads the file written by core/perf_trace.py (~/Library/Logs/Jarvis/perf.jsonl
on a Mac). Nothing here touches the network, and the output contains only
counts, timings and short labels — no conversation, notes, messages or file
contents are ever recorded — so it is safe to paste into a bug report.

The point is the comparison the lag question needs: the same numbers for a run
started with `python3 main.py` and a run started from Jarvis.app, side by side.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
from collections import Counter
from pathlib import Path

FLAP_WINDOW_S = 3.0          # leaving SPEAKING and coming back within this is a flap
BETWEEN_SENTENCES_S = 1.0    # a longer gap between writes is silence, not a stall


def default_log() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Logs" / "Jarvis" / "perf.jsonl"
    return Path(__file__).resolve().parents[1] / "logs" / "perf.jsonl"


def load_records(path: Path) -> list[dict]:
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue            # a half-written last line is normal
                if isinstance(item, dict):
                    records.append(item)
    except OSError:
        pass
    return records


def split_runs(records: list[dict]) -> list[list[dict]]:
    """Group records by process. A new `launch` record always starts a new run,
    so a recycled process id cannot merge two sessions."""
    runs: list[list[dict]] = []
    current: dict[object, list[dict]] = {}
    for record in records:
        pid = record.get("pid")
        if record.get("kind") == "launch" or pid not in current:
            current[pid] = []
            runs.append(current[pid])
        current[pid].append(record)
    return runs


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def summarize_run(run: list[dict]) -> dict:
    launch = next((r for r in run if r.get("kind") == "launch"), {})
    duration = max((r.get("t", 0.0) for r in run), default=0.0)
    out = [r for r in run if r.get("kind") == "audio_out"]
    flagged = [r for r in out if r.get("underflow")]
    waiting = [r for r in flagged if (r.get("queued") or 0) > 0]
    empty = [r for r in flagged if r.get("queued") == 0]
    gaps = [r["gap_ms"] for r in out
            if isinstance(r.get("gap_ms"), (int, float)) and r["gap_ms"] < BETWEEN_SENTENCES_S * 1000]
    latencies = [r["latency_ms"] for r in out if isinstance(r.get("latency_ms"), (int, float))]
    lags = [r for r in run if r.get("kind") == "lag"]
    loop_lags = [r["ms"] for r in lags if r.get("source") == "asyncio"]
    gui = [r for r in lags if r.get("source") == "gui"]

    states = [r for r in run if r.get("kind") == "state"]
    left_speaking = Counter()
    entered = Counter()
    flaps = Counter()
    for index, change in enumerate(states):
        entered[(change.get("to"), change.get("reason"))] += 1
        if change.get("from") != "SPEAKING":
            continue
        left_speaking[change.get("reason")] += 1
        for later in states[index + 1:]:
            if later.get("t", 0) - change.get("t", 0) > FLAP_WINDOW_S:
                break
            if later.get("to") == "SPEAKING":
                flaps[change.get("reason")] += 1
                break

    return {
        "launch": launch,
        "duration_s": duration,
        "writes": len(out),
        "audio_s": sum(r.get("frames", 0) for r in out) / 24000.0,
        "underflows": len(flagged),
        "underflow_audio_waiting": len(waiting),
        "underflow_nothing_waiting": len(empty),
        "underflow_unknown": len(flagged) - len(waiting) - len(empty),
        "gap_median_ms": statistics.median(gaps) if gaps else 0.0,
        "gap_p95_ms": _percentile(gaps, 0.95),
        "gap_worst_ms": max(gaps, default=0.0),
        "device_buffer_ms": statistics.median(latencies) if latencies else 0.0,
        "input_overflows": sum(1 for r in run if r.get("kind") == "audio_in" and r.get("overflow")),
        "loop_stalls": len(loop_lags),
        "loop_worst_ms": max(loop_lags, default=0.0),
        "gui_stalls": len(gui),
        "gui_worst_ms": max((r["ms"] for r in gui), default=0.0),
        "gui_stalls_hidden": sum(1 for r in gui if r.get("visible") is False),
        "state_changes": len(states),
        "left_speaking": dict(left_speaking),
        "flaps": dict(flaps),
        "sleeping_entered": {reason: n for (to, reason), n in entered.items() if to == "SLEEPING"},
        "reconnects": [(r.get("reason"), r.get("ms", 0.0)) for r in run if r.get("kind") == "reconnect"],
    }


def _per_minute(count: int, duration_s: float) -> float:
    return count / (duration_s / 60.0) if duration_s >= 60.0 else float(count)


def _mmss(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _labels(counts: dict) -> str:
    return ", ".join(f"{name} x{n}" for name, n in sorted(counts.items(), key=lambda kv: -kv[1])) or "none"


def launch_name(summary: dict) -> str:
    launch = summary["launch"]
    if not launch:
        return "unknown launch"
    return "Finder app" if launch.get("launched_by_app") else "terminal / VS Code"


def render(summary: dict) -> str:
    launch = summary["launch"]
    dirty = "" if "git_dirty" not in launch else (" with local changes" if launch["git_dirty"] else ", clean")
    lines = [
        f"Run (pid {launch.get('pid', '?')}) - {launch_name(summary)} - "
        f"Python {launch.get('python', '?')} ({launch.get('machine', '?')}) - "
        f"commit {launch.get('git_commit', '?')}{dirty} - {_mmss(summary['duration_s'])}",
        f"  Speaker: {summary['writes']} writes, {summary['audio_s']:.0f}s of audio, "
        f"device buffer ~{summary['device_buffer_ms']:.0f} ms",
        f"    underflows: {summary['underflows']}"
        f" (audio was waiting: {summary['underflow_audio_waiting']},"
        f" nothing waiting: {summary['underflow_nothing_waiting']},"
        f" not recorded: {summary['underflow_unknown']})",
        f"    hand-over gap: median {summary['gap_median_ms']:.0f} ms, "
        f"p95 {summary['gap_p95_ms']:.0f} ms, worst {summary['gap_worst_ms']:.0f} ms",
        f"  Microphone input overflows: {summary['input_overflows']}",
        f"  Stalls: event loop {summary['loop_stalls']} (worst {summary['loop_worst_ms']:.0f} ms late); "
        f"Qt thread {summary['gui_stalls']} (worst {summary['gui_worst_ms']:.0f} ms late, "
        f"{summary['gui_stalls_hidden']} while the window was hidden)",
        f"  HUD: {summary['state_changes']} state changes; left SPEAKING via "
        f"{_labels(summary['left_speaking'])}",
        f"    came straight back to SPEAKING within {FLAP_WINDOW_S:.0f}s (flicker): {_labels(summary['flaps'])}",
        f"    entered SLEEPING via: {_labels(summary['sleeping_entered'])}",
    ]
    if summary["reconnects"]:
        lines.append("  Reconnects: " + ", ".join(f"{why} ({ms / 1000:.1f}s)" for why, ms in summary["reconnects"]))
    else:
        lines.append("  Reconnects: none")
    return "\n".join(lines)


def reading(summary: dict) -> list[str]:
    """Plain-language hints. Each one names the evidence it rests on."""
    notes = []
    if summary["launch"].get("machine") == "x86_64":
        notes.append("Python reports x86_64. On an Apple Silicon Mac that means it is running under Rosetta, "
                     "which makes audio code slower and less steady.")
    if summary["underflow_audio_waiting"] >= 3:
        notes.append("Underflows happened while audio was already queued: Jarvis was slow handing it to the speaker. "
                     "A bigger device buffer absorbs this - try output_latency \"high\" (docs/lag-diagnosis.md).")
    if summary["underflow_nothing_waiting"] > summary["underflow_audio_waiting"] >= 0 and summary["underflow_nothing_waiting"] >= 3:
        notes.append("Most underflows happened with nothing queued: the audio had not arrived yet. "
                     "That points at the network or the model, and a bigger buffer will not cure it.")
    if summary["loop_stalls"]:
        notes.append(f"The event loop that carries the voice was blocked up to {summary['loop_worst_ms']:.0f} ms. "
                     "Anything that long is heard as a gap.")
    if summary["gui_stalls"] and summary["gui_stalls_hidden"] >= summary["gui_stalls"] / 2:
        notes.append("Most Qt-thread stalls happened while the window was hidden or covered: "
                     "macOS throttles background apps, which can delay the HUD.")
    elif summary["gui_stalls"]:
        notes.append(f"The Qt thread stalled up to {summary['gui_worst_ms']:.0f} ms while visible: "
                     "something is doing slow work on the main thread.")
    stall = summary["flaps"].get("stall_gap", 0)
    if stall:
        notes.append(f"{stall} HUD flickers came from the server going quiet mid-answer (stall_gap), "
                     "not from a normal end of speech.")
    return notes


def comparison(summaries: list[dict]) -> list[str]:
    kinds = {launch_name(s) for s in summaries}
    if len(kinds) < 2:
        return []
    lines = ["Side by side (per minute of running time):"]
    for kind in sorted(kinds):
        group = [s for s in summaries if launch_name(s) == kind]
        minutes = sum(max(s["duration_s"], 1.0) for s in group) / 60.0
        writes = sum(s["writes"] for s in group) or 1
        lines.append(
            f"  {kind}: underflows {sum(s['underflows'] for s in group) / writes:.1%} of writes, "
            f"event-loop stalls {sum(s['loop_stalls'] for s in group) / minutes:.1f}/min, "
            f"Qt stalls {sum(s['gui_stalls'] for s in group) / minutes:.1f}/min, "
            f"flickers {sum(sum(s['flaps'].values()) for s in group) / minutes:.1f}/min")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise Jarvis's performance trace.")
    parser.add_argument("--file", type=Path, default=None, help="perf.jsonl to read")
    parser.add_argument("--runs", type=int, default=3, help="how many recent runs to show")
    args = parser.parse_args(argv)
    path = args.file or default_log()
    records = load_records(path)
    if not records:
        print(f"No trace found at {path}. Start Jarvis once, talk to it, then run this again.")
        return 1
    summaries = [summarize_run(run) for run in split_runs(records)][-max(1, args.runs):]
    for summary in summaries:
        print(render(summary))
        for note in reading(summary):
            print(f"  -> {note}")
        print()
    for line in comparison(summaries):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
