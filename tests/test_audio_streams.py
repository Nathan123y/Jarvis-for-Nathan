"""The microphone and speaker survive Live session rotations.

Reopening them on every rotation is what froze Jarvis after a long mute:
stopping a CoreAudio stream can deadlock, and the session never came back.
"""

import asyncio
import contextlib
import io
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np

from core.audio_streams import KeptStream, close_stream
from test_voice_recovery import _method


class FakeStream:
    latency = 0.05

    def __init__(self, hang: threading.Event | None = None):
        self.active = False
        self.stopped = self.closed = False
        self.hang = hang

    def start(self):
        self.active = True

    def write(self, data):
        return False

    def stop(self):
        if self.hang is not None:
            self.hang.wait()
        self.stopped, self.active = True, False

    def close(self):
        self.closed = True


class CloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_wedged_close_is_abandoned_not_awaited_forever(self):
        release = threading.Event()
        stream = FakeStream(hang=release)
        began = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            closed = await close_stream(stream, timeout=0.1)
        self.assertFalse(closed)
        self.assertLess(time.monotonic() - began, 1.0)
        release.set()

    async def test_a_normal_close_stops_and_closes(self):
        stream = FakeStream()
        self.assertTrue(await close_stream(stream))
        self.assertTrue(stream.stopped and stream.closed)


class KeptStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_reused_only_for_the_same_key_while_running(self):
        kept, stream = KeptStream(), FakeStream()
        stream.start()
        kept.keep(stream, "MacBook Air Microphone")
        self.assertTrue(kept.reusable("MacBook Air Microphone"))
        self.assertFalse(kept.reusable("AirPods"))
        stream.active = False
        self.assertFalse(kept.reusable("MacBook Air Microphone"))

    async def test_close_releases_stream_and_executor(self):
        kept, stream = KeptStream(), FakeStream()
        executor = ThreadPoolExecutor(max_workers=1)
        kept.keep(stream, None, executor)
        await kept.close()
        self.assertTrue(stream.closed)
        self.assertIsNone(kept.stream)
        with self.assertRaises(RuntimeError):
            executor.submit(lambda: None)


class SpeakerRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_speaker_is_opened_once_across_session_rotations(self):
        opened = []

        def open_speaker(**kwargs):
            opened.append(FakeStream())
            return opened[-1]

        namespace = {
            "asyncio": asyncio, "time": time, "np": np,
            "ThreadPoolExecutor": ThreadPoolExecutor,
            "audio_devices": SimpleNamespace(resolve=lambda *args: None),
            "get_output_device": lambda: None,
            "get_output_latency": lambda: None,
            "parse_output_latency": lambda value: None,
            "log_audio_write": lambda *args, **kwargs: None,
            "sd": SimpleNamespace(RawOutputStream=open_speaker),
            "RECEIVE_SAMPLE_RATE": 24000, "CHANNELS": 1, "CHUNK_SIZE": 1024,
            "_TAIL_MARGIN": 0.25, "_CURSOR_SLACK": 0.15,
            "_FIRST_SOUND": 1024 / 24000,
            "_pcm_visemes": lambda *args, **kwargs: [],
            "_pcm_level": lambda samples: 0.0,
            "playback_idle": lambda *args: False,
        }
        player = _method("_play_audio", namespace)()
        player._kept_spk = KeptStream()
        player._turn_done_event = asyncio.Event()
        player._is_speaking = False
        player._play_cursor = 0.0
        player._out_latency = 0.05
        player.set_speaking = lambda value, reason=None: None
        player.ui = SimpleNamespace(set_audio_level=lambda value: None)

        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):              # three sessions, each torn down by cancel
                player.audio_in_queue = asyncio.Queue()
                task = asyncio.create_task(player._play_audio())
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertEqual(len(opened), 1)
        self.assertFalse(opened[0].stopped, "rotation must not stop the speaker")
        await player._kept_spk.close()


class MicRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mic_is_opened_once_across_session_rotations(self):
        opened = []

        def open_mic(**kwargs):
            opened.append(FakeStream())
            return opened[-1]

        namespace = {
            "asyncio": asyncio,
            "sd": SimpleNamespace(InputStream=open_mic),
            "audio_devices": SimpleNamespace(resolve=lambda *args: None),
            "get_input_device": lambda: None,
            "SEND_SAMPLE_RATE": 16000, "CHANNELS": 1, "CHUNK_SIZE": 1024,
        }
        player = _method("_listen_audio", namespace)()
        player._kept_mic = KeptStream()

        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                player._mic_ready = asyncio.Event()
                task = asyncio.create_task(player._listen_audio())
                await asyncio.wait_for(player._mic_ready.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].active, "rotation must not stop the microphone")
        await player._kept_mic.close()


if __name__ == "__main__":
    unittest.main()
