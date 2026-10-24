"""Control the texts Jarvis sends to the user's phone while they're away.

The texts themselves come from the background worker (worker/alerts.py), so they
keep coming with the Jarvis window closed, and, with lid_awake on, with the lid
closed. Here: on/off, which kinds, a test text, and the lid-closed setting.
"""
from __future__ import annotations

import time

from core import phone_alerts as pa
from memory.config_manager import _patch_config, load_api_keys, save_plugin_config

PLUGIN = {
    "name": "phone_alerts",
    "description": (
        "Texts Jarvis sends to the user's own phone (iMessage) while they're away from the Mac: "
        "reminders (Jarvis and Apple Reminders), important emails, emails from VIP senders, calendar "
        "events starting soon, website business news, big trading losses, Canvas work due soon, "
        "Canvas grades posted, memecoin bot trades and big moves, Gumroad sales, "
        "fantasy football injuries and lineup problems, packages, Jarvis problems; plus a morning "
        "text, a weekday trading summary, the bots' nightly review, an 8 PM 'still due tonight' "
        "Canvas check and a Sunday recap of the week, sent at set times. Use for 'text me when…', 'stop "
        "texting me about Canvas', 'turn phone alerts off/on', 'send me a test text', 'are phone "
        "alerts on?', 'keep the Mac awake with the lid closed', 'always text me when Ms. Lee "
        "emails' (vip_add, value=name/email/domain), 'stop texting me about her emails' (vip_remove), "
        "'who are my VIPs' (vip_list), 'text me my morning summary now' (send_now, kind=morning), "
        "'text me the trading summary now' (send_now, kind=trading_daily), 'text me a fantasy summary' "
        "(send_now, kind=fantasy), 'text me my weekly recap' (send_now, kind=weekly); add in_minutes for 'in 3 minutes'. Actions: status, on, off, "
        "mute (kind), unmute (kind), test, lid_awake (state=on|off), vip_add, vip_remove, vip_list, "
        "send_now. Only for texts to the user's own phone; to text someone else use send_message."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["status", "on", "off", "mute", "unmute", "test", "lid_awake",
                                                  "vip_add", "vip_remove", "vip_list", "send_now"]},
            "kind": {"type": "STRING", "enum": list(pa.KINDS), "description": "For mute/unmute/send_now"},
            "value": {"type": "STRING", "description": "For vip_add/vip_remove: a name, email address or domain"},
            "in_minutes": {"type": "INTEGER", "description": "For send_now: wait this many minutes first (0-720)"},
            "state": {"type": "STRING", "enum": ["on", "off"], "description": "For lid_awake"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": pa.NS,
    "title": "Phone alerts (texts to you while you're away)",
    "fields": [
        {"key": "my_number", "type": "text", "label": "Your phone number (blank = your Contacts card)"},
        {"key": "away_minutes", "type": "text", "label": "Minutes idle before you count as away", "placeholder": "5"},
        {"key": "quiet_start", "type": "text", "label": "Quiet hours start (hour, 0-23)", "placeholder": "23"},
        {"key": "quiet_end", "type": "text", "label": "Quiet hours end (hour, 0-23)", "placeholder": "7"},
        {"key": "loss_alert", "type": "text", "label": "Text me when a paper trade loses more than ($)", "placeholder": "500"},
        {"key": "canvas_hours", "type": "text", "label": "Warn about Canvas work due within (hours)", "placeholder": "3"},
        {"key": "calendar_minutes", "type": "text", "label": "Text me before calendar events (minutes)", "placeholder": "15"},
        {"key": "morning_time", "type": "text", "label": "Morning text time (HH:MM)", "placeholder": "07:00"},
        {"key": "trading_time", "type": "text", "label": "Weekday trading summary time (HH:MM)", "placeholder": "13:30"},
        {"key": "tonight_time", "type": "text", "label": "'Still due tonight' Canvas check time (HH:MM)", "placeholder": "20:00"},
        {"key": "weekly_time", "type": "text", "label": "Sunday recap time (HH:MM)", "placeholder": "18:00"},
        {"key": "meme_move", "type": "text", "label": "Text me when a memecoin moves this much (%)", "placeholder": "20"},
        {"key": "city", "type": "text", "label": "City for the morning weather", "placeholder": "San Jose"},
    ],
}

LID_SETUP = (
    "One-time setup so Jarvis may keep the Mac awake with the lid closed. Paste this into "
    "Terminal (it asks for your Mac password once):\n\n"
    "echo \"$USER ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 1, /usr/bin/pmset -a disablesleep 0\" "
    "> /tmp/jarvis-lid-awake && visudo -cf /tmp/jarvis-lid-awake && "
    "sudo install -m 0440 -o root -g wheel /tmp/jarvis-lid-awake /etc/sudoers.d/jarvis-lid-awake\n\n"
    "It lets Jarvis switch exactly one setting (sleep on lid close) on and off, nothing else."
)


def _store():
    from core.events import Store
    return Store()


def _status() -> str:
    cfg = pa.settings()
    if not cfg["enabled"]:
        return "Phone alerts are off. Say 'turn phone alerts on' to start them."
    on = [label for k, label in pa.KINDS.items() if k not in cfg["off"]]
    off = [label for k, label in pa.KINDS.items() if k in cfg["off"]]
    try:
        number = pa.my_handle(cfg)
        where = f"by iMessage to the number ending {number[-4:]}"
    except Exception as exc:
        where = f"but {exc}"
    parts = [f"Phone alerts are on, {where}, when you've been away {cfg['away_minutes']} minutes or the screen is locked.",
             f"I text you about {', '.join(on) or 'nothing'}."]
    if off:
        parts.append(f"Muted: {', '.join(off)}.")
    from memory.config_manager import get_plugin_config
    if "grades" not in cfg["off"] and not str(get_plugin_config("canvas").get("token") or "").strip():
        parts.append("Grade texts need a Canvas access token in Plugin Settings; the calendar feed has no grades.")
    if cfg["quiet_start"] != cfg["quiet_end"]:
        parts.append(f"From {cfg['quiet_start']}:00 to {cfg['quiet_end']}:00 only reminders come through; "
                     "the rest wait for one text in the morning.")
    keys = load_api_keys()
    if keys.get("worker_keep_awake_lid"):
        from worker.runtime import LID_RULE, lid_sleep_disabled
        if not LID_RULE.exists():
            parts.append("Lid-closed mode is on but needs its one-time setup, so the Mac still sleeps when you close it.")
        else:
            floor = keys.get("worker_keep_awake_min_battery", 40)
            state = "active now" if lid_sleep_disabled() else "waiting for power"
            parts.append(f"The Mac stays awake with the lid closed ({state}) while charging or above {floor}% battery.")
    else:
        parts.append("With the lid closed the Mac sleeps, so texts stop until you open it.")
    return " ".join(parts)


def _set_kind(kind: str, muted: bool) -> str:
    if kind not in pa.KINDS:
        return f"Which kind? {', '.join(pa.KINDS)}."
    off = [k for k in pa.settings()["off"] if k != kind] + ([kind] if muted else [])
    save_plugin_config(pa.NS, {"off": off})
    return (f"Okay, no more texts about {pa.KINDS[kind]}." if muted
            else f"Okay, I'll text you about {pa.KINDS[kind]} again.")


def _lid(state: str, player) -> str:
    from worker.runtime import LID_RULE
    if state == "off":
        _patch_config(worker_keep_awake_lid=False)
        return "Okay, the Mac will sleep again when you close the lid, so texts pause until you open it."
    _patch_config(worker_keep_awake=True, worker_keep_awake_lid=True)
    floor = load_api_keys().get("worker_keep_awake_min_battery", 40)
    if not LID_RULE.exists():
        if player:
            player.show_content("LID-CLOSED MODE — ONE-TIME SETUP", LID_SETUP)
        return ("Lid-closed mode is switched on, but macOS needs a one-time approval first. I put a "
                "single Terminal command on screen; run it, type your Mac password, and you're set.")
    return (f"Done. The Mac stays awake with the lid closed while it's charging or above {floor}% "
            "battery, so the texts keep coming. Don't zip it into a bag for long; it will get warm.")


def _vip(action: str, value: str) -> str:
    vips = pa.settings()["vip"]
    if action == "vip_list":
        return ("I always text you when these email: " + ", ".join(vips) + "." if vips
                else "No VIP senders yet. Say 'always text me when <name> emails'.")
    value = " ".join(value.split()).strip(" .,'\"")
    if not value:
        return "Whose emails? Give me a name, an email address or a domain."
    if action == "vip_add":
        if value.lower() not in (v.lower() for v in vips):
            save_plugin_config(pa.NS, {"vip": vips + [value]})
        return (f"Okay, I'll text you whenever {value} emails, even if Gmail doesn't mark it important. "
                "Mail already in your inbox doesn't count.")
    left = [v for v in vips if v.lower() != value.lower()]
    if len(left) == len(vips):
        return f"{value} isn't on your VIP list." + (f" It has: {', '.join(vips)}." if vips else "")
    save_plugin_config(pa.NS, {"vip": left})
    return f"Okay, no more special texts for {value}'s emails."


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "status").strip().lower()
    try:
        if action == "status":
            return _status()
        if action in ("on", "off"):
            save_plugin_config(pa.NS, {"enabled": action == "on"})
            return ("Phone alerts are on. I'll text you important things while you're away from the Mac."
                    if action == "on" else "Phone alerts are off. I won't text your phone.")
        if action in ("mute", "unmute"):
            return _set_kind(str(args.get("kind") or "").strip().lower(), action == "mute")
        if action == "test":
            cfg = pa.settings()
            if not cfg["my_number"]:
                # The worker runs without Contacts access, so look the number up here and save it.
                save_plugin_config(pa.NS, {"my_number": pa.my_handle(cfg)})
            _store().set(pa.TEST_FLAG, time.time())
            return ("The background worker will text you a test within about 30 seconds. If macOS asks "
                    "whether Python may control Messages, click OK; that's what lets texts go out "
                    "with Jarvis closed.")
        if action.startswith("vip_"):
            return _vip(action, str(args.get("value") or ""))
        if action == "send_now":
            kind = str(args.get("kind") or "morning").strip().lower()
            if kind not in ("morning", "trading_daily", "fantasy", "weekly"):
                return "I can text you the morning summary, the trading summary, a fantasy summary or the weekly recap."
            try:
                wait = max(0, min(720, int(args.get("in_minutes") or 0)))
            except (TypeError, ValueError):
                wait = 0
            _store().set(pa.NOW_FLAG, {"kind": kind, "at": time.time() + wait * 60})
            what = {"morning": "the morning summary", "trading_daily": "the trading summary",
                    "fantasy": "a fantasy summary", "weekly": "your weekly recap"}[kind]
            if wait:
                return (f"Okay, I'll text you {what} in {wait} minute{'s' if wait != 1 else ''}. "
                        "You can close the lid; it'll still come.")
            return f"Putting together {what} now; it'll be on your phone in a minute or two."
        if action == "lid_awake":
            return _lid(str(args.get("state") or "on").strip().lower(), player)
        return "Phone alert actions: status, on, off, mute, unmute, test, lid_awake, vip_add, vip_remove, vip_list, send_now."
    except Exception as exc:
        return f"Phone alerts {action} failed ({type(exc).__name__}: {str(exc)[:120]})."
