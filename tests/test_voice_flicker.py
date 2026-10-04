"""The HUD must not claim 'sleeping' while Jarvis is awake, and every state
change must say why it happened."""
import ast
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.live_session import safe_label
from core.voice_state import can_auto_sleep, runtime_state

SOURCE = Path(__file__).resolve().parents[1] / "main.py"
METHODS = {"_set_ui_state", "set_speaking", "_on_ptt", "wake", "sleep"}


def player_class(log):
    owner = next(n for n in ast.parse(SOURCE.read_text()).body
                 if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
    body = [n for n in owner.body if getattr(n, "name", None) in METHODS]
    stub = ast.ClassDef(name="Player", bases=[], keywords=[], body=body, decorator_list=[])
    ns = dict(threading=threading, time=time, runtime_state=runtime_state,
              can_auto_sleep=can_auto_sleep, safe_label=safe_label, _TAIL_MARGIN=0.25,
              log_state_transition=lambda before, after, reason: log.append((before, after, reason)))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[])),
                 str(SOURCE), "exec"), ns)
    return ns["Player"]


class FlickerTests(unittest.TestCase):
    def make(self, awake=True, wake_enabled=False):
        self.trace = []
        self.shown = []
        p = player_class(self.trace)()
        p.ui = SimpleNamespace(muted=False, set_state=self.shown.append, write_log=lambda text: None)
        p._state_lock = threading.RLock()
        p._speaking_lock = threading.Lock()
        p._trace_ui_state = None
        p._awake = awake
        p._wake_enabled = wake_enabled
        p._is_speaking = False
        p._pending_tool_batches = 0
        p._tail_until = 0.0
        p._out_latency = 0.05
        p._out_level = 0.0
        p._ptt_held = False
        p._last_user_speech = 0.0
        return p

    # ── push-to-talk release ─────────────────────────────────────────────────

    def test_releasing_the_key_while_awake_does_not_show_sleeping(self):
        p = self.make()
        p._on_ptt(True)
        p._on_ptt(False)
        self.assertEqual(self.shown, ["LISTENING"])
        self.assertNotIn("SLEEPING", self.shown)

    def test_releasing_the_key_mid_answer_leaves_speaking_alone(self):
        p = self.make()
        p.set_speaking(True)
        p._on_ptt(False)
        self.assertEqual(self.shown, ["SPEAKING"])

    def test_releasing_the_key_while_a_tool_runs_shows_thinking(self):
        p = self.make()
        p._pending_tool_batches = 1
        p._on_ptt(False)
        self.assertEqual(self.shown, ["THINKING"])

    def test_releasing_the_key_is_still_asleep_if_the_wake_gate_is_closed(self):
        p = self.make(awake=False, wake_enabled=True)
        p._on_ptt(False)
        self.assertEqual(self.shown, ["SLEEPING"])

    def test_pressing_the_key_wakes_the_assistant(self):
        p = self.make(awake=False, wake_enabled=True)
        p._on_ptt(True)
        self.assertTrue(p._awake)
        self.assertEqual(self.shown, ["LISTENING"])

    # ── reasons ──────────────────────────────────────────────────────────────

    def test_speech_start_and_end_carry_their_reasons(self):
        p = self.make()
        p.set_speaking(True)
        p.set_speaking(False, "stall_gap")
        p.set_speaking(True)
        p.set_speaking(False, "turn_complete")
        p.set_speaking(True)
        p.set_speaking(False)
        self.assertEqual([reason for _, _, reason in self.trace],
                         ["audio_start", "stall_gap", "audio_start", "turn_complete",
                          "audio_start", "audio_stop"])

    def test_push_to_talk_reasons_are_recorded(self):
        p = self.make()
        p._on_ptt(True)
        p.set_speaking(True)
        p.set_speaking(False, "turn_complete")
        p._on_ptt(False)
        self.assertIn(("SPEAKING", "LISTENING", "turn_complete"), self.trace)
        self.assertEqual(self.trace[0][2], "ptt_press")

    def test_sleep_says_which_path_put_it_to_sleep(self):
        p = self.make(awake=True, wake_enabled=True)
        p.sleep(reason="no speech for 2 minutes")
        self.assertEqual(self.shown, ["SLEEPING"])
        self.assertEqual(self.trace[-1][2], "sleep:no-speech-for-2-minutes")

    def test_wake_says_how_it_was_woken(self):
        p = self.make(awake=False, wake_enabled=True)
        p.wake(reason="wake word")
        self.assertEqual(self.trace[-1], ("UNKNOWN", "LISTENING", "wake:wake-word"))


if __name__ == "__main__":
    unittest.main()
