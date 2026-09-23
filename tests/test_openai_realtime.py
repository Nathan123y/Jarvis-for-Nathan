import asyncio
import base64
import json
import unittest
from types import SimpleNamespace

from core.openai_realtime import OpenAIRealtimeSession, openai_tools


class _FakeSocket:
    def __init__(self, events=()):
        self.sent = []
        self.events = iter(events)

    async def send(self, value):
        self.sent.append(json.loads(value))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration


class OpenAIRealtimeTests(unittest.TestCase):
    def test_tool_schema_is_converted_to_json_schema(self):
        tools = openai_tools([{
            "name": "example",
            "parameters": {"type": "OBJECT", "properties": {"q": {"type": "STRING"}}},
        }])
        self.assertEqual(tools[0]["parameters"]["type"], "object")
        self.assertEqual(tools[0]["parameters"]["properties"]["q"]["type"], "string")

    def test_audio_is_base64_encoded(self):
        socket = _FakeSocket()
        session = OpenAIRealtimeSession(socket)
        asyncio.run(session.send_realtime_input(SimpleNamespace(data=b"pcm")))
        self.assertEqual(socket.sent[0]["type"], "input_audio_buffer.append")
        self.assertEqual(socket.sent[0]["audio"], base64.b64encode(b"pcm").decode())

    def test_function_call_event_is_normalized(self):
        event = json.dumps({
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "consult_sol",
            "arguments": '{"task":"explain it"}',
        })
        session = OpenAIRealtimeSession(_FakeSocket([event]))

        async def collect():
            return [item async for item in session.receive()]

        result = asyncio.run(collect())[0]
        call = result.tool_call.function_calls[0]
        self.assertEqual(call.name, "consult_sol")
        self.assertEqual(call.args["task"], "explain it")


if __name__ == "__main__":
    unittest.main()
