"""A slow action must not hold back already-arriving voice packets."""

import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace


def _receive_method():
    source = Path(__file__).resolve().parents[1] / "main.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    owner = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
    method = next(n for n in owner.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_receive_audio")
    stub = ast.ClassDef(name="Receiver", bases=[], keywords=[], body=[method], decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[]))
    namespace = {"asyncio": asyncio}
    exec(compile(code, str(source), "exec"), namespace)
    return namespace["Receiver"]


class VoiceReceiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_tool_does_not_block_voice_packets(self):
        started = asyncio.Event()
        release = asyncio.Event()
        voice = b"\x01\x00" * 1200
        function_call = SimpleNamespace(name="lookup", id="1")

        class FakeSession:
            async def receive(self):
                common = {"session_resumption_update": None, "server_content": None}
                yield SimpleNamespace(**common, data=None,
                                      tool_call=SimpleNamespace(function_calls=[function_call]))
                yield SimpleNamespace(**common, data=voice, tool_call=None)
                await asyncio.Event().wait()

            async def send_tool_response(self, **kwargs):
                pass

        class Player(_receive_method()):
            async def _execute_tool(self, fc):
                started.set()
                await release.wait()
                return "done"

            async def _flush_pending_vision(self):
                return False

        player = Player()
        player.session = FakeSession()
        player.audio_in_queue = asyncio.Queue()
        player._interrupted = False
        player._turn_done_event = asyncio.Event()
        task = asyncio.create_task(player._receive_audio())
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(await asyncio.wait_for(player.audio_in_queue.get(), 1), voice)
        finally:
            release.set()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
