"""The web side of the phone line: what Twilio talks to.

    POST /twilio/voice   Twilio asks what to do with an incoming call. Only a
                         request carrying a valid Twilio signature (HMAC of the
                         URL and form, keyed with the account's auth token) gets
                         an answer; it connects the call's audio to /twilio/media
                         with a one-time token.
    WS   /twilio/media   the call's audio. Refused unless its "start" carries a
                         token that /twilio/voice issued in the last minute.

Reached from the internet only through the tunnel (see tunnel.py); it listens
on 127.0.0.1.
"""
import base64
import hashlib
import hmac
import secrets
import time
from html import escape
from typing import Callable
from urllib.parse import urlsplit

# At module level: FastAPI reads the endpoint annotations to know what to inject,
# and can't resolve names imported inside build_app.
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

TOKEN_SECONDS = 60
MAX_CALLS = 3


def twilio_signature(auth_token: str, url: str, params: dict) -> str:
    """Twilio's request signature: base64(HMAC-SHA1(token, url + sorted key+value pairs))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    if not (auth_token and signature):
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)


class Tokens:
    """One-time tokens linking a signed /voice request to its audio stream."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._issued: dict[str, float] = {}
        self.clock = clock

    def issue(self) -> str:
        now = self.clock()
        self._issued = {t: at for t, at in self._issued.items() if now - at < TOKEN_SECONDS}
        token = secrets.token_urlsafe(24)
        self._issued[token] = now
        return token

    def redeem(self, token: str) -> bool:
        at = self._issued.pop(str(token or ""), None)
        return at is not None and self.clock() - at < TOKEN_SECONDS


def twiml_stream(public_base: str, token: str, caller: str, call_sid: str) -> str:
    host = urlsplit(public_base).netloc
    return ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f'<Stream url="wss://{escape(host)}/twilio/media">'
            f'<Parameter name="token" value="{escape(token)}"/>'
            f'<Parameter name="from" value="{escape(caller)}"/>'
            f'<Parameter name="callSid" value="{escape(call_sid)}"/>'
            '</Stream></Connect></Response>')


TWIML_BUSY = ('<?xml version="1.0" encoding="UTF-8"?><Response><Say>Sorry, all lines are busy. '
              'Please call back in a few minutes.</Say><Hangup/></Response>')


def build_app(*, public_base: Callable[[], str], auth_token: Callable[[], str],
              handle_call, log: Callable[[str], None] = print):
    """`handle_call(receive_text, send_text, close, token_ok)` runs one call.
    `public_base()` is the tunnel's current https URL (signatures cover it)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    tokens = Tokens()
    live = {"calls": 0}

    @app.post("/twilio/voice")
    async def voice(request: Request):
        form = dict((await request.form()).items())
        url = public_base().rstrip("/") + "/twilio/voice"
        if not valid_signature(auth_token(), url, form, request.headers.get("X-Twilio-Signature", "")):
            log("[Phone] refused a /voice request without a valid Twilio signature")
            return PlainTextResponse("forbidden", status_code=403)
        if live["calls"] >= MAX_CALLS:
            return Response(TWIML_BUSY, media_type="text/xml")
        return Response(twiml_stream(public_base(), tokens.issue(), form.get("From", ""),
                                     form.get("CallSid", "")), media_type="text/xml")

    @app.websocket("/twilio/media")
    async def media(ws: WebSocket):
        await ws.accept()
        live["calls"] += 1
        try:
            async def receive_text():
                return await ws.receive_text()

            async def close():
                try:
                    await ws.close()
                except Exception:
                    pass
            await handle_call(receive_text, ws.send_text, close, tokens.redeem)
        except WebSocketDisconnect:
            pass
        finally:
            live["calls"] -= 1

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app
