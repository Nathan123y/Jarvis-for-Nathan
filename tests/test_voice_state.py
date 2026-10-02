"""Reconnects and overlapping tools must preserve the user's listening state."""
import ast
import asyncio
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from core.voice_state import runtime_state, can_auto_sleep

SOURCE = Path(__file__).resolve().parents[1] / 'main.py'

def player_class(names):
    owner = next(n for n in ast.parse(SOURCE.read_text()).body
                 if isinstance(n, ast.ClassDef) and n.name == 'JarvisLive')
    body = [n for n in owner.body if getattr(n, 'name', None) in names]
    stub = ast.ClassDef(name='Player', bases=[], keywords=[], body=body, decorator_list=[])
    ns = dict(asyncio=asyncio, threading=threading, time=time,
              runtime_state=runtime_state, can_auto_sleep=can_auto_sleep,
              log_state_transition=lambda *a: None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[])), str(SOURCE), 'exec'), ns)
    return ns['Player']

class StateTests(unittest.TestCase):
    def make_player(self, awake=True):
        p = player_class({'_set_ui_state', '_show_session_ready', 'set_speaking'})()
        self.states = []
        p.ui = SimpleNamespace(muted=False, set_state=self.states.append, write_log=lambda x: None)
        p._state_lock = threading.RLock()
        p._trace_ui_state = None
        p._awake = awake
        p._wake_enabled = True
        p._is_speaking = False
        p._pending_tool_batches = 0
        p._ensure_wake_detector = lambda: True
        return p

    def test_reconnect_preserves_awake_and_sleep_choices(self):
        for awake in (True, False):
            p = self.make_player(awake)
            # Repeated connection setup, including fallback without a handle.
            p._show_session_ready()
            p._show_session_ready()
            self.assertEqual(p._awake, awake)
            self.assertEqual(self.states, ['INITIALISING' if awake else 'SLEEPING'])

    def test_tool_completion_and_mic_ready_cannot_override_speech(self):
        p = self.make_player()
        p._is_speaking = True
        p._set_ui_state('THINKING')
        p._set_ui_state('LISTENING')
        self.assertEqual(self.states, ['SPEAKING'])
        p._is_speaking = False
        p._pending_tool_batches = 2
        p._set_ui_state('LISTENING')
        p._pending_tool_batches = 1
        p._set_ui_state('LISTENING')
        p._pending_tool_batches = 0
        p._set_ui_state('LISTENING')
        self.assertEqual(self.states, ['SPEAKING', 'THINKING', 'LISTENING'])

    def test_finishing_tool_while_asleep_does_not_display_listening(self):
        p = self.make_player(False)
        p._set_ui_state('LISTENING')
        self.assertEqual(self.states, ['SLEEPING'])

    def test_sleep_is_deferred_for_work_or_queued_speech(self):
        self.assertTrue(can_auto_sleep(speaking=False, pending_tools=0, queued_audio=False))
        for field in ('speaking', 'pending_tools', 'queued_audio'):
            flags = dict(speaking=False, pending_tools=0, queued_audio=False)
            flags[field] = True
            self.assertFalse(can_auto_sleep(**flags))

if __name__ == '__main__':
    unittest.main()
