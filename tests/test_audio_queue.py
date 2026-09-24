"""Test the lightweight audio backpressure helper without starting Qt or a live API."""
import ast
import asyncio
import unittest
from pathlib import Path


def _load_enqueue():
    source = Path(__file__).resolve().parents[1] / "main.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    owner = next(node for node in module.body
                 if isinstance(node, ast.ClassDef) and node.name == "JarvisLive")
    method = next(node for node in owner.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_enqueue_audio")
    # Compile only the helper; importing main would start numerous device dependencies.
    fake = ast.ClassDef(name="AudioQueue", bases=[], keywords=[], body=[method],
                        decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[fake], type_ignores=[]))
    namespace = {"asyncio": asyncio}
    exec(compile(code, str(source), "exec"), namespace)
    return namespace["AudioQueue"]


class AudioQueueTests(unittest.TestCase):
    def setUp(self):
        self.player = _load_enqueue()()
        self.player.out_queue = asyncio.Queue(maxsize=2)

    def test_new_audio_replaces_oldest_when_network_stalls(self):
        old_queue = self.player.out_queue
        self.player._enqueue_audio({"data": b"old"}, old_queue)
        self.player._enqueue_audio({"data": b"middle"}, old_queue)
        self.player._enqueue_audio({"data": b"new"}, old_queue)
        self.assertEqual(old_queue.get_nowait()["data"], b"middle")
        self.assertEqual(old_queue.get_nowait()["data"], b"new")

    def test_callback_from_previous_session_is_discarded(self):
        old_queue = self.player.out_queue
        self.player.out_queue = asyncio.Queue(maxsize=2)
        self.player._enqueue_audio({"data": b"stale"}, old_queue)
        self.assertTrue(self.player.out_queue.empty())

    def test_absent_queue_does_not_raise(self):
        self.player.out_queue = None
        self.player._enqueue_audio({"data": b"late"})


if __name__ == "__main__":
    unittest.main()
