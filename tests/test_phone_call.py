"""The phone line's call bridge, with a fake Twilio stream and a scripted fake AI."""
import asyncio
import base64
import contextlib
import json
import unittest

import numpy as np

from worker.phone import agent, audio, call as call_mod

BIZ = {"id": 7, "name": "Joe's Tacos", "category": "restaurant", "city": "San Jose",
       "address": "1 Main St, San Jose", "phone": "(408) 555-0100", "website": "",
       "data": {"preview_url": "https://example.github.io/joes/",
                "audit": {"problems": [{"detail": "No website, so search shows only a map pin."}]}}}


class FakeTwilio:
    def __init__(self, caller="+14085550100"):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.inbox.put_nowait(json.dumps({"event": "connected"}))
        self.inbox.put_nowait(json.dumps({"event": "start", "start": {
            "streamSid": "MZ1", "callSid": "CA1", "customParameters": {"from": caller}}}))

    def say(self, seconds=0.1):
        ulaw = audio.pcm16_to_ulaw(np.zeros(int(8000 * seconds), np.int16))
        self.inbox.put_nowait(json.dumps({"event": "media", "media": {"payload": base64.b64encode(ulaw).decode()}}))

    def hang_up(self):
        self.inbox.put_nowait(json.dumps({"event": "stop"}))

    async def receive_text(self):
        return await self.inbox.get()

    async def send_text(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["event"] == "mark":                       # Twilio echoes marks once played
            self.inbox.put_nowait(json.dumps({"event": "mark", "mark": msg["mark"]}))

    async def close(self):
        self.closed = True

    def wrap(self):
        return call_mod.Twilio(self.receive_text, self.send_text, self.close)


class FakeAI:
    """Plays a script of events; records what the call sent it."""

    def __init__(self, script, wait_for_audio=0):
        self.script, self.wait_for_audio = script, wait_for_audio
        self.audio_in, self.texts, self.results = 0, [], []
        self.system = ""
        self._results = asyncio.Queue()

    @contextlib.asynccontextmanager
    async def connect(self, system, tools):
        self.system, self.tools = system, tools
        yield self

    async def send_audio(self, pcm):
        self.audio_in += len(pcm)

    async def send_text(self, text):
        self.texts.append(text)

    async def send_tool_result(self, call_id, name, result):
        self.results.append((name, result))
        self._results.put_nowait(result)

    async def events(self):
        while self.audio_in < self.wait_for_audio:
            await asyncio.sleep(0.01)
        for event in self.script:
            yield event
            if event[0] == "tool":
                await self._results.get()                # a real model waits for the result
        await asyncio.sleep(3600)


def run_call(twilio, ai, tools=None, **kw):
    async def tool(call, name, args):
        (tools or {}).setdefault("calls", []).append((name, args))
        return {"find_business": "Found Joe's Tacos.", "send_link": "Sent to joe@tacos.com.",
                "take_message": "Noted."}[name]
    directory = agent.Directory([BIZ])
    c = call_mod.Call(twilio.wrap(), ai.connect, directory, tool, **kw)
    return asyncio.run(asyncio.wait_for(c.run(), 10)), c


class CallTests(unittest.TestCase):
    def test_a_full_call_greets_sells_and_hangs_up(self):
        twilio = FakeTwilio()
        twilio.say(0.5)
        speech = np.zeros(2400, np.int16).tobytes()          # 100 ms at 24 kHz
        ai = FakeAI([
            ("said", "Hi, thanks for calling"), ("said", "about your website."), ("audio", speech),
            ("heard", "Yeah I saw the site"), ("interrupted",),
            ("tool", "t1", "send_link", {"email": "joe@tacos.com"}),
            ("said", "Sent! Goodbye."), ("audio", speech),
            ("tool", "t2", "end_call", {}),
        ], wait_for_audio=1)
        record, _ = run_call(twilio, ai)
        # It knew the caller from caller ID before saying a word.
        self.assertIn("The caller ID matches this business", ai.system)
        self.assertIn("Joe's Tacos (restaurant)", ai.system)
        self.assertIn("Am I speaking with someone from Joe's Tacos?", ai.system)
        self.assertIn("No website, so search shows only a map pin.", ai.system)
        self.assertEqual(ai.texts, ["The call has just connected. Greet the caller now."])
        self.assertEqual(record.business.name, "Joe's Tacos")
        # Caller audio reached the AI at 16 kHz (double the 8 kHz bytes x2 for 16-bit).
        self.assertEqual(ai.audio_in, 4000 * 2 * 2)
        # AI audio reached Twilio as mu-law, a barge-in cleared it, then a mark, then hang-up.
        events = [m["event"] for m in twilio.sent]
        self.assertEqual(events, ["media", "clear", "media", "mark"])
        self.assertEqual(len(base64.b64decode(twilio.sent[0]["media"]["payload"])), 800)   # 100 ms at 8 kHz
        self.assertEqual(record.transcript, [("assistant", "Hi, thanks for calling about your website."),
                                             ("caller", "Yeah I saw the site"),
                                             ("assistant", "Sent! Goodbye.")])
        self.assertEqual(record.actions, [("send_link", {"email": "joe@tacos.com"}, "Sent to joe@tacos.com.")])
        self.assertEqual(record.ended_by, "assistant_hung_up")
        self.assertTrue(twilio.closed)
        self.assertEqual((record.call_sid, record.caller), ("CA1", "+14085550100"))

    def test_unknown_caller_is_asked_which_business(self):
        twilio = FakeTwilio(caller="+16505550199")
        twilio.hang_up()
        ai = FakeAI([])
        record, _ = run_call(twilio, ai)
        self.assertIsNone(record.business)
        self.assertIn("didn't match a business", ai.system)
        self.assertIn("which business is this for?", ai.system)
        self.assertEqual(record.ended_by, "caller_hung_up")

    def test_caller_hanging_up_ends_the_call(self):
        twilio = FakeTwilio()
        twilio.say(0.1)
        twilio.hang_up()
        record, _ = run_call(twilio, FakeAI([("said", "Hello?")]))
        self.assertEqual(record.ended_by, "caller_hung_up")
        self.assertTrue(twilio.closed)

    def test_a_failing_tool_is_reported_to_the_ai_not_the_caller(self):
        twilio = FakeTwilio()
        ai = FakeAI([("tool", "t1", "take_message", {"outcome": "callback", "summary": "x"})])

        async def broken(call, name, args):
            raise RuntimeError("database locked")
        directory = agent.Directory([BIZ])
        c = call_mod.Call(twilio.wrap(), ai.connect, directory, broken, max_seconds=0.5)
        record = asyncio.run(c.run())
        self.assertIn("offer to have Nathan follow up", ai.results[0][1])
        self.assertEqual(record.ended_by, "time_limit")

    def test_a_stream_without_a_valid_token_never_reaches_the_ai(self):
        twilio = FakeTwilio()
        ai = FakeAI([("said", "should never be heard")])
        c = call_mod.Call(twilio.wrap(), ai.connect, agent.Directory([BIZ]), None,
                          token_ok=lambda token: False, log=lambda m: None)
        record = asyncio.run(c.run())
        self.assertEqual(record.ended_by, "refused")
        self.assertEqual(ai.system, "")                     # no AI session was opened
        self.assertTrue(twilio.closed)

    def test_time_limit_hangs_up(self):
        twilio = FakeTwilio()
        record, _ = run_call(twilio, FakeAI([]), max_seconds=0.2)
        self.assertEqual(record.ended_by, "time_limit")
        self.assertTrue(twilio.closed)


class AgentTests(unittest.TestCase):
    def test_directory_matches_phone_formats_and_spoken_names(self):
        d = agent.Directory([BIZ, {"id": 8, "name": "Joe's Auto Repair", "city": "Santa Clara", "data": {}}])
        self.assertEqual(d.by_phone("+1 408-555-0100").name, "Joe's Tacos")
        self.assertIsNone(d.by_phone("555-0100"))
        self.assertEqual(d.by_name("joes tacos")[0].name, "Joe's Tacos")
        self.assertEqual(d.by_name("joe's auto", city="Santa Clara")[0].name, "Joe's Auto Repair")
        self.assertEqual(d.by_name("pizza palace"), [])

    def test_prompt_discloses_ai_and_transcription_and_limits_promises(self):
        prompt = agent.system_prompt("Nathan", "Ava", None, agent.offer("249", "25"))
        self.assertIn("an AI assistant for Nathan's web design, and this call is transcribed", prompt)
        self.assertIn("price: $249, paid once", prompt)
        self.assertIn("$25 a month", prompt)
        self.assertIn("never say a payment went through", prompt)
        self.assertIn("Nathan will explain them", agent.system_prompt("Nathan", "Ava", None, agent.offer("249")))


if __name__ == "__main__":
    unittest.main()
