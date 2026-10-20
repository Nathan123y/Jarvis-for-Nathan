"""Send the Mac's sound (music, videos, anything in Chrome) to the user's big
speakers, or back to the MacBook, while Jarvis keeps talking on the MacBook.

Jarvis's own voice is opened on a named device in its audio settings, so it
does not follow the system output. The speakers count as "off" when macOS no
longer lists them, which is what happens when the monitor they hang off is off
or unplugged.
"""
from __future__ import annotations

from memory.config_manager import get_output_device, get_plugin_config


PLUGIN = {
    "name": "speakers",
    "description": (
        "Choose where the Mac's sound plays: the user's big speakers (a TV sound bar on "
        "their monitor) or the MacBook. Use for 'play it on the speakers', 'switch to my "
        "speakers', 'put the sound on the MacBook', 'are my speakers on'. To play music on "
        "the speakers, use youtube_music with speakers true instead. Jarvis's own voice "
        "always stays on the MacBook."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["speakers", "macbook", "status"],
                       "description": "speakers: sound to the big speakers; macbook: back "
                                      "to the MacBook; status: which is playing"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "speakers",
    "title": "Speakers",
    "fields": [
        {"key": "speaker_name", "type": "text", "label": "Big speakers (sound output name)",
         "placeholder": "P24h-10"},
        {"key": "mac_name", "type": "text", "label": "MacBook speakers (sound output name)",
         "placeholder": "MacBook Air Speakers"},
    ],
}

SPEAKERS_OFF = "Your speakers are off. Turn them on so I can play there."


def _names() -> tuple[str, str]:
    cfg = get_plugin_config("speakers")
    return (str(cfg.get("speaker_name") or "P24h-10").strip(),
            str(cfg.get("mac_name") or "MacBook Air Speakers").strip())


def route_to_speakers() -> str | None:
    """Send system sound to the big speakers. None on success, otherwise the
    sentence to say instead (they're off, or switching failed)."""
    from core import system_output
    speaker, _ = _names()
    try:
        if system_output.find(speaker) is None:
            return SPEAKERS_OFF
        if not system_output.switch_to(speaker):
            return "I couldn't switch the sound to your speakers."
    except OSError as e:
        return str(e)
    return None


def _voice_note() -> str:
    # With Jarvis on "System default" its voice would move to the speakers too.
    return ("" if get_output_device() else
            " My voice is set to the system default, so it moved too; pick the MacBook "
            "speakers in my audio settings to keep it there.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    from core import system_output
    action = str((parameters or {}).get("action") or "status").strip().lower()
    speaker, mac = _names()
    try:
        if action == "speakers":
            problem = route_to_speakers()
            return problem or f"Sound is playing on your speakers now.{_voice_note()}"
        if action == "macbook":
            if not system_output.switch_to(mac):
                return f"I couldn't find the MacBook speakers ({mac})."
            return "Sound is back on the MacBook speakers."
        if action == "status":
            if system_output.find(speaker) is None:
                return "Your speakers are off or unplugged, so sound is on the MacBook."
            where = system_output.current()
            return ("Your speakers are on and sound is playing on them." if where == system_output.find(speaker)
                    else f"Your speakers are on, but sound is playing on {where or 'another output'}.")
        return "Speaker commands are speakers, macbook, or status."
    except OSError as e:
        return str(e)
    except Exception as e:
        return f"I couldn't change the sound output: {e}"
