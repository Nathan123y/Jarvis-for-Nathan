"""Hand a spoken question to the user's ChatGPT app without API billing.

ChatGPT subscriptions do not expose a response API to other desktop apps. This
action deliberately stops at the user's clipboard and ChatGPT window; it never
pretends to have sent the message or read a reply.
"""

import platform
import subprocess
import webbrowser


CHATGPT_URL = "https://chatgpt.com/"


def ask_chatgpt(parameters: dict, player=None) -> str:
    question = parameters.get("question", "")
    if not isinstance(question, str) or not question.strip():
        return "What would you like me to ask ChatGPT, sir?"

    question = question.strip()
    if len(question) > 20_000:
        return "That question is too long for the ChatGPT handoff. Please shorten it."

    try:
        if platform.system() == "Darwin":
            subprocess.run(
                ["pbcopy"], input=question, text=True, check=True,
                capture_output=True, timeout=5,
            )
        else:
            import pyperclip
            pyperclip.copy(question)
    except Exception:
        return "I could not copy the question to the clipboard, so I did not open ChatGPT."

    opened = False
    if platform.system() == "Darwin":
        try:
            subprocess.run(
                ["open", "-a", "ChatGPT"], check=True,
                capture_output=True, timeout=8,
            )
            opened = True
        except (OSError, subprocess.SubprocessError):
            pass
    if not opened:
        try:
            opened = webbrowser.open(CHATGPT_URL)
        except Exception:
            pass

    if not opened:
        return "I copied your question, but could not open ChatGPT. Open it and paste the question."

    if player:
        try:
            player.write_log("[ChatGPT] Opened ChatGPT with a question on the clipboard")
        except Exception:
            pass
    return (
        "I opened ChatGPT and copied your question, sir. "
        "Paste and send it there. I cannot read its reply back into Jarvis through your ChatGPT account."
    )


TOOL = {
    "name": "ask_chatgpt",
    "description": (
        "When the user explicitly says 'ask ChatGPT' or wants to send a specific question "
        "to their ChatGPT account, pass the exact question to this tool. It opens ChatGPT "
        "and copies the question for the user to paste and send. It does NOT send the "
        "question, read ChatGPT's answer, or answer on ChatGPT's behalf. For ordinary "
        "questions, answer normally without this tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING", "description": "The user's exact question for ChatGPT"},
        },
        "required": ["question"],
    },
    "handler": ask_chatgpt,
}
