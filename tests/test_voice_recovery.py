"""The speaker must recover from device errors without losing future replies."""

import ast
import asyncio
import contextlib
import io
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np


SOURCE = Path(__file__).resolve().parents[1] / "main.py"


def _method(name, namespace):
    module = ast.parse(SOURCE.read_text(encoding="utf-8"))
    owner = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
    method = next(n for n in owner.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    stub = ast.ClassDef(name="Player", bases=[], keywords=[], body=[method], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[])),
                 str(SOURCE), "exec"), namespace)
    return namespace["Player"]


class VoiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_speaker_device_failure_raises_for_session_recovery(self):
        class BrokenSpeaker:
            latency = 0.05
            stopped = closed = False

            def start(self):
                pass

            def write(self, data):
                raise RuntimeError("output device disconnected")

            def stop(self):
                self.stopped = True

            def close(self):
                self.closed = True

        speaker = BrokenSpeaker()
        namespace = {
            "asyncio": asyncio, "time": time, "np": np,
            "ThreadPoolExecutor": ThreadPoolExecutor,
            "audio_devices": SimpleNamespace(resolve=lambda *args: None),
            "get_output_device": lambda: None,
            "sd": SimpleNamespace(RawOutputStream=lambda **kwargs: speaker),
            "RECEIVE_SAMPLE_RATE": 24000, "CHANNELS": 1, "CHUNK_SIZE": 1024,
            "_TAIL_MARGIN": 0.25, "_CURSOR_SLACK": 0.15,
            "_FIRST_SOUND": 1024 / 24000,
            "_pcm_visemes": lambda *args, **kwargs: [],
            "_pcm_level": lambda samples: 0.0,
            "playback_idle": lambda *args: False,
        }
        Player = _method("_play_audio", namespace)
        player = Player()
        player.audio_in_queue = asyncio.Queue()
        player.audio_in_queue.put_nowait(b"\x00\x00" * 4800)
        player._turn_done_event = asyncio.Event()
        player._turn_done_event.set()
        player._is_speaking = False
        player._play_cursor = 0.0
        player._out_latency = 0.05
        player._echo = SimpleNamespace(note_output=lambda *args: None)
        player.ui = SimpleNamespace(set_audio_level=lambda value: None)

        def set_speaking(value):
            player._is_speaking = value

        player.set_speaking = set_speaking
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "output device disconnected"):
                await asyncio.wait_for(player._play_audio(), 1)
        self.assertTrue(speaker.stopped)
        self.assertTrue(speaker.closed)
        self.assertFalse(player._is_speaking)

    async def test_tool_state_does_not_override_active_speech(self):
        states = []
        namespace = {
            "types": SimpleNamespace(FunctionResponse=lambda **kwargs: kwargs),
            "update_memory": lambda value: None,
        }
        Player = _method("_execute_tool", namespace)
        player = Player()
        player._is_speaking = True
        player.ui = SimpleNamespace(muted=False, set_state=states.append)
        fc = SimpleNamespace(name="save_memory", id="1",
                             args={"category": "notes", "key": "test", "value": "ok"})
        with contextlib.redirect_stdout(io.StringIO()):
            result = await player._execute_tool(fc)
        self.assertEqual(result["response"]["result"], "ok")
        self.assertEqual(states, [])

        player._is_speaking = False
        with contextlib.redirect_stdout(io.StringIO()):
            await player._execute_tool(fc)
        self.assertEqual(states, ["THINKING", "LISTENING"])


if __name__ == "__main__":
    unittest.main()
