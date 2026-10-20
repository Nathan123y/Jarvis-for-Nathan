"""One phone call: Twilio's media stream on one side, the AI on the other.

Twilio sends JSON over a websocket ("start", then a "media" frame every 20 ms of
mu-law audio, then "stop"). Audio goes to the AI as it arrives; the AI's speech
comes back in 24 kHz chunks and is sent to Twilio as mu-law. When the caller
talks over the AI, Twilio is told to "clear" what it has queued, so the AI
stops mid-sentence like a person would. Tool requests (find the business, email
the link, leave a note, hang up) are run by `actions`.

Whatever happens, the call is written up when it ends: who called, which
business, what the AI did, the transcript and how long it took.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from worker.phone import agent
from worker.phone.audio import GeminiToTwilio, twilio_to_gemini

MAX_SECONDS = 8 * 60          # a runaway call costs money; the prompt aims for five
GOODBYE_WAIT = 12.0           # how long to let the last words play before hanging up
QUIET_BEFORE_HANGUP = 1.2     # the AI may ask to hang up before it has finished saying goodbye


@dataclass
class CallRecord:
    call_sid: str = ""
    caller: str = ""
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    business: Optional[agent.Business] = None
    transcript: list = field(default_factory=list)    # [("caller"|"assistant", text)]
    actions: list = field(default_factory=list)       # what the tools did, in order
    outcome: str = ""
    ended_by: str = ""

    def add_line(self, who: str, text: str) -> None:
        # Live transcription arrives in fragments; join a speaker's fragments.
        text = text.strip()
        if not text:
            return
        if self.transcript and self.transcript[-1][0] == who:
            self.transcript[-1] = (who, f"{self.transcript[-1][1]} {text}".strip())
        else:
            self.transcript.append((who, text))


class Twilio:
    """The media-stream websocket, as the call sees it."""

    def __init__(self, receive_text: Callable[[], Awaitable[str]], send_text: Callable[[str], Awaitable[None]],
                 close: Callable[[], Awaitable[None]]):
        self.receive_text, self.send_text, self.close = receive_text, send_text, close
        self.stream_sid = ""

    async def play(self, ulaw: bytes) -> None:
        if ulaw:
            await self.send_text(json.dumps({"event": "media", "streamSid": self.stream_sid,
                                             "media": {"payload": base64.b64encode(ulaw).decode()}}))

    async def clear(self) -> None:
        await self.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))

    async def mark(self, name: str) -> None:
        await self.send_text(json.dumps({"event": "mark", "streamSid": self.stream_sid,
                                         "mark": {"name": name}}))


class Call:
    def __init__(self, twilio: Twilio, live_connect, directory: agent.Directory,
                 run_tool: Callable[["Call", str, dict], Awaitable[str]], *,
                 owner: str = "Nathan", assistant: str = "Ava", terms: Optional[dict] = None,
                 max_seconds: float = MAX_SECONDS, log: Callable[[str], None] = print,
                 token_ok: Callable[[str], bool] = lambda token: True):
        self.twilio, self.live_connect, self.directory = twilio, live_connect, directory
        self.run_tool, self.owner, self.assistant, self.terms = run_tool, owner, assistant, terms
        self.max_seconds, self.log, self.token_ok = max_seconds, log, token_ok
        self.record = CallRecord()
        self._hangup = asyncio.Event()
        self._goodbye_played = asyncio.Event()
        self._last_audio = 0.0

    async def _await_start(self) -> bool:
        while True:
            msg = json.loads(await self.twilio.receive_text())
            if msg.get("event") == "start":
                start = msg.get("start") or {}
                self.twilio.stream_sid = start.get("streamSid") or msg.get("streamSid", "")
                params = start.get("customParameters") or {}
                self.record.call_sid = start.get("callSid", "") or params.get("callSid", "")
                self.record.caller = params.get("from", "")
                # Only a stream opened by our own signed /voice answer carries a live token.
                if not self.token_ok(params.get("token", "")):
                    self.log("[Phone] refused a media stream without a valid token")
                    self.record.ended_by = "refused"
                    return False
                return True
            if msg.get("event") == "stop":
                return False

    async def run(self) -> CallRecord:
        try:
            if not await self._await_start():
                return self._finish("no_start")
            self.record.business = self.directory.by_phone(self.record.caller)
            system = agent.system_prompt(self.owner, self.assistant, self.record.business, self.terms)
            async with self.live_connect(system, agent.TOOLS) as live:
                await live.send_text("The call has just connected. Greet the caller now.")
                jobs = [asyncio.create_task(self._from_caller(live)),
                        asyncio.create_task(self._to_caller(live)),
                        asyncio.create_task(self._hangup.wait())]
                done, pending = await asyncio.wait(jobs, timeout=self.max_seconds,
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    self.record.ended_by = "time_limit"
                for job in pending:
                    job.cancel()
                for job in done:
                    if not job.cancelled() and job.exception():
                        self.log(f"[Phone] call task failed: {job.exception()!r}")
                        self.record.ended_by = self.record.ended_by or "error"
        except Exception as exc:
            self.log(f"[Phone] call failed: {exc!r}")
            self.record.ended_by = self.record.ended_by or "error"
        finally:
            try:
                await self.twilio.close()
            except Exception:
                pass
        return self._finish(self.record.ended_by or "caller_hung_up")

    def _finish(self, how: str) -> CallRecord:
        self.record.ended = time.time()
        self.record.ended_by = self.record.ended_by or how
        return self.record

    async def _from_caller(self, live) -> None:
        while True:
            msg = json.loads(await self.twilio.receive_text())
            event = msg.get("event")
            if event == "media":
                await live.send_audio(twilio_to_gemini(base64.b64decode(msg["media"]["payload"])))
            elif event == "mark" and (msg.get("mark") or {}).get("name") == "goodbye":
                self._goodbye_played.set()
            elif event == "stop":
                self.record.ended_by = self.record.ended_by or "caller_hung_up"
                return

    async def _to_caller(self, live) -> None:
        speaker = GeminiToTwilio()
        async for event in live.events():
            kind = event[0]
            if kind == "audio":
                self._last_audio = time.monotonic()
                await self.twilio.play(speaker.convert(event[1]))
            elif kind == "interrupted":
                await self.twilio.clear()
            elif kind == "heard":
                self.record.add_line("caller", event[1])
            elif kind == "said":
                self.record.add_line("assistant", event[1])
            elif kind == "tool":
                _, call_id, name, args = event
                if name == "end_call":
                    await live.send_tool_result(call_id, name, "Hanging up.")
                    asyncio.create_task(self._end_after_goodbye())
                    continue
                try:
                    result = await self.run_tool(self, name, args)
                except Exception as exc:
                    self.log(f"[Phone] tool {name} failed: {exc!r}")
                    result = "That didn't work. Apologise and offer to have Nathan follow up."
                self.record.actions.append((name, args, result))
                await live.send_tool_result(call_id, name, result)

    async def _end_after_goodbye(self) -> None:
        """Let the goodbye finish: wait until the AI has stopped sending speech,
        then queue a mark, which Twilio echoes once everything before it played."""
        deadline = time.monotonic() + GOODBYE_WAIT
        while time.monotonic() - self._last_audio < QUIET_BEFORE_HANGUP and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        await self.twilio.mark("goodbye")
        try:
            await asyncio.wait_for(self._goodbye_played.wait(), GOODBYE_WAIT)
        except asyncio.TimeoutError:
            pass
        self.record.ended_by = "assistant_hung_up"
        self._hangup.set()
