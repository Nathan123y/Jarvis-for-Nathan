"""The few Twilio REST calls the phone line needs, over plain HTTPS.

Credentials are the account SID and auth token from Plugin Settings; nothing
here prints or returns them.
"""
from __future__ import annotations

import re
from typing import Optional

API = "https://api.twilio.com/2010-04-01"


class TwilioError(RuntimeError):
    pass


def e164(number: str) -> str:
    digits = re.sub(r"\D", "", str(number or ""))
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if digits else ""


def _request(method: str, sid: str, token: str, path: str, data: Optional[dict] = None,
             params: Optional[dict] = None) -> dict:
    import requests
    r = requests.request(method, f"{API}/Accounts/{sid}{path}", auth=(sid, token), data=data,
                         params=params, timeout=15, allow_redirects=False)
    if r.status_code == 401:
        raise TwilioError("Twilio rejected the Account SID or Auth Token.")
    if r.status_code >= 400:
        try:
            message = r.json().get("message", "")
        except Exception:
            message = ""
        raise TwilioError(f"Twilio said no ({r.status_code}): {message[:160]}")
    return r.json()


def account_status(sid: str, token: str) -> dict:
    """{'status': 'active', 'type': 'Full' | 'Trial', 'name': ...}"""
    data = _request("GET", sid, token, ".json")
    return {"status": data.get("status", ""), "type": data.get("type", ""),
            "name": data.get("friendly_name", "")}


def number_sid(sid: str, token: str, number: str) -> str:
    data = _request("GET", sid, token, "/IncomingPhoneNumbers.json", params={"PhoneNumber": e164(number)})
    found = data.get("incoming_phone_numbers") or []
    if not found:
        raise TwilioError(f"{e164(number)} isn't a number on this Twilio account.")
    return found[0]["sid"]


def point_number_at(sid: str, token: str, number: str, voice_url: str) -> None:
    """Send this number's incoming calls to `voice_url` (our /twilio/voice)."""
    pn = number_sid(sid, token, number)
    _request("POST", sid, token, f"/IncomingPhoneNumbers/{pn}.json",
             data={"VoiceUrl": voice_url, "VoiceMethod": "POST"})
