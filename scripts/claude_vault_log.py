#!/usr/bin/env python3
"""Claude Code Stop hook: one line per Claude turn in the Obsidian brain.

Registered in ~/.claude/settings.json (hooks.Stop). Claude Code pipes the hook
input (session_id, transcript_path, cwd, ...) on stdin; this reads the turn that
just ended from the transcript and writes, to today's note in
<vault>/Jarvis Brain/Log (core/vault_brain.py):

    - 14:02 · [[Claude Code]] · "your request" → what Claude answered · edited a.py, b.py · committed → [[Shop]]

Linked to the areas whose files it touched. Always exits 0 and prints nothing,
so it can never get in the way of a session.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_TAIL_BYTES = 4_000_000
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def _read_records(path: Path) -> list[dict]:
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > _TAIL_BYTES:
            f.seek(size - _TAIL_BYTES)
            f.readline()                      # drop the partial first line
        data = f.read()
    out = []
    for raw in data.splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _prompt_text(rec: dict) -> str | None:
    """The words of a prompt the person typed, or None if this record is not one."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    origin = rec.get("origin") or {}
    if rec.get("turnOrigin") not in (None, "human") or (origin and origin.get("kind") not in (None, "human")):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    cmd = re.search(r"<command-name>(.*?)</command-name>", content, re.S)
    if cmd:
        args = re.search(r"<command-args>(.*?)</command-args>", content, re.S)
        return f"{cmd.group(1).strip()} {(args.group(1).strip() if args else '')}".strip()
    text = re.sub(r"<(system-reminder|local-command-stdout|local-command-caveat)>.*?</\1>", " ", content, flags=re.S)
    text = re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", " [pasted text] ", text, flags=re.S)
    text = " ".join(text.split())
    return text or None


def summarize_turn(records: list[dict], last_message: str | None = None) -> dict | None:
    """{'prompt', 'reply', 'edited', 'committed', 'pushed'} for the last turn, or None."""
    start = None
    for i in range(len(records) - 1, -1, -1):
        if _prompt_text(records[i]) is not None:
            start = i
            break
    if start is None:
        return None
    prompt = _prompt_text(records[start]) or ""
    edited: list[str] = []
    committed = pushed = False
    reply = ""
    for rec in records[start + 1:]:
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        for b in (rec.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                reply = b["text"]
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b.get("name") in _EDIT_TOOLS and inp.get("file_path"):
                    if inp["file_path"] not in edited:
                        edited.append(inp["file_path"])
                elif b.get("name") == "Bash":
                    cmd = str(inp.get("command", ""))
                    committed = committed or bool(re.search(r"\bgit\b[^|;&]*\bcommit\b", cmd))
                    pushed = pushed or bool(re.search(r"\bgit\b[^|;&]*\bpush\b", cmd))
    if last_message:
        reply = last_message
    return {"prompt": prompt, "reply": reply, "edited": edited, "committed": committed, "pushed": pushed}


def _short(path: str, cwd: str) -> str:
    p = str(path)
    for base in (cwd, str(REPO), str(Path.home())):
        if base and p.startswith(base.rstrip("/") + "/"):
            return p[len(base.rstrip("/")) + 1:]
    m = re.search(r"/scratchpad/wt[^/]*/(.*)$", p)            # a git worktree of the repo
    return m.group(1) if m else Path(p).name


def turn_line(turn: dict, cwd: str = "") -> str:
    prompt = " ".join(turn["prompt"].split())
    if len(prompt) > 220:
        prompt = prompt[:219] + "…"
    reply = re.sub(r"[`*_#>|]", "", turn["reply"] or "")
    reply = " ".join(reply.split())
    if len(reply) > 260:
        reply = reply[:259] + "…"
    parts = [f'"{prompt}"' + (f" → {reply}" if reply else "")]
    if turn["edited"]:
        names = [_short(p, cwd) for p in turn["edited"]]
        shown = ", ".join(names[:8]) + (f" (+{len(names) - 8} more)" if len(names) > 8 else "")
        parts.append(f"edited {shown}")
    if turn["committed"] or turn["pushed"]:
        parts.append(" + ".join(w for w, on in (("committed", turn["committed"]), ("pushed", turn["pushed"])) if on))
    return " · ".join(parts)


def main() -> int:
    try:
        hook = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    try:
        path = Path(hook.get("transcript_path") or "")
        if not path.is_file():
            return 0
        turn = summarize_turn(_read_records(path), hook.get("last_assistant_message"))
        if not turn or not turn["prompt"]:
            return 0
        from core import vault_brain
        cwd = hook.get("cwd") or os.getcwd()
        vault_brain.log("Claude Code", turn_line(turn, cwd),
                        links=vault_brain.areas_for_paths(turn["edited"]), wait=5.0, limit=1000)
    except Exception as e:
        print(f"claude_vault_log: {e!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
