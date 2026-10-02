"""A slow action must not hold back already-arriving voice packets."""

import ast
import asyncio
import time
import unittest
from core.live_session import seconds_until_go_away
from pathlib import Path
from types import SimpleNamespace


def _receive_method():
    source = Path(__file__).resolve().parents[1] / "main.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    owner = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
    method = next(n for n in owner.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_receive_audio")
    stub = ast.ClassDef(name="Receiver", bases=[], keywords=[], body=[method], decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[]))
    namespace = {"asyncio": asyncio, "time": time,
                 "seconds_until_go_away": seconds_until_go_away}
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
        player._pending_tool_batches = 0
        player.ui = SimpleNamespace(muted=False)
        player._set_ui_state = lambda *a: None
        player.session = FakeSession()
        player.audio_in_queue = asyncio.Queue()
        player._interrupted = False
        player._turn_done_event = asyncio.Event()
        task = asyncio.create_task(player._receive_audio())
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(await asyncio.wait_for(player.audio_in_queue.get(), 1), voice)
            self.assertEqual(player._pending_tool_batches, 1)
            before = time.monotonic()
            player._last_user_speech = before - 200
            release.set()
            for _ in range(30):
                if player._pending_tool_batches == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(player._pending_tool_batches, 0)
            self.assertGreaterEqual(player._last_user_speech, before)
        finally:
            release.set()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(player._pending_tool_batches, 0)

    async def test_go_away_rotates_once_after_speech_drains(self):
        rotated = asyncio.Event()
        calls = []

        class FakeSession:
            async def receive(self):
                yield SimpleNamespace(
                    session_resumption_update=None,
                    go_away=SimpleNamespace(time_left="3s"),
                    data=None, server_content=None, tool_call=None,
                )
                await asyncio.Event().wait()

        player = _receive_method()()
        player._pending_tool_batches = 0
        player.ui = SimpleNamespace(muted=False)
        player._set_ui_state = lambda *a: None
        player.session = FakeSession()
        player.audio_in_queue = asyncio.Queue()
        player._resume_handle = "valid-token"
        player._is_speaking = True
        player.request_reconnect = lambda **kw: (calls.append(kw), rotated.set())
        task = asyncio.create_task(player._receive_audio())
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(rotated.is_set())
            player._is_speaking = False
            await asyncio.wait_for(rotated.wait(), 1)
            self.assertEqual(calls, [{"keep_context": True, "reason": "server rotation"}])
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(player._pending_tool_batches, 0)


if __name__ == "__main__":
    unittest.main()
