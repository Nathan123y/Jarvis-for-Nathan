"""Open Claude with a question on the clipboard, without using a paid API."""

import platform
import subprocess
import webbrowser


CLAUDE_URL = "https://claude.ai/"


def ask_claude(parameters: dict, player=None) -> str:
    question = parameters.get("question", "")
    if not isinstance(question, str) or not question.strip():
        return "What would you like me to ask Claude, sir?"
    question = question.strip()
    if len(question) > 20_000:
        return "That question is too long for the Claude handoff. Please shorten it."

    try:
        if platform.system() == "Darwin":
            subprocess.run(["pbcopy"], input=question, text=True,
                           check=True, capture_output=True, timeout=5)
        else:
            import pyperclip
            pyperclip.copy(question)
    except Exception:
        return "I could not copy the question to the clipboard, so I did not open Claude."

    opened = False
    if platform.system() == "Darwin":
        try:
            subprocess.run(["open", "-a", "Claude"], check=True,
                           capture_output=True, timeout=8)
            opened = True
        except (OSError, subprocess.SubprocessError):
            pass
    if not opened:
        try:
            opened = webbrowser.open(CLAUDE_URL)
        except Exception:
            pass

    if not opened:
        return "I copied your question, but could not open Claude. Open it and paste the question."
    if player:
        try:
            player.write_log("[Claude] Opened Claude with a question on the clipboard")
        except Exception:
            pass
    return (
        "I opened Claude and copied your question, sir. Paste and send it there. "
        "I cannot read Claude's reply back into Jarvis through your Claude account."
    )


TOOL = {
    "name": "ask_claude",
    "description": (
        "Only when the user explicitly asks to ask Claude, pass the exact question here. "
        "Opens the Claude desktop app or web page and copies the question for the user "
        "to paste and send. It does not send the question, read Claude's answer, "
        "or answer on Claude's behalf. Ordinary questions should be answered normally."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING", "description": "The user's exact question for Claude"},
        },
        "required": ["question"],
    },
    "handler": ask_claude,
}
