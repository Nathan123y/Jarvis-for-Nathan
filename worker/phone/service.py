"""The phone line as a long-running service inside the background worker.

Started by `python3 -m worker run` (the always-on worker), so calls are answered
whether or not the Jarvis window is open. It stays off until the user turns it
on and fills in the Twilio details under Plugin Settings → Phone line, and
writes what it is doing to config/phone_status.json for the Jarvis plugin.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from worker.phone import actions as actions_mod, agent, live, tunnel, twilio_api
from worker.phone.call import Call, Twilio
from worker.phone.server import build_app

PORT = 8770
STATUS_FILE = Path(__file__).resolve().parents[2] / "config" / "phone_status.json"


def credentials() -> dict:
    from memory.config_manager import get_plugin_config
    cfg = get_plugin_config("phone_line")
    return {"enabled": str(cfg.get("enabled") or "").strip().lower() in ("on", "yes", "true", "1"),
            "sid": str(cfg.get("twilio_sid") or "").strip(),
            "token": str(cfg.get("twilio_token") or "").strip(),
            "number": twilio_api.e164(cfg.get("twilio_number") or "")}


def gemini_key() -> str:
    from memory.config_manager import load_api_keys
    return str(load_api_keys().get("gemini_api_key") or "")


def missing() -> list[str]:
    """What still has to be set up before the line can answer, in plain words."""
    c = credentials()
    need = []
    if not c["enabled"]:
        need.append("turn the phone line on")
    if not (c["sid"] and c["token"]):
        need.append("the Twilio Account SID and Auth Token")
    if not c["number"]:
        need.append("the Twilio phone number")
    if not actions_mod.settings()["payment_link"]:
        need.append("the Stripe payment link")
    if not gemini_key():
        need.append("a Gemini API key")
    if not tunnel.binary():
        need.append("cloudflared (the tunnel program)")
    return need


def write_status(**fields) -> None:
    try:
        current = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
    except (OSError, ValueError):
        current = {}
    current.update(fields, updated=time.time())
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=1))
        tmp.replace(STATUS_FILE)
    except OSError:
        pass


class PhoneLine:
    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.public = ""
        self._tunnel: Optional[tunnel.Tunnel] = None

    # ── calls ──
    async def handle_call(self, receive_text, send_text, close, token_ok) -> None:
        from core.events import Store as Events
        from worker.campaign.store import CampaignStore
        cfg = actions_mod.settings()
        tools = actions_mod.PhoneActions(CampaignStore(), Events(), actions_mod.gmail_sender, cfg, self.log)
        key = gemini_key()

        def connect(system, decls):
            return live.connect(key, system, decls)

        call = Call(Twilio(receive_text, send_text, close), connect, tools.directory(), tools,
                    owner=cfg["owner"], assistant=cfg["assistant"],
                    terms=agent.offer(cfg["price"], cfg["hosting"]), log=self.log, token_ok=token_ok)
        record = await call.run()
        if record.ended_by in ("refused", "no_start"):
            return
        try:
            tools.write_up(record)
        except Exception as exc:
            self.log(f"[Phone] couldn't log the call: {exc!r}")
        write_status(last_call=record.started, last_caller=(record.business.name if record.business
                                                             else record.caller))
        self.log(f"[Phone] call ended ({record.ended_by}, {round(record.ended - record.started)}s)")

    # ── plumbing ──
    def _on_url(self, url: str) -> None:
        self.public = url
        c = credentials()
        twilio_api.point_number_at(c["sid"], c["token"], c["number"], url + "/twilio/voice")
        write_status(state="answering", number=c["number"], url=url, error="")
        self.log(f"[Phone] {c['number']} now rings through {url}")

    def start(self) -> bool:
        need = missing()
        if need:
            write_status(state="off", missing=need)
            return False
        c = credentials()
        try:
            account = twilio_api.account_status(c["sid"], c["token"])
        except Exception as exc:
            write_status(state="error", error=str(exc)[:200])
            self.log(f"[Phone] Twilio check failed: {exc}")
            return False
        if account.get("type") == "Trial":
            self.log("[Phone] Twilio account is a trial: callers will hear Twilio's trial notice.")
        write_status(state="starting", missing=[], account_type=account.get("type", ""))
        threading.Thread(target=self._serve, daemon=True, name="phone-server").start()
        self._tunnel = tunnel.Tunnel(PORT, self._on_url, self.log)
        self._tunnel.start()
        return True

    def _serve(self) -> None:
        import uvicorn
        app = build_app(public_base=lambda: self.public, auth_token=lambda: credentials()["token"],
                        handle_call=self.handle_call, log=self.log)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
        asyncio.run(server.serve())

    def stop(self) -> None:
        if self._tunnel:
            self._tunnel.stop()
        write_status(state="off")


def start_if_configured(log: Callable[[str], None] = print) -> Optional[PhoneLine]:
    line = PhoneLine(log)
    try:
        return line if line.start() else None
    except Exception as exc:
        log(f"[Phone] couldn't start: {exc!r}")
        write_status(state="error", error=str(exc)[:200])
        return None
