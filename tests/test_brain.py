import tempfile
import unittest
from pathlib import Path

from core import brain


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(reply=None, clock=None):
    tmp = tempfile.mkdtemp()
    calls = []

    def think(system, prompt):
        calls.append((system, prompt))
        return reply

    b = brain.Brain(Path(tmp) / "brain.json", think=think, snapshot=lambda turns: "NOW: test",
                    clock=clock or Clock(), name="Jarvis", user="Nathan")
    return b, calls


GOOD = {"thinking": "Homework is late.",
        "thoughts": [{"kind": "heads_up", "urgency": 4, "topic": "EE98 homework",
                      "text": "EE98 Homework 3 was due two days ago and it's still open."}]}


class CheckInTests(unittest.TestCase):
    def test_robotic_endings(self):
        for s in ("Done, the lights are off. Is there anything else I can help you with?",
                  "Timer set. Would you like me to add it to your calendar too?",
                  "Sent. Do you want me to text Sam as well?",
                  "All sorted. Anything else?"):
            self.assertTrue(brain.ends_with_check_in(s), s)

    def test_real_questions_pass(self):
        for s in ("Done, the lights are off.",
                  "I've drafted it to Sam. Should I go ahead and send it?",
                  "Which Sam, your brother or the one from work?",
                  "Ringing Mum now. Want me to place the call?"):
            self.assertFalse(brain.ends_with_check_in(s), s)


class BrainTests(unittest.TestCase):
    def test_think_keeps_thoughts_and_persists(self):
        b, calls = make(GOOD)
        new = b.think_once("test")
        self.assertEqual(len(new), 1)
        self.assertEqual(b.pending()[0]["urgency"], 4)
        self.assertIn("Nathan", calls[0][0])
        again = brain.Brain(b.path, think=lambda *a: None, snapshot=lambda t: "", clock=b._clock)
        self.assertEqual(len(again.pending()), 1)
        self.assertEqual(again.thinking, "Homework is late.")

    def test_same_topic_not_repeated(self):
        b, _ = make(GOOD)
        b.think_once()
        self.assertEqual(b.absorb(GOOD), [])

    def test_shared_topic_quiet_then_allowed(self):
        clock = Clock()
        b, _ = make(GOOD, clock)
        t = b.think_once()[0]
        b.mark(t["id"], "shared")
        clock.t += 3600
        self.assertEqual(b.absorb(GOOD), [])
        clock.t += brain.TOPIC_QUIET_S
        self.assertEqual(len(b.absorb(GOOD)), 1)

    def test_drops_bad_thoughts(self):
        b, _ = make()
        out = b.absorb({"thoughts": [
            {"text": "short"},
            {"kind": "suggestion", "topic": "x", "text": "Your shop is quiet. Would you like me to make a new design?"},
            {"kind": "weird", "urgency": "nine", "topic": "views", "text": "The last three videos all beat 1,000 views."},
        ]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "noticed")
        self.assertEqual(out[0]["urgency"], 2)

    def test_garbage_reply(self):
        b, _ = make("not json")
        self.assertEqual(b.think_once(), [])

    def test_thoughts_expire(self):
        clock = Clock()
        b, _ = make(GOOD, clock)
        b.think_once()
        clock.t += brain.THOUGHT_TTL_S + 1
        self.assertEqual(b.pending(), [])

    def test_due_after_conversation_and_gaps(self):
        clock = Clock()
        b, _ = make(GOOD, clock)
        b.state["last_think"] = clock.t - brain.MIN_GAP_S - 1
        b.heard(["User: hi"])
        self.assertIsNone(b.due())
        clock.t += brain.AFTER_TALK_S
        self.assertEqual(b.due(), "conversation")
        b.think_once()
        clock.t += 60
        b.heard(["User: again"])
        clock.t += brain.AFTER_TALK_S
        self.assertIsNone(b.due())          # too soon after the last think

    def test_daily_cap(self):
        clock = Clock()
        b, _ = make(None, clock)
        b.state.update(day=__import__("datetime").date.fromtimestamp(clock.t).isoformat(),
                       count=brain.DAILY_CAP, last_think=0)
        self.assertIsNone(b.due())

    def test_primed_thought_marked_shared_when_said(self):
        b, _ = make(GOOD)
        t = b.think_once()[0]
        self.assertEqual(b.unprimed()[0]["id"], t["id"])
        b.mark(t["id"], "primed")
        self.assertEqual(b.unprimed(), [])
        b.heard_reply("Lights are off. By the way, EE98 Homework 3 was due two days ago and it's still sitting open.")
        self.assertEqual(b.pending(), [])

    def test_unrelated_reply_keeps_thought(self):
        b, _ = make(GOOD)
        t = b.think_once()[0]
        b.mark(t["id"], "primed")
        b.heard_reply("The weather is mild today.")
        self.assertEqual(len(b.pending()), 1)

    def test_prompts_carry_the_thought(self):
        t = {"kind": "heads_up", "urgency": 4, "text": "Battery is at 9 percent."}
        self.assertIn("Battery is at 9 percent.", brain.speak_text(t))
        p = brain.priming_text([t], "worried about the battery")
        self.assertTrue(p.startswith("[INNER_THOUGHTS]"))
        self.assertIn("worried about the battery", p)

    def test_gather_runs_on_this_machine(self):
        snap = brain.gather(["User: hello"])
        self.assertIn("NOW:", snap)
        self.assertIn("User: hello", snap)


if __name__ == "__main__":
    unittest.main()
