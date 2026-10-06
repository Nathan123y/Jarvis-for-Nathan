"""Summarise the most recent Jarvis/Python crash so it can be shared.

    python3 tools/last_crash.py

Prints (1) what macOS recorded about the last Python crash — the kind of
crash and the native functions on the crashed thread — and (2) the Python
stack Jarvis wrote at that moment (see core/crashlog.py). Only code
locations are printed: no conversation text, notes or keys.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPORTS = Path.home() / "Library" / "Logs" / "DiagnosticReports"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def latest_report(folder: Path = REPORTS) -> Path | None:
    try:
        cands = [p for p in folder.glob("*.ips") if p.name.lower().startswith("python")]
    except OSError:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def summarise_ips(text: str, frames: int = 18) -> list[str]:
    head, _, body = text.partition("\n")
    try:
        meta = json.loads(head)
        data = json.loads(body)
    except ValueError:
        return ["(could not read this crash report)"]
    out = [f"Crash time: {meta.get('timestamp', '?')}  app: {meta.get('app_name', '?')}"]
    exc = data.get("exception", {})
    out.append(f"Kind: {exc.get('type', '?')} {exc.get('signal', '')} {exc.get('subtype', '')}".rstrip())
    term = data.get("termination", {}).get("indicator")
    if term:
        out.append(f"Reason: {term}")
    images = data.get("usedImages", [])
    threads = data.get("threads", [])
    n = data.get("faultingThread", 0)
    if 0 <= n < len(threads):
        t = threads[n]
        label = t.get("name") or t.get("queue") or ""
        out.append(f"Crashed thread {n} {label}".rstrip())
        for fr in t.get("frames", [])[:frames]:
            img = fr.get("imageIndex")
            name = images[img].get("name", "?") if isinstance(img, int) and 0 <= img < len(images) else "?"
            out.append(f"  {name:<28} {fr.get('symbol', '?')}")
    return out


def last_python_stack(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    start = text.rfind("Fatal Python error")
    if start < 0:
        return []
    return text[start:].strip().splitlines()[:80]


def main() -> None:
    from core.crashlog import crash_log_path
    rep = latest_report()
    print("── macOS crash report ──")
    if rep is None:
        print("No Python crash report found in ~/Library/Logs/DiagnosticReports.")
    else:
        print(f"File: {rep.name}")
        print("\n".join(summarise_ips(rep.read_text(encoding="utf-8", errors="replace"))))
    print("\n── Python stack at the crash ──")
    stack = last_python_stack(crash_log_path())
    print("\n".join(stack) if stack else
          "None recorded yet. It is written from the next crash on, after this update.")


if __name__ == "__main__":
    main()
